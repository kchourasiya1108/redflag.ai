"""The agent loop: a deterministic Orchestrator over RedFlag's agents.

One investigation run, shared by run.py and the FastAPI app. The state
machine (docs/DESIGN.md, Orchestrator) runs the cheap, high-signal lookups
first (names, banker, watchlist), lets the Planner pick a depth, runs the
depth's work, scores with the backtest's own feature function, lets the
Evaluator send gaps back to the Planner (at most twice), and reports. Every
transition and its reason goes to the trace. Data goes through the Data Port
(Zetaris live, or the SQLite stand-in); shared state is the Case File (the
World Model engine).
"""
from __future__ import annotations

import os
import time
import uuid

from agent.agents.banker import BankerTrackRecord
from agent.agents.base import Agent, AgentResult, RunContext
from agent.agents.evaluator import Evaluator
from agent.agents.intake import Intake, UnknownIPO
from agent.agents.names import Names
from agent.agents.planner import Planner
from agent.agents.reporter import DISCLAIMER, Reporter
from agent.agents.scorer import Scorer
from agent.agents.watchlist import Watchlist
from agent.config import Settings
from agent.tools_zetaris import MockZetaris, ZetarisRestClient
from agent.trace import Tracer

__all__ = ["investigate", "RunError", "DISCLAIMER", "Orchestrator"]

TARGET_S, HARD_LIMIT_S = 180, 600

# The fixed order of a run. Optional stages (extraction, specialists, the
# Skeptic) are skipped, with a note, until their agents are present.
PIPELINE = ["Intake", "Names", "BankerTrackRecord", "Watchlist", "Planner",
            "Scorer", "Evaluator", "Reporter"]


class RunError(RuntimeError):
    def __init__(self, type_: str, message: str, recoverable: bool):
        super().__init__(message)
        self.type, self.recoverable = type_, recoverable


def data_client(cfg: Settings, sample_mode: bool):
    if sample_mode:
        return MockZetaris(cfg.data / "sample" / "archive_sample.db"), "sample (mock-sqlite)"
    if os.environ.get("ZETARIS_REST_URL") and os.environ.get("ZETARIS_API_KEY"):
        return ZetarisRestClient(), "zetaris"
    return MockZetaris(cfg.archive_db), "local archive (mock-sqlite; Zetaris not configured)"


class Orchestrator:
    def __init__(self, agents: list[Agent] | None = None):
        agents = agents or [Intake(), Names(), BankerTrackRecord(), Watchlist(), Planner(),
                            Scorer(), Evaluator(), Reporter()]
        self.agents = {a.name: a for a in agents}

    def next_state(self, current: str, result: AgentResult, ctx: RunContext) -> str | None:
        if current == "Reporter":
            return None
        if ctx.seconds_left() <= 0 and current not in ("Scorer", "Evaluator"):
            if not ctx.scratch.get("partial"):
                ctx.scratch["partial"] = ctx.scratch["cut_at"] = current
            return "Scorer" if "score" not in ctx.scratch else "Reporter"
        if current == "Evaluator" and result.target == "Planner":
            return "Planner"            # a re-plan re-runs the depth's work, not the lookups
        return self._after(current)

    def _after(self, current: str) -> str:
        i = PIPELINE.index(current)
        for name in PIPELINE[i + 1:]:
            if name in self.agents:
                return name
        return "Reporter"

    def run(self, ctx: RunContext) -> None:
        state: str | None = "Intake"
        while state:
            agent = self.agents[state]
            t0 = time.perf_counter()
            result = agent.run(ctx)
            if state == "Scorer":
                ctx.scratch["score"] = True
            ctx.tracer.event(agent.name, _action(state), result.summary,
                             target_agent=result.target, confidence=result.confidence,
                             elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))
            nxt = self.next_state(state, result, ctx)
            if nxt:
                cut = ctx.scratch.pop("cut_at", None) == state
                ctx.tracer.event("Orchestrator", "transition", f"{state} -> {nxt}",
                                 target_agent=nxt,
                                 reason="hard time limit reached: scoring with what we have"
                                 if cut else result.summary[:120])
            state = nxt


def _action(state: str) -> str:
    return {"Intake": "start", "Planner": "plan", "Scorer": "score", "Evaluator": "evaluate",
            "Reporter": "final_synthesis"}.get(state, "delegate_task")


def investigate(cfg: Settings, ipo_id: str, *, run_id: str | None = None,
                sample_mode: bool | None = None, gateway=None, memory=None) -> dict:
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    sample_mode = (os.environ.get("SAMPLE_MODE", "false").lower() == "true"
                   if sample_mode is None else sample_mode)
    started = time.perf_counter()
    tracer = Tracer(run_id, cfg.logs)
    client, source = data_client(cfg, sample_mode)
    ctx = RunContext(run_id=run_id, ipo_id=ipo_id, cfg=cfg, tracer=tracer, client=client,
                     source=source, sample_mode=sample_mode,
                     deadline=started + HARD_LIMIT_S, gateway=gateway, memory=memory)
    orchestrator = Orchestrator()
    tracer.event("Orchestrator", "start", f"investigating {ipo_id}", input_summary=ipo_id,
                 data_source=source, sample_mode=sample_mode)
    try:
        orchestrator.run(ctx)
    except UnknownIPO as e:
        tracer.event("Intake", "error", str(e), status="error")
        raise RunError("UnknownIPO", str(e), recoverable=False) from e
    output = ctx.scratch["output"]
    return {"run_id": run_id, "status": "partial" if ctx.scratch.get("partial") else "success",
            "output": output,
            "agents": [{"name": a.name, "role": a.role, "uses_llm": a.uses_llm}
                       for a in orchestrator.agents.values()],
            "trace_id": f"trace-{run_id}", "log_file": str(tracer.path),
            "case_file": str(ctx.scratch["output_dir"] / "case_file.json"),
            "report": str(ctx.scratch["output_dir"] / "report.md"),
            "execution_time_seconds": round(time.perf_counter() - started, 2)}
