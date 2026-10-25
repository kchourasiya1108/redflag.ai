"""Readable stdout logging plus JSONL events in logs/, and failures.csv."""
from __future__ import annotations

import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_FAILURE_FIELDS = ["timestamp", "stage", "key", "reason", "detail"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StageLog:
    """Logs one pipeline stage: stdout lines, a JSONL trace, and failures.

    The JSONL fields follow the hackathon's recommended trace format.
    """

    def __init__(self, stage: str, logs_dir: Path, failures_csv: Path):
        self.stage = stage
        self.failures_csv = failures_csv
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = logs_dir / f"pipeline-{stage}.jsonl"
        self.failure_count = 0
        self._log = logging.getLogger(f"redflag.{stage}")
        if not logging.getLogger("redflag").handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            root = logging.getLogger("redflag")
            root.addHandler(handler)
            root.setLevel(logging.INFO)

    def info(self, msg: str, **detail) -> None:
        self._log.info("stage=%s %s", self.stage, msg)
        self.event("info", msg, **detail)

    def warn(self, msg: str, **detail) -> None:
        self._log.warning("stage=%s %s", self.stage, msg)
        self.event("warn", msg, status="warning", **detail)

    def event(self, action: str, output_summary: str,
              status: str = "success", **detail) -> None:
        record = {"timestamp": _now(), "run_id": f"pipeline-{self.stage}",
                  "agent_name": self.stage, "action": action,
                  "output_summary": output_summary, "status": status}
        if detail:
            record["detail"] = detail
        with self.trace_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def failure(self, key: str, reason: str, detail: str = "") -> None:
        """Record one bad row and carry on; never raise."""
        self.failure_count += 1
        new = not self.failures_csv.exists()
        self.failures_csv.parent.mkdir(parents=True, exist_ok=True)
        with self.failures_csv.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_FAILURE_FIELDS)
            if new:
                w.writeheader()
            w.writerow({"timestamp": _now(), "stage": self.stage, "key": key,
                        "reason": reason, "detail": detail[:500]})
        self._log.warning("stage=%s failure key=%s reason=%s", self.stage,
                          key, reason)
        self.event("failure", reason, status="error", key=key)
