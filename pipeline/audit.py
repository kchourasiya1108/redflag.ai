"""Read-only audit: cross-check archive.db against independent sources.

    python run.py audit

Each check compares a stored value with a second source that did not
produce it (raw bhavcopy files, the offer document, NSE's API), and prints
agreement plus examples of disagreement. Nothing is written.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from bisect import bisect_left
from datetime import date, datetime
from pathlib import Path

from pipeline.common import names
from pipeline import offer_docs
from pipeline.listing import PRICE_EXCHANGE

EXAMPLES = 5


def run_audit(conn, settings) -> list[dict]:
    checks = [listing_dates(conn), listing_vs_issue_price(conn), lot_sizes(conn),
              nse_cover_vs_api(conn, settings), bse_cover_dates(conn, settings),
              duplicates(conn), returns_from_raw(conn, settings)]
    return [c for c in checks if c]


def _result(name: str, agree: int, total: int, examples: list, note: str = "") -> dict:
    return {"check": name, "agree": agree, "total": total, "examples": examples[:EXAMPLES],
            "note": note}


def listing_dates(conn) -> dict:
    bad, total = [], 0
    for r in conn.execute(
            """SELECT i.ipo_id, i.exchange, i.listing_date,
                      (SELECT min(date) FROM prices p WHERE p.symbol = i.symbol
                         AND p.exchange = CASE i.exchange WHEN 'NSE Emerge' THEN 'NSE'
                                                          ELSE 'BSE' END
                         AND p.date >= i.open_date) AS first_trade
               FROM ipos i WHERE i.status = 'listed'"""):
        total += 1
        if r["first_trade"] != r["listing_date"]:
            bad.append((r["ipo_id"], r["listing_date"], r["first_trade"]))
    return _result("listing date = first trade in bhavcopy", total - len(bad), total, bad)


def listing_vs_issue_price(conn) -> dict:
    """A listing open far from the issue price usually means a wrong price or scrip."""
    bad, total = [], 0
    for r in conn.execute("SELECT ipo_id, issue_price, listing_open_price, issue_price_basis "
                          "FROM ipos WHERE listing_open_price > 0 AND issue_price > 0"):
        total += 1
        ratio = r["listing_open_price"] / r["issue_price"]
        if not 0.5 <= ratio <= 3.0:
            bad.append((r["ipo_id"], r["issue_price"], r["listing_open_price"],
                        round(ratio, 2), r["issue_price_basis"]))
    return _result("listing open within 0.5x-3x of issue price", total - len(bad), total, bad,
                   "outside the band is possible (SME listings can double) but worth a look")


def lot_sizes(conn) -> dict:
    bad, total = [], 0
    for r in conn.execute(
            """SELECT i.ipo_id, i.lot_size, s.first_lot, s.first_lot_date, i.listing_date
               FROM ipos i JOIN securities s ON s.exchange = 'NSE' AND s.symbol = i.symbol
               WHERE i.exchange = 'NSE Emerge' AND i.lot_size_source IN ('exchange_api')
                 OR (i.exchange = 'NSE Emerge' AND i.lot_size IS NOT NULL
                     AND i.lot_size_source IS NULL)"""):
        if not r["first_lot"] or not r["listing_date"] or \
                (date.fromisoformat(r["first_lot_date"]) -
                 date.fromisoformat(r["listing_date"])).days > 10:
            continue
        total += 1
        if r["first_lot"] != r["lot_size"]:
            bad.append((r["ipo_id"], r["lot_size"], r["first_lot"]))
    return _result("NSE API lot size = bhavcopy board lot at listing", total - len(bad),
                   total, bad, "validates the bhavcopy source used for BSE lot sizes")


def _pages(settings, sha: str) -> list[str]:
    path = settings.cache / "pages" / f"{sha}.json"
    return [p["text"] for p in json.loads(path.read_text())] if path.exists() else []


def nse_cover_vs_api(conn, settings) -> dict:
    """The BSE lead-manager method, scored where NSE's API gives the answer."""
    registrars = [r[0] for r in conn.execute(
        "SELECT DISTINCT registrar_raw FROM ipos WHERE registrar_raw IS NOT NULL")]
    bankers = [r[0] for r in conn.execute("SELECT canonical_name FROM bankers")]
    lm_ok = reg_ok = total = 0
    bad = []
    for r in conn.execute("""SELECT i.ipo_id, i.company, i.banker_raw, i.registrar_raw,
                                    d.sha256 FROM ipos i JOIN docs d USING (ipo_id)
                             WHERE i.exchange = 'NSE Emerge' AND d.status = 'ok'"""):
        pages = _pages(settings, r["sha256"])
        if not pages:
            continue
        got = offer_docs.extract(pages, r["company"], registrars, bankers)
        total += 1
        api = [names.core(x) for x in re.split(r"\s*(?:,|;| and )\s*", r["banker_raw"] or "")]
        lm = any(names.similarity(g, r["banker_raw"] or "") >= 0.85 or names.core(g) in api
                 for g in got["lead_managers"])
        reg = bool(got["registrar"]) and \
            names.similarity(got["registrar"], r["registrar_raw"] or "") >= 0.8
        lm_ok += lm
        reg_ok += reg
        if not (lm and reg):
            bad.append((r["ipo_id"], r["banker_raw"], got["lead_managers"],
                        r["registrar_raw"], got["registrar"]))
    return _result(f"NSE cover extraction vs API (lead manager {lm_ok}/{total}, "
                   f"registrar {reg_ok}/{total})", min(lm_ok, reg_ok), total, bad)


_OPEN = re.compile(r"(?i)(?:ISSUE|OFFER)\s+OPENS\s+ON\W*(?:[A-Z]+DAY\W*)?"
                   r"([A-Z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}(?:ST|ND|RD|TH)?\s+[A-Z]+,?\s+\d{4})")


def _parse_cover_date(text: str) -> date | None:
    t = re.sub(r"(?i)(\d)(st|nd|rd|th)", r"\1", text.replace(",", " "))
    t = re.sub(r"\s+", " ", t).strip().title()
    for fmt in ("%B %d %Y", "%d %B %Y", "%b %d %Y", "%d %b %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    return None


def bse_cover_dates(conn, settings) -> dict:
    """Issue opening date in the user's BSE export vs the RHP cover."""
    bad, total, unreadable = [], 0, 0
    for r in conn.execute("""SELECT i.ipo_id, i.open_date, d.sha256 FROM ipos i
                             JOIN docs d USING (ipo_id)
                             WHERE i.exchange = 'BSE SME' AND d.status = 'ok'"""):
        cover = " ".join(_pages(settings, r["sha256"])[:3])
        m = _OPEN.search(cover)
        got = _parse_cover_date(m.group(1)) if m else None
        if got is None:
            unreadable += 1
            continue
        total += 1
        if got.isoformat() != r["open_date"]:
            bad.append((r["ipo_id"], r["open_date"], got.isoformat()))
    return _result("BSE export open date = RHP cover open date", total - len(bad), total, bad,
                   f"{unreadable} covers had no readable opening date "
                   "(fixed-price prospectuses often print it differently)")


def duplicates(conn) -> dict:
    dup_isin = conn.execute("SELECT isin, group_concat(ipo_id) FROM ipos WHERE isin IS NOT NULL "
                            "GROUP BY isin HAVING count(*) > 1").fetchall()
    dup_sym = conn.execute("SELECT exchange, symbol, group_concat(ipo_id) FROM ipos "
                           "WHERE status='listed' GROUP BY 1, 2 HAVING count(*) > 1").fetchall()
    found = [tuple(r) for r in dup_isin] + [tuple(r) for r in dup_sym]
    n = conn.execute("SELECT count(*) FROM ipos").fetchone()[0]
    return _result("no duplicate ISIN or listed symbol", n - len(found), n, found)


# --- returns recomputed from the raw files, without the prices table ----------

def _raw_closes(settings, exchange: str, symbol: str) -> dict[str, float]:
    """{date: close} for one symbol, read straight from the downloaded files."""
    base, out = settings.raw / "bhavcopy", {}
    if exchange == "NSE":
        for p in sorted((base / "nse_sme").glob("*/*.csv")):
            for r in csv.DictReader(io.StringIO(p.read_text(errors="ignore"))):
                r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
                if r.get("SYMBOL") == symbol and r.get("SERIES") in {"SM", "ST", "SZ"}:
                    out[p.stem] = float(r["CLOSE_PRICE"])
        for p in sorted((base / "nse_udiff").glob("*/*.zip")):
            day = p.name[:10]
            if day in out:
                continue
            with zipfile.ZipFile(p) as z:
                for r in csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode())):
                    if r["TckrSymb"] == symbol and r["SctySrs"] in {"SM", "ST", "SZ"}:
                        out[day] = float(r["ClsPric"])
    else:
        for p in sorted((base / "bse" / "isin").glob("*/*.zip")):
            with zipfile.ZipFile(p) as z:
                for r in csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8", "ignore"))):
                    if r["SC_CODE"].strip() == symbol:
                        out[p.stem] = float(r["CLOSE"])
        for p in sorted((base / "bse" / "udiff").glob("*/*.csv")):
            for r in csv.DictReader(io.StringIO(p.read_text(errors="ignore"))):
                if r["FinInstrmId"].strip() == symbol and r["SctySrs"].strip() in {"M", "MT"}:
                    out[p.stem] = float(r["ClsPric"])
    return out


def _raw_calendar(settings, exchange: str) -> list[str]:
    base = settings.raw / "bhavcopy"
    if exchange == "NSE":
        days = {p.stem for p in (base / "nse_sme").glob("*/*.csv")} | \
               {p.name[:10] for p in (base / "nse_udiff").glob("*/*.zip")}
    else:
        days = {p.stem for p in (base / "bse" / "isin").glob("*/*.zip")} | \
               {p.stem for p in (base / "bse" / "udiff").glob("*/*.csv")}
    return sorted(days)


def returns_from_raw(conn, settings, per_exchange: int = 3) -> dict:
    """Recompute a few 6-month returns from raw files and compare with labels."""
    bad, total, rows = [], 0, []
    for ex_name, ex in PRICE_EXCHANGE.items():
        rows += conn.execute(
            """SELECT i.ipo_id, i.symbol, i.issue_price, i.listing_date, l.ret_6m, l.adj_factor,
                      ? AS ex
               FROM ipos i JOIN labels l USING (ipo_id)
               WHERE i.exchange = ? AND l.ret_6m IS NOT NULL
               ORDER BY (l.adj_factor != 1.0) DESC, i.ipo_id LIMIT ?""",
            (ex, ex_name, per_exchange)).fetchall()
    calendars = {ex: _raw_calendar(settings, ex) for ex in PRICE_EXCHANGE.values()}
    for r in rows:
        cal = calendars[r["ex"]]
        horizon = cal[bisect_left(cal, r["listing_date"]) + 126]
        closes = _raw_closes(settings, r["ex"], r["symbol"])
        last = max(d for d in closes if d <= horizon)
        ret = closes[last] * r["adj_factor"] / r["issue_price"] - 1
        total += 1
        ok = abs(ret - r["ret_6m"]) < 1e-4
        if not ok:
            bad.append((r["ipo_id"], r["ret_6m"], round(ret, 6)))
        print(f"  {r['ipo_id']}: listed {r['listing_date']}, day 126 = {horizon}, close "
              f"{closes[last]} on {last} x{r['adj_factor']} / {r['issue_price']} - 1 = "
              f"{ret:.4f} (label {r['ret_6m']:.4f}) {'OK' if ok else 'MISMATCH'}")
    return _result("6-month return recomputed from raw files", total - len(bad), total, bad,
                   "the adjustment factor itself comes from corp_actions")


def render(results: list[dict]) -> str:
    lines = []
    for c in results:
        mark = "OK " if c["agree"] == c["total"] else "!! "
        lines.append(f"{mark}{c['check']}: {c['agree']}/{c['total']}")
        if c["note"]:
            lines.append(f"     note: {c['note']}")
        for e in c["examples"]:
            lines.append(f"     e.g. {e}")
    return "\n".join(lines)
