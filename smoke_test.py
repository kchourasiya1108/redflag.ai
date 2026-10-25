"""Smoke test for RedFlag's Data and Power layers (Setup Guide, Check 3).

    .venv/bin/python smoke_test.py            # guide checks + provider contract + roles
    .venv/bin/python smoke_test.py --detect   # find a local Nightshift first
    .venv/bin/python smoke_test.py --full     # also compare every table's row count
    .venv/bin/python smoke_test.py --model ID # also test one more route
    .venv/bin/python smoke_test.py --only power   # just Meterless (or --only data)

The guide's three lines come first (Zetaris web interface, Zetaris SQL over
REST, a Nemotron reply). Then the Meterless provider contract v4 checks:
/healthz, GET /v1/models online, a wrong token reads as unauthorized (not
offline), a plain text request on meterless/auto, and each RedFlag role
answered by the model it is pinned to. Tokens and keys are never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlsplit

import httpx

from agent.config import ROOT, load_dotenv
from agent.llm import meterless_token, normalise_base_url, routes_from_env, strip_reasoning
from agent.tools_zetaris import ZetarisError, ZetarisRestClient

PORTS = (8088, 8090, 8787, 11434)
HOSTS = ("127.0.0.1", "localhost", "[::1]")
APP_HEADERS = {"meterless-app": "redflag"}


class Report:
    def __init__(self):
        self.ok = True

    def line(self, passed: bool, name: str, detail: str = "") -> bool:
        self.ok &= passed
        print(f"{'PASS' if passed else 'FAIL'} {name:<44} {detail}".rstrip())
        return passed


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
    return ("online" if any(i.startswith("meterless/") for i in ids) else "offline"), ids


def detect(token: str | None) -> str | None:
    for port in PORTS:
        for host in HOSTS:
            base = f"http://{host}:{port}/v1"
            if status(base, token)[0] in ("online", "unauthorized"):
                return base
    return None


def chat(base: str, token: str | None, model: str, prompt: str) -> tuple[str, str]:
    headers = dict(APP_HEADERS, **{"meterless-scope": "user"},
                   **({"Authorization": f"Bearer {token}"} if token else {}))
    resp = httpx.post(f"{base}/chat/completions", timeout=180, headers=headers,
                      json={"model": model, "messages": [{"role": "user", "content": prompt}]})
    resp.raise_for_status()
    out = resp.json()
    return strip_reasoning(out["choices"][0]["message"]["content"]), out.get("model", model)


def zetaris_web_url() -> str:
    if os.environ.get("ZETARIS_BASE_URL"):
        return os.environ["ZETARIS_BASE_URL"]
    rest = urlsplit(os.environ.get("ZETARIS_REST_URL", "http://localhost:8888"))
    return f"{rest.scheme}://{rest.hostname}:3000"


def data_checks(r: Report) -> None:
    url = zetaris_web_url()
    try:
        code = httpx.get(url, timeout=10, follow_redirects=True).status_code
        r.line(code < 400, "Data: Zetaris web interface", f"{url} -> {code}")
    except httpx.HTTPError as e:
        r.line(False, "Data: Zetaris web interface", f"{url}: {e}")
    try:
        _, rows, ms = ZetarisRestClient(retries=0).query("SELECT 1", 1)
        r.line(bool(rows) and str(rows[0][0]) == "1", "Data: Zetaris runs SQL over its REST API",
               f"SELECT 1 -> {rows} ({ms:.0f} ms)")
    except ZetarisError as e:
        r.line(False, "Data: Zetaris runs SQL over its REST API", str(e)[:150])


def same_model(served: str, pinned: str) -> bool:
    """A pinned id matches the served one with or without its vendor prefix."""
    tail = pinned.rsplit("/", 1)[-1]
    return served == pinned or served.rsplit("/", 1)[-1].startswith(tail)


def power_checks(r: Report, base: str, token: str | None, extra: str | None) -> None:
    root = base[: -len("/v1")]
    roles = routes_from_env(dict(os.environ))
    try:
        up = httpx.get(f"{root}/healthz", timeout=5).status_code < 500
    except httpx.HTTPError:
        up = False
    r.line(up, "Meterless: runtime up (/healthz)", f"{root}/healthz")

    state, ids = status(base, token)
    hint = {"unauthorized": " - set METERLESS_RUNTIME_TOKEN from the Nightshift Console",
            "offline": " - no meterless/* routes at GET /v1/models"}.get(state, "")
    if not r.line(state == "online", "Meterless: models online",
                  f"{state}, {len(ids)} routes{hint}"):
        r.line(False, "Power: Nemotron replies", "skipped: runtime not online")
        return
    wrong, _ = status(base, "smoke-test-wrong-token")
    r.line(wrong == "unauthorized", "Meterless: a wrong token is unauthorized", wrong)

    checks = [("auto", "meterless/auto")] + list(roles.items())
    if extra:
        checks.append(("--model", extra))
    served_by: dict[str, str] = {}
    for label, model in checks:
        name = f"Meterless: {label} -> {model}"[:44]
        if not model.startswith("meterless/") and model not in ids:
            r.line(False, name, "not offered by this runtime")
            continue
        try:
            text, served = chat(base, token, model,
                                "Reply with one sentence confirming this connection works.")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            r.line(False, name, str(e)[:150])
            continue
        served_by[label] = served
        pinned = not model.startswith("meterless/")
        r.line(not pinned or same_model(served, model), name,
               f"served by {served}: {text.strip()[:60]}")
    nemotron = [f"{label}: {m}" for label, m in served_by.items() if "nemotron" in m.lower()]
    r.line(bool(nemotron), "Power: Nemotron replies", ", ".join(nemotron) or
           "no request was served by a Nemotron model")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--detect", action="store_true", help="auto-detect a local Nightshift")
    ap.add_argument("--full", action="store_true", help="compare every table's row count")
    ap.add_argument("--model", help="also test this route or model id")
    ap.add_argument("--only", choices=("data", "power"), help="run one layer's checks")
    args = ap.parse_args()

    load_dotenv()
    r = Report()
    if args.only != "power":
        data_checks(r)
        if args.full:
            full_check(r)
    if args.only == "data":
        return 0 if r.ok else 1

    token = meterless_token(dict(os.environ))
    base = os.environ.get("METERLESS_BASE_URL")
    if args.detect or not base:
        base = detect(token)
        if not r.line(base is not None, "Meterless: auto-detect",
                      base or "nothing on ports 8088/8090/8787/11434 - is Nightshift running?"):
            return 1
    power_checks(r, normalise_base_url(base), token, args.model)
    return 0 if r.ok else 1


def full_check(r: Report) -> None:
    from agent.tools_zetaris import MockZetaris
    from pipeline import zetaris_setup
    name = "Data: Zetaris row counts match archive.db"
    archive = ROOT / "data" / "archive.db"
    if not archive.exists():
        r.line(False, name, f"{archive} not found - run the offline pipeline first")
        return
    try:
        r.line(zetaris_setup.check(ZetarisRestClient(), MockZetaris(archive),
                                   log=lambda m: print(f"     {m}")), name)
    except Exception as e:      # unreachable Zetaris, missing settings, bad archive
        r.line(False, name, str(e)[:150])


if __name__ == "__main__":
    sys.exit(main())
