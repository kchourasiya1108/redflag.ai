"""Stage 02: daily SME prices (S04) and corporate-action adjustments (S05).

Sources (verified 25 Oct 2026):
- NSE Emerge daily bhavcopy, .../archives/sme/bhavcopy/smeDDMMYY.csv, with
  series SM (normal), ST (trade-for-trade) and SZ. NSE stopped publishing it
  after 10 Oct 2025.
- From then on, the UDiFF cash-market bhavcopy
  .../content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip, which carries
  the same SM/ST series.
A weekday where neither file exists was a market holiday, so the files that
exist are the trading calendar. BSE SME prices are loaded the same way by
bse_prices.py, into the same tables, keyed by scrip code.

Bonus issues and splits come from NSE's corporate-actions API
(https://www.nseindia.com/api/corporates-corporateActions?index=sme), whose
subjects read like "BONUS 3:1" or "FACE VALUE SPLIT ... FROM RS 10/- ... TO
RS 2/-". The SME bhavcopy does NOT rescale PREV_CL_PR on ex-dates (checked
25 Oct 2026), so it cannot be used for this; its CORP_IND flag (XB, XS) is
used only to find ex-dates the API missed, which are logged for review.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from bisect import bisect_left
from datetime import date, datetime, timedelta

from redflag.common.db import upsert
from redflag.pipeline import bse_prices
from redflag.pipeline.base import Gate, StageContext
from redflag.pipeline.listing import (PRICE_EXCHANGE, fix_trading_symbols,
                                      reconcile_issue_prices, set_listing_open)

URL = "https://nsearchives.nseindia.com/archives/sme/bhavcopy/sme{d:%d%m%y}.csv"
UDIFF_URL = ("https://nsearchives.nseindia.com/content/cm/"
             "BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
SME_SERIES = {"SM", "ST", "SZ"}
CORP_ACTIONS = ("https://www.nseindia.com/api/corporates-corporateActions"
                "?index=sme&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}")
EXCHANGE = "NSE"
START_MARGIN_DAYS = 40      # BSE's Droneacharya demo case opened in Dec 2022


def run(ctx: StageContext) -> dict:
    conn = ctx.conn
    start = ctx.settings.universe_from - timedelta(days=START_MARGIN_DAYS)
    end = date.today() - timedelta(days=1)
    loaded = {(r[0], r[1]) for r in conn.execute("SELECT exchange, date FROM trading_days")}
    securities: dict[str, dict[str, dict]] = {"NSE": {}, "BSE": {}}
    loaders = {"NSE": lambda d: load_day(ctx, d, securities["NSE"]),
               "BSE": lambda d: bse_prices.load_day(ctx, d, securities["BSE"])}

    days = {ex: 0 for ex in loaders}
    rows_in = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            for ex, load in loaders.items():
                if (ex, d.isoformat()) in loaded:
                    continue
                n = load(d)
                if n is not None:
                    days[ex] += 1
                    rows_in += n
        if d.day == 1:
            conn.commit()
            ctx.log.info(f"prices loaded up to {d}", **days)
        d += timedelta(days=1)
    conn.commit()

    update_securities(ctx, securities)
    symbols_fixed = fix_trading_symbols(ctx)
    actions = load_corp_actions(ctx, start, end)
    set_listing_open(ctx)
    prices_fixed = reconcile_issue_prices(ctx)
    conn.commit()
    summary = {"new_days": days, "rows": rows_in, "corp_actions": actions,
               "symbols_fixed": symbols_fixed, "issue_prices_fixed": prices_fixed}
    ctx.log.info(f"loaded {days} new days, {rows_in} rows, "
                 f"{actions} corporate actions", **summary)
    return summary


def update_securities(ctx: StageContext, found: dict[str, dict[str, dict]]) -> None:
    """Names and ISINs seen in the files, plus each security's first and last day."""
    rows = [{"exchange": ex, "symbol": sym, "name": info.get("name"), "isin": info.get("isin"),
             "ticker": info.get("ticker"), "first_date": None, "last_date": None}
            for ex, syms in found.items() for sym, info in syms.items()]
    ctx.conn.executemany(
        """INSERT INTO securities (exchange, symbol, name, isin, ticker) VALUES (?,?,?,?,?)
           ON CONFLICT (exchange, symbol) DO UPDATE SET
             name = coalesce(excluded.name, name), isin = coalesce(excluded.isin, isin),
             ticker = coalesce(excluded.ticker, ticker)""",
        [(r["exchange"], r["symbol"], r["name"], r["isin"], r["ticker"]) for r in rows])
    ctx.conn.executemany(
        """UPDATE securities SET first_lot=?, first_lot_date=?
           WHERE exchange=? AND symbol=? AND (first_lot_date IS NULL OR first_lot_date > ?)""",
        [(info["first_lot"], info["first_lot_date"], ex, sym, info["first_lot_date"])
         for ex, syms in found.items() for sym, info in syms.items() if info.get("first_lot")])
    ctx.conn.executemany(
        "INSERT OR IGNORE INTO security_names VALUES (?,?,?)",
        [(ex, sym, n) for ex, syms in found.items() for sym, info in syms.items()
         for n in info.get("names", [info.get("name")]) if n])
    ctx.conn.execute(
        """UPDATE securities SET
             first_date = (SELECT min(date) FROM prices p
                           WHERE p.exchange = securities.exchange AND p.symbol = securities.symbol),
             last_date = (SELECT max(date) FROM prices p
                          WHERE p.exchange = securities.exchange AND p.symbol = securities.symbol)""")


def load_day(ctx: StageContext, d: date, securities: dict | None = None) -> int | None:
    """Load one day's prices; None if there was no trading that day."""
    base = ctx.settings.raw / "bhavcopy"
    recent = (date.today() - d).days <= 3
    try:
        body = ctx.fetcher.get(URL.format(d=d), base / "nse_sme" / f"{d.year}" / f"{d}.csv",
                               remember_missing=not recent)
        if body is not None:
            rows = parse_bhavcopy(body.decode("utf-8", "ignore"), d, securities)
        else:
            body = ctx.fetcher.get(UDIFF_URL.format(d=d),
                                   base / "nse_udiff" / f"{d.year}" / f"{d}.csv.zip",
                                   remember_missing=not recent)
            if body is None:
                return None
            rows = parse_udiff(body, d, securities)
    except Exception as e:
        ctx.log.failure(f"bhavcopy {d}", "fetch_or_parse", f"{type(e).__name__}: {e}")
        return None
    if not rows:
        ctx.log.failure(f"bhavcopy {d}", "no_sme_rows")
        return None
    upsert(ctx.conn, "prices", rows, ("exchange", "symbol", "date", "series"))
    ctx.conn.execute("INSERT OR IGNORE INTO trading_days VALUES (?,?)",
                     (EXCHANGE, d.isoformat()))
    return len(rows)


def _f(value: str | None) -> float | None:
    try:
        return float(value.strip()) if value and value.strip() else None
    except ValueError:
        return None


def parse_bhavcopy(text: str, d: date, securities: dict | None = None) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
        symbol = r.get("SYMBOL")
        if not symbol or r.get("SERIES") not in SME_SERIES:
            continue
        if securities is not None:
            info = securities.setdefault(symbol, {"names": set(), "isin": None,
                                                  "ticker": symbol})
            info["name"] = r.get("SECURITY")
            info["names"].add(r.get("SECURITY"))
        rows.append({
            "exchange": EXCHANGE, "symbol": symbol, "series": r["SERIES"],
            "date": d.isoformat(),
            "open": _f(r.get("OPEN_PRICE")), "high": _f(r.get("HIGH_PRICE")),
            "low": _f(r.get("LOW_PRICE")), "close": _f(r.get("CLOSE_PRICE")),
            "prev_close": _f(r.get("PREV_CL_PR")),
            "volume": int(_f(r.get("NET_TRDQTY")) or 0),
            "turnover": _f(r.get("NET_TRDVAL")),
            "corp_ind": r.get("CORP_IND") or None,
        })
    return rows


def parse_udiff(zipped: bytes, d: date, securities: dict | None = None) -> list[dict]:
    """SME rows from the UDiFF cash-market bhavcopy (a zip with one CSV)."""
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "ignore")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        if (r.get("SctySrs") or "").strip() not in SME_SERIES:
            continue
        if securities is not None:
            info = securities.setdefault(r["TckrSymb"].strip(), {"names": set()})
            info["name"] = (r.get("FinInstrmNm") or "").strip()
            info["names"].add(info["name"])
            info["isin"] = (r.get("ISIN") or "").strip() or None
            info["ticker"] = r["TckrSymb"].strip()
            bse_prices.remember_lot(info, d, r.get("NewBrdLotQty"))
        rows.append({
            "exchange": EXCHANGE, "symbol": r["TckrSymb"].strip(),
            "series": r["SctySrs"].strip(), "date": d.isoformat(),
            "open": _f(r.get("OpnPric")), "high": _f(r.get("HghPric")),
            "low": _f(r.get("LwPric")), "close": _f(r.get("ClsPric")),
            "prev_close": _f(r.get("PrvsClsgPric")),
            "volume": int(_f(r.get("TtlTradgVol")) or 0),
            "turnover": _f(r.get("TtlTrfVal")), "corp_ind": None,
        })
    return rows


def daily_closes(conn, symbol: str,
                 exchange: str = EXCHANGE) -> list[tuple[str, float, float | None, int]]:
    """One row per day for a symbol (the busiest series if it has several)."""
    return [(r["date"], r["close"], r["prev_close"], r["volume"]) for r in conn.execute(
        """SELECT date, close, prev_close, volume FROM (
             SELECT *, row_number() OVER (PARTITION BY date ORDER BY volume DESC) AS rn
             FROM prices WHERE exchange=? AND symbol=?)
           WHERE rn = 1 ORDER BY date""", (exchange, symbol))]


def load_corp_actions(ctx: StageContext, start: date, end: date) -> int:
    """Bonus and split ratios from NSE, one calendar year per request."""
    raw = ctx.settings.raw / "universe" / "corp_actions"
    found = []
    for year in range(start.year, end.year + 1):
        lo, hi = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        # The current year is re-fetched daily; past years never change.
        stamp = f"_{date.today():%Y%m%d}" if year == date.today().year else ""
        try:
            body = ctx.fetcher.get(CORP_ACTIONS.format(start=lo, end=hi),
                                   raw / f"nse_sme_{year}{stamp}.json",
                                   referer="https://www.nseindia.com/companies-listing/"
                                           "corporate-filings-actions")
        except Exception as e:
            ctx.log.failure(f"corp_actions {year}", "fetch", str(e))
            continue
        for item in json.loads(body or b"[]"):
            parsed = parse_action(item.get("subject") or "")
            ex = item.get("exDate")
            if parsed and ex and ex != "-":
                kind, ratio = parsed
                found.append({"exchange": EXCHANGE, "symbol": item["symbol"].strip(),
                              "ex_date": datetime.strptime(ex, "%d-%b-%Y").date().isoformat(),
                              "type": kind, "ratio": ratio, "source": "nse_api"})
    upsert(ctx.conn, "corp_actions", found, ("exchange", "symbol", "ex_date", "type"))
    check_ex_dates(ctx)
    inferred = bse_prices.infer_corp_actions(ctx.conn)
    ctx.conn.execute("DELETE FROM corp_actions WHERE source='bse_price_gap'")
    upsert(ctx.conn, "corp_actions", inferred, ("exchange", "symbol", "ex_date", "type"))
    for a in inferred:
        ctx.log.event("corp_action_inferred", f"BSE {a['symbol']} {a['ex_date']} x{a['ratio']}",
                      status="warning")
    return len(found) + len(inferred)


def parse_action(subject: str) -> tuple[str, float] | None:
    """(type, price multiplier on the ex-date) for bonuses and splits.

    >>> parse_action("BONUS 3:1")       # 3 new shares for every 1 held
    ('bonus', 0.25)
    >>> parse_action("FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 10/- PER SHARE TO RS 2/- PER SHARE")
    ('split', 0.2)
    """
    s = subject.upper()
    if m := re.search(r"BONUS\s*(\d+)\s*:\s*(\d+)", s):
        new, held = int(m.group(1)), int(m.group(2))
        return "bonus", round(held / (new + held), 6)
    if m := re.search(r"(SPLIT|CONSOLIDATION).*?FROM\s*R[SE]\.?\s*([\d.]+).*?TO\s*R[SE]\.?\s*([\d.]+)", s):
        old, new = float(m.group(2)), float(m.group(3))
        return ("split" if new < old else "consolidation"), round(new / old, 6)
    return None


def check_ex_dates(ctx: StageContext) -> None:
    """Every XB/XS day in the bhavcopy should have an action from the API."""
    for r in ctx.conn.execute(
            """SELECT DISTINCT p.symbol, p.date, p.corp_ind FROM prices p
               WHERE p.exchange=? AND p.corp_ind IN ('XB', 'XS')
                 AND NOT EXISTS (SELECT 1 FROM corp_actions c WHERE c.exchange=p.exchange
                                 AND c.symbol=p.symbol AND c.ex_date=p.date)""",
            (EXCHANGE,)):
        ctx.log.failure(f"{r['symbol']} {r['date']}", "corp_action_missing",
                        f"bhavcopy flags {r['corp_ind']} but NSE lists no ratio")


def gate(conn, settings) -> Gate:
    g = Gate("prices")
    for ipo_exchange, ex in PRICE_EXCHANGE.items():
        days = conn.execute("SELECT count(*) FROM trading_days WHERE exchange=?",
                            (ex,)).fetchone()[0]
        g.add(f"{ex} trading days", days > 0, f"{days} days loaded")
        cal = [r[0] for r in conn.execute(
            "SELECT date FROM trading_days WHERE exchange=? ORDER BY date", (ex,))]
        ipos = conn.execute(
            "SELECT symbol, listing_date FROM ipos WHERE status='listed' AND exchange=? "
            "AND listing_date IS NOT NULL", (ipo_exchange,)).fetchall()
        at_listing = at_6m = due_6m = 0
        for ipo in ipos:
            idx = bisect_left(cal, ipo["listing_date"])
            if idx >= len(cal):
                continue
            near = cal[idx:idx + 3]
            if conn.execute("SELECT 1 FROM prices WHERE exchange=? AND symbol=? AND date IN (%s) "
                            "LIMIT 1" % ",".join("?" * len(near)),
                            (ex, ipo["symbol"], *near)).fetchone():
                at_listing += 1
            if idx + 126 < len(cal):
                due_6m += 1
                if conn.execute("SELECT 1 FROM prices WHERE exchange=? AND symbol=? "
                                "AND date BETWEEN ? AND ?",
                                (ex, ipo["symbol"], cal[idx + 105], cal[idx + 126])).fetchone():
                    at_6m += 1
        n = max(len(ipos), 1)
        g.add(f"{ex} price at listing >= 90%", at_listing >= 0.9 * n,
              f"{at_listing}/{len(ipos)} listed IPOs")
        g.add(f"{ex} price near +126 days >= 90%", at_6m >= 0.9 * max(due_6m, 1),
              f"{at_6m}/{due_6m} IPOs old enough")
    return g
