"""Regenerate output_examples/ by running the agent on input_examples/.

Each input is POSTed to the real FastAPI app (in process, the same path as
`curl -X POST /run`); the response and the run's trace are saved next to it.
Nothing is read back from output_examples/: every output comes from a fresh run.

    .venv/bin/python scripts/make_examples.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SAMPLE_MODE", "true")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _relative(text: str) -> str:
    """Repo-relative paths, so the examples do not carry this machine's layout."""
    return text.replace(str(ROOT) + os.sep, "")


def _clear_run(run_id: str | None) -> None:
    """A fixed run_id appends to its old trace; start each example from nothing."""
    if not run_id:
        return
    (ROOT / "logs" / f"trace-{run_id}.jsonl").unlink(missing_ok=True)
    for folder in (ROOT / "logs" / run_id, ROOT / "output" / run_id):
        shutil.rmtree(folder, ignore_errors=True)


def _relative_run_files(run_id: str | None) -> None:
    """The run's own files are committed too (the response points at them)."""
    if not run_id:
        return
    files = [ROOT / "logs" / f"trace-{run_id}.jsonl"]
    for folder in (ROOT / "logs" / run_id, ROOT / "output" / run_id):
        files += [f for f in folder.glob("*") if f.suffix in (".json", ".jsonl", ".md")]
    for f in files:
        if f.is_file():
            f.write_text(_relative(f.read_text()))


def main() -> int:
    inputs = sorted((ROOT / "input_examples").glob("example_*.json"))
    if len(inputs) < 3:
        print(f"need at least 3 inputs in input_examples/, found {len(inputs)}")
        return 1
    out_dir = ROOT / "output_examples"
    out_dir.mkdir(exist_ok=True)
    client = TestClient(app)
    failed = 0
    for path in inputs:
        body = json.loads(path.read_text())
        _clear_run(body.get("run_id"))
        res = client.post("/run", json=body)
        out = res.json()
        ok = res.status_code == 200 and out.get("status") in ("success", "partial")
        failed += not ok
        (out_dir / path.name).write_text(_relative(json.dumps(out, indent=2, default=str)) + "\n")
        trace = ROOT / out.get("log_file", "") if out.get("log_file") else None
        if trace and trace.is_file():
            (out_dir / f"{path.stem}.trace.jsonl").write_text(_relative(trace.read_text()))
        report = out.get("report")
        if isinstance(report, str) and (ROOT / report).is_file():
            (out_dir / f"{path.stem}.report.md").write_text(_relative((ROOT / report).read_text()))
        _relative_run_files(out.get("run_id"))
        rating = (out.get("output") or {}).get("rating")
        print(f"{path.name}: HTTP {res.status_code} {out.get('status')} rating={rating}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
