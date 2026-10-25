"""Reading an RHP: page text, section location, and the restated financials.

Pages come from the same cache the offline pipeline writes
(data/cache/pages/<pdf sha256>.json), so past and live runs read identical
text. `parse_statements` is the Extractor's first strategy: a deterministic
parser over the text layer of the restated (summary) statements. It finds
the unit line, the period columns and the rows it knows, and converts every
value to crore. It never guesses. A row it cannot place is left out, and the
Verifier's accounting identities decide whether the result can be used.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

UNIT_TO_CRORE = {"crore": 1.0, "lakh": 0.01, "million": 0.1, "thousand": 1e-4, "rupee": 1e-7}
_UNIT = re.compile(r"(?i)(?:amount|amt|figures|all\s+amounts?|₹|rs\.?|inr|rupees)[^\n]{0,50}?"
                   r"\b(lakhs?|lacs?|millions?|crores?|thousands?|rupees)\b|"
                   r"\(\s*(?:₹|rs\.?|inr)\s*(?:in\s+)?(lakhs?|lacs?|millions?|crores?|thousands?)\s*\)|"
                   r"\bin\s+(lakhs?|lacs?|millions?|crores?)\b")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_PATTERNS = [
    re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?[\s.\-/,]*([a-z]{3})[a-z]*[\s.\-/,]*((?:20)?\d{2})\b"),
    re.compile(r"(?i)\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b"),
    re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b"),
    re.compile(r"(?i)\b(?:f\.?\s?y\.?\s*)?(20\d{2})\s*-\s*(\d{2})\b"),
]
_NUMBER = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?|(?<!\w)-(?!\w)|(?<!\w)–(?!\w)")


@dataclass(frozen=True)
class Period:
    end: date

    @property
    def label(self) -> str:
        """FY24 for a March year-end; otherwise the stub's end date."""
        if self.end.month == 3 and self.end.day == 31:
            return f"FY{self.end.year % 100:02d}"
        return f"S{self.end.isoformat()}"

    @property
    def is_full_year(self) -> bool:
        return self.end.month == 3 and self.end.day == 31


@dataclass
class Statement:
    kind: str                                   # bs | pl | cf
    page: int
    unit: str
    periods: list[Period]
    values: dict[str, list[float | None]] = field(default_factory=dict)   # crore, per period
    rows: dict[str, str] = field(default_factory=dict)                   # field -> source label
    totals: list[list[float]] = field(default_factory=list)              # every "Total" row (bs)


def load_pages(cache_dir: Path, sha256: str) -> list[str]:
    path = cache_dir / f"{sha256}.json"
    return [p["text"] for p in json.loads(path.read_text())] if path.exists() else []


def find_unit(text: str) -> str | None:
    m = _UNIT.search(text)
    if not m:
        return None
    word = next(g for g in m.groups() if g).lower()
    word = {"lacs": "lakh", "lac": "lakh"}.get(word, word).rstrip("s")
    return word if word in UNIT_TO_CRORE else None


def find_periods(text: str) -> list[Period]:
    """Distinct period-end dates in reading order (newest usually first)."""
    flat = re.sub(r"\s+", " ", text[:2500])
    found: list[tuple[int, date]] = []
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(flat):
            d = _to_date(pat, m.groups())
            # Period ends are month-ends; "from 1st April" is a period start.
            if d and 2015 <= d.year <= 2030 and (d + timedelta(days=1)).day == 1:
                found.append((m.start(), d))
    out: list[Period] = []
    for _, d in sorted(found):
        if all(p.end != d for p in out):
            out.append(Period(d))
    return out


def _to_date(pat: re.Pattern, g: tuple) -> date | None:
    try:
        if pat is _DATE_PATTERNS[0]:
            day, mon, year = int(g[0]), _MONTHS.get(g[1][:3].lower()), int(g[2])
            year = year + 2000 if year < 100 else year
        elif pat is _DATE_PATTERNS[1]:
            mon, day, year = _MONTHS.get(g[0][:3].lower()), int(g[1]), int(g[2])
        elif pat is _DATE_PATTERNS[2]:
            day, mon, year = int(g[0]), int(g[1]), int(g[2])
        else:
            start, end2 = int(g[0]), int(g[1])
            if (start + 1) % 100 != end2:
                return None
            return date(start + 1, 3, 31)
        return date(year, mon, day) if mon else None
    except ValueError:
        return None


def parse_number(token: str) -> float | None:
    token = token.strip()
    if token in ("-", "–"):
        return 0.0
    neg = token.startswith("(") and token.endswith(")") or token.startswith("-")
    digits = token.strip("()-").replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if neg else value


# Field -> label patterns, per statement. First match wins within a statement.
ROWS = {
    "pl": [("revenue", r"revenue\s+from\s+operations"),
           ("other_income", r"^\W*(?:\(?[a-z0-9]\)?\W*)?other\s+income"),
           ("total_income", r"total\s+(?:income|revenue)"),
           ("employee", r"employee\s+benefit"),
           ("finance_cost", r"finance\s+cost"),
           ("depreciation", r"depreciation"),
           ("total_expenses", r"total\s+expen"),
           ("pbt", r"profit\s*/?\s*(?:\(loss\)\s*)?before\s+tax|profit\s+before\s+tax"),
           ("tax", r"(?:total\s+tax|net\s+(?:current\s+)?tax|tax\s+expense)"),
           ("pat", r"profit\s*/?\s*(?:\(loss\)\s*)?(?:for\s+the\s+(?:year|period)|after\s+tax)|"
                   r"net\s+profit")],
    "bs": [("share_capital", r"share\s+capital"),
           ("reserves", r"reserves?\s+(?:and|&)\s+surplus|other\s+equity"),
           ("total_eq_liab", r"total\s+(?:equity\s+(?:and|&)\s+liabilit(?:y|ies)|liabilit(?:y|ies)\s+"
                             r"(?:and|&)\s+equity)"),
           ("equity", r"(?:total\s+equity|net\s+worth|shareholders?['’]?\s*funds?)"
                      r"(?!\s+(?:and|&)\s+liabilit)"),
           ("lt_borrowings", r"long[\s-]+term\s+borrowings|non[\s-]+current\s+borrowings"),
           ("st_borrowings", r"short[\s-]+term\s+borrowings|current\s+borrowings"),
           ("trade_payables", r"trade\s+payables"),
           ("inventories", r"inventor(?:y|ies)"),
           ("receivables", r"trade\s+receivables"),
           ("cash", r"cash\s+(?:and|&)\s+(?:bank\s+balances?|cash\s+equivalents?)"),
           ("total_assets", r"total\s+assets"),
           ("nc_liabilities", r"total\s+non[\s-]*current\s+liabilities"),
           ("c_liabilities", r"total\s+current\s+liabilities"),
           ("total_liabilities", r"total\s+liabilities"),
           ("total", r"^\W*(?:grand\s+)?total\W*(?:\([a-z0-9+\-\s]{1,12}\))?\W*$")],
    "cf": [("cfo", r"net\s+cash.{0,60}?operating"),
           ("cfi", r"net\s+cash\s+.{0,40}?investing"),
           ("cff", r"net\s+cash\s+.{0,40}?financing"),
           ("net_change", r"net\s+(?:increase|decrease|change|\(decrease\)|\(increase\))"
                          r".{0,40}?cash"),
           ("opening_cash", r"(?:opening|beginning|start).{0,40}?cash|"
                            r"cash.{0,60}?(?:beginning|opening|start)"),
           ("closing_cash", r"(?:closing).{0,40}?cash|cash.{0,60}?(?:end\s+of|closing)")],
}
_HEADS = {"bs": r"(?i)statement\s+of\s+assets\s+(?:and|&)\s+liabilities|balance\s+sheet",
          "pl": r"(?i)statement\s+of\s+profit\s+(?:and|&)\s+loss|profit\s+(?:and|&)\s+loss\s+"
                r"(?:account|statement)",
          "cf": r"(?i)statement\s+of\s+cash\s+flows?|cash\s+flow\s+statement"}


def statement_pages(pages: list[str], kind: str, limit: int = 6) -> list[int]:
    """Pages (1-based) that look like this statement: its heading plus many numbers."""
    scored = []
    for i, text in enumerate(pages, 1):
        if re.search(_HEADS[kind], text) and len(_NUMBER.findall(text)) > 30:
            known = sum(bool(re.search(p, text, re.I | re.M)) for _, p in ROWS[kind])
            scored.append((known, -i, i))
    return [i for *_, i in sorted(scored, reverse=True)[:limit]]


def parse_statement(text: str, kind: str, page: int) -> Statement | None:
    unit = find_unit(text)
    periods = find_periods(text)
    if unit is None or len(periods) < 2:
        return None
    k = len(periods)
    st = Statement(kind, page, unit, periods)
    factor = UNIT_TO_CRORE[unit]
    # The heading of this statement starts its rows; another statement's heading ends them.
    start = re.search(_HEADS[kind], text)
    body = text[start.start():] if start else text
    for other, head in _HEADS.items():
        if other != kind:
            end = re.search(head, body[40:])
            if end:
                body = body[:40 + end.start()]
    for line in _join_wrapped(body.splitlines()):
        nums = [n for n in _NUMBER.findall(line)]
        if len(nums) < k:
            continue
        label = line[:line.find(nums[-k])] if nums[-k] in line else line
        values = [parse_number(n) for n in nums[-k:]]
        if any(v is None for v in values):
            continue
        for fld, pattern in ROWS[kind]:
            if re.search(pattern, label, re.I | re.M):
                scaled = [round(v * factor, 4) for v in values]
                if fld == "total":          # assets and equity + liabilities both print one
                    st.totals.append(scaled)
                elif fld not in st.values:
                    st.values[fld] = scaled
                    st.rows[fld] = re.sub(r"\s+", " ", label).strip()[:80]
                break
    return st if st.values or st.totals else None


def _join_wrapped(lines: list[str]) -> list[str]:
    """Join a label that wrapped onto the next line, so it meets its numbers."""
    out: list[str] = []
    for line in (raw.strip() for raw in lines):
        if (out and line and not _NUMBER.search(out[-1])
                and (line[0].islower() or out[-1].lower().endswith(("&", " and", " of", ",", "/")))):
            out[-1] += " " + line
        else:
            out.append(line)
    return out


def parse_statements(pages: list[str]) -> dict[str, Statement]:
    """The best balance sheet, P&L and cash flow among the candidate pages.

    A candidate whose periods pass the accounting identities beats one that
    merely has more rows; ties go to the one with more known rows.
    """
    from agent.verify import verify
    out: dict[str, Statement] = {}
    for kind in ("bs", "pl", "cf"):
        best, best_key = None, None
        for page in statement_pages(pages, kind):
            # A statement often starts on one page and ends on the next.
            text = pages[page - 1] + "\n" + (pages[page] if page < len(pages) else "")
            st = parse_statement(text, kind, page)
            if st is None:
                continue
            key = (sum(c.passed for c in verify(st)), len(st.values))
            if best_key is None or key > best_key:
                best, best_key = st, key
        if best:
            out[kind] = best
    return out


def section_pages(pages: list[str], pattern: str, window: int = 8,
                  min_numbers: int = 0) -> list[int]:
    """1-based pages where a section heading appears, skipping the table of contents."""
    hits = [i for i, t in enumerate(pages, 1)
            if re.search(pattern, t[:400], re.I) and len(_NUMBER.findall(t)) >= min_numbers
            and not re.search(r"(?i)table\s+of\s+contents", t[:300])]
    if not hits:
        return []
    start = hits[0] if len(hits) == 1 else max(hits[:3], key=lambda i: len(pages[i - 1]))
    return list(range(start, min(start + window, len(pages) + 1)))
