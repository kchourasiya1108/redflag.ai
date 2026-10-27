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
# General corporate purposes cap: 25% of gross proceeds under the earlier ICDR
# rules; min(15%, Rs 10 cr) under SEBI's SME amendment effective 3 Mar 2025.
SME_RULES_2025 = date(2025, 3, 3)


def gcp_cap(gross_cr: float, as_of: date) -> float:
    return min(0.15 * gross_cr, 10.0) if as_of >= SME_RULES_2025 else 0.25 * gross_cr


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
    "basis": Section(
        "basis", r"basis\s+for\s+(?:the\s+)?(?:issue|offer)\s+price",
        r"(?i)peer[^\n]{0,80}(?:comparison|accounting\s+ratios|competitors)|comparison\s+with[^\n]"
        r"{0,30}peers|industry\s+peers",
        "Extract the comparison with listed peers in the basis for the issue price. Return "
        '{"issue_pe": the P/E ratio of the issuing company at the issue price as printed (or '
        'null), "issue_eps": its EPS as printed (or null), "peers": [{"name": peer company, '
        '"cmp": its price as printed, "eps": as printed, "pe": as printed, "page": N}]} for '
        "this part only. Do not include the issuing company in peers.", max_pages=3),
    "rpt": Section(
        "rpt", r"related\s+party\s+transactions",
        r"(?i)related\s+party\s+transactions[\s\S]{0,600}?nature\s+of\s+(?:transaction|relationship)",
        "Extract the related party transactions table. Return {\"unit\": \"lakh\" | \"crore\" | "
        '"million" | "rupee" as printed, "rows": [{"party": name, "relationship": as printed, '
        '"nature": "sale" | "purchase" | "rent" | "remuneration" | "loan_given" | "loan_taken" | '
        '"guarantee" | "other", "fiscal_year": the year-end of the column as YYYY-MM-DD, '
        '"amount": number as printed, "page": N}]} for this part only. One row per party '
        'and transaction; leave out total and subtotal rows.', max_pages=3),
}


def section_windows(pages: list[str], section: Section) -> list[tuple[int, str]]:
    """(page, text) windows for a section: start at its table, else at its heading.

    The table is looked for inside the section first (within 40 pages after a page
    titled with its heading): the same words also appear in the glossary and the risk
    factors, which come earlier ("date of allotment" is defined on page 13).
    """
    def is_toc(t: str) -> bool:
        return bool(re.search(r"(?i)table\s+of\s+contents|\.{6,}\s*\d+", t[:600]))
    titled = re.compile(rf"(?im)^\W*(?:section\s+[ivx]+\W+[^\n]*\n\W*)?(?:summary\s+of\s+)?{section.heading}\W*$")
    heads = [i for i, t in enumerate(pages, 1) if titled.search(t[:600]) and not is_toc(t)] or \
            [i for i, t in enumerate(pages, 1) if re.search(section.heading, t[:400], re.I) and not is_toc(t)]
    tables = [i for i, t in enumerate(pages, 1)
              if re.search(section.table, t) and len(_NUMBER.findall(t)) > 15 and not is_toc(t)]
    inside = [t for t in tables if any(h <= t <= h + 40 for h in heads)]
    starts = inside or tables or heads
    if not starts:
        return []
    first = starts[0]
    return [(p, pages[p - 1]) for p in range(first, min(first + section.max_pages, len(pages) + 1))]


_ROW_KEYS = {"date", "object", "party", "name", "nature", "allottee"}
_ALIASES = {"shares": ("shares_allotted", "no_of_shares", "number_of_shares", "num_shares",
                       "equity_shares", "shares_issued"),
            "issue_price": ("price", "issue_price_per_share", "price_per_share"),
            "amount": ("amount_cr", "amount_lakh", "value", "estimated_amount", "total")}


def _objects_in(text: str) -> list:
    """Every top-level JSON value in a reply, in order (a model may print one row per line)."""
    out, depth, start, in_str = [], 0, None, False
    for i, ch in enumerate(text):
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            in_str = not in_str
        elif not in_str and ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif not in_str and ch in "}]" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    out.append(json.loads(text[start:i + 1]))
                except ValueError:
                    pass
                start = None
    if depth and start is not None:
        # A reply cut off by its token budget: keep the complete rows inside it.
        dec, tail, j = json.JSONDecoder(), text[start + 1:], 0
        unit = re.match(r'\s*"unit"\s*:\s*"([^"]+)"', tail)
        if unit:                        # the table's unit, printed before its rows
            out.append({"unit": unit.group(1)})
        while (j := tail.find("{", j)) >= 0:
            try:
                value, end = dec.raw_decode(tail, j)
            except ValueError:
                j += 1
                continue
            if isinstance(value, dict):
                out.append(value)
            j = end
    return out


def _normalise_row(r: dict) -> dict:
    r = dict(r)
    for key, names in _ALIASES.items():
        if r.get(key) in (None, ""):
            hit = next((r[n] for n in names if r.get(n) not in (None, "")), None)
            if hit is not None:
                r[key] = hit
    return r


def _chunk_data(content: str) -> dict:
    """The chunk's object and rows: a {"rows": [...]} reply, a bare list of rows, or rows
    printed one object per line, with common column-name variants normalised."""
    values = _objects_in(content)
    merged: dict = {"rows": []}
    for v in values:
        rows = v if isinstance(v, list) else v.get("rows") if isinstance(v, dict) else None
        if isinstance(rows, list):
            merged["rows"] += [_normalise_row(r) for r in rows if isinstance(r, dict)]
        if isinstance(v, dict):
            if "rows" not in v and _ROW_KEYS & set(v) and not ({"peers", "issue_pe"} & set(v)):
                merged["rows"].append(_normalise_row(v))
                if v.get("unit") and "unit" not in merged:
                    merged["unit"] = v["unit"]
                continue
            for k, x in v.items():
                if k != "rows" and x not in (None, "", []) and k not in merged:
                    merged[k] = x
    if not values:
        merged.update(_rows_from(content))
    return merged


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
        part = _chunk_data(chunk.content)
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
    recent, bonus, unparsed = [], [], 0
    for r in rows:
        d, shares, price = _date(r.get("date")), _num(r.get("shares")), _num(r.get("issue_price"))
        if d is None or shares is None:
            unparsed += 1
            continue
        if not (start <= d < as_of):
            continue
        if str(r.get("consideration", "")).lower() == "bonus" or "bonus" in str(
                r.get("nature", "")).lower():
            bonus.append(r)
        elif price:                     # a missing price (a swap, "N.A.") is not evidence
            recent.append((shares, price, r))
    out = {"recent_allotments": len(recent), "bonus_in_window": bool(bonus),
           "unparsed_rows": unparsed, "issue_price": issue_price or None, "window_start":
           start.isoformat(), "pages": sorted({int(_num(r.get("page")) or 0) for *_, r in recent
                                              if _num(r.get("page"))})}
    if recent and issue_price:
        shares = sum(s for s, _, _ in recent)
        wavg = sum(s * p for s, p, _ in recent) / shares
        out |= {"weighted_price": round(wavg, 2), "issue_price": issue_price,
                "insider_discount": round(issue_price / wavg, 2) if wavg else None}
    return out


def _median(values: list[float]) -> float | None:
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


_GENERIC = {"limited", "ltd", "private", "india", "indian", "industries", "industry",
            "technologies", "technology", "tech", "solutions", "engineering", "enterprises",
            "enterprise", "services", "systems", "products", "international", "global",
            "ventures", "projects", "infra", "infrastructure", "foods", "pharma", "textiles",
            "corporation", "company", "group", "holdings", "digital", "trading", "exports"}


def _distinctive(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{3,}", name.lower()) if w not in _GENERIC]


def _is_issuer(own: list[str], name: str) -> bool:
    """The issuer's row: its first distinctive word matches, and every distinctive word
    of the issuer appears (generic words such as 'Technologies' never decide it)."""
    words = set(_distinctive(name))
    return bool(own) and own[0] in words and set(own) <= words | {own[0]}


_PEER_HEAD = re.compile(r"(?i)comparison\s+(?:of\s+accounting\s+ratios?\s+)?with\s+(?:the\s+)?(?:listed\s+)?"
                        r"(?:industry\s+)?peers?|peer\s+competitors|comparison\s+of\s+accounting\s+ratios?|"
                        r"accounting\s+ratios?\s+with\s+(?:listed\s+)?(?:industry\s+)?peers")
_PEER_COLS = (("revenue", r"revenue|total\s+income"), ("cmp", r"\bcmp\b|market\s+price|closing\s*price"),
              ("fv", r"face\s*value"), ("eps", r"\beps\b|earnings\s+per\s+share"),
              ("pe", r"p\s*/\s*e|\bpe\s+ratio"), ("ronw", r"ro\s*n\s*w|return\s+on\s+net"),
              ("pnav", r"price\s*/\s*nav|p\s*/\s*b"), ("nav", r"\bnav\b|net\s+asset"))
_PEER_PROSE = re.compile(r"(?is)peer[^.]{0,80}?\bi\.?\s*e\.?,?\s+([A-Z][\w&.,\- ]{2,80}?(?:Limited|Ltd\.?))"
                         r"[^.]{0,30}?\bwhose\s+P\s*/?\s*E[^.]{0,60}?\bis\s+(\d+(?:\.\d+)?)")
# "Based on the basic and diluted EPS of Rs. 6.18 ... for ... March 31, 2022, the P/E ratio is
# 38.35": the issuer's own P/E at the issue price; the first such line is the latest year.
_OWN_PE = re.compile(r"(?is)based\s+on\s+(?:the\s+)?(?:basic\s+(?:and|&)\s+diluted\s+|basic\s+|diluted\s+)?"
                     r"EPS\s+of\s+(?:Rs\.?|₹|INR)\s*(\d+(?:\.\d+)?)(?![^.]{0,200}?weighted)[^.]{0,200}?"
                     r"P\s*/\s*E\s+(?:ratio\s+)?is\s+(\d+(?:\.\d+)?)")
_CELL = re.compile(r"(?<![\w.])\(?-?\d[\d,]*(?:\.\d+)?\)?%?(?![\w])|\[[●•]\]\*?|\bNA\b|N\.A\.")


def _cell(token: str) -> float | None:
    t = token.strip("()%*").replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


_NOT_A_NAME = re.compile(r"(?i)\b(?:ebitda|pat\b|profit|margin|ro[ce]e?\b|roce|eps\b|book\s+value|revenue|"
                         r"page\s+\d+|crore|lakh|million|standalone|consolidated|particulars|name\s+of|"
                         r"financial\s+(?:information|year)|total|average|highest|lowest|ratio|nav\b|"
                         r"face\s+value|cmp\b|net\s+worth|industry\s+composite|audited|our\s+company|"
                         r"stand\s*-?\s*alone|thousands|mcap|sales|\b(?:january|february|march|april|may|june|"
                         r"july|august|september|october|november|december)\b|\bfy\s*\d|\b20\d{2}\b|listed\s+peer)")


def _peer_name(raw: str) -> str | None:
    """A peer's name with serial numbers and markers removed, or None for a label that is
    not a company (a metric row of the KPI table, a page footer, a 'Standalone' tag)."""
    name = re.sub(r"(?i)^\W*(?:\d+[.)]?\s+)?(?:peer[\s\ufffe-]*group|listed\s+peers?|peers?)\W*", "", raw)
    name = re.sub(r"[*^#@]+|\(\s*(?:standalone|consolidated)\s*\)|\(\d\)|\s+", " ", name).strip(" .,:-")
    if (len(name) < 5 or len(name) > 90 or _NOT_A_NAME.search(name) or not re.search(r"[A-Za-z]{3}", name)
            or re.search(r"\d+\.\d+", name)):          # numbers in a name: the row is misaligned
        return None
    return name


def peer_table(pages: list[str], window: list[int], company: str = "") -> dict:
    """The listed-peer comparison read in code, for F8: {"peers": [...], "issue_eps"}.

    The header fixes each column's place ("CMP, Face Value, EPS, P/E, RoNW, NAV", with
    "Basic Diluted" splitting EPS in two), and a row's P/E is the cell in that place.
    When the header does not account for a row's cells, the P/E is found by its
    arithmetic instead: the cell equal to another cell (the price) over a third (EPS).
    """
    own = _distinctive(company)
    candidates = sorted(set(window) | set(range(max(1, window[0] - 2), min(window[0] + 8, len(pages) + 1)))) \
        if window else []
    # The latest full year's line, not a part-year one ("for the nine months ended December").
    found = [m for p in candidates for m in _OWN_PE.finditer(pages[p - 1])]
    full = [m for m in found if re.search(r"(?i)march\s*,?\s*31|31(?:st)?\s+march|financial\s+year|fiscal|\bfy",
                                         m.group(0)) and not re.search(r"(?i)months|december|september|june", m.group(0))]
    stated = (full or found or [None])[0]
    mine = {"issue_eps": float(stated.group(1)), "issue_pe": float(stated.group(2))} if stated else {}
    for p in candidates:
        text = pages[p - 1]
        m = _PEER_PROSE.search(text)
        if m and not _PEER_HEAD.search(text):
            return {"peers": [{"name": m.group(1).strip(), "pe": float(m.group(2)), "page": p}],
                    "source": "rhp text", **mine}
        head = _PEER_HEAD.search(text)
        if not head or not re.search(r"(?i)p\s*/\s*e", text[head.start():]):
            continue
        body = text[head.end():] + "\n" + (pages[p] if p < len(pages) else "")
        lines = [ln.strip() for ln in body.splitlines()]
        first = next((i for i, ln in enumerate(lines) if len(_CELL.findall(ln)) >= 4
                      and re.search(r"[A-Za-z]{3}", ln + " ".join(lines[max(0, i - 3):i]))), None)
        if first is None:
            continue
        header = " ".join(lines[:first]).lower()
        # A wrapped company name sits just above its numbers, after the last header cell.
        last_head = max((i for i, ln in enumerate(lines[:first])
                         if any(re.search(pat, ln.lower()) for _, pat in _PEER_COLS)
                         or re.search(r"(?i)^\(?(?:₹|rs\.?|in\s|times|x\)|%)|basic|diluted|ratio", ln)),
                        default=-1)
        hits = sorted((m.start(), col) for col, pat in _PEER_COLS for m in [re.search(pat, header)] if m)
        cols = []
        for _, col in hits:
            if col == "nav" and "pnav" in cols and cols[-1] == "pnav":
                continue
            cols.append(col)
        if "eps" in cols and re.search(r"basic\s+(?:and\s+|&\s+)?diluted|basic\s+diluted", header) \
                and not re.search(r"basic\s+(?:and|&)\s+diluted\s+eps|eps\s*\(basic\s+(?:and|&)\s+diluted", header):
            cols.insert(cols.index("eps") + 1, "eps2")
        peers, issue_eps, name, bad = [], None, "", False
        for ln in lines[last_head + 1:]:
            if re.match(r"(?i)\W*(notes?|source|\*|considering|\d+\.\s+[A-Z]|key\s+(?:performance|financial))", ln) \
                    and (peers or re.match(r"(?i)\W*(?:\d+\.\s+[A-Z]|key\s+)", ln)):
                break
            cells = _CELL.findall(ln)
            if len(cells) < 4:
                if re.search(r"[A-Za-z]{3}", ln) and not re.search(r"(?i)^peer\s+group|^listed\s+peers|^industry\s+peers", ln):
                    name = (name + " " + ln).strip() if name and not re.search(r"(?i)limited|ltd", name) else ln
                continue
            label = ln[:ln.find(cells[0])].strip() if cells[0] in ln else ""
            if re.search(r"[A-Za-z]{3}", label):
                name = (name + " " + label).strip() if name and not re.search(r"(?i)limited|ltd", name) else label
            values = [_cell(c) for c in cells]
            pe = None
            if len(values) == len(cols) and "pe" in cols:
                pe = values[cols.index("pe")]
            else:
                nums = [v for v in values if v]
                pe = next((c for a in nums for b in nums for c in nums
                           if len({a, b, c}) == 3 and b > 0 and c > 0 and abs(a / b - c) <= 0.03 * c), None)
            raw = re.sub(r"\s+", " ", name).strip()
            clean = _peer_name(raw)
            if _is_issuer(own, raw):
                if "eps" in cols and len(values) == len(cols):
                    issue_eps = values[cols.index("eps2") if "eps2" in cols else cols.index("eps")]
            elif clean is None:
                if re.search(r"(?i)ebitda|\bpat\b|margin|ro[ce]e?\b|book\s+value", raw):
                    break               # the KPI table that follows: the peer rows are over
                if raw and not re.search(r"(?i)stand\s*-?\s*alone|consolidated|audited", raw):
                    peers, bad = [], True   # a row we cannot name: do not trust this table
                    break
            elif pe and 0 < pe <= 1000:
                peers.append({"name": clean, "pe": pe, "page": p})
            name = ""
        if peers and not bad:
            return {"peers": peers, "source": "rhp table", **mine,
                    "issue_eps": issue_eps or mine.get("issue_eps")}
        if bad:
            return {}
    return {}


def peer_valuation(data: dict, company: str = "", issue_price: float | None = None) -> dict:
    """Issue P/E over the median P/E of the listed peers the RHP itself chose (F8).

    An RHP printed before the price band shows its own P/E as [●]; it is then the
    announced issue price over the EPS the RHP prints."""
    own = _distinctive(company)
    peers = []
    for p in data.get("peers", []):
        name = str(p.get("name", ""))
        if _is_issuer(own, name):
            continue                    # the issuer's own row in the table
        pe = _num(p.get("pe"))
        if pe and pe > 0:
            peers.append({"name": name, "pe": pe, "eps": _num(p.get("eps")), "page": _num(p.get("page"))})
    own_pe = [_num(p.get("pe")) for p in data.get("peers", [])
              if _is_issuer(own, str(p.get("name", "")))]
    issue_pe = _num(data.get("issue_pe")) or next((x for x in own_pe if x), None)
    eps = _num(data.get("issue_eps")) or next((_num(p.get("eps")) for p in data.get("peers", [])
                                               if _is_issuer(own, str(p.get("name", ""))) and _num(p.get("eps"))), None)
    if not issue_pe and issue_price and eps and eps > 0:
        issue_pe = round(issue_price / eps, 2)
    med = _median([p["pe"] for p in peers])
    return {"issue_pe": issue_pe, "peer_median_pe": med, "peers": peers,
            "premium": issue_pe / med if issue_pe and med else None,
            "pages": sorted({int(p["page"]) for p in peers if p["page"]})}


_SYMBOL_OK = re.compile(r"^[A-Za-z0-9_.:-]+$")
PEER_MATCH_AT = 0.85


def reprice_peers(valuation: dict, port, as_of: date) -> dict:
    """F8 on the bid date (data guide 9.1): each listed peer's P/E from its NSE close on
    the last trading day before as_of over the EPS the RHP prints. Peers that cannot
    be matched to an NSE equity keep the RHP's printed P/E."""
    from pipeline.common import names as namelib
    peers = valuation.get("peers") or []
    if not peers or port is None:
        return valuation
    try:
        market = port.run("market_security_names").rows
    except Exception:           # no market table on this route: keep the printed ratios
        return valuation
    repriced, dates = 0, set()
    for p in peers:
        eps = _num(p.get("eps"))
        if not eps or eps <= 0:
            continue
        best = max(market, key=lambda m: namelib.similarity(m["name"], p["name"]), default=None)
        if not best or namelib.similarity(best["name"], p["name"]) < PEER_MATCH_AT \
                or not _SYMBOL_OK.match(best["symbol"]):
            continue
        rows = port.run("market_close_before", symbol=best["symbol"], as_of=as_of).rows
        if rows:
            p["pe_printed"], p["pe"] = p["pe"], round(rows[0]["close"] / eps, 2)
            p["symbol"], p["price_date"] = best["symbol"], str(rows[0]["date"])
            repriced += 1
            dates.add(str(rows[0]["date"]))
    out = dict(valuation)
    med = _median([p["pe"] for p in peers if p.get("pe")])
    out.update(peers=peers, peer_median_pe=med, repriced=repriced,
               priced_on=max(dates) if dates else None,
               premium=out["issue_pe"] / med if out.get("issue_pe") and med else None)
    return out


def _nature(value) -> str:
    """The model's own wording mapped to the categories F6 counts ("Sale of goods" -> sale)."""
    v = str(value or "").lower()
    if v in ("sale", "purchase"):
        return v
    if re.search(r"sale|revenue|income\s+from|service[s]?\s+rendered", v) and "purchase" not in v:
        return "sale"
    if re.search(r"purchase|procure|material|job\s*work", v):
        return "purchase"
    return v


_DATE_KEY = re.compile(r"(?i)(20\d{2})[_\-/ ](\d{2})[_\-/ ](\d{2})")


def _long_rows(rows: list[dict]) -> list[dict]:
    """A row printed wide, one column per period ("period_ended_2026_03_31": 197.14),
    becomes one row per period."""
    out = []
    for r in rows:
        dated = {k: v for k, v in r.items() if _DATE_KEY.search(k)}
        if r.get("amount") is None and dated:
            base = {k: v for k, v in r.items() if k not in dated}
            for k, v in dated.items():
                y, m, d = _DATE_KEY.search(k).groups()
                out.append({**base, "fiscal_year": f"{y}-{m}-{d}", "amount": v})
        else:
            out.append(r)
    return out


def rpt_share(data: dict, revenue_by_fy: dict[str, float]) -> dict:
    """Related-party sales and purchases as a share of revenue, latest year (F6).

    The latest full year is used when it has both; a stub period only otherwise."""
    factor = UNIT_TO_CRORE.get(str(data.get("unit", "")).lower().rstrip("s").replace("lac", "lakh"))
    if factor is None:
        return {"error": "unit not stated"}
    by_fy: dict[str, float] = {}
    pages, seen = set(), set()
    for r in _long_rows(data.get("rows", [])):
        r = {**r, "nature": _nature(r.get("nature"))}
        if r.get("nature") not in ("sale", "purchase"):
            continue
        if re.search(r"(?i)\btotal\b", f"{r.get('party', '')} {r.get('relationship', '')}"):
            continue                    # a total row would double the amount
        key = (str(r.get("party", "")).lower().strip(), r.get("nature"), r.get("fiscal_year"),
               str(r.get("amount")))
        if key in seen:                 # the same row read in two overlapping windows
            continue
        seen.add(key)
        d, amount = _date(r.get("fiscal_year")), _num(r.get("amount"))
        if d is None or amount is None:
            continue
        fy = f"FY{d.year % 100:02d}" if (d.month, d.day) == (3, 31) else f"S{d.isoformat()}"
        by_fy[fy] = by_fy.get(fy, 0.0) + abs(amount) * factor
        if _num(r.get("page")):
            pages.add(int(_num(r.get("page"))))
    years = sorted((fy for fy in by_fy if fy in revenue_by_fy and revenue_by_fy[fy]),
                   key=lambda fy: (fy.startswith("FY"), fy), reverse=True)
    if not years:
        return {"by_fy_cr": by_fy, "error": "no year with both RPT and revenue"}
    fy = years[0]
    return {"fiscal_year": fy, "rpt_cr": round(by_fy[fy], 4), "revenue_cr": revenue_by_fy[fy],
            "share": by_fy[fy] / revenue_by_fy[fy], "pages": sorted(pages)}


_GROSS = re.compile(r"(?i)gross\s+proceeds[^\n]{0,80}?(\d[\d,]*\.\d{2})\b")


def printed_gross(pages: list[str], window: list[int]) -> float | None:
    """Gross proceeds as printed on the objects pages, in crore (read in code)."""
    from agent.rhp import find_unit
    # The section's own pages first, then other pages about the use of the
    # proceeds (the line is often on the objects page, after the issue summary).
    others = [i for i in range(1, len(pages) + 1) if i not in window
              and re.search(r"(?i)objects?\s+of\s+the\s+(?:issue|offer)|net\s+proceeds", pages[i - 1])]
    for p in list(window) + others:
        text = pages[p - 1]
        m = _GROSS.search(text)
        # The unit printed closest above the line (a table's "(Rs. in lakhs)").
        unit = find_unit(text[max(0, m.start() - 1500):m.end()]) if m else None
        if m and unit:
            return float(m.group(1).replace(",", "")) * UNIT_TO_CRORE[unit]
    return None


_KINDS = ("gcp", "working_capital", "loan_repayment", "capex", "issue_expenses", "acquisition", "other")


def _kind(row: dict) -> str:
    """A use of the money in F9's categories, from the model's kind or the line itself."""
    kind = str(row.get("kind") or "").lower().strip()
    if kind in _KINDS:
        return kind
    text = f"{kind} {row.get('object', '')}".lower()
    for k, pattern in (("gcp", r"general\s+corporate"), ("working_capital", r"working\s+capital"),
                       ("issue_expenses", r"issue\s+(?:related\s+)?expense|offer\s+expense"),
                       ("loan_repayment", r"repay|pre-?pay|borrowing|loan"),
                       ("acquisition", r"acqui(?:re|sition)\s+(?:of\s+)?(?:a\s+)?(?:compan|business|stake)|investment\s+in"),
                       ("capex", r"capital\s+(?:expenditure|equipment)|plant|machinery|capex|construct|facility|asset")):
        if re.search(pattern, text):
            return k
    return "other"


def objects_summary(data: dict, as_of: date | None = None, gross_cr: float | None = None) -> dict:
    """GCP and working capital as shares of the gross proceeds, in crore."""
    factor = UNIT_TO_CRORE.get(str(data.get("unit", "")).lower().rstrip("s").replace("lac", "lakh"))
    if factor is None:
        return {"error": "unit not stated"}
    by_kind: dict[str, float] = {}
    for r in data.get("rows", []):
        if re.match(r"(?i)\W*(?:total|net\s+proceeds|gross\s+proceeds)\b\W*(?:\(|$)?",
                    str(r.get("object", ""))) and not re.search(
                        r"(?i)\b(?:for|towards|funding|repayment|working)\b", str(r.get("object", ""))):
            continue                    # a total row would double the base
        amount = _num(r.get("amount"))
        if amount is not None:
            kind = _kind(r)
            by_kind[kind] = by_kind.get(kind, 0.0) + amount * factor
    stated = _num(data.get("gross_proceeds"))
    uses = sum(by_kind.values()) or None
    # The RHP's own "Gross Proceeds" line (read in code) wins; a total smaller
    # than the uses it funds cannot be right.
    gross = gross_cr or (stated * factor if stated else None)
    # A gross below the uses it funds, or far above them, is a misread.
    if gross is None or (uses and not uses * 0.99 <= gross <= uses * 1.5):
        gross = uses
    gcp, wc = by_kind.get("gcp"), by_kind.get("working_capital")
    cap = gcp_cap(gross, as_of or date.today()) if gross else None
    return {"gross_cr": gross, "gcp_cr": gcp, "working_capital_cr": wc,
            "gcp_share": gcp / gross if gcp is not None and gross else None,
            "gcp_cap_cr": cap, "wc_share": wc / gross if wc is not None and gross else None,
            "by_kind_cr": {k: round(v, 4) for k, v in by_kind.items()}}
