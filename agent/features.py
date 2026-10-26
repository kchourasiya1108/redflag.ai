"""compute_features: the one feature function shared by the backtest and the agents.

Everything is read through the Data Port as of the bid date. The only thing
that differs between a backtest and a live run is where RHP facts come from
(`facts`); banker features need no facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median

from agent.engines.world import decide, name_similarity
from agent.specialists import ArchiveFacts, FactSource, rhp_features
from agent.tools_zetaris import DataPort

SHRINK_K = 5            # pseudo-IPOs pulling a thin track record to the overall rate
LOW_HISTORY_N = 5       # fewer known outcomes than this is a "thin history"


def adverse_as_of(row: dict, as_of: date) -> bool:
    """A past IPO's outcome as it looked on as_of.

    The stored `adverse` column can flip later, when a SEBI order is issued
    after as_of, so it is never read here.
    """
    sebi = bool(row["sebi_action"]) and row["sebi_action_date"] is not None \
        and row["sebi_action_date"] < as_of
    return bool(row["crashed"]) or bool(row["suspended"]) or sebi


def banker_features(data: DataPort, ipo_id: str, as_of: date) -> dict[str, float | None]:
    everyone = data.run("outcomes_known_before", as_of=as_of).rows
    base_rate = (sum(adverse_as_of(r, as_of) for r in everyone) / len(everyone)
                 if everyone else None)
    bankers = data.run("ipo_bankers", ipo_id=ipo_id).rows
    worst = None
    for b in bankers:
        history = [r for r in data.run("banker_track_record", banker_id=b["banker_id"],
                                       as_of=as_of).rows if r["ipo_id"] != ipo_id]
        n = len(history)
        bad = sum(adverse_as_of(r, as_of) for r in history)
        rate = ((bad + SHRINK_K * base_rate) / (n + SHRINK_K)
                if base_rate is not None else None)
        returns = [r["ret_6m"] for r in history if r["ret_6m"] is not None]
        stats = {"f_banker_crash": rate, "f_banker_n": float(n),
                 "f_banker_low_history": float(n < LOW_HISTORY_N),
                 "f_banker_median_ret": median(returns) if returns else None,
                 "banker_id": b["banker_id"]}
        # With several lead managers, the riskiest one speaks for the issue.
        if worst is None or (rate or 0) > (worst["f_banker_crash"] or 0):
            worst = stats
    if worst is None:
        return {"f_banker_crash": None, "f_banker_n": None,
                "f_banker_low_history": None, "f_banker_median_ret": None,
                "base_rate": base_rate}
    worst.pop("banker_id")
    return worst | {"base_rate": base_rate}


@dataclass
class Named:
    """Someone involved in the IPO, as the watchlist sees them."""
    role: str                   # banker | auditor | market_maker | registrar | company | person
    name: str
    entity_id: str | None = None    # archive id (ent:banker:<id>, ent:company:<ipo_id>)
    source: str = ""


@dataclass
class Link:
    """One path from a named party to a SEBI order dated before as_of."""
    named: Named
    order: dict
    hop: int                    # 1 = named directly; 2 = through one other entity
    similarity: float
    via: str = ""               # the intermediate entity on a 2-hop path
    status: str = "pending"     # confirmed | pending (not reviewed yet) | near_match

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


def archive_parties(data: DataPort, ipo_id: str, company: str) -> list[Named]:
    """Everyone the archive names for this IPO: the issuer, lead managers,
    auditor, market maker and registrar (all printed in the RHP)."""
    named = [Named("company", company, f"ent:company:{ipo_id}", "archive:ipos")]
    named += [Named("banker", b["canonical_name"], f"ent:banker:{b['banker_id']}",
                    "archive:ipo_banker") for b in data.run("ipo_bankers", ipo_id=ipo_id).rows]
    named += [Named(p["role"], p["canonical_name"], None, f"archive:{p['role']}")
              for p in data.run("ipo_parties", ipo_id=ipo_id).rows]
    named += [Named(p["role"], p["name"], f"ent:person:{p['person_key']}", "archive:ipo_people")
              for p in data.run("ipo_people", ipo_id=ipo_id).rows]
    return named


def watchlist_links(data: DataPort, ipo_id: str, as_of: date,
                    named: list[Named]) -> list[Link]:
    """Paths from `named` to SEBI orders dated before as_of, within 2 hops.

    Hop 1: the party itself is named in an order (same archive id, or a name
    match at similarity >= 0.85; 0.82-0.85 is a near match, never a flag).
    Hop 2: a lead manager's earlier issue (opened before as_of) was named.
    A link is `confirmed` only when that order row was reviewed as a match
    (reviewed = 1, by Nemotron or a person); rows marked -1 are dropped and
    unreviewed rows stay pending.
    """
    orders = [o for o in data.run("sebi_named_before", as_of=as_of).rows if o["reviewed"] != -1]

    def status(order: dict, verdict: str) -> str:
        if verdict == "review":
            return "near_match"
        return "confirmed" if order["reviewed"] == 1 else "pending"

    links: dict[tuple, Link] = {}

    def add(link: Link) -> None:        # one link per (party, order, hop)
        links.setdefault((link.named.role, link.named.name, link.order["order_id"], link.hop), link)

    for n in named:
        for o in orders:
            if n.entity_id and o["entity_id"]:
                if n.entity_id == o["entity_id"]:   # both archived: only the ids decide
                    add(Link(n, o, 1, 1.0, status=status(o, "auto-merge")))
                continue
            if not _roles_compatible(n.role, o["entity_role"]):
                continue
            if not _could_match(n.name, o["entity_name"]):
                continue
            sim = name_similarity(n.name, o["entity_name"])
            if n.role in PERSON_ROLES and sim < PERSON_MATCH_AT:
                continue
            verdict = decide(sim)
            if verdict != "new":
                add(Link(n, o, 1, round(sim, 3), status=status(o, verdict)))
    by_company = {}
    for o in orders:
        if o["entity_role"] == "company" and o["entity_id"]:
            by_company.setdefault(o["entity_id"], []).append(o)
    for n in named:
        if n.role in PERSON_ROLES and n.entity_id:
            # A promoter or director of an earlier issue whose company was named in an order.
            key = n.entity_id.rsplit(":", 1)[1]
            for past in data.run("person_past_ipos", person_key=key, as_of=as_of).rows:
                if past["ipo_id"] == ipo_id:
                    continue
                for o in by_company.get(f"ent:company:{past['ipo_id']}", []):
                    add(Link(n, o, 2, 1.0, via=f"{past['company']} ({past['ipo_id']}, as {past['role']})",
                             status=status(o, "auto-merge")))
            continue
        if n.role != "banker" or not n.entity_id:
            continue
        banker_id = int(n.entity_id.rsplit(":", 1)[1])
        for past in data.run("banker_past_ipos", banker_id=banker_id, as_of=as_of).rows:
            if past["ipo_id"] == ipo_id:
                continue
            for o in by_company.get(f"ent:company:{past['ipo_id']}", []):
                add(Link(n, o, 2, 1.0, via=f"{past['company']} ({past['ipo_id']})",
                         status=status(o, "auto-merge")))
    return list(links.values())


# An order's banker row can only be a banker; its company row only an issuer; a
# promoter or director only a person named in the order. Other firms may appear
# under any role.
PERSON_ROLES = {"promoter", "director", "person"}
PERSON_MATCH_AT = 0.95      # people share names far more than firms do
_STRICT_ROLES = {"banker": {"banker"}, "company": {"company"},
                 "promoter": {"person"}, "director": {"person"}}


def _roles_compatible(party_role: str, order_role: str) -> bool:
    allowed = _STRICT_ROLES.get(party_role)
    return allowed is None or order_role in allowed


def _could_match(a: str, b: str, lower: float = 0.82) -> bool:
    """Cheap pre-filter: names whose lengths differ by more than the review
    band allows can never reach similarity 0.82."""
    la, lb = len(a.strip()), len(b.strip())
    return min(la, lb) >= lower * max(la, lb, 1)


def watchlist_features(links: list[Link]) -> dict[str, float]:
    confirmed = [l for l in links if l.confirmed]
    return {"f_watchlist_hit": float(bool(confirmed)),
            "f_watchlist_hop1": float(sum(l.hop == 1 for l in confirmed)),
            "f_watchlist_pending": float(sum(not l.confirmed for l in links))}


def auditor_features(data: DataPort, ipo_id: str, as_of: date,
                     base_rate: float | None) -> dict[str, float | None]:
    """F7 inputs: an auditor change in the 3 years before the RHP, and the auditor's
    own track record as of the bid date (shrunk like the banker's)."""
    rows = data.run("ipo_auditor", ipo_id=ipo_id).rows
    if not rows:
        return {"f_auditor_changed": None, "f_auditor_crash": None, "f_auditor_n": None}
    a = rows[0]
    history = [r for r in data.run("auditor_track_record", auditor_id=a["auditor_id"],
                                   as_of=as_of).rows if r["ipo_id"] != ipo_id]
    bad = sum(adverse_as_of(r, as_of) for r in history)
    rate = ((bad + SHRINK_K * base_rate) / (len(history) + SHRINK_K)
            if base_rate is not None else None)
    return {"f_auditor_changed": None if a["changed_3y"] is None else float(a["changed_3y"]),
            "f_auditor_crash": rate, "f_auditor_n": float(len(history))}


def compute_features(ipo_id: str, as_of: date, data: DataPort,
                     facts: FactSource | None = None, *,
                     named: list[Named] | None = None) -> dict[str, float | None]:
    """The single feature function for the backtest and the live run.

    `named` defaults to the archive's parties for the IPO; a live run passes
    the same list plus the people its Extractor read from the RHP.
    """
    if data.as_of != as_of:
        raise ValueError("the Data Port must be bound to the same as_of")
    features = banker_features(data, ipo_id, as_of)
    if named is None:
        meta = data.run("ipo_meta", ipo_id=ipo_id).rows
        named = archive_parties(data, ipo_id, meta[0]["company"] if meta else ipo_id)
    features |= watchlist_features(watchlist_links(data, ipo_id, as_of, named))
    features |= auditor_features(data, ipo_id, as_of, features.get("base_rate"))
    # RHP features: verified restated numbers only. The backtest reads past
    # extractions from Zetaris (ArchiveFacts); a live run passes CaseFileFacts.
    features |= rhp_features(facts if facts is not None else ArchiveFacts(data, ipo_id))
    return features
