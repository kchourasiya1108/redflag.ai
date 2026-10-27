"""BSE SME daily prices, loaded by the prices stage alongside NSE.

Sources (verified 25 Oct 2026), both on www.bseindia.com/download/BhavCopy/Equity/:
- EQ_ISINCODE_DDMMYY.zip: the older equity bhavcopy (SC_CODE, SC_NAME,
  SC_GROUP, ..., ISIN_CODE), available for the whole period;
- BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV: the UDiFF file, from mid-2024.
SME scrips trade in groups M and MT. A missing file comes back as an HTML
page with status 200, so the body is checked, not just the status.

Symbols are BSE scrip codes (e.g. 543894), the one key both formats share.

BSE's corporate-actions API refuses scripted requests, and its bhavcopy does
not rescale the previous close on ex-dates (checked 25 Oct 2026). Bonus
issues and splits are therefore inferred: an overnight gap within 3% of a
standard ratio (1/2, 1/3, ...) that holds for the following days is recorded
in corp_actions with source 'bse_price_gap', so it is visibly an inference.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

from pipeline.common.db import upsert
from pipeline.base import StageContext

NEW_URL = ("https://www.bseindia.com/download/BhavCopy/Equity/"
           "BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV")
OLD_URL = "https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_{d:%d%m%y}.zip"
REFERER = "https://www.bseindia.com/"
UDIFF_FROM = date(2024, 7, 1)
# Price multipliers of common bonus issues (1:1 -> 1/2, 2:1 -> 1/3, ...) and splits.
# Only drops larger than SME price bands allow (20%) are used: a 4/5 or 5/6
# "gap" is indistinguishable from an ordinary limit-down day.
STANDARD_RATIOS = sorted({1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 6, 1 / 10, 1 / 11, 2 / 3, 3 / 4,
                          2 / 5, 3 / 5})
RATIO_TOLERANCE = 0.03
SME_GROUPS = {"M", "MT"}
EXCHANGE = "BSE"


def _f(value) -> float | None:
    try:
        return float(str(value).strip()) if str(value).strip() else None
    except ValueError:
        return None


def load_day(ctx: StageContext, d: date, securities: dict) -> int | None:
    """Load one BSE trading day; None when there was no trading."""
    base = ctx.settings.raw / "bhavcopy" / "bse"
    recent = (date.today() - d).days <= 3
    rows = None
    try:
        if d >= UDIFF_FROM:
            body = _get(ctx, NEW_URL.format(d=d), base / "udiff" / f"{d.year}" / f"{d}.csv",
                        recent, lambda b: b.lstrip()[:1] != b"<")
            if body is not None:
                rows = parse_udiff(body.decode("utf-8", "ignore"), d, securities)
        if rows is None:
            body = _get(ctx, OLD_URL.format(d=d), base / "isin" / f"{d.year}" / f"{d}.zip",
                        recent, lambda b: b[:2] == b"PK")
            if body is not None:
                rows = parse_isin_zip(body, d, securities)
    except Exception as e:
        ctx.log.failure(f"bse bhavcopy {d}", "fetch_or_parse", f"{type(e).__name__}: {e}")
        return None
    if not rows:
        return None
    upsert(ctx.conn, "prices", rows, ("exchange", "symbol", "date", "series"))
    ctx.conn.execute("INSERT OR IGNORE INTO trading_days VALUES (?,?)", (EXCHANGE, d.isoformat()))
    return len(rows)


def _get(ctx, url, dest, recent, looks_valid) -> bytes | None:
    body = ctx.fetcher.get(url, dest, referer=REFERER, remember_missing=not recent)
    if body is not None and not looks_valid(body):
        dest.unlink(missing_ok=True)        # an HTML "not found" page, not data
        return None
    return body


def parse_udiff(text: str, d: date, securities: dict) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        group = (r.get("SctySrs") or "").strip()
        if group not in SME_GROUPS:
            continue
        code = (r.get("FinInstrmId") or "").strip()
        _remember(securities, code, (r.get("FinInstrmNm") or "").strip(),
                  (r.get("ISIN") or "").strip() or None, (r.get("TckrSymb") or "").strip() or None)
        remember_lot(securities[code], d, r.get("NewBrdLotQty"))
        rows.append(_row(code, group, d, r.get("OpnPric"), r.get("HghPric"), r.get("LwPric"),
                         r.get("ClsPric"), r.get("PrvsClsgPric"), r.get("TtlTradgVol"),
                         r.get("TtlTrfVal")))
    return rows


def parse_isin_zip(zipped: bytes, d: date, securities: dict) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "ignore")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        group = r.get("SC_GROUP", "")
        if group not in SME_GROUPS:
            continue
        code = r["SC_CODE"]
        _remember(securities, code, r.get("SC_NAME", ""), r.get("ISIN_CODE") or None, None)
        rows.append(_row(code, group, d, r.get("OPEN"), r.get("HIGH"), r.get("LOW"),
                         r.get("CLOSE"), r.get("PREVCLOSE"), r.get("NO_OF_SHRS"),
                         r.get("NET_TURNOV")))
    return rows


def remember_lot(info: dict, d: date, lot) -> None:
    """Keep the board lot from the earliest UDiFF day this security appears."""
    n = _f(lot)
    if n and (info.get("first_lot_date") is None or d.isoformat() < info["first_lot_date"]):
        info["first_lot"], info["first_lot_date"] = int(n), d.isoformat()


def _remember(securities: dict, code: str, name: str, isin, ticker) -> None:
    """Latest name, ISIN and ticker, plus every name seen (for matching)."""
    info = securities.setdefault(code, {"names": set()})
    if name:
        info["names"].add(name)
        info["name"] = name
    info["isin"] = isin or info.get("isin")
    info["ticker"] = ticker or info.get("ticker")


def _row(code, group, d, o, h, lo, c, prev, vol, turnover) -> dict:
    return {"exchange": EXCHANGE, "symbol": code, "series": group, "date": d.isoformat(),
            "open": _f(o), "high": _f(h), "low": _f(lo), "close": _f(c),
            "prev_close": _f(prev), "volume": int(_f(vol) or 0), "turnover": _f(turnover),
            "corp_ind": None}


def infer_corp_actions(conn) -> list[dict]:
    """Bonus or split ex-dates read from price gaps (see the module docstring)."""
    found = []
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM ipos WHERE exchange='BSE SME' AND status='listed'")]
    for symbol in symbols:
        days = [(r["date"], r["open"], r["close"]) for r in conn.execute(
            """SELECT date, open, close FROM (
                 SELECT *, row_number() OVER (PARTITION BY date ORDER BY volume DESC) rn
                 FROM prices WHERE exchange=? AND symbol=?) WHERE rn = 1 ORDER BY date""",
            (EXCHANGE, symbol))]
        for i in range(1, len(days) - 3):
            last_close, (day, open_, _) = days[i - 1][2], days[i]
            if not last_close or not open_:
                continue
            gap = open_ / last_close
            ratio = min(STANDARD_RATIOS, key=lambda r: abs(gap / r - 1))
            if abs(gap / ratio - 1) > RATIO_TOLERANCE:
                continue
            after = sorted(c for _, _, c in days[i:i + 4] if c)
            held = after and abs((after[len(after) // 2] / last_close) / ratio - 1) < 0.15
            if held:
                found.append({"exchange": EXCHANGE, "symbol": symbol, "ex_date": day,
                              "type": "inferred", "ratio": round(ratio, 6),
                              "source": "bse_price_gap"})
    return found
