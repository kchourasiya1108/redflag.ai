"""Stage: draft prospectuses (DRHP) and their restated financials, for F12 (data guide S20).

Source: BSE SME's offer-documents index (bse_docs) links each company's DRHP. NSE's
issue detail does not, so NSE IPOs have no DRHP here yet.

Each DRHP is read with the RHP's deterministic table pass only (no model), and its
verified facts go to `drhp_facts`. F12 compares them with the RHP's own facts for the
same field and year: restated numbers should not move between the two drafts.
"""
from __future__ import annotations

import hashlib

from agent.extract import extract_financials, facts_from
from pipeline.base import Gate, StageContext
from pipeline.rhp import MIN_PAGE_CHARS, MIN_TEXT_RATIO, page_texts, pick_rhp_pdf

SCHEMA = """
CREATE TABLE IF NOT EXISTS drhp_facts (
    ipo_id          TEXT NOT NULL REFERENCES ipos(ipo_id),
    field           TEXT NOT NULL,
    fiscal_year     TEXT NOT NULL,
    value           REAL,
    unit            TEXT,
    page            INTEGER,
    verified        INTEGER NOT NULL,
    PRIMARY KEY (ipo_id, field, fiscal_year)
);
"""


def drhp_facts(pages: list[str]) -> list[dict]:
    """Restated facts of a DRHP, by the no-model table pass (same parser as the RHP)."""
    return [f for f in facts_from(extract_financials(pages, gateway=None, label="drhp"))
            if f["value"] is not None]


def fetch(ctx: StageContext, ipo_id: str, url: str) -> list[str] | None:
    pdf_path = ctx.settings.raw / "drhp" / f"{ipo_id}.pdf"
    if not pdf_path.exists():
        body = ctx.fetcher.get(url, referer="https://www.bsesme.com/")
        if body is None:
            return None
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(pick_rhp_pdf(body, url))
    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    pages = page_texts(pdf_path, ctx.settings.cache / "pages" / f"{sha}.json")
    if sum(len(t) >= MIN_PAGE_CHARS for t in pages) < MIN_TEXT_RATIO * max(len(pages), 1):
        return []          # scanned: no text layer to read
    return pages


def run(ctx: StageContext) -> dict:
    ctx.conn.executescript(SCHEMA)
    ipos = ctx.conn.execute(
        """SELECT ipo_id, drhp_url FROM ipos WHERE drhp_url IS NOT NULL AND status = 'listed'
             AND ipo_id NOT IN (SELECT DISTINCT ipo_id FROM drhp_facts)
           ORDER BY open_date DESC""").fetchall()
    if ctx.limit:
        ipos = ipos[:ctx.limit]
    counts = {"drhps": 0, "facts": 0, "unreadable": 0}
    for n, ipo in enumerate(ipos, 1):
        try:
            pages = fetch(ctx, ipo["ipo_id"], ipo["drhp_url"])
        except Exception as e:      # one bad file never stops the stage
            ctx.log.failure(ipo["ipo_id"], "drhp", f"{type(e).__name__}: {e}")
            continue
        if not pages:
            counts["unreadable"] += 1
            continue
        facts = drhp_facts(pages)
        ctx.conn.executemany(
            "INSERT OR REPLACE INTO drhp_facts VALUES (?,?,?,?,?,?,?)",
            [(ipo["ipo_id"], f["field"], f["fiscal_year"], f["value"], f["unit"], f["page"],
              int(f["verified"])) for f in facts])
        ctx.conn.commit()
        counts["drhps"] += 1
        counts["facts"] += len(facts)
        if n % 25 == 0:
            ctx.log.info(f"{n}/{len(ipos)} DRHPs processed", **counts)
    ctx.log.info(f"DRHPs read {counts['drhps']}, facts {counts['facts']}, "
                 f"unreadable {counts['unreadable']}", **counts)
    return counts


def gate(conn, settings) -> Gate:
    g = Gate("drhp")
    n, ipos = conn.execute("SELECT count(*), count(DISTINCT ipo_id) FROM drhp_facts").fetchone()
    g.add("DRHP facts", n > 0, f"{n} facts for {ipos} IPOs")
    return g
