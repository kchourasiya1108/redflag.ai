"""Name normalisation and matching for companies and intermediaries.

A false match could tie an innocent firm to a SEBI case, so matching is
deliberately strict: below `MERGE_AT` a pair is never merged automatically.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

MERGE_AT = 0.85     # data guide S08: merge at or above this
REVIEW_AT = 0.82    # 0.82-0.85 goes to a review queue, never a flag

_SUFFIXES = [
    (r"\bprivate\b", "pvt"), (r"\blimited\b", "ltd"), (r"\bcompany\b", "co"),
    (r"\bcorporation\b", "corp"), (r"\b(and|&)\b", "and"),
]
_FORMERLY = re.compile(r"\(?\s*(formerly|earlier|previously)\b.*$", re.I)


def normalise(name: str) -> str:
    """Lower-case, drop 'formerly ...' tails and punctuation, unify suffixes.

    >>> normalise("First Overseas Capital Limited")
    'first overseas capital ltd'
    >>> normalise("FIRST OVERSEAS CAPITAL LTD.")
    'first overseas capital ltd'
    """
    s = _FORMERLY.sub("", name).lower().replace("&", " and ")
    s = re.sub(r"(private)(limited)", r"\1 \2", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    for pattern, repl in _SUFFIXES:
        s = re.sub(pattern, repl, s)
    return re.sub(r"\s+", " ", s).strip()


def core(name: str) -> str:
    """The distinctive part of a normalised name, without legal suffixes."""
    s = re.sub(r"\b(pvt|ltd|llp|co|inc|corp)\b", " ", normalise(name))
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, core(a), core(b)).ratio()


def split_names(text: str) -> list[str]:
    """Split a list of firms, but only after a legal suffix.

    Names like 'D & A Financial Services Pvt. Ltd.' contain '&', so a plain
    split on '&' or 'and' would cut them in half.
    """
    names, start = [], 0
    for sep in re.finditer(r"\s*(?:,|;|&|\band\b)\s*", text, flags=re.I):
        before = text[start:sep.start()].strip()
        if _LEGAL_END.search(before):
            names.append(before)
            start = sep.end()
    names.append(text[start:].strip())
    return [n.strip(" ,;") for n in names if n.strip(" ,;")]


_LEGAL_END = re.compile(r"\b(limited|ltd\.?|llp)$", re.I)
