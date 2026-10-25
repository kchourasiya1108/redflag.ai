"""compute_features: the one feature function shared by the backtest and the agents.

Everything is read through the Data Port as of the bid date. The only thing
that differs between a backtest and a live run is where RHP facts come from
(`facts`); banker features need no facts.
"""
from __future__ import annotations

from datetime import date
from statistics import median
from typing import Protocol

from agent.tools_zetaris import DataPort

SHRINK_K = 5            # pseudo-IPOs pulling a thin track record to the overall rate
LOW_HISTORY_N = 5       # fewer known outcomes than this is a "thin history"


class FactSource(Protocol):
    def get(self, field: str, fiscal_year: str = "") -> float | None:
        ...


def adverse_as_of(row: dict, as_of: date) -> bool:
    """A past IPO's outcome as it looked on as_of.

    The stored `adverse` column can flip later, when a SEBI order is issued
    after as_of, so it is never read here.
    """
    sebi = bool(row["sebi_action"]) and row["sebi_action_date"] is not None \
        and row["sebi_action_date"] < as_of
    return bool(row["crashed"]) or bool(row["suspended"]) or sebi


def banker_features(data: DataPort, ipo_id: str, as_of: date) -> dict[str, float | None]:
    everyone = data.run("outcomes_known_before", as_of=as_of).rows
    base_rate = (sum(adverse_as_of(r, as_of) for r in everyone) / len(everyone)
                 if everyone else None)
    bankers = data.run("ipo_bankers", ipo_id=ipo_id).rows
    worst = None
    for b in bankers:
        history = [r for r in data.run("banker_track_record", banker_id=b["banker_id"],
                                       as_of=as_of).rows if r["ipo_id"] != ipo_id]
        n = len(history)
        bad = sum(adverse_as_of(r, as_of) for r in history)
        rate = ((bad + SHRINK_K * base_rate) / (n + SHRINK_K)
                if base_rate is not None else None)
        returns = [r["ret_6m"] for r in history if r["ret_6m"] is not None]
        stats = {"f_banker_crash": rate, "f_banker_n": float(n),
                 "f_banker_low_history": float(n < LOW_HISTORY_N),
                 "f_banker_median_ret": median(returns) if returns else None,
                 "banker_id": b["banker_id"]}
        # With several lead managers, the riskiest one speaks for the issue.
        if worst is None or (rate or 0) > (worst["f_banker_crash"] or 0):
            worst = stats
    if worst is None:
        return {"f_banker_crash": None, "f_banker_n": None,
                "f_banker_low_history": None, "f_banker_median_ret": None,
                "base_rate": base_rate}
    worst.pop("banker_id")
    return worst | {"base_rate": base_rate}


def compute_features(ipo_id: str, as_of: date, data: DataPort,
                     facts: FactSource | None = None) -> dict[str, float | None]:
    if data.as_of != as_of:
        raise ValueError("the Data Port must be bound to the same as_of")
    features = banker_features(data, ipo_id, as_of)
    # RHP features (F1-F9) join here once extraction lands; they read `facts`.
    return features
