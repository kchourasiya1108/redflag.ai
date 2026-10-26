"""Fill an uploaded RHP's details from the best source, so the investor only gives the PDF.

Sources, most trusted first; each field records where it came from:

1. Our archive: an IPO we already know (matched by CIN, else by name) has
   every detail from the exchange's own records.
2. The exchange, for open and upcoming issues: NSE's upcoming-issues and
   issue-detail APIs (dates, price band, lead managers, registrar, market
   maker, lot) and BSE SME's public-issues page (dates, offer price).
3. The RHP cover, read in code (agent/uploads.cover_facts), with the
   archive's lead-manager names as a fallback list.
4. Nemotron reading the cover, only for what is still missing. Every value
   must come with a verbatim quote that appears on those pages; a value whose
   quote is not on the cover is dropped, so nothing is invented.
"""
from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date

from pydantic import BaseModel

from agent.config import Settings
from pipeline import offer_docs
from pipeline.common import names as namelib

AI_BUDGET_S = 30.0       # an upload never waits longer than this for the model

FIELDS = ("company", "open_date", "issue_price", "exchange", "lead_managers", "registrar",
          "market_maker", "auditor", "lot_size")
LABEL = {"archive": "our IPO archive", "nse": "NSE", "bse": "BSE SME", "rhp": "the RHP cover",
         "ai": "the RHP cover (read by AI, quote checked)", "investor": "you"}


def prefill(cfg: Settings, record: dict, pages: list[str], *, fetcher=None, gateway=None,
            exchange_hint: dict | None = None) -> tuple[dict, dict, list[str]]:
    """(fields, provenance, notes) for one upload. Investor edits are applied later, on top."""
    fields: dict = {}
    prov: dict[str, str] = {}
    notes: list[str] = []

    def take(values: dict, source: str) -> None:
        for k in FIELDS:
            v = values.get(k)
            if k not in fields and v not in (None, "", [], 0):
                fields[k], prov[k] = v, source

    detected = record.get("detected") or {}
    take(archive_match(cfg, detected), "archive")
    if exchange_hint:
        take(exchange_hint, exchange_hint.get("source") or "nse")
    elif fetcher is not None and len(fields) < len(FIELDS):
        try:
            values, source = exchange_match(cfg, fetcher, detected.get("company") or fields.get("company"))
            take(values, source)
        except Exception as e:      # an exchange site down never blocks the check
            notes.append(f"exchange lookup failed: {type(e).__name__}")
    take({**detected, "lead_managers": detected.get("lead_managers")
          or known_bankers(cfg, pages)}, "rhp")
    missing = [k for k in FIELDS if k not in fields]
    if gateway is not None and missing:
        # In a thread with a budget: a slow or failing model never holds up the check.
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prefill-ai")
        future = pool.submit(ai_cover, gateway, pages, missing)
        try:
            take(future.result(timeout=AI_BUDGET_S), "ai")
        except FutureTimeout:
            notes.append(f"AI cover read gave no answer within {AI_BUDGET_S:.0f} s")
        except Exception as e:      # no model, or a bad reply: keep what we have
            notes.append(f"AI cover read skipped: {type(e).__name__}")
        finally:
            pool.shutdown(wait=False)
    return fields, prov, notes


# -- 1. the archive -----------------------------------------------------------------

def _archive(cfg: Settings) -> sqlite3.Connection | None:
    for path in (cfg.archive_db, cfg.data / "sample" / "archive_sample.db"):
        if path.exists():
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
    return None


def archive_match(cfg: Settings, detected: dict) -> dict:
    conn = _archive(cfg)
    if conn is None:
        return {}
    with conn:
        row = None
        if detected.get("cin"):
            row = conn.execute("SELECT * FROM ipos WHERE cin = ? ORDER BY open_date DESC",
                               (detected["cin"],)).fetchone()
        if row is None and detected.get("company"):
            target = namelib.normalise(detected["company"])
            for r in conn.execute("SELECT * FROM ipos"):
                if namelib.normalise(r["company"]) == target:
                    row = r
                    break
        if row is None:
            return {}
        aud = conn.execute("SELECT coalesce(x.auditor_raw, a.canonical_name) FROM ipo_auditor x "
                           "LEFT JOIN auditors a USING (auditor_id) WHERE x.ipo_id = ?",
                           (row["ipo_id"],)).fetchone()
    return {"company": row["company"], "open_date": row["open_date"],
            "issue_price": row["issue_price"], "exchange": row["exchange"],
            "lead_managers": namelib.split_names(row["banker_raw"]) if row["banker_raw"] else [],
            "registrar": row["registrar_raw"], "market_maker": row["market_maker_raw"],
            "auditor": aud[0] if aud else None, "lot_size": row["lot_size"],
            "archive_ipo_id": row["ipo_id"]}


def known_bankers(cfg: Settings, pages: list[str]) -> list[str]:
    """Lead managers named on the cover, from the archive's list of merchant bankers."""
    conn = _archive(cfg)
    if conn is None or not pages:
        return []
    with conn:
        candidates = [r[0] for r in conn.execute("SELECT canonical_name FROM bankers")]
    cover = offer_docs.cover_text(pages, 4)
    return [b for b in offer_docs.known_names(cover, candidates)
            if not offer_docs.is_registrar(b)][:4]


# -- 2. the exchanges ---------------------------------------------------------------

def exchange_match(cfg: Settings, fetcher, company: str | None) -> tuple[dict, str]:
    """The open or upcoming issue with this company's name, from NSE, then BSE SME."""
    from pipeline import upcoming
    if not company:
        return {}, ""
    for issues, source in ((lambda: upcoming.nse_issues(cfg, fetcher, need_rhp=False), "nse"),
                           (lambda: upcoming.bse_public_issues(fetcher), "bse")):
        for issue in issues():
            if namelib.similarity(issue["company"], company) >= 0.9:
                return {k: issue.get(k) for k in FIELDS}, source
    return {}, ""


# -- 4. Nemotron, quote-checked -------------------------------------------------------

class Quoted(BaseModel):
    value: str | None = None
    quote: str | None = None            # copied verbatim from the pages


class CoverRead(BaseModel):
    company: Quoted = Quoted()
    open_date: Quoted = Quoted()        # the public bid/issue opening date (YYYY-MM-DD)
    issue_price: Quoted = Quoted()      # fixed price, or the top of the price band, in rupees
    exchange: Quoted = Quoted()         # "NSE Emerge" or "BSE SME"
    lead_managers: list[Quoted] = []
    registrar: Quoted = Quoted()
    market_maker: Quoted = Quoted()
    auditor: Quoted = Quoted()
    lot_size: Quoted = Quoted()


def _flat(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", text.lower()).strip()


def ai_cover(gateway, pages: list[str], wanted: list[str]) -> dict:
    """Ask the model for the missing cover facts; keep only values whose quote is on the cover."""
    cover = offer_docs.cover_text(pages, 4)[:14000]
    prompt = (
        "Below are the first pages of an Indian SME IPO red herring prospectus (RHP). "
        f"Fill these fields: {', '.join(wanted)}. For each field give the value and a short "
        "quote copied exactly from the pages that shows it. Use null when the pages do not "
        "state it (a price printed as [●] is not stated). open_date is the public bid/issue "
        "opening date (not the anchor investor date), as YYYY-MM-DD. issue_price is the fixed "
        "issue price or the top of the price band, a number in rupees. exchange is "
        "'NSE Emerge' or 'BSE SME'. lead_managers are the book running lead managers only.\n\n"
        f"PAGES:\n{cover}")
    res = gateway.generate(prompt, "You copy facts from documents. Never guess; quote exactly.",
                           schema=CoverRead, route="extract", purpose="prefill:cover")
    got: CoverRead = res.parsed
    if got is None:
        return {}
    flat = _flat(cover)

    def ok(q: Quoted | None) -> bool:
        return bool(q and q.value and q.quote and len(q.quote) >= 4 and _flat(q.quote) in flat)

    out: dict = {}
    for k in ("company", "registrar", "market_maker", "auditor"):
        q = getattr(got, k)
        if k in wanted and ok(q) and _flat(q.value) in flat:
            out[k] = q.value.strip()
    if "exchange" in wanted and ok(got.exchange) and got.exchange.value in ("NSE Emerge", "BSE SME"):
        out["exchange"] = got.exchange.value
    if "open_date" in wanted and ok(got.open_date):
        try:
            d = date.fromisoformat(got.open_date.value[:10])
            from agent.uploads import first_date
            if first_date(got.open_date.quote) == d:      # the quote itself says that date
                out["open_date"] = d.isoformat()
        except ValueError:
            pass
    for k, cast in (("issue_price", float), ("lot_size", int)):
        q = getattr(got, k)
        if k in wanted and ok(q):
            try:
                n = cast(float(re.sub(r"[^\d.]", "", q.value)))
            except ValueError:
                continue
            if n > 0 and re.search(rf"(?<![\d.]){re.escape(str(n).rstrip('0').rstrip('.'))}(?![\d])",
                                   q.quote.replace(",", "")):
                out[k] = n
    if "lead_managers" in wanted:
        lms = [q.value.strip() for q in got.lead_managers if ok(q) and _flat(q.value) in flat]
        if lms:
            out["lead_managers"] = lms[:4]
    return out
