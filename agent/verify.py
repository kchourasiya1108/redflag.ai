"""The Verifier's accounting identities, per statement and per period (no LLM).

A statement passes for a period only when every identity that can be
evaluated holds within tolerance, and at least one could be evaluated. Values
are in crore. The tolerance is 2% of the larger side, with a floor of
₹0.05 cr (5 lakh), so rounding in the RHP's own tables never fails a check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REL_TOL, ABS_TOL = 0.02, 0.05


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(REL_TOL * max(abs(a), abs(b)), ABS_TOL)


@dataclass
class Check:
    statement: str
    period: str
    passed: bool
    ran: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _get(values: dict, name: str, i: int) -> float | None:
    col = values.get(name)
    return col[i] if col and i < len(col) else None


def check_pl(values: dict, i: int) -> tuple[list[str], list[str]]:
    ran, failed = [], []
    rev, oi, ti = (_get(values, k, i) for k in ("revenue", "other_income", "total_income"))
    if None not in (rev, oi, ti):
        ran.append("total income = revenue + other income")
        if not close(ti, rev + oi):
            failed.append(ran[-1])
    te, pbt, pat = (_get(values, k, i) for k in ("total_expenses", "pbt", "pat"))
    if None not in (ti, te, pbt):
        ran.append("PBT = total income - total expenses")
        if not close(pbt, ti - te) and not close(abs(pbt), abs(ti - te)):
            failed.append(ran[-1])
    tax = _get(values, "tax", i)
    if None not in (pbt, pat, tax) and close(pbt - tax, pat):
        # The printed tax line closes the statement exactly: stronger than the band
        # below, and right for a loss year or a large deferred tax charge.
        ran.append("PAT = PBT - tax")
    elif None not in (pbt, pat):
        ran.append("tax (PBT - PAT) is plausible")
        tax = pbt - pat
        if not (-0.3 * abs(pbt) - ABS_TOL <= tax <= 0.6 * abs(pbt) + ABS_TOL):
            failed.append(ran[-1])
    return ran, failed


def check_bs(values: dict, totals: list[list[float]], i: int) -> tuple[list[str], list[str]]:
    """Assets against equity plus liabilities, however the RHP totals its two sides."""
    ran, failed = [], []
    assets, eq, eq_liab, liab, ncl, cl = (_get(values, k, i) for k in (
        "total_assets", "equity", "total_eq_liab", "total_liabilities", "nc_liabilities",
        "c_liabilities"))
    other_sides = [v for v in (eq_liab,
                               eq + liab if None not in (eq, liab) else None,
                               eq + ncl + cl if None not in (eq, ncl, cl) else None)
                   if v is not None]
    col = [t[i] for t in totals if i < len(t)]
    if assets is not None and other_sides:
        ran.append("assets = equity + liabilities")
        if not any(close(assets, v) for v in other_sides):
            failed.append(ran[-1])
    elif len(col) >= 2:                     # older format: two bare "TOTAL" rows
        ran.append("assets = equity + liabilities")
        top = max(col)
        if sum(close(top, v) for v in col) < 2:
            failed.append(ran[-1])
    return ran, failed


def check_cf(values: dict, i: int) -> tuple[list[str], list[str]]:
    ran, failed = [], []
    cfo, cfi, cff = (_get(values, k, i) for k in ("cfo", "cfi", "cff"))
    if None in (cfo, cfi, cff):
        return ran, failed
    opening, closing, change = (_get(values, k, i) for k in ("opening_cash", "closing_cash",
                                                             "net_change"))
    if None not in (opening, closing):
        ran.append("CFO + CFI + CFF = change in cash")
        # Opening and closing cash can be defined differently from the printed net change
        # (cash vs cash and bank); the printed net change closing the flows also verifies.
        if not close(cfo + cfi + cff, closing - opening) and not (
                change is not None and close(cfo + cfi + cff, change)):
            failed.append(ran[-1])
    elif change is not None:
        ran.append("CFO + CFI + CFF = net change in cash")
        if not close(cfo + cfi + cff, change):
            failed.append(ran[-1])
    return ran, failed


def verify(statement) -> list[Check]:
    """One Check per period of a parsed agent.rhp.Statement."""
    out = []
    for i, period in enumerate(statement.periods):
        if statement.kind == "pl":
            ran, failed = check_pl(statement.values, i)
        elif statement.kind == "bs":
            ran, failed = check_bs(statement.values, statement.totals, i)
        else:
            ran, failed = check_cf(statement.values, i)
        out.append(Check(statement.kind, period.label, bool(ran) and not failed, ran, failed))
    return out
