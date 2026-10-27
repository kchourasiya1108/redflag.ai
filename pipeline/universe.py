"""Stage 01: the SME IPO universe (S01) and the banker master (S02).

Sources (verified 25 Oct 2026):
- NSE past issues: https://www.nseindia.com/api/public-past-issues
  (all NSE issues; SME rows have securityType "SME").
- NSE issue detail: https://www.nseindia.com/api/ipo-detail?symbol=X&series=SME
  (lead managers, registrar, lot size, issue size text, RHP link, ISIN).
- SEBI register of merchant bankers (intmId=9), paged through
  /sebiweb/ajax/other/getintmfpiinfo.jsp.

BSE SME is not covered yet; its IPOs are a known gap in the universe.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timezone

from pipeline.common import names
from pipeline.common.db import upsert
from pipeline.listing import set_listing_open
from pipeline.base import Gate, StageContext

PAST_ISSUES = "https://www.nseindia.com/api/public-past-issues"
DETAIL = "https://www.nseindia.com/api/ipo-detail?symbol={symbol}&series=SME"
SEBI_MB_PAGE = ("https://www.sebi.gov.in/sebiweb/other/OtherAction.do"
                "?doRecognisedFpi=yes&intmId=9")
SEBI_MB_AJAX = "https://www.sebi.gov.in/sebiweb/ajax/other/getintmfpiinfo.jsp"
EXCHANGE = "NSE Emerge"


def run(ctx: StageContext) -> dict:
    raw = ctx.settings.raw / "universe"
    today = date.today().strftime("%Y%m%d")
    body = ctx.fetcher.get(PAST_ISSUES, raw / f"nse_past_issues_{today}.json")
    issues = [i for i in json.loads(body) if i.get("securityType") == "SME"]
    in_range = []
    for issue in issues:
        opened = parse_date(issue.get("ipoStartDate"))
        if (opened and ctx.settings.universe_from <= opened <= ctx.settings.universe_to
                and is_equity_ipo(issue)):
            in_range.append(issue)
    ctx.log.info(f"{len(issues)} NSE SME issues, {len(in_range)} in range "
                 f"{ctx.settings.universe_from}..{ctx.settings.universe_to}")
    if ctx.limit:
        in_range = in_range[:ctx.limit]

    register = load_sebi_register(ctx)
    ctx.log.info(f"SEBI merchant banker register: {len(register)} firms")
    resolver = BankerResolver(ctx, register)

    rows, links = [], []
    for issue in in_range:
        symbol = issue["symbol"].strip()
        opened = parse_date(issue["ipoStartDate"])
        try:
            detail = json.loads(ctx.fetcher.get(
                DETAIL.format(symbol=symbol),
                raw / "nse_detail" / f"{symbol}_{opened:%Y%m%d}.json",
                referer="https://www.nseindia.com/market-data/all-upcoming-issues-ipo"))
        except Exception as e:  # one bad issue never stops the stage
            ctx.log.failure(symbol, "detail_fetch", str(e))
            continue
        row = parse_issue(issue, detail)
        banker_ids = [resolver.resolve(n) for n in names.split_names(row["banker_raw"] or "")]
        banker_ids = list(dict.fromkeys(b for b in banker_ids if b is not None))
        if not banker_ids:
            ctx.log.failure(row["ipo_id"], "no_banker", row["banker_raw"] or "")
        row["banker_id"] = banker_ids[0] if banker_ids else None
        rows.append(row)
        links += [{"ipo_id": row["ipo_id"], "banker_id": b, "position": i}
                  for i, b in enumerate(banker_ids)]

    keep = {r["ipo_id"] for r in rows}
    if not ctx.limit:   # drop rows a previous run kept that are no longer IPOs
        stale = [r[0] for r in ctx.conn.execute(
            "SELECT ipo_id FROM ipos WHERE exchange=?", (EXCHANGE,)) if r[0] not in keep]
        for table in ("ipo_banker", "labels", "docs", "facts", "features", "ipos"):
            ctx.conn.executemany(f"DELETE FROM {table} WHERE ipo_id=?", [(i,) for i in stale])
    upsert(ctx.conn, "ipos", rows, ("ipo_id",))
    upsert(ctx.conn, "ipo_banker", links, ("ipo_id", "banker_id"))
    set_listing_open(ctx)
    ctx.conn.commit()
    summary = {"ipos": len(rows), "bankers": resolver.created,
               "fuzzy_matches": resolver.fuzzy, "review": resolver.review}
    ctx.log.info(f"wrote {len(rows)} IPOs", **summary)
    return summary


def is_equity_ipo(issue: dict) -> bool:
    """NSE lists debt issues, FPOs and withdrawal windows among SME issues."""
    name = (issue.get("companyName") or issue.get("company") or "").lower()
    symbol = (issue.get("symbol") or "").strip()
    price = _num(issue.get("issuePrice"))
    return not (symbol[:1].isdigit() or "fpo" in name or "withdrawal" in name
                or (price is not None and price >= 10000))


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip().title(), fmt).date()
        except ValueError:
            continue
    return None


def _num(text: str | None) -> float | None:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    return float(m.group().replace(",", "")) if m else None


def parse_issue(issue: dict, detail: dict) -> dict:
    """Turn one NSE past-issue row plus its detail JSON into an `ipos` row."""
    info = issue_info(detail)
    meta = detail.get("metaInfo") or {}
    symbol = issue["symbol"].strip()
    opened = parse_date(issue["ipoStartDate"])
    period = re.findall(r"\d{2}-[A-Za-z]{3}-\d{4}", info.get("Issue Period") or "")
    band = [float(x) for x in re.findall(r"Rs\.?\s*([\d.]+)", issue.get("priceRange") or "")]
    issue_price = _num(issue.get("issuePrice")) or (band[-1] if band else None)
    shares = parse_issue_size(info.get("Issue Size") or "")
    to_cr = (lambda n: round(n * issue_price / 1e7, 4)
             if n and issue_price else None)
    subscribed = (detail.get("demandGraph") or {}).get("noOfTimesIssueSubscribed")
    listing = parse_date(meta.get("listingDate") or issue.get("listingDate"))
    return {
        "ipo_id": f"NSE-{opened.year}-{symbol}",
        "company": (issue.get("companyName") or issue.get("company") or "").strip(),
        "exchange": EXCHANGE,
        "symbol": symbol,
        "isin": meta.get("isin"),
        "industry": meta.get("industry"),
        "issue_type": shares["type"],
        "issue_shares": shares["total"],
        "fresh_shares": shares["fresh"],
        "ofs_shares": shares["ofs"],
        "issue_size_cr": to_cr(shares["total"]),
        "fresh_issue_cr": to_cr(shares["fresh"]),
        "ofs_cr": to_cr(shares["ofs"]),
        "price_band_low": band[0] if band else None,
        "price_band_high": band[-1] if band else None,
        "issue_price": issue_price,
        "issue_price_basis": "final",
        "face_value": _num(info.get("Face Value")),
        "lot_size": lot_size(info),
        "open_date": opened.isoformat(),
        "close_date": close_date(period, issue, opened).isoformat(),
        "listing_date": listing.isoformat() if listing else None,
        "banker_raw": _clean(info.get("Book Running Lead Managers")
                             or info.get("Book Running Lead Manager")
                             or info.get("Lead Managers")),
        "banker_id": None,
        "registrar_raw": _clean(info.get("Name of the Registrar")),
        "market_maker_raw": _clean(info.get("Market Maker")),
        "rhp_url": next((u for t in ("Red Herring Prospectus", "Prospectus", "Offer Document")
                         if (u := _url(info.get(t)))), None),
        "drhp_url": _url(info.get("Draft Red Herring Prospectus")),
        "final_subscription_x": _num(subscribed) if subscribed not in (None, "-") else None,
        "status": "listed" if listing and listing <= date.today() else "upcoming",
        "source": "nse_api",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def lot_size(info: dict) -> int | None:
    """Shares per lot. NSE titles it "Bid Lot" on most issues, "Lot Size" or
    "Market Lot" on others. "Minimum Order Quantity" is not used: under the
    2025 SME rules the minimum application is two lots, not one."""
    for title in ("Bid Lot", "Lot Size", "Market Lot"):
        n = _num(info.get(title))
        if n:
            return int(n)
    return None


def issue_info(detail: dict) -> dict:
    """{title: value} from NSE's issueInfo, normalising its document links.

    Some issues put the URL folder in the title and the file name in the
    value ('https://.../content/ipo/Prospectus' -> 'PROSPECTUS_X.zip').
    """
    info = {}
    data = detail.get("issueInfo")
    for d in (data.get("dataList", []) if isinstance(data, dict) else []):
        title, value = (d.get("title") or "").strip(), d.get("value")
        if title.startswith("http"):
            folder, title = title.rsplit("/", 1)
            value = f"{folder}/{str(value).strip()}"
        info[title.strip()] = value
    return info


def close_date(period: list[str], issue: dict, opened: date) -> date:
    """Issue close date; NSE's free text is occasionally mistyped (e.g. a year off)."""
    for candidate in ([parse_date(period[1])] if len(period) > 1 else []) + [
            parse_date(issue.get("ipoEndDate"))]:
        if candidate and candidate >= opened:
            return candidate
    return opened


def parse_issue_size(text: str) -> dict:
    """Read share counts from NSE's free-text 'Issue Size'.

    >>> parse_issue_size("Fresh issue of 33,80,000 Equity Shares and OFS of 15,00,000")["total"]
    4880000
    """
    t = text.replace("\n", " ")
    grab = lambda p: (int(m.group(1).replace(",", ""))
                      if (m := re.search(p, t, re.I)) else None)
    fresh = grab(r"fresh\s+(?:issue\s+)?(?:of\s+)?(?:up\s*to\s+)?([\d,]{4,})")
    ofs = grab(r"(?:ofs|offer\s+for\s+sale)\s+(?:of\s+)?(?:up\s*to\s+)?([\d,]{4,})")
    first = grab(r"([\d,]{5,})")
    if fresh and ofs:
        total, kind = fresh + ofs, "fresh+ofs"
    elif ofs:
        total, kind = ofs, "ofs"
    else:
        total, kind = fresh or first, "fresh"
        fresh = fresh or first
    return {"total": total, "fresh": fresh, "ofs": ofs, "type": kind}


def _clean(value) -> str | None:
    if value is None:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    text = re.sub(r"\s+", " ", text).strip(' "')
    return text or None


def _url(value) -> str | None:
    m = re.search(r"https?://[^\s\"'<>]+", str(value or ""))
    return m.group() if m else None


# SEBI intermediary registers: intmId -> local folder.
REGISTERS = {9: "sebi_merchant_bankers", 10: "sebi_registrars"}


def load_sebi_register(ctx: StageContext, intm_id: int = 9) -> dict[str, str]:
    """{name: registration number} from one SEBI intermediary register
    (9 = merchant bankers, INM...; 10 = registrars to an issue, INR...)."""
    raw = ctx.settings.raw / "universe" / REGISTERS[intm_id]
    page_url = SEBI_MB_PAGE.replace("intmId=9", f"intmId={intm_id}")
    today = date.today().strftime("%Y%m%d")
    try:
        first = ctx.fetcher.get(page_url, raw / f"{today}_page_0.html").decode(
            "utf-8", "ignore")
    except Exception as e:
        ctx.log.failure("sebi_register", "fetch", str(e))
        return {}
    last = max([int(n) for n in re.findall(r"searchFormFpi\('n',\s*'(\d+)'\)", first)]
               or [0])
    pages = [first]
    for k in range(1, last + 1):
        form = {"nextValue": "2", "next": "n", "intmId": str(intm_id), "name": "",
                "regNo": "", "contPer": "", "email": "", "location": "",
                "exchange": "", "affiliate": "", "alp": "", "doDirect": str(k),
                "intmIds": ""}
        try:
            pages.append(ctx.fetcher.post_form(
                SEBI_MB_AJAX, form, referer=page_url,
                dest=raw / f"{today}_page_{k}.html").decode("utf-8", "ignore"))
        except Exception as e:
            ctx.log.failure(f"sebi_register_page_{k}", "fetch", str(e))
    register = {}
    for page in pages:
        register.update(parse_register(page))
    return register


def parse_register(page: str) -> dict[str, str]:
    # The first page uses double quotes and the AJAX pages single quotes.
    fields = re.findall(
        r"<div class=[\"']title[\"']><span>([^<]+)</span></div>\s*"
        r"<div class=[\"']value[^\"']*[\"']><span>([^<]*)</span>", page)
    register, name = {}, None
    for title, value in fields:
        value = html.unescape(value).strip()
        if title.strip() == "Name":
            name = value
        elif title.strip().startswith("Registration No") and name:
            register[name] = value
            name = None
    return register


class Resolver:
    """Maps raw intermediary names to master-table IDs, creating rows as needed.

    Order: a known alias; SEBI's register (exact, then fuzzy >= names.MERGE_AT);
    an existing row (fuzzy); else a new row. Near matches are logged for
    review and kept separate, never merged.
    """

    def __init__(self, ctx: StageContext, register: dict[str, str], *, kind: str,
                 table: str, id_col: str, alias_table: str,
                 manual: dict[str, str] | None = None):
        self.ctx, self.register, self.kind = ctx, register, kind
        self.manual = {names.normalise(k): v for k, v in (manual or {}).items()}
        self.table, self.id_col, self.alias_table = table, id_col, alias_table
        self.created = self.fuzzy = self.review = 0
        self._register_norm = {names.normalise(n): n for n in register}

    def resolve(self, raw: str) -> int | None:
        alias = names.normalise(raw)
        if not alias:
            return None
        conn = self.ctx.conn
        hit = conn.execute(f"SELECT {self.id_col} FROM {self.alias_table} WHERE alias=?",
                           (alias,)).fetchone()
        if hit:
            return hit[0]
        known = [r[0] for r in conn.execute(f"SELECT canonical_name FROM {self.table}")]
        if alias in self.manual:
            canonical, match_type, score = self.manual[alias], "manual", 1.0
        else:
            canonical, match_type, score = self._match_register(raw, alias)
        if canonical is None:
            canonical, match_type, score = self._contained(raw, known)
        if canonical is None:
            canonical, match_type, score = self._best(raw, known)
        if canonical is None:
            canonical, match_type, score = raw.strip(), "self", 1.0
        row_id = self._ensure(canonical)
        conn.execute(f"INSERT OR IGNORE INTO {self.alias_table} VALUES (?,?,?,?)",
                     (alias, row_id, match_type, score))
        return row_id

    def _match_register(self, raw: str, alias: str):
        if alias in self._register_norm:
            return self._register_norm[alias], "exact", 1.0
        hit = self._contained(raw, self.register.keys())
        return hit if hit[0] else self._best(raw, self.register.keys())

    @staticmethod
    def _contained(raw: str, candidates) -> tuple:
        """A single candidate whose name sits inside the raw text, or vice versa,
        ignoring spaces: catches "Rajiv Sharma Bigshare Services Pvt Ltd" and
        SEBI's own "PURVA SHAREGISTRY (INDIA) P LTD (BUSICOMP ...)"."""
        flat = names.core(raw).replace(" ", "")
        hits = set()
        for cand in candidates:
            c = names.core(cand).replace(" ", "")
            c = c[:-1] if c.endswith("p") and len(c) > 12 else c   # "... P LTD"
            if len(c) >= 10 and len(flat) >= 10 and (c in flat or flat in c):
                hits.add(cand)
        return (hits.pop(), "contained", 0.9) if len(hits) == 1 else (None, None, None)

    def _best(self, raw: str, candidates):
        best, score = None, 0.0
        for cand in candidates:
            s = names.similarity(raw, cand)
            if s > score:
                best, score = cand, s
        if best and score >= names.MERGE_AT:
            self.fuzzy += 1
            return best, "fuzzy", round(score, 3)
        if best and score >= names.REVIEW_AT:
            self.review += 1
            self.ctx.log.failure(raw, f"{self.kind}_review",
                                 f"near match {best!r} ({score:.3f}); kept separate")
        return None, None, None

    def _ensure(self, canonical: str) -> int:
        conn = self.ctx.conn
        row = conn.execute(f"SELECT {self.id_col} FROM {self.table} WHERE canonical_name=?",
                           (canonical,)).fetchone()
        if row:
            return row[0]
        reg = self.register.get(canonical)
        self.created += 1
        return conn.execute(
            f"INSERT INTO {self.table} (canonical_name, sebi_reg_no, source) VALUES (?,?,?)",
            (canonical, reg, "sebi_register" if reg else "documents")).lastrowid


class BankerResolver(Resolver):
    def __init__(self, ctx: StageContext, register: dict[str, str]):
        super().__init__(ctx, register, kind="banker", table="bankers", id_col="banker_id",
                         alias_table="banker_alias")


def gate(conn, settings) -> Gate:
    g = Gate("universe")
    n = conn.execute("SELECT count(*) FROM ipos").fetchone()[0]
    g.add("rows", n > 0, f"{n} IPOs")
    for col in ("issue_price", "listing_date", "banker_id"):
        missing = conn.execute(
            f"SELECT count(*) FROM ipos WHERE status='listed' AND {col} IS NULL"
        ).fetchone()[0]
        g.add(f"{col} present", missing == 0, f"{missing} listed IPOs missing")
    bad = conn.execute(
        "SELECT count(*) FROM ipos WHERE NOT (open_date <= close_date "
        "AND (listing_date IS NULL OR close_date < listing_date))").fetchone()[0]
    g.add("dates ordered", bad == 0, f"{bad} rows out of order")
    spot = conn.execute("SELECT count(*) FROM ipos WHERE rhp_url IS NOT NULL").fetchone()[0]
    g.add("rhp links", spot >= 0.9 * max(n, 1), f"{spot}/{n} have an RHP link")
    note = settings.data / "gates" / "universe_spotcheck.md"
    g.add("10-row exchange spot-check", note.exists(),
          f"manual; recorded in {note.relative_to(settings.data.parent)}"
          if note.exists() else f"manual; record it in {note.name}")
    return g
