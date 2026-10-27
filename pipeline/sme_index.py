"""Stage (S06): the SME market index, for index-relative labels and market mood.

Source (verified 25 Oct 2026): NSE's index history API,
https://www.nseindia.com/api/historicalOR/indicesHistory?indexType=NIFTY%20SME%20EMERGE
&from=DD-MM-YYYY&to=DD-MM-YYYY: at most 365 days and 70 rows per request.

BSE's SME IPO index sits behind BSE's API, which refuses scripted requests,
so NIFTY SME EMERGE is the SME-market benchmark for both exchanges; labels
record which index they were measured against.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from pipeline.common.db import upsert
from pipeline.base import Gate, StageContext

INDEX = "NIFTY SME EMERGE"
URL = ("https://www.nseindia.com/api/historicalOR/indicesHistory?indexType=NIFTY%20SME%20EMERGE"
       "&from={start:%d-%m-%Y}&to={end:%d-%m-%Y}")
WINDOW_DAYS = 90          # the API returns at most 70 rows per request
START_MARGIN_DAYS = 40


def run(ctx: StageContext) -> dict:
    start = ctx.settings.universe_from - timedelta(days=START_MARGIN_DAYS)
    end = date.today() - timedelta(days=1)
    raw = ctx.settings.raw / "index"
    rows = []
    lo = start
    while lo <= end:
        hi = min(lo + timedelta(days=WINDOW_DAYS), end)
        # Windows that end before this month never change; the open one is re-fetched daily.
        stamp = f"_{date.today():%Y%m%d}" if hi >= end - timedelta(days=31) else ""
        try:
            body = ctx.fetcher.get(URL.format(start=lo, end=hi),
                                   raw / f"nifty_sme_emerge_{lo}_{hi}{stamp}.json",
                                   referer="https://www.nseindia.com/reports-indices-historical-index-data")
            data = json.loads(body).get("data") or []
        except Exception as e:
            ctx.log.failure(f"index {lo}..{hi}", "fetch", str(e))
            data = []
        for r in data:
            if r.get("EOD_CLOSE_INDEX_VAL") is not None:
                rows.append({"index_name": INDEX,
                             "date": datetime.strptime(r["EOD_TIMESTAMP"], "%d-%b-%Y").date().isoformat(),
                             "close": float(r["EOD_CLOSE_INDEX_VAL"])})
        lo = hi + timedelta(days=1)
    upsert(ctx.conn, "index_prices", rows, ("index_name", "date"))
    ctx.conn.commit()
    n = ctx.conn.execute("SELECT count(*), min(date), max(date) FROM index_prices "
                         "WHERE index_name=?", (INDEX,)).fetchone()
    ctx.log.info(f"{INDEX}: {n[0]} days, {n[1]} to {n[2]}")
    return {"days": n[0], "from": n[1], "to": n[2]}


def index_close_on_or_before(conn, day: str, index_name: str = INDEX) -> float | None:
    r = conn.execute("SELECT close FROM index_prices WHERE index_name=? AND date<=? "
                     "ORDER BY date DESC LIMIT 1", (index_name, day)).fetchone()
    return r[0] if r else None


def gate(conn, settings) -> Gate:
    g = Gate("sme_index")
    n, first, last = conn.execute("SELECT count(*), min(date), max(date) FROM index_prices "
                                  "WHERE index_name=?", (INDEX,)).fetchone()
    nse_days = conn.execute("SELECT count(*) FROM trading_days WHERE exchange='NSE' "
                            "AND date BETWEEN ? AND ?", (first or "", last or "")).fetchone()[0]
    g.add("index history loaded", n > 0, f"{n} days, {first} to {last}")
    g.add("index covers >= 98% of NSE trading days", n >= 0.98 * max(nse_days, 1),
          f"{n}/{nse_days}")
    return g
