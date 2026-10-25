"""Stage 06: restated financials from every downloaded RHP, into `facts`.

Uses the live Extractor's own code (agent/extract.py), so past and live
facts are made the same way. Per the 25 Oct 2026 decision the backtest uses
the deterministic strategy only (no model calls): statements that do not
verify are stored with verified = 0, and compute_features never reads them.
Narrative sections (capital structure, objects, customers, related parties)
are extracted with Nemotron in live runs and for the demo IPOs only.
"""
from __future__ import annotations

from collections import Counter

from agent.extract import extract_financials, facts_from
from agent.rhp import load_pages
from pipeline.base import Gate, StageContext
from pipeline.common.db import upsert


def run(ctx: StageContext) -> dict:
    docs = ctx.conn.execute("SELECT ipo_id, sha256 FROM docs WHERE doc_type = 'rhp' "
                            "AND status = 'ok' ORDER BY ipo_id").fetchall()
    if ctx.limit:
        docs = docs[:ctx.limit]
    verified, rows = Counter(), 0
    for doc in docs:
        pages = load_pages(ctx.settings.cache / "pages", doc["sha256"])
        if not pages:
            ctx.log.failure(doc["ipo_id"], "extract", "page cache missing")
            continue
        try:
            result = extract_financials(pages, label=doc["ipo_id"])
        except Exception as e:
            ctx.log.failure(doc["ipo_id"], "extract", f"{type(e).__name__}: {e}")
            continue
        facts = [{"ipo_id": doc["ipo_id"], **f, "verified": int(f["verified"])}
                 for f in facts_from(result)]
        ctx.conn.execute("DELETE FROM facts WHERE ipo_id = ? AND method LIKE 'text_table%'",
                         (doc["ipo_id"],))
        upsert(ctx.conn, "facts", facts, ("ipo_id", "field", "fiscal_year"))
        rows += len(facts)
        for kind, x in result.items():
            full = [c for c in x.checks if not c.period.startswith("S")]
            verified[kind] += bool(full and full[0].passed)
    ctx.conn.commit()
    summary = {"rhps": len(docs), "facts": rows,
               **{f"{k}_latest_verified": verified[k] for k in ("pl", "bs", "cf")}}
    ctx.log.info(f"facts from {len(docs)} RHPs", **summary)
    return summary


def gate(conn, settings) -> Gate:
    g = Gate("extract")
    rhps = conn.execute("SELECT count(*) FROM docs WHERE doc_type='rhp' AND status='ok'"
                        ).fetchone()[0]
    with_pl = conn.execute("SELECT count(DISTINCT ipo_id) FROM facts WHERE field='pl.pat' "
                           "AND verified=1").fetchone()[0]
    g.add("verified profit for >= 60% of RHPs", with_pl >= 0.6 * max(rhps, 1),
          f"{with_pl}/{rhps}")
    neg_units = conn.execute("SELECT count(*) FROM facts WHERE unit != 'cr'").fetchone()[0]
    g.add("every fact in crore", neg_units == 0, f"{neg_units} not in crore")
    return g
