"""Read issue facts from an offer document's text, without a model.

SME RHPs (and fixed-price prospectuses) on both exchanges share a cover
layout: a "BOOK RUNNING LEAD MANAGER" block, a "REGISTRAR TO THE ISSUE"
block, and an issue-size table. The market maker is named in the "General
Information" chapter. Blocks are found by their headers (a header must stand
on its own line, since "Lead Manager" also appears in prose), and firms are
read as runs of capitalised words ending in Limited/Ltd/LLP. Bankers are
then matched to SEBI's register by the caller.

Anything not found is left as None or empty; nothing is guessed.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from redflag.common import names as namelib
from redflag.pipeline.universe import parse_issue_size

# A header line: "BOOK RUNNING LEAD MANAGER TO THE ISSUE", optionally followed on
# the same line by "REGISTRAR TO THE ISSUE" (two-column covers).
_LM_HEAD = re.compile(r"(?im)^[ \t]*(?:BOOK[ \t]+RUNNING[ \t]+)?LEAD[ \t]+MANAGERS?(?:\(S\))?"
                      r"(?:[ \t]+TO[ \t]+THE[ \t]+(?:ISSUE|OFFER))?[ \t]*:?[ \t]*"
                      r"(?P<both>REGIST?RARS?[ \t]+TO[ \t]+THE[ \t]+(?:ISSUE|OFFER))?[ \t]*:?[ \t]*$")
_REG_HEAD = re.compile(r"(?im)^[ \t]*REGIST?RARS?[ \t]+TO[ \t]+THE[ \t]*(?:\n[ \t]*)?"
                       r"(?:ISSUE|OFFER)[ \t]*:?")
# Name parts that mark a registrar (RTA) rather than a banker.
_RTA_HINT = re.compile(r"(?i)registr|intime|bigshare|kfin|karvy|cameo|skyline|maashitla|purva|"
                       r"mas services|beetal|adroit|alankit|sharex|niche technologies|"
                       r"satellite corporate|cil securities|accurate securities|link intime|"
                       r"integrated registry|kfintech|mufg")
# Registration numbers and phone numbers never sit inside a firm name.
_MIDDLE_NOISE = re.compile(r"\b(?:[ULul]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}|IN[MR]-?\d{6,}|"
                           r"\+?\d[\d -]{7,}\d)\b")
_LEADING_NOISE = re.compile(r"^(?:(?:NO\.?|No\.?|Being|being|Market|MARKET|Maker|MAKER|Lead|LEAD|"
                            r"Manager\.?|MANAGER\.?|Issue|ISSUE|The|THE|In|M/s\.?|"
                            r"Registrar|REGISTRAR|Registrars|REGISTRARS|"
                            r"[ULul]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}|"      # CIN
                            r"INM-?\d+|INR-?\d+|"                           # SEBI reg. no.
                            r"(?:Mr|Ms|Mrs|Dr|Shri|Smt)\.?(?:\s+[A-Z][a-z]+\.?){1,3}|"
                            r"\+?\d[\d -]*)\s+)+")
_END_HEAD = re.compile(r"(?i)BID\s*/?\s*(?:ISSUE|OFFER)|ISSUE\s+(?:PROGRAMME|OPENS)|"
                       r"OFFER\s+PROGRAMME|ISSUE\s+PERIOD")
# Table labels that sit next to firm names on covers.
_LABELS = re.compile(r"(?i)\b(?:name\s+of\s+(?:the\s+)?book\s+running\s+lead\s+manager"
                     r"(?:\s+to\s+the\s+(?:issue|offer))?|name\s+and\s+logo|name\s+of\s+(?:the\s+)?contact\s+person|"
                     r"(?:&\s*)?contact\s+(?:person|no\.?|number|details)|"
                     r"e-?mail\s*(?:and|&)\s*tele(?:phone)?|e-?mail|"
                     r"telephone|tel\.?(?:\s*no\.?)?|website|name|logo|sebi\s+reg\w*)\b\s*:?")
_FIRM = re.compile(r"(?<![A-Za-z(])((?:(?:\(?[A-Z0-9][A-Za-z0-9.'’()-]*|&)\s+){1,10}?"
                   r"(?:PRIVATE\s+LIMITED|Private\s+Limited|LIMITED|Limited|LTD\.?|Ltd\.?|LLP))"
                   r"(?![A-Za-z])")
_NOT_A_PARTY = re.compile(r"(?i)\b(?:rating|monitoring|stock\s+exchange|bse\s+limited|"
                          r"national\s+securities\s+depository|central\s+depository|"
                          r"our\s+company|bank|banker)\b")


def firms(text: str, exclude: str = "") -> list[str]:
    """Firm names in a block of text, in order of appearance."""
    flat = re.sub(r"\s+", " ", _LABELS.sub(" | ", text.replace("\r", "\n")))
    out = []
    for m in _FIRM.finditer(flat):
        name = _MIDDLE_NOISE.split(m.group(1).strip(" .,"))[-1].strip()
        name = _LEADING_NOISE.sub("", name)
        words = name.split()
        if words and words[-1].isupper():
            # An upper-case firm name: drop title-case words that came before
            # it from a wrapped address line ("Mumbai BIGSHARE SERVICES ...").
            while len(words) > 1 and not (words[0].isupper() or words[0] == "&"):
                words.pop(0)
            name = " ".join(words)
        if len(name) < 8 or _NOT_A_PARTY.search(name):
            continue
        if exclude and exclude.lower().split(" limited")[0] in name.lower():
            continue
        out.append(name)
    return list(dict.fromkeys(out))


def cover_text(pages: list[str], n: int = 3) -> str:
    return "\n".join(pages[:n]).replace("\r", "")


def is_registrar(name: str, registrars: Iterable[str] = ()) -> bool:
    core = namelib.core(name)
    return bool(_RTA_HINT.search(name)) or any(core == namelib.core(r) for r in registrars)


def known_names(text: str, candidates: Iterable[str]) -> list[str]:
    """Candidates whose distinctive name appears in the text, in text order."""
    padded = f" {namelib.normalise(text)} "
    hits = []
    for name in candidates:
        core = namelib.core(name)
        if len(core) >= 6 and (i := padded.find(f" {core} ")) >= 0:
            hits.append((i, name))
    return [n for _, n in sorted(hits)]


def intermediaries(cover: str, company: str = "", registrars: Iterable[str] = (),
                   bankers: Iterable[str] = ()) -> tuple[list[str], str | None]:
    """(lead managers, registrar) from the cover page."""
    registrars = list(registrars)
    lm_names, reg_name = [], None
    lm = _LM_HEAD.search(cover)
    if lm and lm.group("both"):
        # Two-column cover: both firms sit in one block after a shared header.
        end = _END_HEAD.search(cover, lm.end())
        found = firms(cover[lm.end(): end.start() if end else lm.end() + 1500], company)
        reg_name = next((f for f in found if is_registrar(f, registrars)), None)
        lm_names = [f for f in found if f != reg_name and not is_registrar(f, registrars)]
    else:
        if lm:
            reg = _REG_HEAD.search(cover, lm.end())
            lm_names = [f for f in firms(cover[lm.end(): reg.start() if reg else lm.end() + 1200],
                                         company) if not is_registrar(f, registrars)]
        reg = _REG_HEAD.search(cover)
        if reg:
            end = _END_HEAD.search(cover, reg.end())
            found = firms(cover[reg.end(): end.start() if end else reg.end() + 800], company)
            reg_name = next((f for f in found if is_registrar(f, registrars)),
                            found[0] if found else None)
    if not lm_names and bankers:
        # Fall back to registered merchant bankers named anywhere on the cover.
        lm_names = [b for b in known_names(cover, bankers) if not is_registrar(b, registrars)]
    return lm_names[:4], reg_name


def market_maker(pages: list[str], company: str = "", limit: int = 150) -> str | None:
    """The market maker named in the General Information chapter."""
    pat = re.compile(r"(?i)MARKET\s+MAKER(?:\s+TO\s+THE\s+(?:ISSUE|OFFER))?\b")
    for text in pages[:limit]:
        text = text.replace("\r", "")
        for m in pat.finditer(text):
            names = firms(text[m.end(): m.end() + 300], company)
            if names:
                return names[0]
    return None


def issue_shares(cover: str) -> dict:
    m = re.search(r"DETAILS\s+OF\s+(?:THE\s+)?(?:ISSUE|OFFER)", cover, re.I)
    block = cover[m.end(): m.end() + 1200] if m else cover[:3000]
    return parse_issue_size(block)


def issue_price(cover: str) -> float | None:
    """A fixed-price issue's price, as printed on the prospectus cover."""
    m = re.search(r"(?is)(?:at\s+an?\s+|issue\s+|offer\s+)price\s+of\s+(?:₹|rs\.?|inr|`)\s*"
                  r"([\d,]+(?:\.\d+)?)\s*(?:/-)?\s*(?:per|each)", cover)
    return float(m.group(1).replace(",", "")) if m else None


_CIN = re.compile(r"\b([UL])(\d{5})([A-Z]{2})(\d{4})(PLC|PTC|OPC|GOI|SGC|FLC|NPL)(\d{6})\b")
# NIC 2008 sections by 2-digit division (the same sections as ISIC Rev. 4).
NIC_SECTIONS = [
    (1, 3, "A", "Agriculture, forestry and fishing"), (5, 9, "B", "Mining and quarrying"),
    (10, 33, "C", "Manufacturing"), (35, 35, "D", "Electricity, gas, steam and air conditioning"),
    (36, 39, "E", "Water supply, sewerage and waste management"), (41, 43, "F", "Construction"),
    (45, 47, "G", "Wholesale and retail trade"), (49, 53, "H", "Transportation and storage"),
    (55, 56, "I", "Accommodation and food service"), (58, 63, "J", "Information and communication"),
    (64, 66, "K", "Financial and insurance activities"), (68, 68, "L", "Real estate activities"),
    (69, 75, "M", "Professional, scientific and technical activities"),
    (77, 82, "N", "Administrative and support services"), (84, 84, "O", "Public administration"),
    (85, 85, "P", "Education"), (86, 88, "Q", "Human health and social work"),
    (90, 93, "R", "Arts, entertainment and recreation"), (94, 96, "S", "Other service activities"),
    (97, 98, "T", "Activities of households"), (99, 99, "U", "Extraterritorial organisations"),
]
_LOT = [re.compile(p, re.I) for p in (
    r"market\s+lot\s+and\s+trading\s+lot\s+for\s+the\s+equity\s+shares?\s+(?:is|will\s+be)\s+"
    r"([\d,]+)",
    r"(?:bid|market)\s+lot\s*:?\s*(?:of\s+)?([\d,]+)\s+equity\s+shares",
    r"lot\s+size\s+(?:is|of)\s+([\d,]+)\s+equity",
    r"lots?\s+of\s+([\d,]+)\s*(?:\([^)]*\))?\s+equity\s+shares\s+each",
)]


def cin(pages: list[str]) -> str | None:
    """The issuer's Corporate Identification Number, from the first pages."""
    m = _CIN.search(" ".join(pages[:4]))
    return m.group(0) if m else None


def nic_section(cin_value: str | None) -> tuple[str | None, str | None]:
    """(NIC code, 'C Manufacturing') from a CIN; the code is the activity at registration."""
    if not cin_value:
        return None, None
    code = cin_value[1:6]
    division = int(code[:2])
    for lo, hi, letter, name in NIC_SECTIONS:
        if lo <= division <= hi:
            return code, f"{letter} {name}"
    return code, None


def lot_size(pages: list[str]) -> int | None:
    """The lot size where the document states it (book-built RHPs often print [●])."""
    for pat in _LOT:
        for text in pages:
            m = pat.search(text.replace("\r", " "))
            if m:
                n = int(m.group(1).replace(",", ""))
                if n >= 50:
                    return n
    return None


_AUDITOR_HEAD = re.compile(r"(?i)statutory\s+(?:and\s+peer\s+review\s+)?auditors?")
# An audit firm is the run of capitalised words right before "Chartered Accountants"
# ("M/s. Singhi & Co LLP, Chartered Accountants", "Gnanoba & Bhat, Chartered ...").
_CA_FIRM = re.compile(r"((?:[A-Z][A-Za-z.'’-]*\.?\s*(?:(?:&|and)\s+)?){1,8}?(?:LLP)?)\s*[,(]?\s*"
                      r"Chartered\s+Accountants?")
_CA_NOISE = re.compile(r"^(?:(?:The|Statutory|Peer|Review|Reviewed|Independent|Practi[cs]ing|"
                       r"Auditors?|Our|Being|Namely|And|"
                       r"M/s\.?|Ms\.?|Messrs\.?|STATUTORY|PEER|REVIEW|AUDITORS?)(?:\s+|$))+")
_FRN = re.compile(r"(?i)(?:firm\s+registration\s+(?:no|number)|F\.?\s*R\.?\s*N\.?)\.?\s*:?\s*"
                  r"([0-9]{5,6}[A-Z]{1,2})")
_PEER = re.compile(r"(?i)peer\s+review\s+(?:certificate\s+)?(?:no|number)\.?\s*:?\s*([0-9]{4,6})")
_NO_CHANGE = re.compile(r"(?i)(?:there\s+(?:has|have)\s+been\s+no|no\s+instance\s+of)\s+"
                        r"(?:instances?\s+of\s+)?change")
_CHANGE_HEAD = re.compile(r"(?i)changes?\s+in\s+(?:the\s+)?(?:statutory\s+)?auditors?")


def auditor(pages: list[str], limit: int = 150) -> dict:
    """Statutory auditor, ICAI FRN, peer-review number, and a change in 3 years."""
    out = {"name": None, "frn": None, "peer_review_no": None, "page": None, "changed_3y": None}
    for i, text in enumerate(pages[:limit]):
        text = text.replace("\r", "")
        for head in _AUDITOR_HEAD.finditer(text):
            window = text[head.end(): head.end() + 900]
            for m in _CA_FIRM.finditer(window.replace("M/s.", " ").replace("M/s", " ")):
                name = _CA_NOISE.sub("", re.sub(r"\s+", " ", m.group(1)).strip(" ,.&"))
                name = re.sub(r"\s+(?:and|&)$", "", name)
                if len(name) >= 4 and name.lower() not in {"company", "associates", "co", "llp"}:
                    break
            else:
                m, name = None, ""
            if name and not out["name"]:
                out["name"] = name
                out["page"] = i + 1
                tail = window[m.end(): m.end() + 600]
                if f := _FRN.search(tail):
                    out["frn"] = f.group(1)
                if p := _PEER.search(tail):
                    out["peer_review_no"] = p.group(1)
        if out["changed_3y"] is None and (c := _CHANGE_HEAD.search(text)):
            section = text[c.end(): c.end() + 700]
            if _NO_CHANGE.search(section):
                out["changed_3y"] = 0
            elif len({re.sub(r"\s+", " ", f.group(1)).lower()
                      for f in _CA_FIRM.finditer(section)}) >= 2:
                out["changed_3y"] = 1
        if out["name"] and out["changed_3y"] is not None:
            break
    return out


def extract(pages: list[str], company: str = "", registrars: Iterable[str] = (),
            bankers: Iterable[str] = ()) -> dict:
    cover = cover_text(pages)
    shares = issue_shares(cover)
    lead, reg = intermediaries(cover, company, registrars, bankers)
    return {"lead_managers": lead, "registrar": reg,
            "market_maker": market_maker(pages, company), "issue_shares": shares["total"],
            "fresh_shares": shares["fresh"], "ofs_shares": shares["ofs"],
            "issue_type": shares["type"], "issue_price": issue_price(cover)}
