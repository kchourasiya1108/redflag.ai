"""Stage 05: download RHPs and cache their page text (Lane B, first half).

Source (verified 25 Oct 2026): NSE serves each SME RHP as a zip,
e.g. https://archives.nseindia.com/content/ipo/RHP_KLL.zip, holding the RHP
PDF and sometimes a general information document (GID) alongside it.

The PDF is kept under data/raw/rhp/ (never committed); its text goes to
data/cache/pages/{sha256}.json, one entry per page, so no PDF is ever read
twice. A document where fewer than 80% of pages have a text layer is marked
`scanned` and skipped by extraction: OCR is not run in the pipeline.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pypdfium2 as pdfium

from redflag.common.db import upsert
from redflag.pipeline.base import Gate, StageContext

MIN_PAGE_CHARS = 200     # a page with less text than this has no usable text layer
MIN_TEXT_RATIO = 0.8


def run(ctx: StageContext) -> dict:
    ipos = ctx.conn.execute(
        """SELECT i.ipo_id, i.rhp_url FROM ipos i
           LEFT JOIN docs d ON d.ipo_id = i.ipo_id AND d.doc_type = 'rhp'
           WHERE i.rhp_url IS NOT NULL AND i.status = 'listed' AND i.exchange = 'NSE Emerge'
             AND (d.status IS NULL OR d.status = 'error')
           ORDER BY i.open_date""").fetchall()
    if ctx.limit:
        ipos = ipos[:ctx.limit]
    ctx.log.info(f"{len(ipos)} RHPs to fetch")
    counts: dict[str, int] = {}
    for n, ipo in enumerate(ipos, 1):
        doc = fetch_rhp(ctx, ipo["ipo_id"], ipo["rhp_url"])
        upsert(ctx.conn, "docs", [doc], ("ipo_id", "doc_type"))
        ctx.conn.commit()
        counts[doc["status"]] = counts.get(doc["status"], 0) + 1
        if n % 25 == 0:
            ctx.log.info(f"{n}/{len(ipos)} RHPs processed", **counts)
    ctx.log.info(f"processed {len(ipos)} RHPs", **counts)
    return counts


def fetch_rhp(ctx: StageContext, ipo_id: str, url: str) -> dict:
    raw = ctx.settings.raw / "rhp"
    doc = {"ipo_id": ipo_id, "doc_type": "rhp", "url": url, "path": None,
           "sha256": None, "pages": None, "text_pages": None, "status": "error"}
    pdf_path = raw / f"{ipo_id}.pdf"
    try:
        if not pdf_path.exists():
            body = ctx.fetcher.get(url)   # zips are not kept; only the PDF is
            if body is None:
                doc["status"] = "missing"
                ctx.log.failure(ipo_id, "rhp_404", url)
                return doc
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(pick_rhp_pdf(body, url))
        sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        pages = page_texts(pdf_path, ctx.settings.cache / "pages" / f"{sha}.json")
        text_pages = sum(len(t) >= MIN_PAGE_CHARS for t in pages)
        doc.update(path=str(pdf_path.relative_to(ctx.settings.data)), sha256=sha,
                   pages=len(pages), text_pages=text_pages,
                   status="ok" if text_pages >= MIN_TEXT_RATIO * len(pages) else "scanned")
    except Exception as e:
        ctx.log.failure(ipo_id, "rhp", f"{type(e).__name__}: {e}")
    return doc


def pick_rhp_pdf(body: bytes, url: str) -> bytes:
    """The RHP inside NSE's zip: prefer a name containing 'RHP', else the largest PDF."""
    if body[:4] == b"%PDF":
        return body
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        pdfs = [i for i in z.infolist() if i.filename.lower().endswith(".pdf")]
        if not pdfs:
            raise ValueError(f"no PDF in {url}")
        named = [i for i in pdfs if "rhp" in Path(i.filename).name.lower()]
        best = max(named or pdfs, key=lambda i: i.file_size)
        return z.read(best)


def page_texts(pdf_path: Path, cache: Path) -> list[str]:
    """Text of every page, cached by the PDF's sha256."""
    if cache.exists():
        return [p["text"] for p in json.loads(cache.read_text())]
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        texts = [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
    finally:
        pdf.close()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([{"page": i + 1, "text": t} for i, t in enumerate(texts)]))
    return texts


def gate(conn, settings) -> Gate:
    g = Gate("rhp")
    total, ok, scanned = conn.execute(
        "SELECT count(*), sum(status='ok'), sum(status='scanned') FROM docs "
        "WHERE doc_type='rhp'").fetchone()
    listed = conn.execute("SELECT count(*) FROM ipos WHERE status='listed' "
                          "AND rhp_url IS NOT NULL").fetchone()[0]
    g.add("downloaded", total >= 0.9 * max(listed, 1), f"{total}/{listed} RHPs")
    g.add("text-extractable", (ok or 0) >= 0.8 * max(total, 1),
          f"{ok or 0} with text, {scanned or 0} scanned")
    note = settings.data / "gates" / "rhp_demo_check.md"
    g.add("demo RHPs confirmed clean", note.exists(), f"manual; record it in {note.name}")
    return g
