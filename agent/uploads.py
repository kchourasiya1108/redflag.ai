"""Uploaded RHPs: an upcoming IPO checked from the prospectus an investor provides.

An upload becomes an IPO record (`UP-<hex>`) kept under data/uploads/<id>/.
Its page text goes to the same page cache as the archive's RHPs, so every
agent reads it exactly as it reads a past RHP. The cover facts (company, bid
date, price, exchange, lead managers, registrar, market maker, auditor) are
read from the text without a model; the investor confirms or corrects them
before the check runs, since a book-built RHP often prints the price as [●].

During a run, the IPO-specific templates (`ipo_meta`, `ipo_bankers`,
`ipo_parties`, `ipo_rhp_doc`, `ipo_auditor`, `ipo_facts`) are answered from
the upload (`local_rows`); every history lookup (the lead manager's and
auditor's past IPOs, SEBI orders, base rates) still goes through Zetaris.
Lead managers and the auditor are matched to the archive's firms through
Zetaris too, so their track records can be found.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import date
from pathlib import Path

from agent.config import Settings
from pipeline import offer_docs
from pipeline.common import names as namelib

PREFIX = "UP-"
MAX_BYTES = 80 * 1024 * 1024
MIN_PAGE_CHARS, MIN_TEXT_RATIO = 200, 0.8        # as pipeline/rhp.py
REQUIRED = ("company", "open_date", "issue_price", "exchange")
EXCHANGES = ("NSE Emerge", "BSE SME")
_ID = re.compile(r"^UP-[0-9a-f]{8}$")


class UploadError(ValueError):
    pass


def is_upload(ipo_id: str) -> bool:
    return ipo_id.startswith(PREFIX)


# -- reading the cover ----------------------------------------------------------

_MONTH = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                      "sep", "oct", "nov", "dec"], 1)}
_DATES = [
    re.compile(r"(?i)\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b"),   # May 02, 2025
    re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*,?\s+(20\d{2})\b"),     # 2nd May 2025
    re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b"),                              # 02.05.2025
]
_OPENS = re.compile(r"(?i)(?:BID|ISSUE|OFFER)\s*/?\s*(?:ISSUE|OFFER)?\s*OPENS?\s+ON")
_PRICE_BAND = re.compile(r"(?is)price\s+band\s*(?:of|:|is)?\s*(?:₹|rs\.?|inr|`)?\s*([\d,]+(?:\.\d+)?)"
                         r"\s*(?:/-)?\s*(?:to|-|–)\s*(?:₹|rs\.?|inr|`)?\s*([\d,]+(?:\.\d+)?)")
_CIN_LINE = re.compile(r"(?i)\b(?:corporate\s+identification\s+number|CIN)\b")
_COMPANY = re.compile(r"^[ \t]*([A-Z0-9][A-Z0-9 &.,'’()\-]{2,90}?\bLIMITED)\b", re.M)


def first_date(text: str) -> date | None:
    best: tuple[int, date] | None = None
    for i, pat in enumerate(_DATES):
        for m in pat.finditer(text):
            g = m.groups()
            try:
                if i == 0:
                    d = date(int(g[2]), _MONTH[g[0][:3].lower()], int(g[1]))
                elif i == 1:
                    d = date(int(g[2]), _MONTH[g[1][:3].lower()], int(g[0]))
                else:
                    d = date(int(g[2]), int(g[1]), int(g[0]))
            except (KeyError, ValueError):
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), d)
            break
    return best[1] if best else None


def bid_open_date(cover: str) -> date | None:
    """The date after "BID/ISSUE OPENS ON" ([●] in a draft prints no date).

    Covers often print the anchor investors' bidding day first, sometimes under
    the same label; it is never after the public bid date, so the latest wins.
    Two-column layouts print "OPENS ON ... CLOSES ON <open> <close>", so only
    the first date after each label counts.
    """
    found = [d for m in _OPENS.finditer(cover)
             if (d := first_date(cover[m.end(): m.end() + 260]))]
    return max(found) if found else None


_LIMITED_LINE = re.compile(r"(?i)^[ \t]*([A-Za-z0-9][A-Za-z0-9 &.,'’()\-]{2,90}?\bLimited)\b[ \t.]*$", re.M)
_NOT_ISSUER = re.compile(r"(?i)\b(?:BSE|NATIONAL\s+STOCK|REGISTRAR|SECURITIES|CAPITAL\s+MARKETS|"
                         r"FINANCIAL\s+SERVICES|FINSHARE|MERCHANT|ADVISORS|KFIN|INTIME|formerly|"
                         r"private\s+limited)\b")


def _clean_name(name: str) -> str:
    """Drop QR-code captions before an upper-case name ("Scan to view ... ACME LIMITED")."""
    words = re.sub(r"\s+", " ", name).strip(" ,.").split()
    if words and words[-1] == "LIMITED":
        i = len(words)
        while i > 0 and (words[i - 1].isupper() or not any(c.isalpha() for c in words[i - 1])):
            i -= 1
        words = words[i:]
    return " ".join(words).strip(" ,.()")


def company_name(pages: list[str]) -> str | None:
    """The issuer's name: the "... Limited" line nearest above its CIN, in any case."""
    for text in pages[:4]:
        text = text.replace("\r", "")
        cin = _CIN_LINE.search(text)
        if not cin:
            continue
        above = [m for m in _LIMITED_LINE.finditer(text[:cin.start()])
                 if not _NOT_ISSUER.search(m.group(1))]
        if above:
            return _clean_name(above[-1].group(1))
    for text in pages[:2]:       # no CIN label on the cover: the first capitalised "LIMITED" line
        found = [m for m in _COMPANY.finditer(text.replace("\r", "")) if not _NOT_ISSUER.search(m.group(1))]
        if found:
            return _clean_name(found[0].group(1))
    return None


def exchange(text: str) -> str | None:
    t = re.sub(r"\s+", " ", text)
    nse = re.search(r"(?i)NSE\s*EMERGE|EMERGE\s+platform|SME\s+platform\s+of\s+(?:the\s+)?(?:NSE|National)", t)
    bse = re.search(r"(?i)BSE\s*SME|SME\s+platform\s+of\s+(?:the\s+)?BSE", t)
    if nse and bse:
        return "NSE Emerge" if nse.start() < bse.start() else "BSE SME"
    return "NSE Emerge" if nse else "BSE SME" if bse else None


def price(cover: str) -> tuple[float | None, str | None]:
    """(price, how): a fixed price, or the top of a printed price band."""
    if p := offer_docs.issue_price(cover):
        return p, "fixed price on the cover"
    if m := _PRICE_BAND.search(cover):
        hi = float(m.group(2).replace(",", ""))
        if hi > 0:
            return hi, "top of the price band"
    return None, None


def cover_facts(pages: list[str]) -> dict:
    """Everything the check needs from the RHP's first pages; None where not printed."""
    cover = offer_docs.cover_text(pages, 4)
    company = company_name(pages)
    issue = offer_docs.extract(pages, company or "")
    p, how = price(cover)
    aud = offer_docs.auditor(pages)
    opened = bid_open_date(cover)
    return {
        "company": company,
        "cin": offer_docs.cin(pages),
        "open_date": opened.isoformat() if opened else None,
        "issue_price": p, "price_source": how,
        "exchange": exchange("\n".join(pages[:6])),
        "lead_managers": issue["lead_managers"],
        "registrar": issue["registrar"],
        "market_maker": issue["market_maker"],
        "auditor": aud["name"], "auditor_page": aud["page"], "auditor_changed_3y": aud["changed_3y"],
        "lot_size": offer_docs.lot_size(pages),
        "issue_type": issue["issue_type"],
        "issue_shares": issue["issue_shares"],
    }


# -- storage --------------------------------------------------------------------

def folder(cfg: Settings) -> Path:
    return cfg.data / "uploads"


def _path(cfg: Settings, upload_id: str) -> Path:
    if not _ID.match(upload_id):
        raise UploadError("not an upload id")
    return folder(cfg) / upload_id / "upload.json"


def load(cfg: Settings, upload_id: str) -> dict | None:
    try:
        path = _path(cfg, upload_id)
    except UploadError:
        return None
    return json.loads(path.read_text()) if path.exists() else None


def _save(cfg: Settings, record: dict) -> None:
    path = _path(cfg, record["upload_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1, ensure_ascii=False))
    tmp.replace(path)


def create(cfg: Settings, pdf: bytes, filename: str = "rhp.pdf") -> dict:
    """Store an uploaded PDF, cache its page text and read the cover. Same PDF, same record."""
    from pipeline.rhp import page_texts

    if len(pdf) > MAX_BYTES:
        raise UploadError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    if pdf[:4] != b"%PDF":
        raise UploadError("That file is not a PDF.")
    sha = hashlib.sha256(pdf).hexdigest()
    for existing in list_all(cfg):
        if existing["sha256"] == sha:
            return existing
    upload_id = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    pdf_path = folder(cfg) / upload_id / "rhp.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(pdf)
    try:
        pages = page_texts(pdf_path, cfg.cache / "pages" / f"{sha}.json")
    except Exception as e:   # pdfium raises its own error types on a broken file
        raise UploadError(f"The PDF could not be read: {e}") from e
    text_pages = sum(len(t) >= MIN_PAGE_CHARS for t in pages)
    status = "ok" if pages and text_pages >= MIN_TEXT_RATIO * len(pages) else "scanned"
    detected = cover_facts(pages) if status == "ok" else {}
    record = {"upload_id": upload_id, "filename": Path(filename).name[:200], "sha256": sha,
              "pages": len(pages), "text_pages": text_pages, "status": status,
              "uploaded_at": time.time(), "detected": detected,
              "fields": {k: v for k, v in detected.items() if k in EDITABLE and v not in (None, [], "")}}
    _save(cfg, record)
    return record


EDITABLE = {"company": str, "open_date": str, "issue_price": float, "exchange": str,
            "lead_managers": list, "registrar": str, "market_maker": str, "auditor": str,
            "lot_size": int}


def update(cfg: Settings, upload_id: str, changes: dict) -> dict:
    record = load(cfg, upload_id)
    if record is None:
        raise UploadError("unknown upload")
    fields = dict(record["fields"])
    for key, value in changes.items():
        if key not in EDITABLE:
            continue                # read-only facts (CIN, auditor page) stay as detected
        if value in (None, "", []):
            fields.pop(key, None)
            continue
        try:
            if key == "lead_managers":
                value = [str(v).strip() for v in value if str(v).strip()][:4]
            elif key == "open_date":
                value = date.fromisoformat(str(value)).isoformat()
            elif key == "exchange" and value not in EXCHANGES:
                raise ValueError(f"exchange must be one of {', '.join(EXCHANGES)}")
            else:
                value = EDITABLE[key](value)
                if isinstance(value, str):
                    value = value.strip()[:200]
                if key in ("issue_price", "lot_size") and value <= 0:
                    raise ValueError("must be positive")
        except (TypeError, ValueError) as e:
            raise UploadError(f"{key}: {e}") from e
        fields[key] = value
    record["fields"] = fields
    _save(cfg, record)
    return record


def set_source(cfg: Settings, upload_id: str, source: dict) -> dict:
    """Mark an upload as listed by an exchange (pipeline/upcoming.py)."""
    record = load(cfg, upload_id)
    if record is None:
        raise UploadError("unknown upload")
    record["source"] = source
    _save(cfg, record)
    return record


def missing(record: dict) -> list[str]:
    return [k for k in REQUIRED if not record["fields"].get(k)]


def list_all(cfg: Settings) -> list[dict]:
    root = folder(cfg)
    out = []
    for path in sorted(root.glob("UP-*/upload.json")) if root.exists() else []:
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: -r.get("uploaded_at", 0))


def as_of(record: dict, today: date | None = None) -> date:
    """Judge as of the bid date, or today if the bid has not opened yet."""
    opened = date.fromisoformat(record["fields"]["open_date"])
    return min(opened, today or date.today())


def meta(record: dict) -> dict:
    """The upload as an `ipo_meta` row."""
    f = record["fields"]
    return {"ipo_id": record["upload_id"], "company": f["company"], "symbol": "",
            "exchange": f["exchange"], "open_date": date.fromisoformat(f["open_date"]),
            "issue_price": float(f["issue_price"]),
            "banker_raw": ", ".join(f.get("lead_managers") or []), "rhp_url": ""}


# -- answering the IPO-specific templates during a run -----------------------------

def _match(name: str, candidates: list[dict], key: str) -> dict | None:
    """The archive firm a printed name refers to: an exact alias, else similarity >= 0.92."""
    alias = namelib.normalise(name)
    for c in candidates:
        if c.get("alias") == alias:
            return c
    best = max(candidates, key=lambda c: namelib.similarity(c[key], name), default=None)
    return best if best and namelib.similarity(best[key], name) >= 0.92 else None


def local_rows(record: dict, port) -> dict[str, list[dict]]:
    """Rows for the IPO-specific templates, with firms matched through the Data Port."""
    f = record["fields"]
    bankers = port.run("banker_names").rows if f.get("lead_managers") else []
    ipo_bankers = []
    for pos, name in enumerate(f.get("lead_managers") or [], 1):
        hit = _match(name, bankers, "canonical_name")
        if hit and all(b["banker_id"] != hit["banker_id"] for b in ipo_bankers):
            ipo_bankers.append({"banker_id": hit["banker_id"], "canonical_name": hit["canonical_name"],
                                "position": pos})
    auditor = []
    if f.get("auditor"):
        hit = _match(f["auditor"], port.run("auditor_names").rows, "canonical_name")
        if hit:
            auditor = [{"auditor_id": hit["auditor_id"], "canonical_name": hit["canonical_name"],
                        "changed_3y": record["detected"].get("auditor_changed_3y"),
                        "page": record["detected"].get("auditor_page")}]
    parties = [{"role": role, "party_id": None, "canonical_name": f[key], "raw_name": f[key]}
               for role, key in (("auditor", "auditor"), ("market_maker", "market_maker"),
                                 ("registrar", "registrar")) if f.get(key)]
    # A lead manager new to the archive has no track record, but is still screened by name.
    matched = {namelib.core(b["canonical_name"]) for b in ipo_bankers}
    parties += [{"role": "banker", "party_id": None, "canonical_name": n, "raw_name": n}
                for n in f.get("lead_managers") or []
                if not _match(n, [{"canonical_name": b["canonical_name"]} for b in ipo_bankers],
                              "canonical_name") and namelib.core(n) not in matched]
    doc = {"doc_type": "rhp", "sha256": record["sha256"], "pages": record["pages"],
           "text_pages": record["text_pages"], "status": record["status"], "url": ""}
    return {"ipo_meta": [meta(record)], "ipo_bankers": ipo_bankers, "ipo_parties": parties,
            "ipo_rhp_doc": [doc], "ipo_auditor": auditor, "ipo_facts": []}
