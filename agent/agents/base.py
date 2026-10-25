"""What every agent gets (RunContext) and returns (AgentResult)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from agent.case_file import CaseFile
from agent.config import Settings
from agent.features import Named
from agent.tools_zetaris import DataPort, QueryResult
from agent.trace import Tracer


@dataclass
class RunContext:
    run_id: str
    ipo_id: str
    cfg: Settings
    tracer: Tracer
    client: Any                         # the Zetaris client (live or SQLite stand-in)
    source: str                         # human label for the data source
    sample_mode: bool
    deadline: float                     # perf_counter() value of the hard time limit
    gateway: Any = None                 # agent.llm.Gateway, or None when no model is set up
    memory: Any = None                  # agent.engines.hmem.HMem, or None
    as_of: date | None = None
    meta: dict = field(default_factory=dict)
    port: DataPort | None = None
    case: CaseFile | None = None
    named: list[Named] = field(default_factory=list)
    features: dict = field(default_factory=dict)
    queries: list[QueryResult] = field(default_factory=list)
    scratch: dict = field(default_factory=dict)     # agent-to-orchestrator signals

    def on_query(self, result: QueryResult) -> None:
        self.queries.append(result)
        self.tracer.event("DataPort", "data.query", f"{result.template} -> {len(result.rows)} rows",
                          input_summary=str(result.params), ref=result.ref, source=result.source,
                          guard_dropped=result.dropped_by_guard)

    def refs(self, template: str) -> list[str]:
        """Evidence references of every query of one template so far."""
        return [q.ref for q in self.queries if q.template == template]

    def seconds_left(self) -> float:
        return self.deadline - time.perf_counter()


@dataclass
class AgentResult:
    summary: str
    target: str | None = None
    confidence: float | None = None
    data: dict = field(default_factory=dict)


class Agent:
    name = "Agent"
    role = ""
    uses_llm = False

    def run(self, ctx: RunContext) -> AgentResult:
        raise NotImplementedError
