"""The RHP-number specialists: formulas F1-F3 (data guide sections 9.1 and 10).

Everything here is code, never the model, and reads only *verified* facts
(an unverified statement is invisible, so it can never move a score). The
same functions feed `compute_features` for the backtest and the flags a live
run reports. Thresholds are the guide's starting values; the backtest decides
how much each one matters.

Facts are keyed `<statement>.<field>` (pl.revenue, bs.receivables, cf.cfo)
and by period label (FY24 for a March year-end; stub periods start with S and
are never compared with full years).
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Protocol

from agent.tools_zetaris import DataPort

ELIGIBILITY_FLOOR_CR = 1.0      # SME eligibility: operating profit (EBITDA) of ₹1 cr
HEADROOM_BAND_CR = 0.2


class FactSource(Protocol):
    def get(self, field: str, fiscal_year: str) -> float | None: ...
    def years(self) -> list[str]: ...
    def page(self, field: str, fiscal_year: str) -> int | None: ...


class _Facts:
    """Verified facts in memory: {(field, fy): (value, page)}."""

    def __init__(self, rows: list[dict]):
        # An undated fact (a share of revenue) has fiscal_year "" (None once read back).
        self._v = {(r["field"], r["fiscal_year"] or ""): (r["value"], r.get("page"))
                   for r in rows if r.get("verified") and r.get("value") is not None}

    def get(self, field: str, fiscal_year: str) -> float | None:
        hit = self._v.get((field, fiscal_year))
        return hit[0] if hit else None

    def page(self, field: str, fiscal_year: str) -> int | None:
        hit = self._v.get((field, fiscal_year))
        return hit[1] if hit else None

    def years(self) -> list[str]:
        """Full financial years, newest first (undated facts such as shares are not years)."""
        return sorted({fy for _, fy in self._v if fy.startswith("FY")}, reverse=True)


class ArchiveFacts(_Facts):
    """Backtest: past RHP extractions, read through the Data Port (Zetaris)."""

    def __init__(self, data: DataPort, ipo_id: str):
        super().__init__(data.run("ipo_facts", ipo_id=ipo_id).rows)


class CaseFileFacts(_Facts):
    """Live run: the facts the Extractor wrote to the Case File."""

    def __init__(self, case):
        super().__init__([{"field": f["field"], "fiscal_year": f["fy"], "value": f["value"],
                           "page": f["page"], "verified": f["verified"]} for f in case.facts()])


def _ratio(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or b == 0 else a / b


def _growth(now: float | None, before: float | None) -> float | None:
    return None if now is None or before is None or before <= 0 else now / before - 1


@dataclass
class Finding:
    flag: str
    severity: str           # HIGH | ELEVATED | LOW
    value: dict
    text: str
    pages: list[int]


def ebitda(f: FactSource, fy: str) -> float | None:
    """PBT + finance costs + depreciation − other income (data guide, EBITDA)."""
    parts = [f.get(k, fy) for k in ("pl.pbt", "pl.finance_cost", "pl.depreciation")]
    if None in parts:
        return None
    return parts[0] + parts[1] + parts[2] - (f.get("pl.other_income", fy) or 0.0)


def rhp_features(f: FactSource) -> dict[str, float | None]:
    """Model inputs from the restated numbers (None where a verified fact is missing)."""
    ys = f.years()
    t, t1, t2 = (ys + [None, None, None])[:3]
    pat = f.get("pl.pat", t) if t else None
    cfo = f.get("cf.cfo", t) if t else None
    prior = [f.get("pl.pat", y) for y in (t1, t2) if y]
    prior = [p for p in prior if p is not None]
    heads = [e - ELIGIBILITY_FLOOR_CR for e in (ebitda(f, y) for y in ys[:3]) if e is not None]
    recv_g = _growth(f.get("bs.receivables", t), f.get("bs.receivables", t1)) if t1 else None
    rev_g = _growth(f.get("pl.revenue", t), f.get("pl.revenue", t1)) if t1 else None
    return {
        "f_cfo_pat": _ratio(cfo, pat) if pat and pat > 0 else None,
        "f_cfo_neg_pat_pos": None if None in (cfo, pat) else float(cfo < 0 < pat),
        "f_recv_rev_gap": None if None in (recv_g, rev_g) else recv_g - rev_g,
        "f_profit_spike": (pat / mean(prior) if pat is not None and len(prior) == 2
                           and mean(prior) > 0 else None),
        "f_ebitda_headroom_min": min(heads) if heads else None,
        "f_other_income_share": _ratio(f.get("pl.other_income", t), f.get("pl.pbt", t))
        if t else None,
        "f_rev_growth": rev_g,
        "f_top10_customers": f.get("biz.top10_customers_pct", ""),
    }


def f1_paper_profits(f: FactSource) -> Finding | None:
    ys = f.years()
    if not ys:
        return None
    t = ys[0]
    cfo, pat = f.get("cf.cfo", t), f.get("pl.pat", t)
    if cfo is None or pat is None:
        return None
    ratio = _ratio(cfo, pat)
    neg_years = [y for y in ys[:3] if (f.get("cf.cfo", y) or 0) < 0 < (f.get("pl.pat", y) or 0)]
    feats = rhp_features(f)
    gap = feats["f_recv_rev_gap"]
    pages = [p for p in (f.page("cf.cfo", t), f.page("pl.pat", t)) if p]
    if len(neg_years) >= 2 or (cfo < 0 < pat):
        sev = "HIGH"
    elif (ratio is not None and pat > 0 and ratio < 0.3) or (gap is not None and gap > 0.5):
        sev = "ELEVATED"
    else:
        sev = "LOW"
    text = (f"{t}: cash from operations ₹{cfo:.2f} cr against profit after tax ₹{pat:.2f} cr"
            + (f" (CFO/PAT {ratio:.2f})" if ratio is not None and pat > 0 else "")
            + (f"; operating cash was negative while profit was positive in "
               f"{', '.join(neg_years)}" if neg_years else "")
            + (f"; trade receivables grew {gap:+.0%} faster than revenue" if gap is not None
               and gap > 0.5 else "") + ".")
    return Finding("F1", sev, {"cfo_pat": ratio, "neg_cfo_years": neg_years,
                               "recv_minus_rev_growth": gap}, text, pages)


def f2_profit_spike(f: FactSource) -> Finding | None:
    ys = f.years()
    if len(ys) < 3:
        return None
    pats = [f.get("pl.pat", y) for y in ys[:3]]
    if None in pats:
        return None
    base = (pats[1] + pats[2]) / 2
    spike = pats[0] / base if base > 0 else None
    sev = ("HIGH" if spike is not None and spike >= 3 else
           "ELEVATED" if (spike is not None and spike >= 2) or (base <= 0 < pats[0]) else "LOW")
    text = (f"Profit after tax {ys[0]} ₹{pats[0]:.2f} cr against an average of ₹{base:.2f} cr "
            f"in {ys[1]}-{ys[2]}" + (f" ({spike:.1f}x)." if spike is not None else
                                     " (a turnaround from losses)."))
    return Finding("F2", sev, {"spike": spike, "pat": dict(zip(ys[:3], pats))}, text,
                   [p for p in (f.page("pl.pat", ys[0]),) if p])


def f3_eligibility(f: FactSource) -> Finding | None:
    ys = f.years()[:3]
    values = {y: ebitda(f, y) for y in ys}
    known = {y: v for y, v in values.items() if v is not None}
    if len(known) < 2:
        return None
    near = [y for y, v in known.items() if 0 <= v - ELIGIBILITY_FLOOR_CR <= HEADROOM_BAND_CR]
    sev = "HIGH" if len(near) >= 2 else "ELEVATED" if near else "LOW"
    text = ("EBITDA by year: " + ", ".join(f"{y} ₹{v:.2f} cr" for y, v in known.items())
            + (f"; within ₹{HEADROOM_BAND_CR} cr above the ₹1 cr eligibility floor in "
               f"{', '.join(near)}" if near else "; not clustered at the ₹1 cr floor") + ".")
    pages = sorted({p for y in known for p in (f.page("pl.pbt", y),) if p})
    return Finding("F3", sev, {"ebitda": known, "near_floor_years": near}, text, pages)


def f5_concentration(f: FactSource) -> Finding | None:
    share = f.get("biz.top10_customers_pct", "")
    if share is None:
        return None
    sev = "HIGH" if share >= 90 else "ELEVATED" if share >= 70 else "LOW"
    return Finding("F5", sev, {"top10_customers_pct": share},
                   f"The top 10 customers account for {share:.1f}% of revenue.",
                   [p for p in (f.page("biz.top10_customers_pct", ""),) if p])


SPECIALISTS = (f1_paper_profits, f2_profit_spike, f3_eligibility, f5_concentration)


def f4_insider_discount(summary: dict) -> Finding | None:
    """IPO price over what insiders paid in the 18 months before the bid (narrative.py)."""
    disc = summary.get("insider_discount")
    if disc is None and not summary.get("bonus_in_window"):
        return Finding("F4", "LOW", summary, f"No shares were allotted for cash in the 18 "
                       f"months before the bid (since {summary['window_start']}).",
                       summary.get("pages", []))
    sev = ("HIGH" if disc is not None and disc >= 5 else
           "ELEVATED" if (disc is not None and disc >= 3) or summary.get("bonus_in_window")
           else "LOW")
    parts = []
    if disc is not None:
        parts.append(f"The IPO price ₹{summary['issue_price']:.2f} is {disc:.1f}x the weighted "
                     f"price of ₹{summary['weighted_price']:.2f} paid in "
                     f"{summary['recent_allotments']} allotment(s) since {summary['window_start']}")
    if summary.get("bonus_in_window"):
        parts.append("a bonus issue was made in the 18 months before the bid")
    return Finding("F4", sev, summary, "; ".join(parts) + ".", summary.get("pages", []))


def f6_related_parties(summary: dict) -> Finding | None:
    share = summary.get("share")
    if share is None:
        return None
    sev = "HIGH" if share >= 0.4 else "ELEVATED" if share >= 0.2 else "LOW"
    return Finding("F6", sev, summary,
                   f"{summary['fiscal_year']}: sales to and purchases from related parties of "
                   f"₹{summary['rpt_cr']:.2f} cr, {share:.0%} of revenue of "
                   f"₹{summary['revenue_cr']:.2f} cr.", summary.get("pages", []))


def f8_overvalued(summary: dict) -> Finding | None:
    premium = summary.get("premium")
    if premium is None:
        return None
    sev = "HIGH" if premium > 2.5 else "ELEVATED" if premium > 1.5 else "LOW"
    names = ", ".join(p["name"] for p in summary["peers"][:4])
    return Finding("F8", sev, summary,
                   f"Issue P/E {summary['issue_pe']:.1f} against a median of "
                   f"{summary['peer_median_pe']:.1f} for the listed peers the RHP names "
                   f"({names}): {premium:.1f}x.", summary.get("pages", []))


def f9_use_of_money(summary: dict, pages: list[int]) -> Finding | None:
    gross, gcp, cap, wc_share = (summary.get(k) for k in ("gross_cr", "gcp_cr", "gcp_cap_cr",
                                                           "wc_share"))
    if not gross or (gcp is None and wc_share is None):
        return None
    over = gcp is not None and cap is not None and gcp > cap * 1.001
    near = gcp is not None and cap is not None and gcp >= 0.9 * cap
    sev = "HIGH" if over else "ELEVATED" if near or (wc_share or 0) > 0.5 else "LOW"
    text = (f"Of ₹{gross:.2f} cr of proceeds, general corporate purposes take "
            + (f"₹{gcp:.2f} cr against a cap of ₹{cap:.2f} cr" if gcp is not None else "nothing stated")
            + (f", and working capital {wc_share:.0%}" if wc_share is not None else "")
            + (". The GCP amount is above the cap the rules allowed at the bid date." if over
               else "."))
    return Finding("F9", sev, summary, text, pages)
