"""Open and upcoming SME IPOs, with their RHPs, ready to check.

Sources (checked 26 Oct 2026):
- NSE: https://www.nseindia.com/api/all-upcoming-issues?category=ipo (open and
  forthcoming issues; SME rows have series "SME"), then the same ipo-detail
  API as the universe stage for the lead managers, registrar, lot and RHP link.
- BSE SME: the "Offer Documents" index (pipeline/bse_docs.py). A company with
  an RHP but no prospectus or allotment notice yet has not finished its IPO.

Each RHP becomes an upload record (agent/uploads.py) with a `source`, so an
investor finds it listed and checks it exactly like an RHP they uploaded.
The exchange's own values (dates, price band, lead managers) win over what
is read from the cover; the investor can still correct them.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from agent import uploads
from agent.config import Settings
from pipeline import bse_docs, universe
from pipeline.common import names
from pipeline.rhp import pick_rhp_pdf

NSE_UPCOMING = "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"
NSE_REFERER = "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"
BSE_RECENT_DAYS = 240        # an RHP whose draft was filed longer ago than this is stale
MAX_PER_EXCHANGE = 12


def status_path(cfg: Settings) -> Path:
    return uploads.folder(cfg) / "upcoming.json"


def last_refresh(cfg: Settings) -> dict:
    path = status_path(cfg)
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def refresh(cfg: Settings, fetcher, log=print) -> dict:
    """Find open and upcoming SME IPOs on both exchanges and load their RHPs."""
    started = time.time()
    found, errors = [], []
    for name, source in (("NSE Emerge", nse_issues), ("BSE SME", bse_issues)):
        try:
            issues = source(cfg, fetcher)[:MAX_PER_EXCHANGE]
        except Exception as e:      # one exchange down never hides the other
            errors.append(f"{name}: {type(e).__name__}: {e}"[:300])
            log(f"{name} list failed: {e}")
            continue
        log(f"{name}: {len(issues)} open or upcoming SME issue(s)")
        for issue in issues:
            try:
                record = load_issue(cfg, fetcher, issue)
                found.append(record["upload_id"])
                log(f"  {issue['company']}: {record['upload_id']}")
            except Exception as e:
                errors.append(f"{issue['company']}: {type(e).__name__}: {e}"[:300])
                log(f"  {issue['company']} failed: {e}")
    summary = {"refreshed_at": time.time(), "seconds": round(time.time() - started, 1),
               "found": found, "errors": errors}
    path = status_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=1))
    return summary


def load_issue(cfg: Settings, fetcher, issue: dict) -> dict:
    """Download one issue's RHP into an upload record, keyed by the PDF, with exchange values."""
    body = fetcher.get(issue["rhp_url"], referer=issue.get("referer"))
    if body is None:
        raise uploads.UploadError("the RHP link returned nothing")
    record = uploads.create(cfg, pick_rhp_pdf(body, issue["rhp_url"]),
                            f"{issue['company']} RHP.pdf")
    source = {k: issue.get(k) for k in ("exchange", "symbol", "status", "open_date", "close_date",
                                        "price_band", "url", "rhp_url")}
    first_time = not record.get("source")
    record = uploads.set_source(cfg, record["upload_id"], source)
    if first_time:      # exchange values fill the form once; the investor's edits stay
        overrides = {k: issue[k] for k in ("company", "open_date", "issue_price", "exchange",
                                           "lead_managers", "registrar", "market_maker",
                                           "lot_size") if issue.get(k)}
        record = uploads.update(cfg, record["upload_id"], overrides)
    return record


# -- NSE ------------------------------------------------------------------------

def nse_issues(cfg: Settings, fetcher) -> list[dict]:
    rows = json.loads(fetcher.get(NSE_UPCOMING, referer=NSE_REFERER))
    out = []
    for row in rows if isinstance(rows, list) else []:
        if (row.get("series") or "").upper() != "SME":
            continue
        symbol = (row.get("symbol") or "").strip()
        detail = json.loads(fetcher.get(universe.DETAIL.format(symbol=symbol), referer=NSE_REFERER))
        info = universe.issue_info(detail)
        rhp = next((u for t in ("Red Herring Prospectus", "Prospectus", "Offer Document")
                    if (u := universe._url(info.get(t)))), None)
        if not rhp:
            continue
        band = [float(x) for x in re.findall(r"Rs\.?\s*([\d.]+)", row.get("issuePrice") or "")]
        opened = universe.parse_date(row.get("issueStartDate"))
        closed = universe.parse_date(row.get("issueEndDate"))
        lead = universe._clean(info.get("Book Running Lead Managers")
                               or info.get("Book Running Lead Manager") or info.get("Lead Managers"))
        out.append({
            "exchange": "NSE Emerge", "symbol": symbol, "status": row.get("status"),
            "company": (row.get("companyName") or "").strip(),
            "open_date": opened.isoformat() if opened else None,
            "close_date": closed.isoformat() if closed else None,
            "price_band": row.get("issuePrice"), "issue_price": band[-1] if band else None,
            "lead_managers": names.split_names(lead) if lead else [],
            "registrar": universe._clean(info.get("Name of the Registrar")),
            "market_maker": universe._clean(info.get("Market Maker")),
            "lot_size": universe.lot_size(info),
            "url": f"https://www.nseindia.com/market-data/issue-information?symbol={symbol}&series=SME&type=Active",
            "rhp_url": rhp, "referer": NSE_REFERER})
    return out


# -- BSE SME --------------------------------------------------------------------

def bse_issues(cfg: Settings, fetcher, today: date | None = None) -> list[dict]:
    page = fetcher.get(bse_docs.INDEX)
    index = bse_docs.parse_index(page.decode("utf-8", "ignore"))
    since = (today or date.today()) - timedelta(days=BSE_RECENT_DAYS)
    archived = _archived_names(cfg)
    out = []
    for doc in index:
        if not doc["rhp_url"] or doc["prospectus_url"] or doc["boa_url"]:
            continue            # no RHP yet, or the IPO has already closed
        if not doc["drhp_date"] or date.fromisoformat(doc["drhp_date"]) < since:
            continue
        if doc["norm"] in archived:
            continue
        out.append({"exchange": "BSE SME", "symbol": doc["bse_id"], "status": "Upcoming",
                    "company": doc["company"], "rhp_url": doc["rhp_url"],
                    "url": bse_docs.INDEX, "referer": bse_docs.INDEX})
    return sorted(out, key=lambda d: d["symbol"], reverse=True)    # newest BSE ids first


def _archived_names(cfg: Settings) -> set[str]:
    import sqlite3
    if not cfg.archive_db.exists():
        return set()
    with sqlite3.connect(f"file:{cfg.archive_db}?mode=ro", uri=True) as conn:
        return {names.normalise(r[0]) for r in conn.execute("SELECT company FROM ipos")}
