"""Checks RedFlag's connection to Meterless Nightshift (provider contract v4).

    .venv/bin/python meterless/check.py              # uses METERLESS_* from .env
    .venv/bin/python meterless/check.py --detect     # find a local runtime first

Prints one PASS/FAIL line per check: runtime up (/healthz), authorised and
online (GET /v1/models), a plain text request on meterless/auto, one per
RedFlag role (MODEL_ROUTE_EXTRACT / MODEL_ROUTE_REASON) and, with --model, any
other route. The token is never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from redflag.config import load_dotenv                               # noqa: E402
from redflag.llm.gateway import (meterless_token, normalise_base_url,  # noqa: E402
                                 routes_from_env, strip_reasoning)

PORTS = (8088, 8090, 8787, 11434)
HOSTS = ("127.0.0.1", "localhost", "[::1]")
APP_HEADERS = {"meterless-app": "redflag"}


def status(base: str, token: str | None) -> tuple[str, list[str]]:
    """'online' (200 with meterless/* ids), 'unauthorized' (401/403) or 'offline'."""
    headers = dict(APP_HEADERS, **({"Authorization": f"Bearer {token}"} if token else {}))
    try:
        resp = httpx.get(f"{base}/models", headers=headers, timeout=5)
    except httpx.HTTPError:
        return "offline", []
    if resp.status_code in (401, 403):
        return "unauthorized", []
    if resp.status_code != 200:
        return "offline", []
    ids = [m.get("id", "") for m in resp.json().get("data", [])]
    meterless_ids = [i for i in ids if i.startswith("meterless/")]
    return ("online" if meterless_ids else "offline"), ids


def detect(token: str | None) -> str | None:
    for port in PORTS:
        for host in HOSTS:
            base = f"http://{host}:{port}/v1"
            if status(base, token)[0] in ("online", "unauthorized"):
                return base
    return None


def chat(base: str, token: str | None, model: str) -> tuple[str, str]:
    resp = httpx.post(f"{base}/chat/completions", timeout=180,
                      headers=dict(APP_HEADERS, **{"meterless-scope": "user",
                                                   "Authorization": f"Bearer {token}"}),
                      json={"model": model, "messages": [{"role": "user", "content":
                            "Reply with one sentence confirming this connection works."}]})
    resp.raise_for_status()
    out = resp.json()
    return strip_reasoning(out["choices"][0]["message"]["content"]), out.get("model", model)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true", help="auto-detect a local runtime")
    ap.add_argument("--model", help="also test this pinned route, e.g. a Nemotron lane")
    args = ap.parse_args()

    load_dotenv()
    token = meterless_token(dict(os.environ))
    base = os.environ.get("METERLESS_BASE_URL")
    if args.detect or not base:
        base = detect(token)
        if not base:
            print("FAIL detect      no runtime on ports 8088/8090/8787/11434 - is Nightshift running?")
            return 1
        print(f"PASS detect      found runtime at {base} (put this in METERLESS_BASE_URL)")
    base = normalise_base_url(base)
    root = base[: -len("/v1")]
    ok = True

    try:
        up = httpx.get(f"{root}/healthz", timeout=5).status_code < 500
    except httpx.HTTPError:
        up = False
    print(f"{'PASS' if up else 'FAIL'} healthz     {root}/healthz")

    state, ids = status(base, token)
    if state == "online":
        print(f"PASS models      online, {len(ids)} routes: {', '.join(ids[:8])}")
    elif state == "unauthorized":
        print("FAIL models      unauthorized - set METERLESS_RUNTIME_TOKEN from the Nightshift Console")
        return 1
    else:
        print("FAIL models      offline - no meterless/* routes at GET /v1/models")
        return 1

    checks = [("auto", "meterless/auto")] + list(routes_from_env(dict(os.environ)).items())
    if args.model:
        checks.append(("--model", args.model))
    for label, model in checks:
        if not model.startswith("meterless/") and model not in ids:
            ok = False
            print(f"FAIL {label:<11} {model} is not offered by this runtime")
            continue
        try:
            text, served = chat(base, token, model)
            print(f"PASS {label:<11} {model} -> {served}: {text.strip()[:100]}")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            ok = False
            print(f"FAIL {label:<11} {model}: {str(e)[:200]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
