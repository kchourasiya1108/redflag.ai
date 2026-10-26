"""BSE SME offer documents: index every company, download what we need.

Source (checked 25 Oct 2026; no robots.txt restrictions): BSE SME
"Offer Documents", https://www.bsesme.com/PublicIssues/SMEIPODRHP.aspx. One
page lists every SME company with links to its draft (DRHP), RHP, final
prospectus and basis-of-allotment advertisement (which names the lead
manager). Links look like /download/<bse_id>/<folder>/<file>.

The IPO list itself comes from the user's export of BSE SME Historical Data
(data/manual/bse_sme_historical_issues.tsv). This stage matches those IPOs
to the index by name and downloads, per IPO, the offer document (RHP, or the
prospectus for fixed-price issues) and the allotment notice. DRHPs are only
indexed; the drift flag (F12) downloads them later.
"""
from __future__ import annotations

import csv
import html
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote

from pipeline.common import names
from pipeline.common.db import upsert
from pipeline.base import Gate, StageContext

BASE = "https://www.bsesme.com"
INDEX = f"{BASE}/PublicIssues/SMEIPODRHP.aspx"
# BSE SME's public-issues tables: id=1 live and forthcoming issues (no price while
# bidding is open), id=2 every issue once bidding has closed, with its price band.
PUBLIC_ISSUES = f"{BASE}/PublicIssues/PublicIssues.aspx?id={{}}"
IPO_TYPES = {"Book Building", "Fixed Price"}
EXTRA_DEMO = ["Droneacharya Aerial Innovations Limited"]   # opened Dec 2022

SCHEMA = """
CREATE TABLE IF NOT EXISTS bse_offer_docs (
    bse_id          TEXT PRIMARY KEY,
    company         TEXT NOT NULL,
    norm            TEXT NOT NULL,
    drhp_date       TEXT,
    drhp_url        TEXT,
    rhp_url         TEXT,
    prospectus_url  TEXT,
    boa_url         TEXT
);
CREATE TABLE IF NOT EXISTS bse_ipos (
    bse_ipo_id      TEXT PRIMARY KEY,     -- BSE-<year>-<bse_id or slug>
    company         TEXT NOT NULL,
    open_date       TEXT NOT NULL,
    close_date      TEXT NOT NULL,
    price_low       REAL,
    price_high      REAL,
    face_value      REAL,
    issue_type      TEXT,
    bse_id          TEXT,
    match_type      TEXT,                 -- exact | fuzzy | none
    offer_doc_url   TEXT,
    offer_doc_path  TEXT,
    boa_path        TEXT,
    demo_only       INTEGER NOT NULL DEFAULT 0
);
"""


def run(ctx: StageContext) -> dict:
    ctx.conn.executescript(SCHEMA)
    raw = ctx.settings.raw / "bse"
    page = ctx.fetcher.get(INDEX, raw / f"offer_documents_{date.today():%Y%m%d}.html")
    index = parse_index(page.decode("utf-8", "ignore"))
    upsert(ctx.conn, "bse_offer_docs", index, ("bse_id",))
    ctx.conn.commit()
    ctx.log.info(f"BSE SME offer documents index: {len(index)} companies")

    ipos = load_ipo_list(ipo_list_path(ctx), ctx.settings.universe_from, ctx.settings.universe_to)
    by_norm = {d["norm"]: d for d in index}
    rows = []
    for ipo in ipos:
        doc, how = match(ipo["company"], by_norm)
        rows.append(ipo | {"bse_id": doc["bse_id"] if doc else None, "match_type": how,
                           "offer_doc_url": (doc["rhp_url"] or doc["prospectus_url"])
                           if doc else None})
        if not doc:
            ctx.log.failure(ipo["company"], "bse_no_offer_docs")
    if ctx.limit:
        rows = rows[:ctx.limit]

    downloaded = 0
    for n, row in enumerate(rows, 1):
        doc = next((d for d in index if d["bse_id"] == row["bse_id"]), None)
        if doc:
            row["offer_doc_path"] = download(ctx, raw / "docs" / doc["bse_id"],
                                             row["offer_doc_url"])
            row["boa_path"] = download(ctx, raw / "docs" / doc["bse_id"], doc["boa_url"])
            downloaded += bool(row["offer_doc_path"])
        if n % 25 == 0:
            ctx.log.info(f"{n}/{len(rows)} IPOs processed, {downloaded} offer documents")
    upsert(ctx.conn, "bse_ipos", rows, ("bse_ipo_id",))
    ctx.conn.commit()
    summary = {"index": len(index), "ipos": len(rows),
               "matched": sum(r["bse_id"] is not None for r in rows),
               "offer_docs": downloaded}
    ctx.log.info(f"{summary['matched']}/{len(rows)} BSE IPOs matched; "
                 f"{downloaded} offer documents downloaded", **summary)
    return summary


def parse_index(text: str) -> list[dict]:
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) != 5:
            continue
        company = html.unescape(re.sub("<[^>]+>", "", tds[0])).strip()
        links = [re.findall(r"href=['\"]([^'\"]+)['\"]", t) for t in tds[1:]]
        ids = {m.group(1) for ls in links for u in ls if (m := re.search(r"/download/(\d+)/", u))}
        if not company or not ids:
            continue
        first = lambda ls: (BASE + ls[0]) if ls else None
        drhp_date = re.search(r"\d{2}/\d{2}/\d{4}", tds[1])
        out[sorted(ids)[0]] = {
            "bse_id": sorted(ids)[0], "company": company, "norm": names.normalise(company),
            "drhp_date": (datetime.strptime(drhp_date.group(), "%d/%m/%Y").date().isoformat()
                          if drhp_date else None),
            "drhp_url": first(links[0]), "rhp_url": first(links[1]),
            "prospectus_url": first(links[2]), "boa_url": first(links[3])}
    return list(out.values())


def public_issues(fetcher, which: int, dest: Path | None = None) -> list[dict]:
    """Rows of a BSE SME public-issues table, in the manual export's column names."""
    page = fetcher.get(PUBLIC_ISSUES.format(which), dest, referer=BASE + "/").decode("utf-8", "ignore")
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        tds = [html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
               for t in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(tds) < 6 or not re.match(r"\d{2}-\d{2}-\d{4}$", tds[1] or ""):
            continue
        out.append({"scrip_name": re.sub(r"\s+", " ", tds[0]), "start_date": tds[1],
                    "end_date": tds[2], "offer_price": tds[3], "face_value": tds[4],
                    "issue_type": tds[5], "status": tds[6] if len(tds) > 6 else "Closed"})
    return out


def ipo_list_path(ctx: StageContext) -> Path:
    """Today's copy of BSE SME's closed-issues table, else the user's manual export."""
    manual = ctx.settings.data / "manual" / "bse_sme_historical_issues.tsv"
    dest = ctx.settings.raw / "bse" / f"historical_issues_{date.today():%Y%m%d}.tsv"
    try:
        rows = public_issues(ctx.fetcher, 2, ctx.settings.raw / "bse" / f"public_issues_2_{date.today():%Y%m%d}.html")
    except Exception as e:      # BSE unreachable: the export still works
        ctx.log.failure("bse_public_issues", "fetch", str(e)[:200])
        return manual
    if len(rows) < 100:
        ctx.log.failure("bse_public_issues", "too_few_rows", str(len(rows)))
        return manual
    cols = ["scrip_name", "start_date", "end_date", "offer_price", "face_value", "issue_type"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as fh:
        w = csv.DictWriter(fh, cols, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    ctx.log.info(f"BSE SME closed issues from bsesme.com: {len(rows)} rows")
    return dest


def load_ipo_list(path: Path, start: date, end: date) -> list[dict]:
    seen, out = set(), []
    for r in csv.DictReader(path.open(), delimiter="\t"):
        company = (r.get("scrip_name") or "").strip()
        try:
            opened = datetime.strptime(r["start_date"], "%d-%m-%Y").date()
            closed = datetime.strptime(r["end_date"], "%d-%m-%Y").date()
        except (KeyError, ValueError):
            continue
        demo = any(names.normalise(company) == names.normalise(d) for d in EXTRA_DEMO)
        if r.get("issue_type") not in IPO_TYPES or not (start <= opened <= end or demo):
            continue
        key = (names.normalise(company), opened)
        if key in seen:
            continue
        seen.add(key)
        prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", r.get("offer_price") or "")
                  if float(p) > 0]
        out.append({"bse_ipo_id": f"BSE-{opened.year}-{re.sub(r'[^A-Z0-9]+', '', company.upper())[:24]}",
                    "company": company, "open_date": opened.isoformat(),
                    "close_date": closed.isoformat(),
                    "price_low": min(prices) if prices else None,
                    "price_high": max(prices) if prices else None,
                    "face_value": float(r["face_value"]) if r.get("face_value") else None,
                    "issue_type": r["issue_type"], "offer_doc_path": None, "boa_path": None,
                    "demo_only": int(demo)})
    for d in EXTRA_DEMO:     # demo cases outside the export's date range
        if not any(names.normalise(o["company"]) == names.normalise(d) for o in out):
            out.append({"bse_ipo_id": f"BSE-DEMO-{re.sub(r'[^A-Z0-9]+', '', d.upper())[:24]}",
                        "company": d, "open_date": "2022-12-13", "close_date": "2022-12-15",
                        "price_low": 52.0, "price_high": 54.0, "face_value": 10.0,
                        "issue_type": "Book Building", "offer_doc_path": None,
                        "boa_path": None, "demo_only": 1})
    return out


def match(company: str, by_norm: dict[str, dict]) -> tuple[dict | None, str]:
    norm = names.normalise(company)
    if norm in by_norm:
        return by_norm[norm], "exact"
    best, score = None, 0.0
    for key, doc in by_norm.items():
        s = names.similarity(norm, key)
        if s > score:
            best, score = doc, s
    if best and score >= 0.92:      # stricter than entity matching: wrong doc = wrong numbers
        return best, "fuzzy"
    return None, "none"


def download(ctx: StageContext, folder: Path, url: str | None) -> str | None:
    if not url:
        return None
    dest = folder / unquote(url.rsplit("/", 1)[-1])
    try:
        body = ctx.fetcher.get(url, dest, referer=INDEX)
    except Exception as e:
        ctx.log.failure(url, "bse_download", str(e))
        return None
    return str(dest.relative_to(ctx.settings.data)) if body else None


def gate(conn, settings) -> Gate:
    g = Gate("bse_docs")
    try:
        n, matched, docs = conn.execute(
            "SELECT count(*), count(bse_id), count(offer_doc_path) FROM bse_ipos").fetchone()
    except Exception:
        g.add("stage run", False, "run: python run.py bse_docs")
        return g
    g.add("BSE IPOs matched to offer documents >= 90%", matched >= 0.9 * max(n, 1),
          f"{matched}/{n}")
    g.add("offer documents downloaded", docs >= 0.9 * max(matched, 1), f"{docs}/{matched}")
    return g
