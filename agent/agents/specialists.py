"""Specialists: red flags computed in code from verified RHP facts (F1-F3)."""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent.specialists import SPECIALISTS


class Specialists(Agent):
    name = "Specialists"
    role = "Computes F1-F3 (paper profits, profit spike, eligibility gaming) in code"

    def run(self, ctx: RunContext) -> AgentResult:
        facts = ctx.scratch.get("facts")
        if facts is None or not facts.years():
            ctx.case.add_not_checked("F1-F3", "no verified restated financials", agent=self.name)
            return AgentResult("skipped: no verified financials")
        raised = []
        for specialist in SPECIALISTS:
            finding = specialist(facts)
            if finding is None:
                ctx.case.add_not_checked(specialist.__name__.split("_")[0].upper(),
                                         "needed numbers are missing or unverified",
                                         agent=self.name)
                continue
            ctx.case.add_flag(finding.flag, finding.severity, value=finding.value,
                              evidence=[f"rhp:p{p}" for p in finding.pages],
                              confidence=0.7, agent=self.name, text=finding.text)
            raised.append(f"{finding.flag}={finding.severity}")
        return AgentResult(", ".join(raised) or "no flags computable", target="Planner")
