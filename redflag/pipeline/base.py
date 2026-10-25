"""What every offline stage receives, and how it reports its stage gate."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from redflag.common.http import Fetcher
from redflag.common.log import StageLog
from redflag.config import Settings


@dataclass
class StageContext:
    settings: Settings
    conn: sqlite3.Connection
    fetcher: Fetcher
    log: StageLog
    limit: int | None = None        # cap rows for a quick probe run


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Gate:
    """A stage's "done when" checks (docs/DESIGN.md, stage gates)."""
    stage: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(Check(name, passed, detail))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def render(self) -> str:
        lines = [f"Gate {self.stage}: {'PASS' if self.passed else 'FAIL'}"]
        lines += [f"  [{'x' if c.passed else ' '}] {c.name}: {c.detail}"
                  for c in self.checks]
        return "\n".join(lines)
