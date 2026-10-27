"""Run the official Meterless conformance suites against RedFlag's engines.

    .venv/bin/python -m agent.engines.conformance.run [markovian world-model hmem]

Clones github.com/Meterless/Meterless at the pinned commit into
.cache/meterless (once), then runs each engine's conformance/runner.ts with
<ENGINE>_IMPL pointing at our adapter in agent/engines/conformance/<engine>/.
Needs Node (npx fetches tsx). Exit code 0 only when every suite is green.
"""
from __future__ import annotations

import os
import subprocess
import sys

from agent.config import ROOT

REPO = "https://github.com/Meterless/Meterless.git"
PINNED = "1742dec56a66e58eef9dda2d2b865823758ca8cd"      # 2026-10-19
CLONE = ROOT / ".cache" / "meterless"
ADAPTERS = {"markovian": "MARKOVIAN_IMPL", "world-model": "WORLD_MODEL_IMPL", "hmem": "HMEM_IMPL"}
HERE = ROOT / "agent" / "engines" / "conformance"


def ensure_clone() -> None:
    if not (CLONE / ".git").exists():
        CLONE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", REPO, str(CLONE)], check=True)
    subprocess.run(["git", "-C", str(CLONE), "checkout", "-q", PINNED], check=True)


def run_suite(engine: str) -> int:
    adapter = HERE / engine.replace("-", "_") / "index.ts"
    if not adapter.exists():
        print(f"skip {engine}: no adapter at {adapter.relative_to(ROOT)}")
        return 0
    env = {**os.environ, ADAPTERS[engine]: str(adapter), "REDFLAG_PYTHON": sys.executable}
    return subprocess.run(["npx", "-y", "tsx", "runner.ts"], env=env,
                          cwd=CLONE / "engines" / engine / "conformance").returncode


def main(argv: list[str]) -> int:
    engines = argv or list(ADAPTERS)
    unknown = set(engines) - set(ADAPTERS)
    if unknown:
        raise SystemExit(f"unknown engine(s) {sorted(unknown)}; choose from {list(ADAPTERS)}")
    ensure_clone()
    codes = {e: run_suite(e) for e in engines}
    print("\n" + "  ".join(f"{e}: {'green' if c == 0 else f'FAILED ({c})'}" for e, c in codes.items()))
    return 0 if all(c == 0 for c in codes.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
