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
            ctx.case.add_not_checked("F1-F3", "the financial statements could not be read reliably", agent=self.name)
            specialists = [s for s in specialists if s.__name__.startswith("f5")]
        for specialist in specialists if facts is not None else []:
            finding = specialist(facts)
            if finding is None:
                ctx.case.add_not_checked(specialist.__name__.split("_")[0].upper(),
                                         "the figures this needs could not be confirmed in the prospectus",
                                         agent=self.name)
                continue
            ctx.case.add_flag(finding.flag, finding.severity, value=finding.value,
                              evidence=[f"rhp:p{p}" for p in finding.pages],
                              confidence=0.7, agent=self.name, text=finding.text)
            raised.append(f"{finding.flag}={finding.severity}")
        f7 = self.auditor(ctx)
        if f7:
            raised.append(f7)
        f13 = self.market_maker(ctx)
        if f13:
            raised.append(f13)
        f13a = self.anchors(ctx)
        if f13a:
            raised.append(f13a)
        f19 = self.offer_for_sale(ctx)
        if f19:
            raised.append(f19)
        f12 = self.drift(ctx, facts)
        if f12:
            raised.append(f12)
        f14 = self.rating(ctx)
        if f14:
            raised.append(f14)
        return AgentResult(", ".join(raised) or "no flags computable", target="Planner")

    def auditor(self, ctx: RunContext) -> str | None:
        """F7: an auditor change before the IPO, or an auditor whose past IPOs often ended badly."""
        from agent.features import auditor_features
        feats = auditor_features(ctx.port, ctx.ipo_id, ctx.as_of, ctx.features.get("base_rate"))
        ctx.features |= feats
        rows = ctx.port.run("ipo_auditor", ipo_id=ctx.ipo_id).rows
        if not rows or feats["f_auditor_crash"] is None:
            ctx.case.add_not_checked("F7 auditor risk", "this auditor has no earlier SME IPOs in our records",
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

    def market_maker(self, ctx: RunContext) -> str | None:
        """F13 (market maker part): how the market maker's earlier SME IPOs turned out."""
        from agent.features import LOW_HISTORY_N, market_maker_features
        feats = market_maker_features(ctx.port, ctx.ipo_id, ctx.as_of, ctx.features.get("base_rate"))
        ctx.features |= feats
        rows = ctx.port.run("ipo_market_maker", ipo_id=ctx.ipo_id).rows
        if not rows or feats["f_mm_crash"] is None:
            ctx.case.add_not_checked("F13 market maker", "this market maker has no earlier SME "
                                     "IPOs in our records", agent=self.name)
            return None
        name, rate, n = rows[0]["canonical_name"], feats["f_mm_crash"], int(feats["f_mm_n"])
        if n < LOW_HISTORY_N:
            history = ("no earlier SME IPOs" if n == 0 else "only 1 earlier SME IPO" if n == 1
                       else f"only {n} earlier SME IPOs")
            ctx.case.add_not_checked("F13 market maker", f"the market maker has {history} with known "
                                     "results, too few to judge", agent=self.name)
            return None
        sev = "HIGH" if rate >= 0.5 else "ELEVATED" if rate >= 0.3 else "LOW"
        ctx.case.add_flag("F13", sev, value={"market_maker": name, "crash_rate": round(rate, 4), "n": n},
                          evidence=ctx.refs("market_maker_track_record")[:1],
                          confidence=0.5 if n < 10 else 0.65, agent=self.name,
                          text=f"Market maker {name}: {n} earlier SME IPOs with a known outcome, "
                               f"adjusted share that ended badly {rate:.2f}.")
        return f"F13={sev}"

    def offer_for_sale(self, ctx: RunContext) -> str | None:
        """F19: how much of the issue goes to selling shareholders."""
        from agent.features import issue_features
        from agent.specialists import f19_offer_for_sale
        ctx.features |= issue_features(ctx.port, ctx.ipo_id)
        rows = ctx.port.run("ipo_issue", ipo_id=ctx.ipo_id).rows
        finding = f19_offer_for_sale(rows[0] if rows else {}, ctx.as_of)
        if finding is None:
            ctx.case.add_not_checked("F19 offer for sale", "the issue's size and split could not "
                                     "be read", agent=self.name)
            return None
        ctx.case.add_flag("F19", finding.severity, value=finding.value,
                          evidence=ctx.refs("ipo_issue")[:1], confidence=0.8, agent=self.name,
                          text=finding.text)
        return f"F19={finding.severity}"

    def anchors(self, ctx: RunContext) -> str | None:
        """F13 (anchors): how earlier issues with the same anchor investors turned out.

        Recorded with the market maker's result under F13; the worse of the two speaks.
        """
        from agent.features import LOW_HISTORY_N, anchor_features
        feats = anchor_features(ctx.port, ctx.ipo_id, ctx.as_of, ctx.features.get("base_rate"))
        ctx.features |= feats
        anchors = ctx.port.run("ipo_anchors", ipo_id=ctx.ipo_id).rows
        if not anchors:
            ctx.case.add_not_checked("F13 anchor investors", "no anchor allocation report was "
                                     "found for this issue", agent=self.name)
            return None
        rate, n = feats["f_anchor_bad_rate"], int(feats["f_anchor_n"] or 0)
        top = ", ".join(a["name"] for a in anchors[:3])
        if rate is None or n < LOW_HISTORY_N:
            ctx.case.add_not_checked("F13 anchor investors", f"the anchors ({top}) backed "
                                     + ("no" if n == 0 else f"only {n}") + " earlier SME IPOs with "
                                     "known results, too few to judge",
                                     agent=self.name)
            return None
        sev = "HIGH" if rate >= 0.5 else "ELEVATED" if rate >= 0.3 else "LOW"
        prev = ctx.case.flag("F13") or {}
        order = ("LOW", "ELEVATED", "HIGH")
        if prev and order.index(prev.get("severity", "LOW")) > order.index(sev):
            return None             # the market maker's record is already worse
        ctx.case.add_flag("F13", sev, value={"anchors": [a["name"] for a in anchors],
                                             "crash_rate": round(rate, 4), "n": n},
                          evidence=ctx.refs("anchor_track_record")[:1],
                          confidence=0.5 if n < 10 else 0.65, agent=self.name,
                          text=f"Anchor investors {top}: {n} earlier SME IPOs they also backed "
                               f"have known outcomes, adjusted share that ended badly {rate:.2f}.")
        return f"F13={sev}"

    def drift(self, ctx: RunContext, facts) -> str | None:
        """F12: restated figures that changed between the draft and the final prospectus."""
        from agent.specialists import f12_drift
        rows = ctx.port.run("ipo_drhp_facts", ipo_id=ctx.ipo_id).rows
        if not rows:
            ctx.case.add_not_checked("F12 drift between drafts", "the draft prospectus (DRHP) "
                                     "is not in our records", agent=self.name)
            return None
        finding = f12_drift(facts, rows) if facts is not None else None
        if finding is None:
            ctx.case.add_not_checked("F12 drift between drafts", "no restated figure could be "
                                     "compared between the draft and the final prospectus",
                                     agent=self.name)
            return None
        ctx.case.add_flag("F12", finding.severity, value=finding.value,
                          evidence=[f"rhp:p{p}" for p in finding.pages] + ctx.refs("ipo_drhp_facts")[:1],
                          confidence=0.7, agent=self.name, text=finding.text)
        return f"F12={finding.severity}"

    def rating(self, ctx: RunContext) -> str | None:
        """F14: credit rating actions disclosed in the RHP."""
        from agent.specialists import f14_rating
        pages = ctx.scratch.get("pages") or []
        finding = f14_rating(pages) if pages else None
        if finding is None:
            ctx.case.add_not_checked("F14 credit rating actions", "the prospectus mentions no "
                                     "credit rating" if pages else "no prospectus text to read",
                                     agent=self.name)
            return None
        ctx.case.add_flag("F14", finding.severity, value=finding.value,
                          evidence=[f"rhp:p{p}" for p in finding.pages], confidence=0.6,
                          agent=self.name, text=finding.text)
        return f"F14={finding.severity}"
