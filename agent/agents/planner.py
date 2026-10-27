"""Planner: picks the investigation depth from the cheap lookups (rules only)."""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext

LIGHT, STANDARD, FORENSIC = "LIGHT", "STANDARD", "FORENSIC"
RANK = {None: -1, LIGHT: 0, STANDARD: 1, FORENSIC: 2}


def choose_depth(features: dict, flags: list[dict], insider_discount: float | None = None
                 ) -> tuple[str, str]:
    """The design's depth policy. Returns (depth, reason)."""
    rate, n = features.get("f_banker_crash"), features.get("f_banker_n") or 0
    highs = [f["id"] for f in flags if f["severity"] == "HIGH"]
    if rate is not None and rate >= 0.5:
        return FORENSIC, f"banker crash rate {rate:.2f} >= 0.5"
    if features.get("f_watchlist_hit"):
        return FORENSIC, "confirmed watchlist link"
    if len(highs) >= 2:
        return FORENSIC, f"two or more HIGH flags ({', '.join(highs)})"
    if insider_discount is not None and insider_discount >= 5:
        return FORENSIC, f"insider discount {insider_discount:.1f}x >= 5x"
    if rate is not None and rate < 0.3 and n >= 5 and not features.get("f_watchlist_pending"):
        return LIGHT, f"clean banker ({rate:.2f} over {int(n)} IPOs) and no watchlist link"
    return STANDARD, "default"


class Planner(Agent):
    name = "Planner"
    role = "Chooses LIGHT, STANDARD or FORENSIC depth and escalates mid-run"

    def run(self, ctx: RunContext) -> AgentResult:
        discount = (ctx.case.flag("F4") or {}).get("value", {}).get("insider_discount")
        depth, reason = choose_depth(ctx.features, ctx.case.flags(), discount)
        before = ctx.case.meta["depth"]
        ctx.case.set_depth(depth, reason)
        verb = ("set to" if before is None or before == depth else
                "escalated to" if RANK[depth] > RANK[before] else "lowered to")
        return AgentResult(f"depth {verb} {depth}: {reason}",
                           data={"depth": depth, "changed": before != depth})
