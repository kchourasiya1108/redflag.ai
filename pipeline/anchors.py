"""Stage: anchor investors per NSE SME IPO (data guide S09, flag F13).

Source (checked 27 Oct 2026): each NSE issue's detail lists an "Anchor Allocation
Report", e.g. https://archives.nseindia.com/content/ipo/ANCHOR_ZTECH.zip, holding the
company's letter to NSE with one row per anchor investor ("1. Kingsman Wealth Fund
PCC- KIF 340,800 35.32% 110/-"). Anchors are allocated the working day before the
bid opens, so an IPO's own anchors are known on its as_of. BSE SME publishes no such
report we can reach, so BSE IPOs have no anchor rows.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date

import pypdfium2 as pdfium

from pipeline import universe
from pipeline.base import Gate, StageContext
from pipeline.common import names

SCHEMA = """
CREATE TABLE IF NOT EXISTS anchors (
    ipo_id          TEXT NOT NULL REFERENCES ipos(ipo_id),
    investor_key    TEXT NOT NULL,          -- normalised name, the same across IPOs
    name            TEXT NOT NULL,
    shares          INTEGER,
    pct             REAL,                   -- of the anchor portion
    PRIMARY KEY (ipo_id, investor_key)
);
"""
_ROW = re.compile(r"(?m)^\s*\d{1,2}\s*[.)]?\s+([A-Za-z][^\n]+?)\s+(\d[\d,]{2,})\s+(\d{1,3}(?:\.\d+)?)\s*%")


def investor_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", names.normalise(name)).strip("-")


def parse_report(text: str) -> list[dict]:
    """Anchor rows from the allocation letter (both the anchor and the mutual-fund tables)."""
    out, seen = [], set()
    for m in _ROW.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" .-")
        if re.match(r"(?i)total\b", name) or len(name) < 4:
            continue
        key = investor_key(name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"investor_key": key, "name": name,
                    "shares": int(m.group(2).replace(",", "")), "pct": float(m.group(3))})
    return out


def run(ctx: StageContext) -> dict:
    ctx.conn.executescript(SCHEMA)
    ipos = ctx.conn.execute("SELECT ipo_id, symbol, open_date FROM ipos WHERE exchange = 'NSE Emerge' "
                            "AND ipo_id NOT IN (SELECT DISTINCT ipo_id FROM anchors) "
                            "ORDER BY open_date").fetchall()
    if ctx.limit:
        ipos = ipos[:ctx.limit]
    raw = ctx.settings.raw / "universe"
    counts = {"reports": 0, "no_report": 0, "rows": 0}
    for ipo in ipos:
        detail_path = raw / "nse_detail" / f"{ipo['symbol']}_{date.fromisoformat(ipo['open_date']):%Y%m%d}.json"
        if not detail_path.exists():
            counts["no_report"] += 1
            continue
        info = universe.issue_info(json.loads(detail_path.read_text()))
        url = universe._url(info.get("Anchor Allocation Report"))
        if not url:
            counts["no_report"] += 1
            continue
        try:
            body = ctx.fetcher.get(url, ctx.settings.raw / "anchors" / f"{ipo['ipo_id']}.zip",
                                   referer="https://www.nseindia.com/")
            rows = parse_report(report_text(body)) if body else []
        except Exception as e:      # one bad report never stops the stage
            ctx.log.failure(ipo["ipo_id"], "anchor_report", f"{type(e).__name__}: {e}")
            continue
        counts["reports"] += 1
        counts["rows"] += len(rows)
        ctx.conn.executemany("INSERT OR IGNORE INTO anchors (ipo_id, investor_key, name, shares, pct) "
                             "VALUES (?,?,?,?,?)",
                             [(ipo["ipo_id"], r["investor_key"], r["name"], r["shares"], r["pct"])
                              for r in rows])
        ctx.conn.commit()
    ctx.log.info(f"anchor reports {counts['reports']}, rows {counts['rows']}, "
                 f"none listed {counts['no_report']}", **counts)
    return counts


def report_text(body: bytes) -> str:
    """Text of every PDF in the report (a zip, or a bare PDF)."""
    pdfs = []
    if body[:4] == b"%PDF":
        pdfs = [body]
    else:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            pdfs = [z.read(i) for i in z.infolist() if i.filename.lower().endswith(".pdf")]
    out = []
    for data in pdfs:
        pdf = pdfium.PdfDocument(data)
        try:
            out += [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
        finally:
            pdf.close()
    return "\n".join(out).replace("\r", "")


def gate(conn, settings) -> Gate:
    g = Gate("anchors")
    n, ipos = conn.execute("SELECT count(*), count(DISTINCT ipo_id) FROM anchors").fetchone()
    nse = conn.execute("SELECT count(*) FROM ipos WHERE exchange='NSE Emerge'").fetchone()[0]
    g.add("anchor rows", n > 0, f"{n} rows for {ipos} of {nse} NSE IPOs")
    return g
