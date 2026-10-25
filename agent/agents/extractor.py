"""Extractor: the restated financials of the IPO's own RHP, into the Case File.

The same code as the offline stage (agent/extract.py): the deterministic
parser first, then Nemotron row mapping (only when a model is configured).
The Verifier's identities run on every statement; failed periods are kept
but marked unverified, so they are reported and never scored.
"""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent.agents.citations import value_on_page
from agent.extract import extract_financials, facts_from, top_customers
from agent.rhp import load_pages
from agent.specialists import CaseFileFacts


def rhp_pages(ctx: RunContext) -> tuple[list[str], dict | None]:
    docs = [d for d in ctx.port.run("ipo_rhp_doc", ipo_id=ctx.ipo_id).rows
            if d["doc_type"] == "rhp" and d["status"] == "ok"]
    if not docs:
        return [], None
    for folder in (ctx.cfg.cache / "pages", ctx.cfg.data / "sample" / "pages"):
        pages = load_pages(folder, docs[0]["sha256"])
        if pages:
            return pages, docs[0]
    return [], docs[0]


class Extractor(Agent):
    name = "Extractor"
    role = ("Reads the restated financials from the RHP: a deterministic table pass, then "
            "Nemotron mapping rows to fields when the numbers do not verify")
    uses_llm = True

    def run(self, ctx: RunContext) -> AgentResult:
        pages, doc = rhp_pages(ctx)
        ctx.scratch["pages"] = pages
        if not pages:
            why = "no RHP in the archive" if doc is None else "RHP has no cached text layer"
            ctx.case.add_not_checked("RHP financials (F1-F3)", why, agent=self.name)
            return AgentResult(why)

        def attempt(kind, method, best):
            ctx.tracer.event(self.name, "extract_attempt",
                             f"{kind} via {method}: verified {best.verified_periods or 'none'}",
                             retry_count=best.attempts - 1, detail_msg=best.detail[:160])
        ctx.scratch["gaps"] = []        # this pass's gaps only
        result = extract_financials(pages, gateway=ctx.gateway,     # replays in sample mode
                                    label=ctx.ipo_id, on_attempt=attempt,
                                    time_left=ctx.seconds_left)
        not_found = 0
        for f in facts_from(result) + [c for c in (top_customers(pages),) if c]:
            verified = f["verified"]
            if verified and f["value"]:
                # Citation check on the number itself: it must be printed on its page
                # (or the next one, where a statement continues), in the RHP's unit.
                text = "\n".join(pages[f["page"] - 1:f["page"] + 1])
                if not value_on_page(f["value"], text):
                    verified, not_found = False, not_found + 1
            ctx.case.add_fact(f["field"], f["fiscal_year"], f["value"], unit=f["unit"], page=f["page"],
                              verified=verified, method=f["method"]
                              + ("" if verified == f["verified"] else ":not_on_page"),
                              agent=self.name)
        for kind, x in result.items():
            for c in x.checks:
                ctx.case.set_verification(kind, c.period, passed=c.passed, attempts=x.attempts,
                                          agent="Verifier", detail="; ".join(c.failed or c.ran))
            if not x.verified_periods:
                ctx.case.add_not_checked(f"restated {kind.upper()} statement",
                                         "could not be read or did not pass the accounting "
                                         f"identities after {x.attempts} attempt(s)",
                                         agent="Verifier")
        ctx.scratch["facts"] = CaseFileFacts(ctx.case)
        # Only a model outage is a gap worth re-planning for; a table that does
        # not verify will not verify on a second identical pass.
        outages = [k for k, x in result.items()
                   if not x.verified_periods and "GatewayError" in x.detail]
        ctx.scratch["gaps"] = [f"restated {k.upper()} (model unavailable)" for k in outages]
        ok = {k: len(x.verified_periods) for k, x in result.items()}
        return AgentResult(f"verified periods per statement: {ok}"
                           + (f"; {not_found} value(s) not found on their page, unverified"
                              if not_found else ""), target="Specialists",
                           data={"methods": {k: x.method for k, x in result.items()}})
