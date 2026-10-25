"""Narrative RHP sections read with the Meterless Markovian engine (live runs).

A long section (the share capital history, the objects of the issue) is read
page window by page window. Each chunk returns the table rows it saw as JSON;
the carryover keeps only what the next window needs to read its rows (the
unit, the column order, an open row), never the rows. Numbers are returned as
printed with their unit, and code does every conversion and computation.

Used by the live Extractor at STANDARD and FORENSIC depth; the backtest does
not run it (25 Oct 2026 decision), so these flags explain but never score.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from agent.engines.markovian import ChunkConfig, Markovian, MarkovianRun
from agent.rhp import UNIT_TO_CRORE, _NUMBER

INSIDER_WINDOW_DAYS = 548           # "recent" allotments: the 18 months before as_of
GCP_CAP_SHARE, GCP_CAP_CR = 0.15, 10.0


@dataclass
class Section:
    name: str
    heading: str                    # where the section starts (top of a page)
    table: str                      # a page holding the table we want
    goal: str
    max_pages: int = 6


SECTIONS = {
    "capital": Section(
        "capital", r"capital\s+structure",
        r"(?i)(?:history|build[\s-]*up|changes?)[^\n]{0,60}share\s+capital|date\s+of\s+allotment",
        "Extract every row of the history of the company's equity share capital: each "
        "allotment or issue of shares since incorporation. For each row return "
        '{"date": "YYYY-MM-DD", "nature": "e.g. subscription to MoA, bonus, rights, '
        'preferential, private placement, conversion", "shares": number of shares allotted, '
        '"face_value": per share, "issue_price": per share as printed (0 for bonus), '
        '"consideration": "cash" | "other" | "bonus", "page": the [page N] it is on}. '
        'Reply with a JSON object {"rows": [...]} for the rows in this part only.'),
    "objects": Section(
        "objects", r"objects?\s+of\s+the\s+(?:issue|offer)",
        r"(?i)general\s+corporate\s+purpose",
        "Extract the table of the objects of the issue (how the issue proceeds will be used). "
        'Return {"unit": "lakh" | "crore" | "million" | "rupee" as printed, "rows": '
        '[{"object": the line as printed, "kind": "gcp" (general corporate purposes) | '
        '"working_capital" | "loan_repayment" | "capex" | "issue_expenses" | "acquisition" | '
        '"other", "amount": number as printed, "page": N}], "gross_proceeds": the total '
        'fresh issue proceeds as printed or null, "net_proceeds": as printed or null} for this '
        "part only."),
}


def section_windows(pages: list[str], section: Section) -> list[tuple[int, str]]:
    """(page, text) windows for a section: start at its table, else at its heading."""
    def is_toc(t: str) -> bool:
        return bool(re.search(r"(?i)table\s+of\s+contents|\.{6,}\s*\d+", t[:600]))
    starts = [i for i, t in enumerate(pages, 1)
              if re.search(section.table, t) and len(_NUMBER.findall(t)) > 15 and not is_toc(t)]
    if not starts:
        starts = [i for i, t in enumerate(pages, 1)
                  if re.search(section.heading, t[:400], re.I) and not is_toc(t)]
    if not starts:
        return []
    first = starts[0]
    return [(p, pages[p - 1]) for p in range(first, min(first + section.max_pages, len(pages) + 1))]


def _rows_from(content: str) -> dict:
    """The chunk's JSON object, even after reasoning text that contains braces."""
    from agent.llm import _last_json
    text = content
    for _ in range(20):                 # step back past markers such as [STATE_CHECKPOINT]
        candidate = _last_json(text)
        if candidate is None:
            break
        try:
            value = json.loads(candidate)
        except ValueError:
            value = None
        if isinstance(value, dict):
            return value
        text = text[:text.rfind(candidate)]
    return {}


@dataclass
class SectionResult:
    name: str
    pages: list[int]
    data: dict = field(default_factory=dict)
    run: MarkovianRun | None = None
    error: str = ""


def read_section(gateway, pages: list[str], name: str, *, history: Path | None = None,
                 run_id: str = "", on_event=None, memory_context: str = "") -> SectionResult:
    section = SECTIONS[name]
    windows = section_windows(pages, section)
    if not windows:
        return SectionResult(name, [], error="section not found")
    engine = Markovian(ChunkConfig(chunk_size=8000, max_chunks=section.max_pages,
                                   carryover_tokens=640),
                       generator=gateway.engine_generator(route="extract",
                                                          purpose=f"narrative:{name}"),
                       history_path=history, on_event=on_event)
    merged: dict = {"rows": []}

    def collect(chunk):
        part = _rows_from(chunk.content)
        merged["rows"] += [r for r in part.get("rows", []) if isinstance(r, dict)]
        for k, v in part.items():
            if k != "rows" and v not in (None, "") and k not in merged:
                merged[k] = v

    run = engine.run(section.goal, mode="EXTRACT", reflect=False, run_id=run_id,
                     memory_context=memory_context,
                     agent_id="Extractor", on_chunk=collect,
                     windows=[f"[page {p}]\n{text}" for p, text in windows])
    error = "" if run.status == "completed" else (run.error or run.status)
    return SectionResult(name, [p for p, _ in windows], merged, run, error)


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?", v)
        return float(m.group().replace(",", "")) if m else None
    return None


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def insider_discount(rows: list[dict], issue_price: float, as_of: date) -> dict:
    """IPO price over the weighted price paid in allotments of the 18 months before as_of."""
    start = as_of - timedelta(days=INSIDER_WINDOW_DAYS)
    recent, bonus = [], []
    for r in rows:
        d, shares, price = _date(r.get("date")), _num(r.get("shares")), _num(r.get("issue_price"))
        if d is None or shares is None or not (start <= d < as_of):
            continue
        if str(r.get("consideration", "")).lower() == "bonus" or "bonus" in str(
                r.get("nature", "")).lower():
            bonus.append(r)
        elif price:                     # a missing price (a swap, "N.A.") is not evidence
            recent.append((shares, price, r))
    out = {"recent_allotments": len(recent), "bonus_in_window": bool(bonus), "window_start":
           start.isoformat(), "pages": sorted({int(_num(r.get("page")) or 0) for *_, r in recent
                                              if _num(r.get("page"))})}
    if recent and issue_price:
        shares = sum(s for s, _, _ in recent)
        wavg = sum(s * p for s, p, _ in recent) / shares
        out |= {"weighted_price": round(wavg, 2), "issue_price": issue_price,
                "insider_discount": round(issue_price / wavg, 2) if wavg else None}
    return out


def objects_summary(data: dict) -> dict:
    """GCP and working capital as shares of the fresh issue, in crore."""
    factor = UNIT_TO_CRORE.get(str(data.get("unit", "")).lower().rstrip("s").replace("lac", "lakh"))
    if factor is None:
        return {"error": "unit not stated"}
    by_kind: dict[str, float] = {}
    for r in data.get("rows", []):
        if re.search(r"(?i)\b(?:total|net\s+proceeds|gross\s+proceeds)\b", str(r.get("object", ""))):
            continue                    # a total row would double the base
        amount = _num(r.get("amount"))
        if amount is not None:
            by_kind[r.get("kind", "other")] = by_kind.get(r.get("kind", "other"), 0.0) + amount * factor
    gross = _num(data.get("gross_proceeds"))
    gross = gross * factor if gross else sum(by_kind.values()) or None
    gcp, wc = by_kind.get("gcp"), by_kind.get("working_capital")
    cap = min(GCP_CAP_SHARE * gross, GCP_CAP_CR) if gross else None
    return {"gross_cr": gross, "gcp_cr": gcp, "working_capital_cr": wc,
            "gcp_share": gcp / gross if gcp is not None and gross else None,
            "gcp_cap_cr": cap, "wc_share": wc / gross if wc is not None and gross else None,
            "by_kind_cr": {k: round(v, 4) for k, v in by_kind.items()}}
