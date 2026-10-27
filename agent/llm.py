"""The Model Gateway: the one way RedFlag calls a language model.

Every agent and every Meterless engine receives `Gateway.generate` (or its
engine-shaped adaptor), never a model SDK. Order of resolution per call:

1. disk cache (data/cache/llm/<key>.json): the same prompt is never paid twice;
2. sample mode: recorded replies (data/replay/llm/<key>.json), no network;
3. Meterless Nightshift, the only provider (OpenAI-compatible, `model` is a
   routing mode such as meterless/fast, or a pinned model id). Nightshift holds
   the NVIDIA credentials and makes the Nemotron call; RedFlag never does.

Callers name a role ("extract", "reason") or a concrete route; roles resolve
through MODEL_ROUTE_EXTRACT / MODEL_ROUTE_REASON, so the Nemotron model behind
each role is pinned in config. Reasoning text before a closing </think> tag is
dropped from replies.

Structured calls validate the reply against a Pydantic schema, with one
repair attempt. Calls may carry images (PNG bytes), sent as OpenAI-style
image_url parts to a vision model (VISION_MODEL). A run budget caps calls and
tokens. Secrets never reach logs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

CACHE_VERSION = 1
ROLES_FILE = "_roles.json"


class GatewayError(RuntimeError):
    pass


# A role that fails falls back once to this role (live runs only).
FALLBACK_ROLE = {"extract": "reason"}


class ReplayMiss(GatewayError):
    """Sample mode has no recorded reply for this prompt."""


class BudgetExceeded(GatewayError):
    pass


class MalformedOutput(GatewayError):
    pass


@dataclass
class GenResult:
    text: str
    parsed: Any = None
    route: str = ""
    provider: str = ""
    served_model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False
    replayed: bool = False
    latency_ms: float = 0.0
    attempts: int = 0
    key: str = ""


class Provider:
    """An OpenAI-compatible chat endpoint."""

    def __init__(self, name: str, base_url: str, token: str | None,
                 headers: dict[str, str] | None = None,
                 chat_headers: dict[str, str] | None = None,
                 model_for: Callable[[str], str] = lambda route: route,
                 timeout_s: float = 90.0, transport: httpx.BaseTransport | None = None):
        self.name = name
        self.base = base_url.rstrip("/")
        self.model_for = model_for
        self.chat_headers = chat_headers or {}
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(timeout=timeout_s, headers=h, transport=transport)

    def models(self) -> list[str]:
        resp = self._http.get(f"{self.base}/models")
        resp.raise_for_status()
        return [m.get("id") for m in resp.json().get("data", [])]

    def chat(self, messages: list[dict], route: str, max_tokens: int,
             model: str | None = None, timeout_s: float | None = None) -> dict:
        body = {"model": model or self.model_for(route), "messages": messages,
                "temperature": 0, "max_tokens": max_tokens}
        resp = self._http.post(f"{self.base}/chat/completions", json=body,
                               headers=self.chat_headers,
                               **({"timeout": timeout_s} if timeout_s else {}))
        if resp.status_code >= 400:
            raise GatewayError(f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}")
        out = resp.json()
        usage = out.get("usage") or {}
        return {"text": strip_reasoning(out["choices"][0]["message"]["content"] or ""),
                "served_model": out.get("model") or body["model"],
                "tokens_in": usage.get("prompt_tokens", 0),
                "tokens_out": usage.get("completion_tokens", 0)}


def strip_reasoning(text: str) -> str:
    """Drop a reasoning preamble some models emit before </think>."""
    if "</think>" not in text:
        return text
    return text.rsplit("</think>", 1)[1].lstrip()


def normalise_base_url(url: str) -> str:
    """Any pasted runtime address (with or without scheme, /v1, /v1/models,
    /v1/chat/completions or /healthz) becomes <scheme>://<host>/v1."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    for suffix in ("/chat/completions", "/models", "/healthz", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url + "/v1"


def meterless_token(env: dict[str, str]) -> str | None:
    """The Nightshift Console's Runtime token (the placeholder rt-local is rejected)."""
    token = env.get("METERLESS_RUNTIME_TOKEN") or env.get("METERLESS_TOKEN")
    return None if token in (None, "", "rt-local") else token


def providers_from_env(env: dict[str, str] | None = None,
                       transport: httpx.BaseTransport | None = None) -> list[Provider]:
    env = dict(os.environ if env is None else env)
    out = []
    if env.get("METERLESS_BASE_URL"):
        out.append(Provider("meterless", normalise_base_url(env["METERLESS_BASE_URL"]),
                            meterless_token(env), headers={"meterless-app": "redflag"},
                            chat_headers={"meterless-scope": "user"},
                            timeout_s=180.0, transport=transport))   # a model load can take minutes
    return out


@dataclass
class Budget:
    max_calls: int = 200
    max_tokens: int = 400_000
    calls: int = 0
    tokens: int = 0

    def charge(self, tokens: int) -> None:
        self.calls += 1
        self.tokens += tokens

    def check(self) -> None:
        if self.calls >= self.max_calls or self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"run budget used: {self.calls} calls, {self.tokens} tokens")


@dataclass
class Gateway:
    providers: list[Provider]
    cache_dir: Path
    replay_dir: Path
    sample_mode: bool = False
    record_replay: bool = False
    vision_model: str | None = None
    routes: dict[str, str] = field(default_factory=dict)  # role -> route or model id
    role_system: dict[str, str] = field(default_factory=dict)     # role -> system prefix
    role_max_tokens: dict[str, int] = field(default_factory=dict)  # role -> default budget
    retry_backoff_s: float = 0.0        # pause before a retry (provider 5xx, empty reply)
    deadline: float | None = None       # time.perf_counter() value; no call starts after it
    budget: Budget = field(default_factory=Budget)
    on_call: Callable[[dict], None] | None = None     # trace hook

    def generate(self, prompt: str, system: str | None = None, *,
                 schema: type[BaseModel] | None = None, route: str = "meterless/auto",
                 purpose: str = "", max_tokens: int | None = None,
                 images: list[bytes] | None = None) -> GenResult:
        fallback = FALLBACK_ROLE.get(route)
        if fallback and fallback in self.routes and not self.sample_mode \
                and not self._is_cached(prompt, system, schema, route, images) \
                and self._is_cached(prompt, system, schema, fallback, images):
            # This call failed on its own role before and was answered by the fallback:
            # reuse that answer instead of waiting for the failing model again.
            return self._generate(prompt, system, schema=schema, route=fallback, purpose=purpose,
                                  max_tokens=max_tokens, images=images)
        try:
            return self._generate(prompt, system, schema=schema, route=route, purpose=purpose,
                                  max_tokens=max_tokens, images=images)
        except (ReplayMiss, BudgetExceeded):
            raise
        except GatewayError as e:
            # The fast extract model can be down while the reasoning model answers
            # (NVIDIA 502s on 27 Oct 2026): retry that one call on the reasoning role,
            # live only, so a check completes instead of skipping sections.
            if self.sample_mode or not fallback or fallback not in self.routes:
                raise
            if self.on_call:
                self.on_call({"action": "model.retry_or_fallback", "purpose": purpose,
                              "error": f"{route} failed, retrying on {fallback}: {str(e)[:120]}"})
            return self._generate(prompt, system, schema=schema, route=fallback, purpose=purpose,
                                  max_tokens=max_tokens, images=images)

    def _is_cached(self, prompt: str, system: str | None, schema, role: str, images=None) -> bool:
        """Whether a reply for this call on this role is in the disk cache."""
        prefix = self.role_system.get(role)
        sysm = (f"{prefix}\n{system}" if system else prefix) if prefix else system
        model = self.vision_model if images else None
        key = cache_key(self.routes.get(role, role), sysm, prompt, schema, images, model)
        return (self.cache_dir / f"{key}.json").exists()

    def _generate(self, prompt: str, system: str | None = None, *,
                  schema: type[BaseModel] | None = None, route: str = "meterless/auto",
                  purpose: str = "", max_tokens: int | None = None,
                  images: list[bytes] | None = None) -> GenResult:
        # Reasoning models spend the token budget thinking before they answer,
        # so a role carries its own budget and an optional system prefix.
        if max_tokens is None:
            max_tokens = self.role_max_tokens.get(route, 2048)
        prefix = self.role_system.get(route)
        if prefix:
            system = f"{prefix}\n{system}" if system else prefix
        route = self.routes.get(route, route)
        model = None
        if images:
            if not self.vision_model:
                raise GatewayError("image input needs VISION_MODEL (a vision-capable model id)")
            model = self.vision_model
        key = cache_key(route, system, prompt, schema, images, model)
        result = self._lookup(key)
        if result is None:
            result = self._call(prompt, system, schema, route, max_tokens, key, images, model)
        elif self.record_replay and result.cached:
            self._store(key, result)        # a recording must include replies served from cache
        if schema is not None and result.parsed is None:
            result.parsed = parse_structured(result.text, schema)
        self._trace(purpose, result)
        return result

    def engine_generator(self, route: str = "meterless/auto", purpose: str = "engine"):
        """The Meterless engines' injected generator signature."""
        def generator(prompt, attachments=None, system_prompt=None, on_stream=None,
                      abort_signal=None):
            res = self.generate(prompt, system_prompt, route=route, purpose=purpose)
            if on_stream:
                on_stream(res.text)
            out = {"text": res.text, "model": res.served_model}
            if res.tokens_out:          # provider-reported: engines label it "measured"
                out["metadata"] = {"usage": {"input_tokens": res.tokens_in,
                                             "output_tokens": res.tokens_out}}
            return out
        return generator

    # -- internals ---------------------------------------------------------

    def _lookup(self, key: str) -> GenResult | None:
        for folder, replayed in ((self.cache_dir, False), (self.replay_dir, True)):
            path = folder / f"{key}.json"
            if path.exists() and (not replayed or self.sample_mode):
                data = json.loads(path.read_text())
                data.update(cached=not replayed, replayed=replayed, parsed=None)
                return GenResult(**data)
        if self.sample_mode:
            raise ReplayMiss(f"no recorded reply for {key} in sample mode")
        return None

    def _call(self, prompt, system, schema, route, max_tokens, key, images=None,
              model=None) -> GenResult:
        if not self.providers:
            raise GatewayError("no model provider configured (METERLESS_BASE_URL)")
        sys_text = system or ""
        if schema is not None:
            sys_text += ("\n\nReply with only a JSON value matching this JSON schema, "
                         "no prose:\n" + json.dumps(schema.model_json_schema()))
        user: str | list = prompt
        if images:
            user = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(img).decode()}}
                for img in images]
        messages = ([{"role": "system", "content": sys_text.strip()}] if sys_text.strip()
                    else []) + [{"role": "user", "content": user}]
        result = self._first_answer(messages, route, max_tokens, key, model)
        if schema is not None:
            try:
                result.parsed = parse_structured(result.text, schema)
            except MalformedOutput as e:
                repair = messages + [{"role": "assistant", "content": result.text},
                                     {"role": "user", "content":
                                      f"That reply did not match the schema ({e}). "
                                      "Reply again with only the corrected JSON."}]
                result = self._first_answer(repair, route, max_tokens, key, model)
                result.parsed = parse_structured(result.text, schema)   # raises if still bad
        self._store(key, result)
        return result

    def _first_answer(self, messages, route, max_tokens, key, model=None) -> GenResult:
        errors = []
        for provider in self.providers:
            for attempt in (1, 2):
                self.budget.check()
                t0 = time.perf_counter()
                left = None if self.deadline is None else self.deadline - t0
                if left is not None and left < 5:
                    raise BudgetExceeded("the run's time limit leaves no room for a model call")
                try:
                    out = provider.chat(messages, route, max_tokens, model,
                                        timeout_s=min(180.0, left) if left else None)
                    if not out["text"].strip():
                        raise GatewayError("empty reply (the token budget went on reasoning?)")
                except (GatewayError, httpx.HTTPError, KeyError, ValueError) as e:
                    errors.append(f"{provider.name}#{attempt}: {e}")
                    if self.on_call:
                        self.on_call({"action": "model.retry_or_fallback",
                                      "provider": provider.name, "error": str(e)[:200]})
                    if attempt == 1 and self.retry_backoff_s:
                        time.sleep(self.retry_backoff_s)
                    continue
                self.budget.charge(out["tokens_in"] + out["tokens_out"])
                return GenResult(text=out["text"], route=route, provider=provider.name,
                                 served_model=out["served_model"], tokens_in=out["tokens_in"],
                                 tokens_out=out["tokens_out"], attempts=attempt,
                                 latency_ms=(time.perf_counter() - t0) * 1000, key=key)
        raise GatewayError("every provider failed: " + " | ".join(errors))

    def _store(self, key: str, result: GenResult) -> None:
        data = asdict(result)
        data.pop("parsed")
        for folder in [self.cache_dir] + ([self.replay_dir] if self.record_replay else []):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{key}.json").write_text(json.dumps(data))
        if self.record_replay:
            # Replays are keyed by the resolved model and the role prompts, so a
            # recording saves the roles it used; sample mode loads them (below).
            (self.replay_dir / ROLES_FILE).write_text(json.dumps(
                {"routes": self.routes, "role_system": self.role_system,
                 "role_max_tokens": self.role_max_tokens}, indent=2))

    def use_recorded_roles(self) -> bool:
        """Sample mode: resolve roles exactly as the recording did, whatever .env says."""
        path = self.replay_dir / ROLES_FILE
        if not path.exists():
            return False
        saved = json.loads(path.read_text())
        self.routes, self.role_system = saved["routes"], saved["role_system"]
        self.role_max_tokens = {k: int(v) for k, v in saved["role_max_tokens"].items()}
        return True

    def _trace(self, purpose: str, r: GenResult) -> None:
        if self.on_call:
            self.on_call({"action": "model.call", "purpose": purpose, "route": r.route,
                          "provider": r.provider, "served_model": r.served_model,
                          "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                          "cached": r.cached, "replayed": r.replayed,
                          "latency_ms": round(r.latency_ms, 1), "attempts": r.attempts})


def cache_key(route: str, system: str | None, prompt: str,
              schema: type[BaseModel] | None, images: list[bytes] | None = None,
              model: str | None = None) -> str:
    payload = json.dumps({"v": CACHE_VERSION, "route": route, "system": system or "",
                          "prompt": prompt,
                          "schema": schema.model_json_schema() if schema else None,
                          "images": [hashlib.sha256(i).hexdigest() for i in images or []],
                          "model": model}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def parse_structured(text: str, schema: type[BaseModel]) -> BaseModel:
    """Validate a reply, tolerating code fences and prose around the JSON."""
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.M)
    candidates = [body]
    m = re.search(r"[\[{].*[\]}]", body, re.S)
    if m:
        candidates.append(m.group())
    # A reasoning model may think aloud before answering: try the last
    # balanced JSON value in the text as well.
    tail = _last_json(body)
    if tail:
        candidates.append(tail)
    last: Exception | None = None
    for c in candidates:
        try:
            return schema.model_validate_json(c)
        except ValidationError as e:
            last = e
    raise MalformedOutput(str(last)[:300] if last else "no JSON found")


def _last_json(text: str) -> str | None:
    """The last balanced {...} or [...] in the text (strings respected), if any."""
    end = max(text.rfind("}"), text.rfind("]"))
    while end >= 0:
        close = text[end]
        open_ = "{" if close == "}" else "["
        depth, in_str, i = 0, False, end
        while i >= 0:
            ch = text[i]
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str:
                if ch == close:
                    depth += 1
                elif ch == open_:
                    depth -= 1
                    if depth == 0:
                        return text[i:end + 1]
            i -= 1
        end = max(text.rfind("}", 0, end), text.rfind("]", 0, end))
    return None


def routes_from_env(env: dict[str, str]) -> dict[str, str]:
    defaults = {"extract": "meterless/fast", "reason": "meterless/deep"}
    return {role: env.get(f"MODEL_ROUTE_{role.upper()}") or route
            for role, route in defaults.items()}


# Measured on Nightshift's Nemotron lanes (25 Oct): below ~4k tokens the
# reasoning models return an empty or cut-off answer; /no_think shortens it.
ROLE_MAX_TOKENS = {"extract": 8000, "reason": 8000}
ROLE_SYSTEM = {"extract": "/no_think"}


def role_settings(env: dict[str, str]) -> tuple[dict[str, str], dict[str, int]]:
    system = {r: env.get(f"MODEL_SYSTEM_{r.upper()}", v) for r, v in
              {**ROLE_SYSTEM, "reason": ROLE_SYSTEM.get("reason", "")}.items()}
    tokens = {r: int(env.get(f"MODEL_MAX_TOKENS_{r.upper()}", v)) for r, v in ROLE_MAX_TOKENS.items()}
    return {r: v for r, v in system.items() if v}, tokens


def gateway_from_env(data_dir: Path, env: dict[str, str] | None = None,
                     on_call=None) -> Gateway:
    env = dict(os.environ if env is None else env)
    return Gateway(
        providers=providers_from_env(env),
        cache_dir=data_dir / "cache" / "llm",
        replay_dir=data_dir / "replay" / "llm",
        sample_mode=env.get("SAMPLE_MODE", "false").lower() == "true",
        record_replay=env.get("RECORD_REPLAY", "false").lower() == "true",
        vision_model=env.get("VISION_MODEL") or None,
        routes=routes_from_env(env),
        role_system=role_settings(env)[0], role_max_tokens=role_settings(env)[1],
        retry_backoff_s=float(env.get("MODEL_RETRY_BACKOFF_S", 3)),
        budget=Budget(int(env.get("RUN_MAX_LLM_CALLS", 200)),
                      int(env.get("RUN_MAX_TOKENS", 400_000))),
        on_call=on_call)
