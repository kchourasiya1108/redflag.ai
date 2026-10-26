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

from pipeline.common import names as namelib
from pipeline.universe import parse_issue_size

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
                            r"Book|BOOK|Running|RUNNING|Managers?|MANAGERS?|TO\s+THE\s+ISSUE|to\s+the\s+Issue|"
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
                          r"our\s+company|(?<!merchant\s)bank|(?<!merchant\s)banker)\b")


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
_CA_NOISE = re.compile(r"^(?:(?:The|THE|Statutory|Peer|Review|Reviewed|REVIEWED|Independent|Practi[cs]ing|"
                       r"Auditors?|Our|OUR|Of|OF|Being|Namely|And|AND|Cum|CUM|Company|COMPANY|"
                       r"M/s\.?|Ms\.?|Messrs\.?|STATUTORY|PEER|REVIEW|AUDITORS?)(?:\s+|$))+")
# A line above the firm's name that belongs to something else: an address, another
# party's company name, or a two-column cover's "..., India".
_NOT_FIRM_LINE = re.compile(r"\d|\b(?:Limited|LIMITED|Ltd|LTD|Private|PRIVATE|Road|ROAD|Marg|MARG|"
                            r"Floor|FLOOR|Nagar|NAGAR)\b|\bIndia\s*$|\bINDIA\s*$")


def _firm_name(raw: str) -> str:
    """The audit firm from a match that may start on an unrelated line."""
    raw = re.split(r"\b(?:Being|being|namely|Namely)\b", raw)[-1]       # "..., being X & Co"
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    while len(lines) > 1 and _NOT_FIRM_LINE.search(lines[0]):
        lines.pop(0)
    text = " ".join(lines)
    return _CA_NOISE.sub("", re.sub(r"\s+", " ", text).strip(" ,.&"))
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
                name = _firm_name(m.group(1))
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


# -- the people behind the issue (watchlist F11, label B) ------------------------------

_PROMOTER_HEAD = re.compile(r"(?i)\b(?:the\s+)?promoters?\s+of\s+(?:our|the)\s+company"
                            r"(?:\s+(?:is|are))?\s*[:\-–]?\s*")
_PROMOTER_END = re.compile(r"(?i)\s(?:DETAILS\s+OF|INITIAL\s+PUBLIC|PUBLIC\s+(?:ISSUE|OFFER)|TYPE\s+FRESH|"
                           r"THE\s+ISSUE|OUR\s+COMPANY|REGISTERED|CORPORATE|CONTACT|For\s+(?:further|details)|"
                           r"\(?Please|ISSUE\s+OF|OFFER\s+FOR)")
_HONORIFIC = re.compile(r"(?i)^(?:mr|ms|mrs|miss|dr|shri|smt|late)\.?\s+")
_PERSON = re.compile(r"^[A-Za-z][A-Za-z.'’-]*(?:\s+[A-Za-z][A-Za-z.'’-]*){1,5}$")
_NOT_PERSON = re.compile(r"(?i)\b(?:limited|ltd|llp|private|pvt|trust|huf|company|corporation|"
                         r"group|holdings?|enterprises?|industries|ventures|associates|foundation)\b")


def _people(text: str) -> list[str]:
    out = []
    for part in re.split(r"\s*(?:,|;|&|\band\b)\s*", text, flags=re.I):
        name = _HONORIFIC.sub("", re.sub(r"\s+", " ", part).strip(" .:-"))
        if _PERSON.match(name) and not _NOT_PERSON.search(name) and len(name) >= 6:
            out.append(name.title() if name.isupper() else name)
    return list(dict.fromkeys(out))


def promoters(pages: list[str]) -> list[str]:
    """Individual promoters named on the cover ("PROMOTERS OF OUR COMPANY: A AND B")."""
    cover = re.sub(r"\s+", " ", cover_text(pages, 3))
    m = _PROMOTER_HEAD.search(cover)
    if not m:
        return []
    tail = cover[m.end(): m.end() + 400]
    end = _PROMOTER_END.search(tail)
    return _people(tail[: end.start() if end else 250])[:12]


_DIN = re.compile(r"(?<![A-Za-z])DIN\s*(?:No\.?)?\s*[:\-]?\s*\d{8}\b")
# Words that sit next to a director's name in board tables but are not part of it.
_NOT_NAME_WORD = re.compile(r"(?i)^(?:director|directors|managing|executive|non|non-executive|independent|"
                            r"chairman|chairperson|chief|financial|officer|whole|time|whole-time|wholetime|"
                            r"designation|residential|address|india|indian|nationality|nationality-indian|"
                            r"business|finance|occupation|name|age|years|term|din|additional|nominee|"
                            r"woman|promoter|cum|and|of|the|our|company|secretary|compliance|directorships?|other|nil|"
                            r"details|past|present|profile|brief|experience|qualification|"
                            r"gujarat|maharashtra|rajasthan|delhi|odisha|karnataka|kerala|bihar|haryana|"
                            r"punjab|telangana|tamil|nadu|bengal|west|uttar|pradesh|madhya|assam|goa|"
                            r"mumbai|ahmedabad|surat|kolkata|chennai|bengaluru|bangalore|hyderabad|pune|"
                            r"jaipur|indore|noida|gurugram|gurgaon|thane)$")


def _name_before(text: str) -> str | None:
    """The person's name ending just before a DIN: 2-4 capitalised words, read backwards."""
    words = re.findall(r"[A-Za-z][A-Za-z.'’-]*|\S", text)
    name = []
    for w in reversed(words):
        if w in "(:,-–" and not name:
            continue
        if not re.match(r"^[A-Z][A-Za-z.'’-]*$", w) or _NOT_NAME_WORD.match(w.strip(".")):
            break
        name.insert(0, w)
        if len(name) == 4:
            break
    name = [w for w in name if not re.match(r"(?i)^(?:mr|ms|mrs|dr|shri|smt)\.?$", w)]
    return " ".join(name) if len(name) >= 2 else None


def directors(pages: list[str], limit: int = 300) -> list[str]:
    """Directors, from "Name (DIN: 12345678)" in the board and management chapters."""
    out = []
    for text in pages[:limit]:
        for m in _DIN.finditer(text):
            name = _name_before(text[max(0, m.start() - 80): m.start()].replace("\n", " "))
            if name:
                out += _people(name)
    counts = {n: out.count(n) for n in out}
    return sorted(counts, key=lambda n: (-counts[n], out.index(n)))[:15]


def person_key(name: str) -> str:
    """One key per person across IPOs: the normalised name, as an id (letters and dashes)."""
    return re.sub(r"[^a-z]+", "-", namelib.normalise(_HONORIFIC.sub("", name.strip()))).strip("-")


def people(pages: list[str]) -> list[dict]:
    """Promoters and directors of the issuer, with their key and role."""
    out, seen = [], set()
    for role, found in (("promoter", promoters(pages)), ("director", directors(pages))):
        for name in found:
            key = person_key(name)
            if len(key) >= 6 and (key, role) not in seen:
                seen.add((key, role))
                out.append({"person_key": key, "name": name, "role": role})
    return out
