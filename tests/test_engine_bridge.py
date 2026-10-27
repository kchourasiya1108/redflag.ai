"""The JSON-lines bridge the TypeScript conformance adapters drive."""
from __future__ import annotations

import json
import subprocess
import sys

from agent.config import ROOT


def _bridge(engine, op, request, answer=lambda args: {"text": "[TASK_COMPLETE]"}):
    proc = subprocess.Popen([sys.executable, "-m", "agent.engines.bridge", engine, op],
                            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    asked = []
    for line in proc.stdout:
        msg = json.loads(line)
        if msg["op"] == "result":
            proc.stdin.close()
            proc.wait(timeout=10)
            return msg, asked
        asked.append(msg["args"])
        proc.stdin.write(json.dumps({"ok": True, "result": answer(msg["args"])}) + "\n")
        proc.stdin.flush()
    raise AssertionError("bridge ended without a result")


def test_bridge_runs_markovian_through_the_callers_generator():
    msg, asked = _bridge("markovian", "run", {"chunkConfig": {}, "options": {
        "goal": "one chunk", "reflect": False}})
    assert msg["ok"] and msg["result"]["status"] == "completed"
    assert msg["result"]["chunkConfig"]["chunkSize"] == 8000
    assert len(asked) == 1 and "User Request: one chunk" in asked[0][0]


def test_bridge_reports_typed_errors():
    msg, _ = _bridge("markovian", "validate", {"chunkConfig": {"chunkSize": 128}})
    assert not msg["ok"] and msg["type"] == "ConfigValidationError"
