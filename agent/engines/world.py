"""Meterless World Model Agent Engine, ported to Python for RedFlag.

An event-sourced, deterministic shared world state: every write is an event
in an append-only log, state is a fold over that log, and views are
projections of the fold. A faithful port of the reference
(github.com/Meterless/Meterless, engines/world-model): stable content-
addressed ids, name normalisation, banded entity resolution (0.85 auto-merge,
[0.82, 0.85) review queue), mandatory provenance, bitemporal relationships
(a superseding edge closes the old one), facts with a conflict policy, the
idempotent ingest pipeline, graph/facts/snapshot views, and an ordered audit
log. `agent/engines/conformance/world_model/` runs the official conformance
suite against this module.

RedFlag's Case File (agent/case_file.py) is built on this store; agents
never hand raw state to each other.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


# -- stable ids (docs/stable-ids.md) -------------------------------------------

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    """Sorted keys, None members of objects dropped: a deterministic diff surface."""
    if isinstance(value, dict):
        items = sorted((k, v) for k, v in value.items() if v is not None)
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical_json(v)
                              for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


_ZERO_WIDTH = re.compile("[​-‍﻿]")


def normalize_name(name: str) -> str:
    """NFKD, strip combining marks and zero-width characters, lowercase, collapse spaces.

    Never abbreviates or stems: "Acme Corp" and "Acme Corporation" stay distinct.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", _ZERO_WIDTH.sub("", stripped).lower()).strip()


def external_id(type_: str, system: str, id_: str) -> str:
    return f"{type_}:{system}:{id_}"


def entity_id_from_name(type_: str, name: str) -> str:
    return "ent_" + sha256_hex(f"{type_}:{normalize_name(name)}")[:16]


def context_id_from_attrs(type_: str, attrs: dict, parent: str | None = None) -> str:
    return "ctx_" + sha256_hex(f"{type_}:{canonical_json(attrs)}:{parent or ''}")[:16]


def relationship_id(from_: str, type_: str, to: str, context: str | None = None,
                    valid_from: str | None = None) -> str:
    return "rel_" + sha256_hex(f"{from_}|{type_}|{to}|{context or ''}|{valid_from or ''}")[:16]


def fact_id(about: str, predicate: str, value: Any, asserted_at: str, source_kind: str,
            source_url: str | None = None) -> str:
    return "fact_" + sha256_hex(f"{about}|{predicate}|{canonical_json(value)}|{asserted_at}|"
                                f"{source_kind}|{source_url or ''}")[:16]


# -- entity resolution ---------------------------------------------------------

def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    longest = max(len(na), len(nb))
    return 1.0 if longest == 0 else 1 - levenshtein(na, nb) / longest


def decide(similarity: float, auto_merge_at: float = 0.85, review_lower: float = 0.82) -> str:
    if similarity >= auto_merge_at:
        return "auto-merge"         # the boundary value merges
    if similarity >= review_lower:
        return "review"             # the boundary value queues
    return "new"


def jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# -- the event store -----------------------------------------------------------

def _iso(at: Any) -> str:
    if isinstance(at, datetime):
        return at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(at)


@dataclass
class EventStore:
    events: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    observations: dict[str, dict] = field(default_factory=dict)
    stories: dict[str, dict] = field(default_factory=dict)
    scores: list[dict] = field(default_factory=list)
    merge_queue: list[dict] = field(default_factory=list)
    entities: dict[str, dict] = field(default_factory=dict)
    contexts: dict[str, dict] = field(default_factory=dict)
    relationships: dict[str, dict] = field(default_factory=dict)
    facts: dict[str, dict] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    def append(self, kind: str, payload: dict, at: str) -> dict:
        event = {"id": f"evt_{len(self.events) + 1}", "seq": len(self.events) + 1, "kind": kind,
                 "payload": copy.deepcopy(payload), "schemaVersion": 1, "at": at}
        self.events.append(event)
        self.fold(event)
        self.persist()
        return event

    def fold(self, event: dict) -> None:
        kind, p = event["kind"], copy.deepcopy(event["payload"])
        if kind == "upsertEntity":
            existing = self.entities.get(p["id"])
            if existing is None:
                self.entities[p["id"]] = p
            else:
                existing["attrs"] = {**existing["attrs"], **p["attrs"]}
                existing["updatedAt"], existing["source"] = p["updatedAt"], p["source"]
                if p.get("name"):
                    if p["name"] not in existing["aliases"]:
                        existing["aliases"].append(p["name"])
                    existing["name"] = p["name"]
        elif kind == "upsertContext":
            existing = self.contexts.get(p["id"])
            if existing is None:
                self.contexts[p["id"]] = p
            else:
                existing["attrs"] = {**existing["attrs"], **p["attrs"]}
                existing["updatedAt"] = p["updatedAt"]
        elif kind == "relate":
            # A new open edge with the same (from, type, context) but another
            # target closes the previous open edge.
            for other in self.relationships.values():
                if (other["from"] == p["from"] and other["type"] == p["type"]
                        and (other.get("context") or "") == (p.get("context") or "")
                        and other["to"] != p["to"] and not other.get("validTo")
                        and not other.get("supersededBy")):
                    other["validTo"] = p.get("validFrom") or p["createdAt"]
                    other["supersededBy"] = p["id"]
            self.relationships.setdefault(p["id"], p)
        elif kind == "assertFact":
            self.facts.setdefault(p["id"], p)
        elif kind == "supersede":
            # The conflict policy's verdict, logged as an event so a reload or
            # point-in-time fold rebuilds it (the reference mutates in memory).
            for loser in p["losers"]:
                if loser in self.facts:
                    self.facts[loser]["supersededBy"] = p["winner"]
            if p["about"] in self.entities:
                self.entities[p["about"]]["attrs"][p["predicate"]] = p["value"]
                self.entities[p["about"]]["updatedAt"] = p["at"]
        elif kind == "alias":
            self.aliases[p["aliasId"]] = p["primaryId"]

    def fold_until(self, at: str) -> "EventStore":
        scratch = EventStore()
        for e in self.events:
            if e["at"] <= at:
                scratch.fold(e)
        return scratch

    def resolve_alias(self, id_: str) -> str:
        seen, cur = set(), id_
        while cur in self.aliases and cur not in seen:
            seen.add(cur)
            cur = self.aliases[cur]
        return cur

    def log_audit(self, kind: str, actor: str, summary: str, event_id: str | None = None,
                  run_id: str | None = None, at: str | None = None) -> None:
        self.audit.append({"seq": len(self.audit) + 1, "at": at or "1970-01-01T00:00:00.000Z",
                           "kind": kind, "actor": actor, "summary": summary,
                           "eventId": event_id, "runId": run_id})
        self.persist()

    def persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"events": self.events, "audit": self.audit,
                "observations": self.observations, "stories": self.stories,
                "scores": self.scores, "mergeQueue": self.merge_queue}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str))
        tmp.replace(self.path)

    @classmethod
    def load(cls, path: Path) -> "EventStore":
        store = cls()
        if path.exists():
            saved = json.loads(path.read_text())
            store.audit = saved.get("audit", [])
            store.observations = saved.get("observations", {})
            store.stories = saved.get("stories", {})
            store.scores = saved.get("scores", [])
            store.merge_queue = saved.get("mergeQueue", [])
            for e in saved.get("events", []):
                store.events.append(e)
                store.fold(e)
        store.path = path
        return store


def world_state_shape(store: EventStore) -> dict:
    """The idempotency diff surface: everything derived from observations, sorted."""
    def by_id(d):
        return [d[k] for k in sorted(d)]
    return {"entities": by_id(store.entities), "contexts": by_id(store.contexts),
            "relationships": by_id(store.relationships), "facts": by_id(store.facts),
            "observations": [store.observations[k] for k in sorted(store.observations)],
            "stories": by_id(store.stories)}


# -- the engine ----------------------------------------------------------------

class ProvenanceError(ValueError):
    pass


class WorldModel:
    def __init__(self, namespace: str, *, storage: str = "memory", directory: Path | None = None,
                 clock: Callable[[], datetime] | None = None, conflict_policy: str = "most-recent-wins",
                 source_priority: list[str] | None = None, auto_merge_at: float = 0.85,
                 review_lower: float = 0.82):
        if not namespace:
            raise ValueError("WorldModel: namespace is required")
        self.namespace = namespace
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.conflict_policy = conflict_policy
        self.source_priority = source_priority or []
        self.auto_merge_at, self.review_lower = auto_merge_at, review_lower
        if storage == "local":
            self.store = EventStore.load(Path(directory or ".") / ".world" / f"{namespace}.json")
        elif storage == "memory":
            self.store = EventStore()
        else:
            raise ValueError(f"WorldModel: unknown storage {storage!r}")

    # -- writes ------------------------------------------------------------

    @staticmethod
    def _provenance(source: dict | None) -> dict:
        if not source or not source.get("kind") or source.get("at") is None:
            raise ProvenanceError("World Model: every write carries provenance (kind + at). "
                                  "No provenance, no write.")
        return {**source, "at": _iso(source["at"])}

    def upsert_entity(self, *, type: str, source: dict, external_key: dict | None = None,
                      name: str | None = None, attrs: dict | None = None,
                      state: str = "active") -> dict:
        src = self._provenance(source)
        name = name or (attrs or {}).get("name")
        if external_key:
            id_ = external_id(type, external_key["system"], external_key["id"])
        elif name:
            id_ = entity_id_from_name(type, name)
        else:
            raise ValueError("World Model: name-keyed entities need a name (or an externalKey)")
        if not external_key and name:
            for other in list(self.store.entities.values()):
                if other["type"] != type or other["id"] == id_ or not other.get("name"):
                    continue
                sim = name_similarity(name, other["name"])
                verdict = decide(sim, self.auto_merge_at, self.review_lower)
                if verdict == "auto-merge":
                    self.store.append("alias", {"aliasId": id_, "primaryId": other["id"],
                                                "mergedAt": src["at"], "mergedBy": "resolver",
                                                "reason": f"similarity {sim:.3f}"}, src["at"])
                    self.store.log_audit("auto-merge", "resolver",
                                         f"{name} -> {other['name']} ({sim:.3f})", at=src["at"])
                    return self._fold_entity(other["id"], type, attrs, src, name, state)
                if verdict == "review" and not any(
                        {p["leftId"], p["rightId"]} == {id_, other["id"]}
                        for p in self.store.merge_queue):
                    self.store.merge_queue.append({
                        "id": f"merge_{len(self.store.merge_queue) + 1}", "leftId": id_,
                        "rightId": other["id"], "leftName": name, "rightName": other["name"],
                        "similarity": sim, "reason": "name similarity in review band [0.82, 0.85)",
                        "status": "pending", "proposedAt": src["at"]})
                    self.store.log_audit("merge-proposed", "resolver",
                                         f"{name} ~ {other['name']} ({sim:.3f}) queued", at=src["at"])
        return self._fold_entity(id_, type, attrs, src, name, state)

    def _fold_entity(self, id_: str, type_: str, attrs: dict | None, src: dict,
                     name: str | None, state: str = "active") -> dict:
        resolved = self.store.resolve_alias(id_)
        entity = {"type": type_, "id": resolved, "attrs": dict(attrs or {}), "schemaVersion": 1,
                  "source": src, "createdAt": src["at"], "updatedAt": src["at"], "name": name,
                  "aliases": [name] if name else [], "state": state}
        event = self.store.append("upsertEntity", entity, src["at"])
        self.store.log_audit("upsertEntity", src.get("by") or "system", f"{resolved} ({type_})",
                             event["id"], src.get("runId"), src["at"])
        return self.store.entities[resolved]

    def upsert_context(self, *, type: str, source: dict, external_key: dict | None = None,
                       attrs: dict | None = None, parent: str | None = None) -> dict:
        src = self._provenance(source)
        id_ = (external_id(type, external_key["system"], external_key["id"]) if external_key
               else context_id_from_attrs(type, attrs or {}, parent))
        ctx = {"type": type, "id": id_, "attrs": dict(attrs or {}), "parent": parent,
               "schemaVersion": 1, "source": src, "createdAt": src["at"], "updatedAt": src["at"]}
        event = self.store.append("upsertContext", ctx, src["at"])
        self.store.log_audit("upsertContext", src.get("by") or "system", f"{id_} ({type})",
                             event["id"], src.get("runId"), src["at"])
        return self.store.contexts[id_]

    def relate(self, *, from_: str, type: str, to: str, source: dict, context: str | None = None,
               attrs: dict | None = None, valid_from: str | None = None,
               valid_to: str | None = None) -> dict:
        src = self._provenance(source)
        frm, dst = self.store.resolve_alias(from_), self.store.resolve_alias(to)
        id_ = relationship_id(frm, type, dst, context, valid_from)
        if id_ in self.store.relationships:
            return self.store.relationships[id_]       # content-addressed no-op
        rel = {"id": id_, "from": frm, "type": type, "to": dst, "context": context,
               "attrs": attrs, "validFrom": valid_from or src["at"], "validTo": valid_to,
               "schemaVersion": 1, "source": src, "createdAt": src["at"]}
        event = self.store.append("relate", rel, src["at"])
        self.store.log_audit("relate", src.get("by") or "system", f"{frm} -[{type}]-> {dst}",
                             event["id"], src.get("runId"), src["at"])
        return self.store.relationships[id_]

    def assert_fact(self, *, about: str, predicate: str, value: Any, source: dict,
                    confidence: float = 0.5, context: str | None = None) -> dict:
        src = self._provenance(source)
        subject = self.store.resolve_alias(about)
        id_ = fact_id(subject, predicate, value, src["at"], src["kind"], src.get("url"))
        if id_ in self.store.facts:
            return self.store.facts[id_]
        fact = {"id": id_, "about": subject, "predicate": predicate, "value": value,
                "confidence": confidence, "context": context, "source": src,
                "assertedAt": src["at"]}
        rivals = [f for f in self.store.facts.values()
                  if f["about"] == subject and f["predicate"] == predicate
                  and not f.get("supersededBy")]
        winner = fact
        for rival in rivals:
            if self._rival_wins(rival, winner):
                winner = rival
        event = self.store.append("assertFact", fact, src["at"])
        if subject not in self.store.entities:
            inferred = subject.split(":")[0] if ":" in subject else "entity"
            self.store.append("upsertEntity", {
                "type": inferred, "id": subject, "attrs": {}, "schemaVersion": 1, "source": src,
                "createdAt": src["at"], "updatedAt": src["at"], "aliases": [],
                "state": "emerging"}, src["at"])
        self.store.append("supersede", {
            "about": subject, "predicate": predicate, "value": winner["value"],
            "winner": winner["id"], "losers": [f["id"] for f in [fact, *rivals]
                                               if f["id"] != winner["id"]],
            "at": src["at"]}, src["at"])
        self.store.log_audit("assertFact", src.get("by") or "system",
                             f"{subject}.{predicate} = {json.dumps(winner['value'], default=str)} "
                             f"({self.conflict_policy})", event["id"], src.get("runId"), src["at"])
        self.store.persist()
        return self.store.facts[id_]

    def _rival_wins(self, rival: dict, winner: dict) -> bool:
        if self.conflict_policy == "highest-confidence-wins":
            return rival["confidence"] > winner["confidence"]
        if self.conflict_policy == "source-priority":
            rank = lambda kind: (self.source_priority.index(kind) if kind in self.source_priority
                                 else len(self.source_priority))
            return rank(rival["source"]["kind"]) < rank(winner["source"]["kind"])
        return rival["assertedAt"] > winner["assertedAt"]      # most-recent-wins

    # -- ingest (AGENTS.md section 4.1) -----------------------------------

    def ingest(self, observations: list[dict]) -> dict:
        """Idempotent batch ingest: the same batch twice yields changed=False."""
        store = self.store
        run_id = "run_" + sha256_hex(canonical_json([o["externalId"] for o in observations]))[:12]
        before = canonical_json(world_state_shape(store))
        events_before = len(store.events)
        latest = max((o["at"] for o in observations), default="")
        for rec in store.observations.values():          # 1. reconcile stale
            rec["stale"] = bool(latest) and rec["at"] < _days_before(latest, 30)
        fresh, skipped = [], 0
        for obs in observations:                          # 2. classify unchanged
            checksum = sha256_hex(canonical_json({"externalId": obs["externalId"],
                                                  "title": obs.get("title"),
                                                  "text": obs.get("text") or "", "at": obs["at"]}))
            old = store.observations.get(obs["externalId"])
            if old and old["checksum"] == checksum:
                skipped += 1
            else:
                fresh.append((obs, checksum))
        for obs, checksum in fresh:                       # 3. persist aggregates + edges
            src = self._provenance({**(obs.get("source") or {}), "at": obs["at"]})
            type_ = obs.get("type") or "item"
            entity = self._fold_entity(external_id(type_, "ingest", obs["externalId"]), type_,
                                       {"title": obs.get("title"), "text": obs.get("text")},
                                       src, obs.get("title"))
            mentioned = []
            for m in obs.get("entities") or []:
                mid = entity_id_from_name(m["type"], m["name"])
                self._fold_entity(mid, m["type"], None, src, m["name"])
                mentioned.append(store.resolve_alias(mid))
                rid = relationship_id(entity["id"], "mentions", store.resolve_alias(mid))
                if rid not in store.relationships:
                    store.append("relate", {"id": rid, "from": entity["id"], "type": "mentions",
                                            "to": store.resolve_alias(mid), "validFrom": src["at"],
                                            "schemaVersion": 1, "source": src,
                                            "createdAt": src["at"]}, src["at"])
            store.observations[obs["externalId"]] = {
                "id": "obs_" + sha256_hex(obs["externalId"])[:12], "externalId": obs["externalId"],
                "entityId": entity["id"], "checksum": checksum, "at": obs["at"],
                "entityIds": sorted(mentioned), "stale": False}
        self._group_stories()                             # 2/4/8. cluster, score, patch
        for entity in store.entities.values():            # 9. rebuild aggregates
            hits = [o for o in store.observations.values() if entity["id"] in o.get("entityIds", [])]
            if hits:
                times = sorted(o["at"] for o in hits)
                entity["aggregates"] = {
                    "mentionCount": len(hits), "firstSeenAt": times[0], "lastSeenAt": times[-1],
                    "linkedStoryIds": sorted({o["storyId"] for o in hits if o.get("storyId")})}
        store.persist()
        report = {"runId": run_id, "processed": len(fresh), "skippedUnchanged": skipped,
                  "clusters": len(store.stories), "eventsAppended": len(store.events) - events_before,
                  "changed": before != canonical_json(world_state_shape(store))}
        store.log_audit("ingest", "pipeline", f"run {run_id}: {len(fresh)} processed, "
                        f"{skipped} skipped", run_id=run_id,
                        at=observations[0]["at"] if observations else None)
        return report

    def _group_stories(self) -> None:
        store = self.store
        records = [store.observations[k] for k in sorted(store.observations)]
        used: set[str] = set()
        store.stories = {}
        for seed in records:
            if seed["externalId"] in used:
                continue
            group = [seed]
            used.add(seed["externalId"])
            for other in records:
                if other["externalId"] not in used and jaccard(seed["entityIds"],
                                                               other["entityIds"]) >= 0.5:
                    group.append(other)
                    used.add(other["externalId"])
            members = sorted(g["externalId"] for g in group)
            story_id = "story_" + sha256_hex(canonical_json(members))[:12]
            times = sorted(g["at"] for g in group)
            store.stories[story_id] = {
                "id": story_id, "title": group[0]["externalId"], "memberIds": members,
                "entityIds": sorted({e for g in group for e in g["entityIds"]}),
                "firstAt": times[0], "lastAt": times[-1],
                "milestones": [{"kind": "multi-source", "at": times[1]}] if len(group) >= 2 else []}
            for g in group:
                g["storyId"] = story_id

    # -- reads -------------------------------------------------------------

    def view(self, name: str):
        views = {"graph": GraphView, "facts": FactsView, "snapshot": SnapshotView}
        if name not in views:
            raise ValueError(f'World Model: unknown view "{name}"')
        return views[name](self.store)

    def merge_queue(self) -> list[dict]:
        return self.store.merge_queue

    def approve_merge(self, proposal_id: str, by: str) -> None:
        p = self._pending(proposal_id)
        at = _iso(self.clock())
        p.update(status="approved", resolvedAt=at, resolvedBy=by)
        self.store.append("alias", {"aliasId": p["leftId"], "primaryId": p["rightId"],
                                    "mergedAt": at, "mergedBy": by, "reason": p["reason"]}, at)
        self.store.log_audit("merge-approved", by, f"{p['leftName']} -> {p['rightName']}", at=at)

    def reject_merge(self, proposal_id: str, by: str) -> None:
        p = self._pending(proposal_id)
        p.update(status="rejected", resolvedAt=_iso(self.clock()), resolvedBy=by)
        self.store.log_audit("merge-rejected", by,
                             f"{p['leftName']} stays separate from {p['rightName']}")

    def _pending(self, proposal_id: str) -> dict:
        for p in self.store.merge_queue:
            if p["id"] == proposal_id and p["status"] == "pending":
                return p
        raise ValueError(f"World Model: no pending merge proposal {proposal_id}")

    def audit(self) -> list[dict]:
        return self.store.audit

    def events(self) -> list[dict]:
        return self.store.events

    def state_shape(self) -> dict:
        return world_state_shape(self.store)

    def state_shape_canonical(self) -> str:
        return canonical_json(world_state_shape(self.store))


def _valid_at(rel: dict, at: str) -> bool:
    if rel.get("validFrom") and at < rel["validFrom"]:
        return False
    return not (rel.get("validTo") and at >= rel["validTo"])


class GraphView:
    def __init__(self, store: EventStore):
        self.store = store

    def entity(self, id_: str) -> dict | None:
        return self.store.entities.get(self.store.resolve_alias(id_))

    def neighbors_of(self, id_: str, *, via: str | None = None, in_context: str | None = None,
                     at: str | None = None) -> list[dict]:
        rid = self.store.resolve_alias(id_)
        out = []
        for rel in self.store.relationships.values():
            if via and rel["type"] != via:
                continue
            if in_context and rel.get("context") != in_context:
                continue
            if at and not _valid_at(rel, at):
                continue
            if not at and rel.get("validTo"):
                continue                     # closed edges leave "now" queries
            other = rel["to"] if rel["from"] == rid else rel["from"] if rel["to"] == rid else None
            if other and other in self.store.entities:
                out.append(self.store.entities[other])
        return sorted(out, key=lambda e: e["id"])

    def incoming(self, id_: str, *, via: str | None = None) -> list[dict]:
        rid = self.store.resolve_alias(id_)
        out = [self.store.entities[r["from"]] for r in self.store.relationships.values()
               if r["to"] == rid and (not via or r["type"] == via) and not r.get("validTo")
               and r["from"] in self.store.entities]
        return sorted(out, key=lambda e: e["id"])

    def traverse(self, start: str, *, via: str, direction: str = "descendants",
                 max_depth: int = 5) -> list[dict]:
        out, visited = [], set()
        frontier = [self.store.resolve_alias(start)]
        for _ in range(max_depth):
            nxt = []
            for node in frontier:
                for rel in self.store.relationships.values():
                    if rel["type"] != via or rel.get("validTo"):
                        continue
                    target = (rel["to"] if rel["from"] == node else None) if direction == "ancestors" \
                        else (rel["from"] if rel["to"] == node else None)
                    if target and target not in visited:
                        visited.add(target)
                        if target in self.store.entities:
                            out.append(self.store.entities[target])
                        nxt.append(target)
            frontier = nxt
            if not frontier:
                break
        return out

    def query(self, *, type: str | None = None, where: Callable[[dict], bool] | None = None) -> list[dict]:
        return sorted((e for e in self.store.entities.values()
                       if (not type or e["type"] == type) and (not where or where(e))),
                      key=lambda e: e["id"])


class FactsView:
    def __init__(self, store: EventStore):
        self.store = store

    def about(self, id_: str) -> list[dict]:
        rid = self.store.resolve_alias(id_)
        return sorted((f for f in self.store.facts.values() if f["about"] == rid),
                      key=lambda f: f["assertedAt"])


class SnapshotView:
    def __init__(self, store: EventStore):
        self.store = store

    def entity_at(self, id_: str, at: str) -> dict | None:
        state = self.store.fold_until(f"{at}T23:59:59.999Z" if len(at) == 10 else at)
        return state.entities.get(state.resolve_alias(id_))


def _days_before(iso: str, days: int) -> str:
    when = datetime.fromisoformat(iso.replace("Z", "+00:00")) - timedelta(days=days)
    return _iso(when)
