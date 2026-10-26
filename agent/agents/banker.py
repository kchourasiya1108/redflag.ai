"""Banker: the lead manager's track record as of the bid date (flag F10)."""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent.features import LOW_HISTORY_N, banker_features


def f10_severity(rate: float | None) -> str:
    if rate is None:
        return "LOW"
    if rate >= 0.5:
        return "HIGH"
    return "ELEVATED" if rate >= 0.3 else "LOW"


class BankerTrackRecord(Agent):
    name = "BankerTrackRecord"
    role = ("Looks up the lead managers' earlier SME IPO outcomes known before as_of, "
            "through Zetaris, and computes the shrunk crash rate")

    def run(self, ctx: RunContext) -> AgentResult:
        feats = banker_features(ctx.port, ctx.ipo_id, ctx.as_of)
        ctx.features |= feats
        rate, n, base = feats["f_banker_crash"], feats["f_banker_n"], feats["base_rate"]
        evidence = ctx.refs("banker_track_record")
        if rate is None:
            why = ("this lead manager has no earlier SME IPOs in our records" if base is not None else
                   f"no SME IPO results were known before {ctx.as_of} to compare against")
            ctx.case.add_not_checked("F10 banker track record", why, agent=self.name)
            return AgentResult(why, target="Watchlist")
        thin = feats["f_banker_low_history"]
        severity = f10_severity(rate)
        if thin and severity == "LOW":
            # A short record is not a pass: say so instead of clearing the check.
            history = ("no earlier SME IPOs" if int(n) == 0 else
                       "only 1 earlier SME IPO" if int(n) == 1 else f"only {int(n)} earlier SME IPOs")
            ctx.case.add_not_checked("F10 banker track record", f"the lead manager has {history} "
                                     "with known results, too few to judge", agent=self.name)
            ctx.case.add_question(f"The lead manager, {ctx.meta['banker_raw']}, has {history} whose "
                                  "results after six months are known. How have the IPOs it "
                                  "managed performed?", agent=self.name,
                                  source=(evidence or [None])[0])
            return AgentResult(f"thin history: {int(n)} past IPOs", target="Watchlist")
        text = (f"{ctx.meta['banker_raw']}: {int(n)} earlier SME IPOs with a known 6-month "
                f"outcome before {ctx.as_of}; adjusted share that ended badly {rate:.2f} "
                f"against an average of {base:.2f}."
                + (f" Thin history: fewer than {LOW_HISTORY_N} known outcomes." if thin else ""))
        ctx.case.add_flag("F10", severity, value={
            "crash_rate": round(rate, 4), "n": int(n), "base_rate": round(base, 4),
            "median_ret_6m": feats["f_banker_median_ret"], "thin_history": bool(thin)},
            evidence=evidence, confidence=min(0.9, 0.4 + 0.05 * n), agent=self.name, text=text)
        return AgentResult(f"crash rate {rate:.2f} over {int(n)} past IPOs (base {base:.2f})",
                           target="Watchlist", confidence=rate)
