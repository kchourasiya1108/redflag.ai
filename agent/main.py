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
from agent.agents.citations import CitationVerifier
from agent.agents.base import Agent, AgentResult, RunContext
from agent.agents.evaluator import Evaluator
from agent.agents.extractor import Extractor
from agent.agents.intake import Intake, UnknownIPO
from agent.agents.names import Names
from agent.agents.narrative import NarrativeReader
from agent.agents.planner import Planner
from agent.agents.reporter import DISCLAIMER, Reporter
from agent.agents.scorer import Scorer
from agent.agents.skeptic import Skeptic
from agent.agents.specialists import Specialists
from agent.agents.watchlist import Watchlist
from agent.config import Settings
from agent.tools_zetaris import MockZetaris, ZetarisRestClient
from agent.trace import Tracer

__all__ = ["investigate", "RunError", "DISCLAIMER", "Orchestrator"]

TARGET_S, HARD_LIMIT_S = 180, 600

# The order of a run. "Escalation" is the Planner's second look after the
# specialists (two HIGH flags), before the narrative sections are read, so an
# escalation can still widen what is read; "Escalation2" looks again after the
# narrative (a >= 5x insider discount). Citations are re-checked before the
# Skeptic's corroboration rule and the score. Missing agents are skipped.
PIPELINE = ["Intake", "Names", "BankerTrackRecord", "Watchlist", "Planner",
            "Extractor", "Specialists", "Escalation", "NarrativeReader", "Escalation2",
            "CitationVerifier", "Skeptic", "Scorer", "Evaluator", "Reporter"]


class RunError(RuntimeError):
    def __init__(self, type_: str, message: str, recoverable: bool):
        super().__init__(message)
        self.type, self.recoverable = type_, recoverable


def model_gateway(cfg: Settings, sample_mode: bool, tracer: Tracer):
    """The Model Gateway for one run: Nightshift live, recorded replies in sample mode.

    Every call is traced with the model that actually served it (the Nemotron
    evidence), its tokens, and whether it came from the cache or a replay.
    """
    from agent.llm import gateway_from_env

    def on_call(e: dict) -> None:
        if e["action"] == "model.call":
            tracer.event("ModelGateway", "model.call",
                         f"{e['purpose']}: {e['served_model'] or e['route']} "
                         f"({e['tokens_in']}+{e['tokens_out']} tokens"
                         + (", cached" if e["cached"] else ", replayed" if e["replayed"] else "")
                         + ")", **e)
        else:
            tracer.event("ModelGateway", e["action"], e.get("error", "")[:160], status="retry",
                         **{k: v for k, v in e.items() if k != "action"})
    gateway = gateway_from_env(cfg.data, on_call=on_call)
    gateway.sample_mode = sample_mode
    return gateway if gateway.providers or sample_mode else None


def data_client(cfg: Settings, sample_mode: bool):
    if sample_mode:
        return MockZetaris(cfg.data / "sample" / "archive_sample.db"), "sample (mock-sqlite)"
    if os.environ.get("ZETARIS_REST_URL") and os.environ.get("ZETARIS_API_KEY"):
        return ZetarisRestClient(), "zetaris"
    return MockZetaris(cfg.archive_db), "local archive (mock-sqlite; Zetaris not configured)"


class Orchestrator:
    def __init__(self, agents: list[Agent] | None = None):
        agents = agents or [Intake(), Names(), BankerTrackRecord(), Watchlist(), Planner(),
                            Extractor(), Specialists(), NarrativeReader(), Skeptic(), Scorer(),
                            Evaluator(), CitationVerifier(), Reporter()]
        self.agents = {a.name: a for a in agents}
        if "Planner" in self.agents:    # the Planner's second and third looks
            self.agents["Escalation"] = self.agents["Escalation2"] = self.agents["Planner"]

    def next_state(self, current: str, result: AgentResult, ctx: RunContext) -> str | None:
        if current == "Reporter":
            return None
        if ctx.seconds_left() <= 0 and current not in ("Scorer", "Evaluator"):
            if not ctx.scratch.get("partial"):
                ctx.scratch["partial"] = ctx.scratch["cut_at"] = current
            return "Scorer" if "score" not in ctx.scratch else "Reporter"
        if current == "Evaluator" and result.target == "Planner":
            return "Planner"            # a re-plan re-runs the depth's work, not the lookups
        if current == "Planner" and ctx.scratch.get("replans"):
            return "Extractor" if "Extractor" in self.agents else self._after(current)
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
            ctx.scratch["state"] = state
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
    return {"Intake": "start", "Planner": "plan", "Escalation": "escalation_check",
            "Escalation2": "escalation_check",
            "Scorer": "score", "Evaluator": "evaluate",
            "Reporter": "final_synthesis"}.get(state, "delegate_task")


def investigate(cfg: Settings, ipo_id: str, *, run_id: str | None = None,
                sample_mode: bool | None = None, gateway=None, memory=None) -> dict:
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    sample_mode = (os.environ.get("SAMPLE_MODE", "false").lower() == "true"
                   if sample_mode is None else sample_mode)
    started = time.perf_counter()
    tracer = Tracer(run_id, cfg.logs)
    client, source = data_client(cfg, sample_mode)
    if gateway is None:
        gateway = model_gateway(cfg, sample_mode, tracer)
    if memory is None:
        from agent.engines.hmem import HMem
        # Sample runs get a fresh, empty memory so they never depend on run order.
        path = (cfg.logs / run_id / "memory.db") if sample_mode else (cfg.data / "memory.db")
        memory = HMem(path, on_event=lambda e: tracer.event(
            "HMEM", e["action"], f"{e['action']} {e.get('query', e.get('id', ''))}"[:120],
            engine="hmem", **{k: v for k, v in e.items() if k not in ("action", "engine")}))
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
