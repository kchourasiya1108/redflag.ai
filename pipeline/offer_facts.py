"""Stage: fill IPO fields from offer documents and bhavcopies, for both exchanges.

Runs after the RHP page caches exist (rhp for NSE, bse_universe for BSE) and
only fills fields that are empty, so exchange-API values always win:

- lot_size: NSE's API where present; otherwise the board lot from the first
  UDiFF bhavcopy day (NewBrdLotQty) if that day is close to listing or no
  bonus/split happened in between; otherwise the number the offer document
  states (book-built RHPs often print "[●]"). `lot_size_source` records which;
- cin, nic_code, nic_section: from the cover page. NIC is the industry code
  registered with the CIN, comparable across exchanges, unlike NSE's own
  industry label (kept in `industry`);
- market_maker_raw: from the General Information chapter;
- the statutory auditor (S13) into `auditors` / `ipo_auditor`, with its ICAI
  firm registration number, peer-review certificate number and whether the
  auditor changed in the last three years.

Where NSE's API gives the lot size, the document and bhavcopy values are
checked against it and the agreement is logged.
"""
from __future__ import annotations

import json
from datetime import date

from pipeline.common import names
from pipeline import offer_docs
from pipeline.base import Gate, StageContext
from pipeline.listing import PRICE_EXCHANGE

LOT_DAYS_FROM_LISTING = 10


def run(ctx: StageContext) -> dict:
    conn = ctx.conn
    rows = conn.execute(
        """SELECT i.ipo_id, i.exchange, i.symbol, i.company, i.listing_date, i.lot_size,
                  i.lot_size_source, i.market_maker_raw, i.cin, d.sha256
           FROM ipos i LEFT JOIN docs d ON d.ipo_id = i.ipo_id AND d.doc_type = 'rhp'
                                        AND d.status = 'ok'""").fetchall()
    if ctx.limit:
        rows = rows[:ctx.limit]
    # Auditors and people are rebuilt from scratch so extraction fixes reach every IPO.
    for table in ("ipo_auditor", "auditor_alias", "auditors", "ipo_people"):
        conn.execute(f"DELETE FROM {table}")
    # Derived lot sizes are recomputed every run; exchange-API values are kept.
    conn.execute("UPDATE ipos SET lot_size = NULL, lot_size_source = NULL "
                 "WHERE lot_size_source IN ('bhavcopy', 'offer_document')")
    rows = [dict(r) | ({"lot_size": None, "lot_size_source": None}
                       if r["lot_size_source"] in ("bhavcopy", "offer_document") else {})
            for r in rows]
    lots = {(r["exchange"], r["symbol"]): (r["first_lot"], r["first_lot_date"]) for r in
            conn.execute("SELECT exchange, symbol, first_lot, first_lot_date FROM securities")}
    check = {"doc": [0, 0], "bhavcopy": [0, 0]}      # [agree, compared] against NSE's API
    filled = {"lot_size": 0, "cin": 0, "market_maker": 0, "auditor": 0, "people": 0}
    for r in rows:
        pages = load_pages(ctx, r["sha256"])
        updates: dict = {}
        board = board_lot(conn, r, lots)
        doc_lot = offer_docs.lot_size(pages) if pages else None
        if r["lot_size"] is not None:
            if r["lot_size_source"] is None:
                updates["lot_size_source"] = "exchange_api"
            for kind, value in (("doc", doc_lot), ("bhavcopy", board)):
                if value is not None:
                    check[kind][1] += 1
                    check[kind][0] += value == r["lot_size"]
        elif board is not None:
            updates |= {"lot_size": board, "lot_size_source": "bhavcopy"}
        elif doc_lot is not None:
            updates |= {"lot_size": doc_lot, "lot_size_source": "offer_document"}
        if pages and r["cin"] is None and (cin := offer_docs.cin(pages)):
            code, section = offer_docs.nic_section(cin)
            updates |= {"cin": cin, "nic_code": code, "nic_section": section}
        if pages and r["market_maker_raw"] is None and \
                (mm := offer_docs.market_maker(pages, r["company"])):
            updates["market_maker_raw"] = mm
        if pages:
            filled["auditor"] += record_auditor(conn, r["ipo_id"], offer_docs.auditor(pages))
            found = offer_docs.people(pages)
            conn.executemany("INSERT OR IGNORE INTO ipo_people (ipo_id, person_key, name, role) "
                             "VALUES (?,?,?,?)", [(r["ipo_id"], p["person_key"], p["name"], p["role"])
                                                  for p in found])
            filled["people"] += len(found)
        if updates:
            filled["lot_size"] += "lot_size" in updates
            filled["cin"] += "cin" in updates
            filled["market_maker"] += "market_maker_raw" in updates
            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE ipos SET {cols} WHERE ipo_id=?",
                         (*updates.values(), r["ipo_id"]))
    conn.commit()
    agreement = {k: f"{a}/{n}" for k, (a, n) in check.items()}
    ctx.log.info(f"filled {filled}; lot size agreement with NSE API: {agreement}",
                 filled=filled, agreement=agreement)
    return filled | {"agreement": agreement}


def record_auditor(conn, ipo_id: str, found: dict) -> bool:
    """Store one IPO's auditor, merging spellings of the same firm."""
    if not found["name"]:
        conn.execute("INSERT OR REPLACE INTO ipo_auditor VALUES (?,?,?,?,?)",
                     (ipo_id, None, None, found["changed_3y"], None))
        return False
    alias = names.normalise(found["name"])
    hit = conn.execute("SELECT auditor_id FROM auditor_alias WHERE alias=?", (alias,)).fetchone()
    if hit:
        auditor_id = hit["auditor_id"]
    else:
        same = next((a for a in conn.execute("SELECT auditor_id, canonical_name, icai_frn "
                                             "FROM auditors")
                     if (found["frn"] and a["icai_frn"] == found["frn"])
                     or names.similarity(a["canonical_name"], found["name"]) >= 0.92), None)
        auditor_id = same["auditor_id"] if same else conn.execute(
            "INSERT INTO auditors (canonical_name, icai_frn, peer_review_no) VALUES (?,?,?)",
            (found["name"], found["frn"], found["peer_review_no"])).lastrowid
        conn.execute("INSERT OR IGNORE INTO auditor_alias VALUES (?,?)", (alias, auditor_id))
    conn.execute("UPDATE auditors SET icai_frn = coalesce(icai_frn, ?), "
                 "peer_review_no = coalesce(peer_review_no, ?) WHERE auditor_id = ?",
                 (found["frn"], found["peer_review_no"], auditor_id))
    conn.execute("INSERT OR REPLACE INTO ipo_auditor VALUES (?,?,?,?,?)",
                 (ipo_id, auditor_id, found["name"], found["changed_3y"], found["page"]))
    return True


def load_pages(ctx: StageContext, sha: str | None) -> list[str]:
    if not sha:
        return []
    path = ctx.settings.cache / "pages" / f"{sha}.json"
    return [p["text"] for p in json.loads(path.read_text())] if path.exists() else []


def board_lot(conn, ipo, lots) -> int | None:
    """Board lot from the first UDiFF day, only if that day is within days of listing.

    Exchanges revise SME lot sizes as prices move, so a lot seen months after
    listing is not the IPO lot (checked against NSE's API: within 10 days 27/27
    agree; any later day only 260/393).
    """
    lot, seen = lots.get((PRICE_EXCHANGE.get(ipo["exchange"]), ipo["symbol"]), (None, None))
    if not lot or not ipo["listing_date"]:
        return None
    days = (date.fromisoformat(seen) - date.fromisoformat(ipo["listing_date"])).days
    return lot if 0 <= days <= LOT_DAYS_FROM_LISTING else None


def gate(conn, settings) -> Gate:
    g = Gate("offer_facts")
    for ex in PRICE_EXCHANGE:
        n, lot, nic, mm = conn.execute(
            "SELECT count(*), count(lot_size), count(nic_section), count(market_maker_raw) "
            "FROM ipos WHERE exchange=? AND status='listed'", (ex,)).fetchone()
        n = max(n, 1)
        g.add(f"{ex} lot size >= 90%", lot >= 0.9 * n, f"{lot}/{n}")
        g.add(f"{ex} NIC industry >= 85%", nic >= 0.85 * n, f"{nic}/{n}")
        g.add(f"{ex} market maker >= 85%", mm >= 0.85 * n, f"{mm}/{n}")
        aud = conn.execute("SELECT count(a.auditor_id) FROM ipo_auditor a JOIN ipos i "
                           "USING (ipo_id) WHERE i.exchange=?", (ex,)).fetchone()[0]
        g.add(f"{ex} auditor >= 85%", aud >= 0.85 * n, f"{aud}/{n}")
    return g
