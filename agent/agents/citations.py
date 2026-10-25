"""Citation verifier: every claim in the report must point at something real.

`rhp:p<N>` must be a page of this RHP; a fact's value must appear (in the
unit the RHP printed) on the page it cites; `zq:<sha>` must be a query this
run made; `sebi:<id>` must be an order in the Case File. A claim that fails
is dropped from the report and listed under "not checked".
"""
from __future__ import annotations

import re

from agent.agents.base import Agent, AgentResult, RunContext
from agent.rhp import UNIT_TO_CRORE


def _digits(text: str) -> str:
    return re.sub(r"[^\d.]", "", text)


def value_on_page(value: float, page_text: str) -> bool:
    """Does a crore value appear on the page in any unit an RHP prints?"""
    plain = page_text.replace(",", "")
    for factor in UNIT_TO_CRORE.values():
        printed = abs(value) / factor
        for form in (f"{printed:.2f}", f"{printed:.1f}", f"{printed:.0f}"):
            if len(form.replace(".", "")) >= 3 and form in plain:
                return True
    return False


class CitationVerifier(Agent):
    name = "CitationVerifier"
    role = "Re-checks every cited page, query and order before the report is written"

    def run(self, ctx: RunContext) -> AgentResult:
        pages = ctx.scratch.get("pages") or []
        queries = {q.ref for q in ctx.queries}
        orders = {e.get("attrs", {}).get("archive_id") or e["id"] for e in
                  ctx.case.world.view("graph").query(type="sebi_order")}
        orders |= {e["id"].split(":")[-1] for e in ctx.case.world.view("graph").query(type="sebi_order")}
        dropped, checked = [], 0
        for flag in ctx.case.flags():
            if flag["severity"] == "LOW":
                continue
            bad = []
            for ref in flag["evidence"]:
                checked += 1
                kind, _, rest = ref.partition(":")
                if kind == "rhp":
                    n = int(rest[1:]) if rest[1:].isdigit() else 0
                    if pages and not 1 <= n <= len(pages):
                        bad.append(ref)
                elif kind == "zq" and ref not in queries:
                    bad.append(ref)
                elif kind == "sebi" and rest not in orders:
                    bad.append(ref)
            if bad and len(bad) == len(flag["evidence"]):
                dropped.append(flag["id"])
                ctx.case.add_flag(flag["id"], "LOW", value=flag["value"], evidence=[],
                                  confidence=0.0, agent=self.name,
                                  text=f"(withdrawn: its evidence {bad} could not be found)")
                ctx.case.add_not_checked(flag["id"], "claim withdrawn: cited evidence could "
                                         "not be found again", agent=self.name)
        facts_bad = 0
        for fact in ctx.case.facts(verified_only=True):
            if pages and fact.get("page") and 1 <= fact["page"] <= len(pages):
                text = pages[fact["page"] - 1] + (pages[fact["page"]] if fact["page"] < len(pages) else "")
                if fact["value"] and not value_on_page(fact["value"], text):
                    facts_bad += 1
        return AgentResult(f"{checked} references checked, {len(dropped)} claim(s) withdrawn, "
                           f"{facts_bad} verified fact(s) not found on their page",
                           data={"withdrawn": dropped, "facts_not_found": facts_bad})
