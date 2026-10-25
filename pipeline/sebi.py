"""Stage 04: SEBI enforcement orders (S07) and the entity map (S08).

Source (verified 25 Oct 2026): SEBI Enforcement -> Orders, searched by name
through /sebiweb/ajax/home/getnewslistinfo.jsp (all order types, smid=0).
Each order page embeds its PDF.

This stage:
1. registers every IPO company and banker as an entity, with aliases;
2. searches SEBI orders for each of them, plus a few SME keywords;
3. keeps orders whose title names one of our entities, downloads the PDF and
   caches its text;
4. proposes `sebi_order_entity` rows with reviewed = 0.

Nothing here raises a flag or a label by itself: a person reviews each
proposed row (`python run.py sebi-review export|import`), and only rows with
reviewed = 1 are used. Entity extraction from the PDF text with an LLM
(promoters, directors, auditors named inside the order) comes with the model
gateway; until then the proposals come from title matches only.
"""
from __future__ import annotations

import csv
import html
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from pypdf import PdfReader

from pipeline.common import names
from pipeline.common.db import upsert
from pipeline.base import Gate, StageContext

LISTING = ("https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
           "?doListing=yes&sid=2&ssid=9&smid=0")
AJAX = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"
KEYWORDS = ["SME", "initial public offer", "IPO proceeds", "Emerge platform",
            "merchant banker"]
MIN_CORE_LEN = 6   # shorter names ("ABC") match too many unrelated titles


def run(ctx: StageContext) -> dict:
    register_entities(ctx)
    entities = load_entities(ctx.conn)
    queries = [(e["entity_id"], e["query"]) for e in entities]
    queries += [(None, k) for k in KEYWORDS]
    if ctx.limit:
        queries = queries[:ctx.limit]
    ctx.log.info(f"searching SEBI orders for {len(queries)} names and keywords")

    ctx.fetcher.get(LISTING)   # session cookies for the AJAX endpoint
    found: dict[str, dict] = {}
    for entity_id, query in queries:
        try:
            for order in search(ctx, query):
                found.setdefault(order["order_id"], order)
        except Exception as e:
            ctx.log.failure(query, "search", str(e))

    proposals, kept = [], []
    for order in found.values():
        hits = match_title(order["title"], entities)
        if not hits:
            continue
        order["matched_query"] = "; ".join(h["query"] for h in hits)
        kept.append(order)
        proposals += [{"order_id": order["order_id"], "entity_name": h["name"],
                       "entity_role": h["type"], "entity_id": h["entity_id"],
                       "finding": None, "action": None, "period": None,
                       "reviewed": 0} for h in hits]
    for order in kept:
        fetch_text(ctx, order)
    upsert(ctx.conn, "sebi_orders", kept, ("order_id",))
    # Never overwrite a person's review decision.
    ctx.conn.executemany(
        "INSERT OR IGNORE INTO sebi_order_entity VALUES (?,?,?,?,?,?,?,?)",
        [(p["order_id"], p["entity_name"], p["entity_role"], p["entity_id"],
          p["finding"], p["action"], p["period"], p["reviewed"]) for p in proposals])
    ctx.conn.commit()
    summary = {"orders_seen": len(found), "orders_kept": len(kept),
               "proposed_links": len(proposals)}
    ctx.log.info(f"kept {len(kept)} of {len(found)} orders; "
                 f"{len(proposals)} links await review", **summary)
    return summary


def register_entities(ctx: StageContext) -> None:
    conn = ctx.conn
    ents, aliases = [], []
    for r in conn.execute("SELECT ipo_id, company FROM ipos"):
        eid = f"ent:company:{r['ipo_id']}"
        ents.append({"entity_id": eid, "type": "company", "canonical_name": r["company"]})
        aliases.append({"alias": names.normalise(r["company"]), "entity_id": eid,
                        "match_type": "exact", "similarity": 1.0})
    for r in conn.execute("SELECT banker_id, canonical_name FROM bankers"):
        eid = f"ent:banker:{r['banker_id']}"
        ents.append({"entity_id": eid, "type": "banker",
                     "canonical_name": r["canonical_name"]})
    for r in conn.execute("SELECT alias, banker_id FROM banker_alias"):
        aliases.append({"alias": r["alias"], "entity_id": f"ent:banker:{r['banker_id']}",
                        "match_type": "exact", "similarity": 1.0})
    upsert(conn, "entities", ents, ("entity_id",))
    upsert(conn, "entity_alias", aliases, ("alias", "entity_id"))
    conn.commit()


def load_entities(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT entity_id, type, canonical_name FROM entities "
                          "WHERE type IN ('company', 'banker')"):
        query = names.core(r["canonical_name"])
        if len(query) >= MIN_CORE_LEN:
            out.append({"entity_id": r["entity_id"], "type": r["type"],
                        "name": r["canonical_name"], "query": query})
    return out


def search(ctx: StageContext, query: str) -> list[dict]:
    raw = ctx.settings.raw / "sebi_orders" / "search"
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:80]
    orders, page, last = [], 0, 0
    while page <= last:
        form = {"nextValue": "1", "next": "s" if page == 0 else "n",
                "search": query, "fromDate": "", "toDate": "", "fromYear": "",
                "toYear": "", "deptId": "-1", "sid": "2", "ssid": "9",
                "smid": "0", "ssidhidden": "9", "intmid": "-1",
                "sText": "Enforcement", "ssText": "Orders", "smText": "",
                "doDirect": "-1" if page == 0 else str(page)}
        body = ctx.fetcher.post_form(AJAX, form, referer=LISTING,
                                     dest=raw / f"{slug}_p{page}.html")
        text = body.decode("utf-8", "ignore")
        orders += parse_listing(text)
        if page == 0:
            m = re.search(r"name='totalpage' value=(\d+)", text)
            last = min(int(m.group(1)) - 1, 4) if m else 0   # cap at 5 pages
        page += 1
    return orders


def parse_listing(text: str) -> list[dict]:
    orders = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        m = re.search(r"<td>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td>\s*"
                      r"<a href='([^']+_(\d+)\.html)'[^>]*>(.*?)</a>", row, re.S)
        if not m:
            continue
        when, url, num, title = m.groups()
        title = re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", "", title))).strip()
        orders.append({
            "order_id": f"sebi-{num}",
            "order_date": datetime.strptime(when, "%b %d, %Y").date().isoformat(),
            "order_type": order_type(title), "title": title, "url": url,
            "pdf_url": None, "matched_query": None, "text_chars": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    return orders


def order_type(title: str) -> str:
    t = title.lower()
    for key, kind in (("settlement", "settlement"), ("interim", "interim"),
                      ("ad interim", "interim"), ("confirmatory", "final"),
                      ("adjudication", "adjudication"), ("final order", "final"),
                      ("revocation", "other")):
        if key in t:
            return kind
    return "final" if t.startswith("order") else "other"


def match_title(title: str, entities: list[dict]) -> list[dict]:
    """Entities whose distinctive name appears, as whole words, in the title."""
    padded = f" {names.normalise(title)} "
    return [e for e in entities if f" {e['query']} " in padded]


def fetch_text(ctx: StageContext, order: dict) -> None:
    """Download the order PDF and cache its text (read once, never again)."""
    base = ctx.settings.raw / "sebi_orders"
    text_path = ctx.settings.cache / "sebi_text" / f"{order['order_id']}.txt"
    try:
        page = (ctx.fetcher.get(order["url"], base / "pages" / f"{order['order_id']}.html")
                or b"").decode("utf-8", "ignore")
        m = re.search(r"file=([^'\"&]+\.pdf)", page, re.I)
        if m:
            order["pdf_url"] = urljoin("https://www.sebi.gov.in/", m.group(1))
        if not text_path.exists():
            if order["pdf_url"]:
                pdf = base / f"{order['order_id']}.pdf"
                ctx.fetcher.get(order["pdf_url"], pdf)
                text = "\n\f".join(p.extract_text() or "" for p in PdfReader(pdf).pages)
            else:
                # Older orders are published as HTML with no PDF.
                text = inline_text(page)
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(text)
        order["text_chars"] = len(text_path.read_text())
        if order["text_chars"] < 500:
            ctx.log.failure(order["order_id"], "little_text", order["url"])
    except Exception as e:
        ctx.log.failure(order["order_id"], "pdf", str(e))


def inline_text(page: str) -> str:
    """Body text of an HTML-only order, from its title down."""
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    start = text.find("SECURITIES AND EXCHANGE BOARD OF INDIA")
    return text[start:] if start >= 0 else text


def export_review(conn, path: Path) -> int:
    """Write proposed links for a person to confirm (set reviewed to 1 or -1)."""
    rows = conn.execute(
        """SELECT e.order_id, o.order_date, o.order_type, o.title, o.url,
                  e.entity_name, e.entity_role, e.entity_id, e.reviewed
           FROM sebi_order_entity e JOIN sebi_orders o USING (order_id)
           ORDER BY o.order_date""").fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys() if rows else ["order_id"])
        w.writerows([tuple(r) for r in rows])
    return len(rows)


def import_review(conn, path: Path) -> int:
    n = 0
    with path.open() as f:
        for r in csv.DictReader(f):
            if r.get("reviewed") in ("1", "-1"):
                n += conn.execute(
                    "UPDATE sebi_order_entity SET reviewed=? WHERE order_id=? "
                    "AND entity_name=? AND entity_role=?",
                    (int(r["reviewed"]), r["order_id"], r["entity_name"],
                     r["entity_role"])).rowcount
    conn.commit()
    return n


def gate(conn, settings) -> Gate:
    g = Gate("sebi")
    dated = conn.execute("SELECT count(*) FROM sebi_orders WHERE order_date IS NOT NULL"
                         ).fetchone()[0]
    g.add("20+ orders with dates", dated >= 20, f"{dated} orders")
    with_text = conn.execute("SELECT count(*) FROM sebi_orders WHERE text_chars > 500"
                             ).fetchone()[0]
    g.add("order text extracted", with_text >= 0.9 * max(dated, 1),
          f"{with_text}/{dated} have text")
    pending = conn.execute("SELECT count(*) FROM sebi_order_entity WHERE reviewed=0"
                           ).fetchone()[0]
    g.add("every extracted row reviewed", pending == 0, f"{pending} awaiting review")
    return g
