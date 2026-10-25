"""Stage: BSE SME IPOs into the same `ipos`, `ipo_banker` and `docs` tables as NSE.

Inputs: `bse_ipos` (bse_docs stage: the user's BSE SME export matched to BSE's
offer documents), `securities` (prices stage: every BSE SME scrip seen in the
bhavcopies), and each IPO's offer document.

Per IPO:
- follow-on offers are dropped: if the company already traded on BSE SME
  before the issue opened, the row is not an IPO;
- issues that never listed (withdrawn, deferred, not yet listed) are kept
  with status 'not_listed', as the data guide asks, so the universe has no
  survivorship bias; they get no label;
- scrip code, ISIN and listing date: the BSE SME scrip whose first trading
  day falls within LISTING_WINDOW_DAYS after the issue closed, with the most
  similar name (a match below MIN_NAME_SIMILARITY is not used);
- lead managers, registrar, market maker, issue shares: read from the offer
  document cover (offer_docs.py); lead managers resolve to the same `bankers`
  rows as NSE, so a banker's track record spans both exchanges;
- issue price: BSE's final price is only in the scanned allotment notice, so
  book-built issues use the top of the price band (`issue_price_basis` says
  which); NSE rows carry the final price from NSE's API.
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from redflag.common import names
from redflag.common.db import upsert
from redflag.pipeline import offer_docs
from redflag.pipeline.base import Gate, StageContext
from redflag.pipeline.listing import set_listing_open
from redflag.pipeline.rhp import MIN_PAGE_CHARS, MIN_TEXT_RATIO, page_texts
from redflag.pipeline.universe import BankerResolver, load_sebi_register

EXCHANGE = "BSE SME"
LISTING_WINDOW_DAYS = 25
LATE_LISTING_DAYS = 120     # some issues list months late; only near-exact names then
LATE_MIN_SIMILARITY = 0.9
MIN_NAME_SIMILARITY = 0.6
SAME_COMPANY = 0.85     # name similarity that marks an earlier listing of the same company


def run(ctx: StageContext) -> dict:
    conn = ctx.conn
    try:
        ipos = conn.execute("SELECT * FROM bse_ipos ORDER BY open_date").fetchall()
    except Exception:
        ctx.log.warn("no bse_ipos table; run the bse_docs stage first")
        return {}
    if ctx.limit:
        ipos = ipos[:ctx.limit]
    scrips = load_scrips(conn)
    drhp = {r["bse_id"]: r["drhp_url"] for r in conn.execute(
        "SELECT bse_id, drhp_url FROM bse_offer_docs")}
    register = load_sebi_register(ctx)
    resolver = BankerResolver(ctx, register)
    banker_names = list(register) + [r[0] for r in conn.execute(
        "SELECT canonical_name FROM bankers")]
    registrars = [r[0] for r in conn.execute(
        "SELECT DISTINCT registrar_raw FROM ipos WHERE registrar_raw IS NOT NULL")]

    rows, links, docs = [], [], []
    stats = {"listed": 0, "not_listed": 0, "follow_on_dropped": 0, "with_doc": 0,
             "with_banker": 0}
    matches = claim_scrips(ipos, scrips)
    for n, ipo in enumerate(ipos, 1):
        earlier = prior_listing(ipo, scrips)
        if earlier:
            stats["follow_on_dropped"] += 1
            ctx.log.event("follow_on_dropped", f"{ipo['company']} already listed as "
                          f"{earlier['symbol']} on {earlier['first_date']}", status="warning")
            continue
        scrip, score = matches[ipo["bse_ipo_id"]]
        if scrip is None:
            stats["not_listed"] += 1
            ctx.log.failure(ipo["bse_ipo_id"], "bse_not_listed",
                            f"{ipo['company']} closed {ipo['close_date']}; no BSE SME scrip "
                            f"listed within {LISTING_WINDOW_DAYS} days (best name match {score:.2f})")
        pages, doc = load_offer_doc(ctx, ipo)
        facts = offer_docs.extract(pages, ipo["company"], registrars, banker_names) if pages \
            else {"lead_managers": [], "registrar": None, "market_maker": None,
                  "issue_shares": None, "fresh_shares": None, "ofs_shares": None,
                  "issue_type": None, "issue_price": None}
        row = build_row(ipo, scrip, facts, drhp.get(ipo["bse_id"]))
        facts["lead_managers"] = pick_lead_managers(facts["lead_managers"], register)
        bankers = list(dict.fromkeys(b for b in (resolver.resolve(x) for x in facts["lead_managers"])
                                     if b is not None))
        row["banker_id"] = bankers[0] if bankers else None
        if not bankers:
            ctx.log.failure(row["ipo_id"], "no_banker", "offer document cover had no lead manager"
                            if pages else "no offer document")
        rows.append(row)
        links += [{"ipo_id": row["ipo_id"], "banker_id": b, "position": i}
                  for i, b in enumerate(bankers)]
        if doc:
            docs.append(doc | {"ipo_id": row["ipo_id"]})
        stats["listed"] += row["status"] == "listed"
        stats["with_doc"] += bool(pages)
        stats["with_banker"] += bool(bankers)
        if n % 25 == 0:
            ctx.log.info(f"{n}/{len(ipos)} BSE IPOs processed", **stats)

    if not ctx.limit:   # drop BSE rows a previous run wrote under other ids
        keep = {r["ipo_id"] for r in rows}
        stale = [r[0] for r in conn.execute("SELECT ipo_id FROM ipos WHERE exchange=?",
                                            (EXCHANGE,)) if r[0] not in keep]
        for table in ("ipo_banker", "labels", "docs", "facts", "features", "ipos"):
            conn.executemany(f"DELETE FROM {table} WHERE ipo_id=?", [(i,) for i in stale])
    upsert(conn, "ipos", rows, ("ipo_id",))
    conn.execute("DELETE FROM ipo_banker WHERE ipo_id IN "
                 "(SELECT ipo_id FROM ipos WHERE exchange=?)", (EXCHANGE,))
    upsert(conn, "ipo_banker", links, ("ipo_id", "banker_id"))
    prune_bankers(conn)
    upsert(conn, "docs", docs, ("ipo_id", "doc_type"))
    set_listing_open(ctx)
    conn.commit()
    stats |= {"ipos": len(rows), "bankers_created": resolver.created}
    ctx.log.info(f"wrote {len(rows)} BSE IPOs", **stats)
    return stats


def load_scrips(conn) -> list[dict]:
    """BSE SME scrips with every name they have carried."""
    all_names: dict[str, list[str]] = {}
    for r in conn.execute("SELECT symbol, name FROM security_names WHERE exchange='BSE'"):
        all_names.setdefault(r["symbol"], []).append(r["name"])
    return [dict(r) | {"names": all_names.get(r["symbol"], [r["name"] or ""])}
            for r in conn.execute("SELECT symbol, name, isin, first_date FROM securities "
                                  "WHERE exchange='BSE' AND first_date IS NOT NULL")]


def name_score(company: str, scrip: dict) -> float:
    """Best similarity over a scrip's names; abbreviations of the first word count."""
    best = max((names.similarity(company, n) for n in scrip.get("names") or [scrip["name"] or ""]),
               default=0.0)
    first = (names.core(company).split() or [""])[0]
    for n in scrip.get("names") or []:
        short = names.core(n).replace(" ", "")
        if len(first) >= 4 and short and (short.startswith(first) or first.startswith(short)) \
                and len(short) >= 4:
            best = max(best, MIN_NAME_SIMILARITY)
    return best


def prior_listing(ipo, scrips) -> dict | None:
    """A BSE SME scrip of the same company that traded before this issue opened."""
    for s in scrips:
        if s["first_date"] < ipo["open_date"] and \
                max(names.similarity(ipo["company"], n) for n in s.get("names") or [s["name"] or ""]) \
                >= SAME_COMPANY:
            return dict(s)
    return None


def prune_bankers(conn) -> None:
    """Drop bankers no IPO points to and SEBI does not list (earlier mis-reads)."""
    dead = [r[0] for r in conn.execute(
        """SELECT banker_id FROM bankers b WHERE sebi_reg_no IS NULL AND NOT EXISTS
             (SELECT 1 FROM ipo_banker ib WHERE ib.banker_id = b.banker_id)
           AND NOT EXISTS (SELECT 1 FROM ipos i WHERE i.banker_id = b.banker_id)""")]
    conn.executemany("DELETE FROM banker_alias WHERE banker_id=?", [(d,) for d in dead])
    conn.executemany("DELETE FROM bankers WHERE banker_id=?", [(d,) for d in dead])


def match_scrip(ipo, scrips) -> tuple[dict | None, float]:
    """The scrip that started trading soon after the issue closed, by name."""
    closed = date.fromisoformat(ipo["close_date"])

    def best_within(days: int) -> tuple[dict | None, float]:
        lo, hi = closed.isoformat(), (closed + timedelta(days=days)).isoformat()
        best, score = None, 0.0
        for s in scrips:
            if lo < s["first_date"] <= hi:
                sim = name_score(ipo["company"], s)
                if sim > score:
                    best, score = s, sim
        return best, score

    best, score = best_within(LISTING_WINDOW_DAYS)
    if best and score >= MIN_NAME_SIMILARITY:
        return dict(best), score
    late, late_score = best_within(LATE_LISTING_DAYS)
    if late and late_score >= LATE_MIN_SIMILARITY:
        return dict(late), late_score
    return None, max(score, late_score)


def pick_lead_managers(found: list[str], register: dict[str, str]) -> list[str]:
    """One entry per firm; when several firms appear, keep SEBI-registered bankers.

    Covers repeat a firm under its old name ("... formerly known as ...") and
    sometimes print the market maker in the same block (e.g. Alpha Numero).
    """
    found = [f for f in found if not re.match(r"(?i)formerly", f)]
    unique: list[str] = []
    for f in found:
        if not any(names.similarity(f, u) >= SAME_COMPANY for u in unique):
            unique.append(f)
    if len(unique) <= 1:
        return unique
    registered = [f for f in unique
                  if any(names.similarity(f, r) >= SAME_COMPANY for r in register)]
    return registered or unique[:1]


def claim_scrips(ipos, scrips) -> dict[str, tuple[dict | None, float]]:
    """Match every IPO; when two issues claim one scrip, the issue that closed
    nearest the listing keeps it (a withdrawn issue relaunched later)."""
    out = {ipo["bse_ipo_id"]: match_scrip(ipo, scrips) for ipo in ipos}
    by_symbol: dict[str, list] = {}
    for ipo in ipos:
        scrip, _ = out[ipo["bse_ipo_id"]]
        if scrip:
            by_symbol.setdefault(scrip["symbol"], []).append(ipo)
    for claimants in by_symbol.values():
        if len(claimants) > 1:
            keep = max(claimants, key=lambda i: i["close_date"])
            for ipo in claimants:
                if ipo is not keep:
                    out[ipo["bse_ipo_id"]] = (None, out[ipo["bse_ipo_id"]][1])
    return out


def load_offer_doc(ctx: StageContext, ipo) -> tuple[list[str], dict | None]:
    """Page texts of the IPO's offer document (cached by sha256), and its docs row."""
    rel = ipo["offer_doc_path"]
    if not rel:
        return [], None
    path = ctx.settings.data / rel
    try:
        if path.suffix.lower() == ".zip":
            path = unzip_pdf(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        pages = page_texts(path, ctx.settings.cache / "pages" / f"{sha}.json")
    except Exception as e:
        ctx.log.failure(ipo["bse_ipo_id"], "offer_doc", f"{type(e).__name__}: {e}")
        return [], None
    text_pages = sum(len(t) >= MIN_PAGE_CHARS for t in pages)
    ok = text_pages >= MIN_TEXT_RATIO * max(len(pages), 1)
    doc = {"doc_type": "rhp", "url": ipo["offer_doc_url"],
           "path": str(path.relative_to(ctx.settings.data)), "sha256": sha,
           "pages": len(pages), "text_pages": text_pages, "status": "ok" if ok else "scanned"}
    return (pages if ok else []), doc


def unzip_pdf(path: Path) -> Path:
    """The largest PDF inside a zipped offer document, extracted beside it once."""
    out = path.with_suffix(".pdf")
    if not out.exists():
        with zipfile.ZipFile(path) as z:
            pdfs = [i for i in z.infolist() if i.filename.lower().endswith(".pdf")]
            out.write_bytes(z.read(max(pdfs, key=lambda i: i.file_size)))
    return out


def build_row(ipo, scrip: dict | None, facts: dict, drhp_url: str | None) -> dict:
    opened = date.fromisoformat(ipo["open_date"])
    fixed = ipo["issue_type"] == "Fixed Price"
    price, basis = ipo["price_high"], "fixed" if fixed else "band_upper"
    if price is None and facts.get("issue_price"):
        # The export left the price blank; the prospectus cover states it.
        price, basis = facts["issue_price"], "offer_document"
    to_cr = (lambda n: round(n * price / 1e7, 4) if n and price else None)
    listing = scrip["first_date"] if scrip else None
    return {
        "ipo_id": f"BSE-{opened.year}-{scrip['symbol']}" if scrip else ipo["bse_ipo_id"],
        "company": ipo["company"], "exchange": EXCHANGE,
        # Not-listed issues have no scrip code; BSE's offer-document id stands in.
        "symbol": scrip["symbol"] if scrip else f"BSEDOC-{ipo['bse_id'] or 'NA'}",
        "isin": scrip["isin"] if scrip else None,
        "issue_type": facts["issue_type"], "issue_shares": facts["issue_shares"],
        "fresh_shares": facts["fresh_shares"], "ofs_shares": facts["ofs_shares"],
        "issue_size_cr": to_cr(facts["issue_shares"]),
        "fresh_issue_cr": to_cr(facts["fresh_shares"]), "ofs_cr": to_cr(facts["ofs_shares"]),
        "price_band_low": ipo["price_low"], "price_band_high": ipo["price_high"],
        "issue_price": price, "issue_price_basis": basis if price is not None else None,
        "face_value": ipo["face_value"],
        "open_date": ipo["open_date"], "close_date": ipo["close_date"],
        "listing_date": listing,
        "banker_raw": "; ".join({names.normalise(m): m for m in facts["lead_managers"]}.values())
                      or None, "banker_id": None,
        "registrar_raw": facts["registrar"], "market_maker_raw": facts["market_maker"],
        "rhp_url": ipo["offer_doc_url"], "drhp_url": drhp_url,
        "final_subscription_x": None,
        "status": "listed" if listing and listing <= date.today().isoformat() else "not_listed",
        "source": "bse_export+bse_docs+bhavcopy",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def gate(conn, settings) -> Gate:
    g = Gate("bse_universe")
    n, listed, banker, doc = conn.execute(
        "SELECT count(*), sum(status='listed'), count(banker_id), count(rhp_url) "
        "FROM ipos WHERE exchange=?", (EXCHANGE,)).fetchone()
    g.add("BSE IPOs in ipos", (n or 0) > 0, f"{n} rows")
    g.add("matched to a listed scrip >= 90%", (listed or 0) >= 0.9 * max(n or 1, 1),
          f"{listed}/{n}")
    g.add("lead manager found >= 90%", (banker or 0) >= 0.9 * max(n or 1, 1), f"{banker}/{n}")
    g.add("offer document >= 90%", (doc or 0) >= 0.9 * max(n or 1, 1), f"{doc}/{n}")
    note = settings.data / "gates" / "bse_spotcheck.md"
    g.add("10-row spot-check against BSE", note.exists(), f"manual; record it in {note.name}")
    return g
