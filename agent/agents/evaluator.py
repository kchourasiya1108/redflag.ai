"""Evaluator: coverage and confidence; decides between re-planning and reporting."""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent.scoring import INSUFFICIENT

MAX_REPLANS = 2


class Evaluator(Agent):
    name = "Evaluator"
    role = "Checks coverage; sends gaps back to the Planner at most twice, else reports"

    def run(self, ctx: RunContext) -> AgentResult:
        score = ctx.case.case_file()["score"] or {}
        if score.get("band") == INSUFFICIENT:
            ctx.case.add_not_checked("score", "core features missing: "
                                     + ", ".join(score.get("missing_core", [])), agent=self.name)
            return AgentResult("INSUFFICIENT DATA: core features missing", target="Reporter")
        gaps = ctx.scratch.get("gaps", [])
        replans = ctx.scratch.get("replans", 0)
        if gaps and replans < MAX_REPLANS and ctx.seconds_left() > 60:
            ctx.scratch["replans"] = replans + 1
            return AgentResult(f"re-plan for {len(gaps)} gap(s): {', '.join(gaps)[:120]}",
                               target="Planner", data={"replan": True})
        for gap in gaps:
            ctx.case.add_question(f"Could not resolve: {gap}. Ask before applying.",
                                  agent=self.name)
        return AgentResult("coverage accepted", target="Reporter")
