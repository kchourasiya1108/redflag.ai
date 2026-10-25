"""Specialists: red flags computed in code from verified RHP facts (F1-F3)."""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent.specialists import SPECIALISTS


class Specialists(Agent):
    name = "Specialists"
    role = ("Computes F1-F3 and F5 (paper profits, profit spike, eligibility gaming, "
            "customer concentration) in code; F7 auditor risk from the archive")

    def run(self, ctx: RunContext) -> AgentResult:
        facts = ctx.scratch.get("facts")
        raised = []
        specialists = list(SPECIALISTS)
        if facts is None or not facts.years():
            ctx.case.add_not_checked("F1-F3", "no verified restated financials", agent=self.name)
            specialists = [s for s in specialists if s.__name__.startswith("f5")]
        for specialist in specialists if facts is not None else []:
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
        f7 = self.auditor(ctx)
        if f7:
            raised.append(f7)
        return AgentResult(", ".join(raised) or "no flags computable", target="Planner")

    def auditor(self, ctx: RunContext) -> str | None:
        """F7: an auditor change before the IPO, or an auditor whose past IPOs often ended badly."""
        from agent.features import auditor_features
        feats = auditor_features(ctx.port, ctx.ipo_id, ctx.as_of, ctx.features.get("base_rate"))
        ctx.features |= feats
        rows = ctx.port.run("ipo_auditor", ipo_id=ctx.ipo_id).rows
        if not rows or feats["f_auditor_crash"] is None:
            ctx.case.add_not_checked("F7 auditor risk", "auditor not in the archive",
                                     agent=self.name)
            return None
        a, rate, n = rows[0], feats["f_auditor_crash"], int(feats["f_auditor_n"])
        changed = feats["f_auditor_changed"] == 1.0
        sev = "HIGH" if changed and rate >= 0.5 else "ELEVATED" if changed or rate >= 0.4 else "LOW"
        text = (f"Statutory auditor {a['canonical_name']}: {n} earlier SME IPOs with a known "
                f"outcome, adjusted share that ended badly {rate:.2f}"
                + ("; the auditor changed in the 3 years before the RHP" if changed else "")
                + ".")
        ctx.case.add_flag("F7", sev, value={"auditor": a["canonical_name"], "changed_3y": changed,
                                            "crash_rate": round(rate, 4), "n": n},
                          evidence=ctx.refs("auditor_track_record")[:1]
                          + ([f"rhp:p{a['page']}"] if a["page"] else []),
                          confidence=0.5 if n < 5 else 0.65, agent=self.name, text=text)
        return f"F7={sev}"
