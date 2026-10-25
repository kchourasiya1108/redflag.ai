"""Per-run trace: logs/trace-<run_id>.jsonl plus readable stdout.

Fields follow the hackathon's recommended trace format; anything extra goes
under `detail`. Values that look like secrets are masked.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

_SECRET_ENV = ("ZETARIS_API_KEY", "METERLESS_TOKEN", "METERLESS_RUNTIME_TOKEN",
               "NVIDIA_API_KEY", "RF_PG_PASSWORD",
               "AWS_SECRET_ACCESS_KEY")
_log = logging.getLogger("redflag.run")


def mask(text: str) -> str:
    for key in _SECRET_ENV:
        secret = os.environ.get(key)
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]{12,}", r"\1***", text)


class Tracer:
    def __init__(self, run_id: str, logs_dir: Path):
        self.run_id = run_id
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.path = logs_dir / f"trace-{run_id}.jsonl"
        self.events: list[dict] = []

    def event(self, agent_name: str, action: str, output_summary: str, *,
              input_summary: str = "", target_agent: str | None = None,
              confidence: float | None = None, retry_count: int = 0,
              status: str = "success", **detail) -> dict:
        record = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "run_id": self.run_id, "agent_name": agent_name, "action": action,
                  "input_summary": input_summary, "output_summary": output_summary,
                  "target_agent": target_agent, "confidence": confidence,
                  "retry_count": retry_count, "status": status}
        if detail:
            record["detail"] = detail
        line = mask(json.dumps(record, default=str))
        with self.path.open("a") as f:
            f.write(line + "\n")
        self.events.append(json.loads(line))
        _log.info("run_id=%s %s %s", self.run_id, agent_name, mask(output_summary))
        return record
