"""Skeptic: argues the innocent side of every ELEVATED or HIGH flag.

For each flag it searches the RHP for pages that could explain it (an order
book behind a revenue jump, a capex plan behind working capital), and asks
Nemotron (the reasoning role) for a ruling that must cite a page. A ruling
without evidence is turned into "upheld". Then the corroboration rule: a HIGH
flag stays HIGH only when a second, independent signal supports it.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from agent.agents.base import Agent, AgentResult, RunContext
from agent.agents.planner import FORENSIC, LIGHT

KEYWORDS = {
    "F1": r"order\s+book|credit\s+period|receivable|collection|working\s+capital\s+cycle",
    "F2": r"order\s+book|new\s+(?:contract|customer|order)|capacity\s+(?:expansion|utili)|"
          r"one[\s-]+(?:time|off)|exceptional",
    "F3": r"eligib|ebitda|operating\s+profit",
    "F4": r"preferential|private\s+placement|employee|esop|conversion",
    "F5": r"top\s+(?:ten|10|five|5)\s+customers|long[\s-]+term\s+contract",
    "F9": r"working\s+capital\s+requirement|basis\s+of\s+estimation|holding\s+(?:period|days)",
    "F10": r"lead\s+manager|merchant\s+banker",
    "F11": r"litigation|regulatory\s+action|sebi",
}


class Ruling(BaseModel):
    ruling: Literal["upheld", "downgraded", "dismissed"]
    reason: str = Field(max_length=600)
    evidence: list[str] = []         # rhp:p<N> references, from the pages shown


def candidate_pages(pages: list[str], flag: str, limit: int = 3) -> list[int]:
    pattern = KEYWORDS.get(flag)
    if not pattern or not pages:
        return []
    scored = sorted(((len(re.findall(pattern, t, re.I)), -i, i) for i, t in enumerate(pages, 1)
                     if re.search(pattern, t, re.I)), reverse=True)
    return [i for *_, i in scored[:limit]]


class Skeptic(Agent):
    name = "Skeptic"
    role = ("Argues both sides of each ELEVATED or HIGH flag with Nemotron, citing RHP pages; "
            "applies the corroboration rule to HIGH flags")
    uses_llm = True

    def run(self, ctx: RunContext) -> AgentResult:
        flags = [f for f in ctx.case.flags() if f["severity"] in ("HIGH", "ELEVATED")]
        depth = ctx.case.meta.get("depth")
        if depth == LIGHT and flags:
            flags = sorted(flags, key=lambda f: (f["severity"] != "HIGH", -f["confidence"]))[:1]
        rulings = []
        pages = ctx.scratch.get("pages") or []
        for flag in flags:
            if ctx.gateway is None or ctx.seconds_left() < 90:
                break
            pick = candidate_pages(pages, flag["id"], 4 if depth == FORENSIC else 3)
            excerpt = "\n\n".join(f"[page {p}]\n{pages[p - 1][:2200]}" for p in pick)
            context = ctx.case.context(f"skeptic:{flag['id']}", mentions=[flag["id"]],
                                       budget_chars=2500).as_prompt_block()
            if ctx.memory is not None:          # earlier rulings on the same kind of flag
                context += "\n" + ctx.memory.context_block(ctx.memory.retrieve(
                    f"skeptic {flag['id']} {flag['text']}", as_of=ctx.as_of,
                    tags=["skeptic", flag["id"]]))
            prompt = (f"Red flag {flag['id']} ({flag['severity']}): {flag['text']}\n"
                      f"Numbers: {flag['value']}\n\n{context}\n\nRHP pages that may explain it:\n"
                      f"{excerpt or '(none found)'}\n\nArgue the innocent side. Is there a "
                      "disclosed, ordinary explanation (e.g. an order book, a capex plan, a "
                      "one-off)? Rule 'dismissed' only if the explanation fully accounts for "
                      "it, 'downgraded' if it partly does, else 'upheld'. Cite only the pages "
                      "shown, as rhp:p<N>. Reply with JSON: ruling, reason, evidence.")
            try:
                res = ctx.gateway.generate(prompt, "You are the Skeptic on an IPO risk team. "
                                           "Be fair to the company and exact about evidence.",
                                           schema=Ruling, route="reason",
                                           purpose=f"skeptic:{flag['id']}")
                ruling = res.parsed
            except Exception as e:      # the model is down: the flag stands as computed
                ctx.case.add_not_checked(f"Skeptic on {flag['id']}", f"model unavailable: "
                                         f"{type(e).__name__}", agent=self.name)
                continue
            shown = {f"rhp:p{p}" for p in pick}
            evidence = [e for e in ruling.evidence if e in shown]
            verdict, reason = ruling.ruling, ruling.reason
            if verdict != "upheld" and not evidence:
                verdict, reason = "upheld", reason + " (No page was cited, so the flag stands.)"
            new = {"upheld": None, "downgraded": {"HIGH": "ELEVATED", "ELEVATED": "LOW"},
                   "dismissed": {"HIGH": "LOW", "ELEVATED": "LOW"}}[verdict]
            ctx.case.rule_on_flag(flag["id"], verdict, reason, evidence, agent=self.name,
                                  severity=new[flag["severity"]] if new else None)
            rulings.append(f"{flag['id']}:{verdict}")
        held = self.corroborate(ctx)
        return AgentResult(f"rulings {', '.join(rulings) or 'none'}"
                           + (f"; HIGH kept by corroboration: {held}" if held else ""))

    def corroborate(self, ctx: RunContext) -> list[str]:
        """A HIGH flag needs a second, independent ELEVATED/HIGH signal to stay HIGH."""
        flags = ctx.case.flags()
        kept = []
        for f in flags:
            if f["severity"] != "HIGH":
                continue
            others = [g["id"] for g in flags if g["id"] != f["id"]
                      and g["severity"] in ("HIGH", "ELEVATED")]
            if others:
                kept.append(f["id"])
                continue
            skeptic = f.get("skeptic") or {}
            ctx.case.rule_on_flag(
                f["id"], skeptic.get("ruling", "corroboration"),
                (skeptic.get("reason", "") + " " if skeptic else "")
                + "No second, independent signal supports it, so it is held at ELEVATED.",
                skeptic.get("evidence", []), agent=self.name, severity="ELEVATED")
        return kept
