"""Extracting the restated financials from an RHP, shared by the live Extractor
and the offline extraction stage (so past and live facts are made the same way).

Strategies, in retry order (at most two retries, docs/DESIGN.md):
1. `text_table`: the deterministic parser in agent/rhp.py.
2. `llm_row_map`: Nemotron maps the statement's numbered row labels to fields
   (and reads the unit and period columns when the parser could not). The
   numbers themselves always come from the RHP text, never from the model.
3. `llm_row_map_wide`: the same over a wider page window (the statement may
   continue onto later pages).
A statement whose periods fail the Verifier after the last strategy is kept
but marked unverified, and is never used for scoring.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel

from agent.rhp import (_HEADS, _NUMBER, ROWS, UNIT_TO_CRORE, Period, Statement, _join_wrapped,
                       find_periods, find_unit, parse_number, statement_pages)
from agent.verify import Check, verify

KINDS = ("pl", "bs", "cf")
NAMES = {"pl": "profit and loss statement", "bs": "statement of assets and liabilities",
         "cf": "cash flow statement"}
FIELD_HELP = {
    "pl": "revenue (revenue from operations), other_income, total_income, total_expenses, "
          "employee (employee benefit expense), finance_cost, depreciation (and amortisation), "
          "pbt (profit before tax), tax (total tax expense), pat (profit after tax for the year)",
    "bs": "share_capital, reserves (reserves and surplus / other equity), equity (total equity / "
          "shareholders' funds), lt_borrowings, st_borrowings, trade_payables, inventories, "
          "receivables (trade receivables), cash (cash and bank balances / cash equivalents), "
          "total_assets, total_eq_liab (total equity and liabilities), nc_liabilities "
          "(total non-current liabilities), c_liabilities (total current liabilities), "
          "total_liabilities",
    "cf": "cfo (net cash from operating activities), cfi (net cash from investing activities), "
          "cff (net cash from financing activities), net_change (net increase/decrease in cash), "
          "opening_cash (cash at the beginning), closing_cash (cash at the end)"}


class RowMap(BaseModel):
    unit: Literal["lakh", "crore", "million", "thousand", "rupee"] | None = None
    period_ends: list[str] = []          # YYYY-MM-DD, in the column order of the table
    rows: dict[str, int | None] = {}     # field -> line number in the numbered list


@dataclass
class Extracted:
    kind: str
    statement: Statement | None
    checks: list[Check] = field(default_factory=list)
    method: str = ""
    attempts: int = 0
    detail: str = ""

    @property
    def verified_periods(self) -> list[str]:
        return [c.period for c in self.checks if c.passed]


def _candidate_lines(text: str, kind: str, k: int | None) -> list[tuple[int, str, list[str]]]:
    start = re.search(_HEADS[kind], text)
    body = text[start.start():] if start else text
    out = []
    for i, line in enumerate(_join_wrapped(body.splitlines()), 1):
        nums = _NUMBER.findall(line)
        if len(nums) >= (k or 2):
            label = line[:line.find(nums[-(k or len(nums))])].strip()
            if re.search(r"[A-Za-z]{3}", label):
                out.append((i, label, nums))
    return out


def _from_row_map(text: str, kind: str, page: int, mapped: RowMap) -> Statement | None:
    unit = find_unit(text) or mapped.unit
    periods = find_periods(text)
    if len(periods) < 2 and mapped.period_ends:
        try:
            periods = [Period(date.fromisoformat(d)) for d in mapped.period_ends]
        except ValueError:
            return None
    if unit is None or len(periods) < 2:
        return None
    k, factor = len(periods), UNIT_TO_CRORE[unit]
    lines = {i: (label, nums) for i, label, nums in _candidate_lines(text, kind, k)}
    st = Statement(kind, page, unit, periods)
    known = {f for f, _ in ROWS[kind]}
    for fld, line_no in mapped.rows.items():
        if fld not in known or fld == "total" or line_no not in lines:
            continue
        label, nums = lines[line_no]
        values = [parse_number(n) for n in nums[-k:]]
        if len(values) == k and None not in values:
            st.values[fld] = [round(v * factor, 4) for v in values]
            st.rows[fld] = label[:80]
    return st if st.values else None


def map_rows(gateway, text: str, kind: str, page: int, purpose: str) -> Statement | None:
    """Strategy 2: Nemotron maps numbered row labels to fields; numbers come from the text."""
    periods = find_periods(text)
    lines = _candidate_lines(text, kind, len(periods) if len(periods) >= 2 else None)
    if not lines:
        return None
    listing = "\n".join(f"L{i}: {label[:90]} | {' '.join(nums[-6:])}" for i, label, nums in lines)
    header = "\n".join(text.splitlines()[:30])[:2500]
    prompt = (f"These are the numbered rows of the restated {NAMES[kind]} on RHP page {page}.\n"
              f"Map each field to the line number (the integer after L) that holds it, or null "
              f"if no line does: {FIELD_HELP[kind]}.\nAlso give the unit of the amounts (from "
              f"the 'Amount in ...' line) and the period-end dates of the number columns in "
              f"order (YYYY-MM-DD).\n\nHEADER:\n{header}\n\nROWS:\n{listing}")
    res = gateway.generate(prompt, "You read Indian IPO prospectus tables. Map labels only; "
                                   "never compute or invent numbers.",
                           schema=RowMap, route="extract", purpose=purpose)
    return _from_row_map(text, kind, page, res.parsed)


def extract_financials(pages: list[str], *, gateway=None, label: str = "rhp",
                       on_attempt=None, time_left=lambda: float("inf")) -> dict[str, Extracted]:
    """The three restated statements, each with its checks and the method that won."""
    out: dict[str, Extracted] = {}
    for kind in KINDS:
        candidates = statement_pages(pages, kind)
        best = Extracted(kind, None)
        attempts = [("text_table", None)]
        if gateway is not None and candidates:
            attempts += [("llm_row_map", 2), ("llm_row_map_wide", 3)]
        for method, span in attempts:
            if method != "text_table" and time_left() < 120:
                best.detail = f"{method}: skipped, the run's time limit is near"
                break
            best.attempts += 1
            try:
                st = _attempt(pages, kind, candidates, method, span, gateway, label)
            except Exception as e:          # a model outage falls through to "unverified"
                best.detail = f"{method}: {type(e).__name__}: {str(e)[:120]}"
                st = None
            if st is not None:
                checks = verify(st)
                if best.statement is None or sum(c.passed for c in checks) > len(best.verified_periods):
                    best.statement, best.checks, best.method = st, checks, method
            if on_attempt:
                on_attempt(kind, method, best)
            if best.checks and _latest_full_year_ok(best):
                break
        out[kind] = best
    return out


def _attempt(pages, kind, candidates, method, span, gateway, label) -> Statement | None:
    if method == "text_table":
        from agent.rhp import parse_statements
        return parse_statements(pages).get(kind)
    for page in candidates[:2]:
        text = "\n".join(pages[page - 1:page - 1 + span])
        st = map_rows(gateway, text, kind, page, purpose=f"extract:{label}:{kind}:{method}")
        if st is not None and any(c.passed for c in verify(st)):
            return st
    return None


def _latest_full_year_ok(x: Extracted) -> bool:
    full = [c for c in x.checks if not c.period.startswith("S")]
    return bool(full) and full[0].passed


_WORDS = {"one": 1, "three": 3, "five": 5, "ten": 10}
_TOPN = re.compile(r"(?is)top\s*(1|3|5|10|one|three|five|ten)\s+(customers?|clients?|buyers?|suppliers?|vendors?)"
                   r"\b.{0,160}?(?<![\d.,])(\d{1,2}(?:\.\d{1,2})?|100(?:\.0+)?)(?!\d)\s*%")


def top_shares(pages: list[str]) -> list[dict]:
    """Shares of revenue (customers) and purchases (suppliers) from the top 1, 3, 5 and
    10, as the RHP prints them (F5, data guide 9.1).

    The first percentage after the phrase is the latest period in the RHP's tables.
    A value is verified only when it is printed on at least two different pages (the
    risk factors and the business section usually both state it).
    """
    seen: dict[tuple[str, float], list[int]] = {}
    for i, text in enumerate(pages, 1):
        for m in _TOPN.finditer(text):
            n = _WORDS.get(m.group(1).lower(), None) or int(m.group(1))
            who = "suppliers" if re.match(r"(?i)suppl|vendor", m.group(2)) else "customers"
            where = seen.setdefault((f"biz.top{n}_{who}_pct", round(float(m.group(3)), 2)), [])
            if i not in where:
                where.append(i)
    best: dict[str, tuple[float, list[int]]] = {}
    for (field, value), where in seen.items():
        if field not in best or (len(where), -where[0]) > (len(best[field][1]), -best[field][1][0]):
            best[field] = (value, where)
    return [{"field": f, "fiscal_year": "", "value": v, "unit": "%", "page": w[0],
             "verified": len(w) >= 2, "method": "text_pattern"} for f, (v, w) in sorted(best.items())]


def top_customers(pages: list[str]) -> dict | None:
    """The top-10 customers' share alone (kept for callers that want one fact)."""
    return next((f for f in top_shares(pages) if f["field"] == "biz.top10_customers_pct"), None)


def facts_from(extracted: dict[str, Extracted]) -> list[dict]:
    """One fact per field and period, in crore, carrying its page, method and verified flag."""
    facts = []
    for x in extracted.values():
        st = x.statement
        if st is None:
            continue
        ok = {c.period: c.passed for c in x.checks}
        for fld, values in st.values.items():
            for period, value in zip(st.periods, values):
                facts.append({"field": f"{st.kind}.{fld}", "fiscal_year": period.label,
                              "value": value, "unit": "cr", "page": st.page,
                              "verified": bool(ok.get(period.label)), "method": x.method})
    return facts
