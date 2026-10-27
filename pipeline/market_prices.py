"""Stage: NSE main-board and SME daily closes, for re-pricing F8's peers on the bid date.

Source: the NSE capital-market UDiFF bhavcopies already cached by the prices stage
(data/raw/bhavcopy/nse_udiff/<year>/<date>.csv.zip), which list every traded
equity from July 2024 on. The peers an RHP names are often main-board companies,
which the SME `prices` table does not carry.

Writes `market_securities` (symbol, ISIN, name, for matching a peer's name) and
`market_closes` (symbol, date, close). Zetaris serves them as RF_ARCHIVE and
RF_PRICES tables; agents read them only through as-of guarded templates.
"""
from __future__ import annotations

import csv
import io
import zipfile

from datetime import date

from pipeline.base import Gate, StageContext
from pipeline.prices import UDIFF_URL

SERIES = {"EQ", "BE", "BZ", "SM", "ST", "SZ"}      # equity series (main board, SME)
SCHEMA = """
CREATE TABLE IF NOT EXISTS market_securities (
    symbol          TEXT PRIMARY KEY,
    isin            TEXT,
    name            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_closes (
    symbol          TEXT NOT NULL,
    date            TEXT NOT NULL,
    close           REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""


def rows_of(path) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        name = next(i for i in z.namelist() if i.lower().endswith(".csv"))
        text = z.read(name).decode("utf-8", "ignore")
    return [r for r in csv.DictReader(io.StringIO(text))
            if r.get("FinInstrmTp") == "STK" and (r.get("SctySrs") or "") in SERIES
            and r.get("ClsPric")]


UDIFF_FROM = date(2024, 7, 8)       # NSE's first UDiFF capital-market bhavcopy


def fetch_missing(ctx: StageContext, root) -> int:
    """Download the full-market file for every NSE trading day that is not cached yet."""
    days = [d for (d,) in ctx.conn.execute(
        "SELECT date FROM trading_days WHERE exchange = 'NSE' AND date >= ? ORDER BY date",
        (UDIFF_FROM.isoformat(),))]
    fetched = 0
    for d in days:
        dest = root / d[:4] / f"{d}.csv.zip"
        if dest.exists() or dest.with_name(dest.name + ".404").exists():
            continue
        try:
            recent = (date.today() - date.fromisoformat(d)).days <= 3
            if ctx.fetcher.get(UDIFF_URL.format(d=date.fromisoformat(d)), dest,
                               remember_missing=not recent):
                fetched += 1
        except Exception as e:
            ctx.log.failure(f"udiff {d}", "fetch", f"{type(e).__name__}: {e}")
    return fetched


def run(ctx: StageContext) -> dict:
    ctx.conn.executescript(SCHEMA)
    root = ctx.settings.raw / "bhavcopy" / "nse_udiff"
    ctx.log.info(f"fetched {fetch_missing(ctx, root)} full-market days")
    done = {r[0] for r in ctx.conn.execute("SELECT DISTINCT date FROM market_closes")}
    files = sorted(p for p in root.glob("*/*.csv.zip") if p.name[:10] not in done)
    if ctx.limit:
        files = files[:ctx.limit]
    securities, days = {}, 0
    for path in files:
        try:
            rows = rows_of(path)
        except Exception as e:      # one unreadable day never stops the stage
            ctx.log.failure(path.name, "udiff", f"{type(e).__name__}: {e}")
            continue
        day = path.name[:10]
        ctx.conn.executemany("INSERT OR REPLACE INTO market_closes VALUES (?,?,?)",
                             [(r["TckrSymb"], day, float(r["ClsPric"])) for r in rows])
        for r in rows:
            securities[r["TckrSymb"]] = (r["TckrSymb"], r.get("ISIN"), r.get("FinInstrmNm") or r["TckrSymb"])
        days += 1
        if days % 50 == 0:
            ctx.conn.commit()
            ctx.log.info(f"{days}/{len(files)} days read")
    ctx.conn.executemany("INSERT OR REPLACE INTO market_securities VALUES (?,?,?)", securities.values())
    ctx.conn.commit()
    total = ctx.conn.execute("SELECT count(*), count(DISTINCT symbol) FROM market_closes").fetchone()
    ctx.log.info(f"read {days} new days; {total[0]} closes for {total[1]} symbols")
    return {"days": days, "closes": total[0], "symbols": total[1]}


def gate(conn, settings) -> Gate:
    g = Gate("market_prices")
    n, s = conn.execute("SELECT count(*), count(DISTINCT symbol) FROM market_closes").fetchone()
    g.add("market closes", n > 0, f"{n} closes for {s} symbols")
    return g
