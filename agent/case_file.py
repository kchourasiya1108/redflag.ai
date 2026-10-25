"""The Case File: one run's shared state, on the Meterless World Model engine.

Agents write through this API and read through `context(intent)`; they never
hand raw state to each other. Underneath, every write is a World Model event
with provenance: the agent that wrote it, the run, and an evidence reference
(`rhp:p<page>`, `zq:<sha>`, `sebi:<order_id>`).

Mapping onto the engine:
- the IPO is the externally keyed entity `company:ipo:<ipo_id>`;
- people and firms named in the RHP are name-keyed entities, so near-duplicate
  spellings auto-merge (similarity >= 0.85) or join the merge queue;
- relationships (lead_manager_of, director_of, named_in_order, ...) are edges;
- facts, verification results, flags, questions, "not checked" items and the
  score are facts about the company, one predicate each (`fact:pat:FY24`,
  `flag:F1`, ...). Re-asserting a predicate supersedes the old value.

All writes in a run use the run's start time, so replaying a run (or
resuming after a crash) reproduces the same ids and nothing is duplicated.
The store persists to `logs/<run_id>/world.json` and `case_file()` renders
`output/<run_id>/case_file.json`.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from agent.engines.world import WorldModel, external_id

SEVERITY = {"HIGH": 3, "ELEVATED": 2, "LOW": 1}


def _kind(ref: str) -> str:
    return ref.split(":", 1)[0] if ":" in ref else "agent"


@dataclass
class ContextItem:
    kind: str           # entity | fact | flag | question
    data: dict
    source: str
    reason: str
    relevance: float


@dataclass
class ContextPayload:
    """Structured, not pre-rendered: each agent formats it for its own prompt."""
    intent: str
    strategy: str
    items: list[ContextItem] = field(default_factory=list)
    truncated: bool = False

    def as_prompt_block(self) -> str:
        lines = [f"# Case context for: {self.intent}"]
        for it in self.items:
            lines.append(f"- [{it.kind}] {json.dumps(it.data, default=str)} (source {it.source})")
        return "\n".join(lines)


class CaseFile:
    def __init__(self, run_id: str, ipo_id: str, as_of: date, *, log_dir: Path | None = None,
                 started_at: datetime | None = None, meta: dict | None = None):
        self.run_id, self.ipo_id, self.as_of = run_id, ipo_id, as_of
        self.started_at = started_at or datetime.now(timezone.utc)
        self.world = WorldModel(f"case-{run_id}", storage="local" if log_dir else "memory",
                                directory=log_dir, clock=lambda: self.started_at)
        self.company = external_id("company", "ipo", ipo_id)
        self.meta = {"run_id": run_id, "ipo_id": ipo_id, "as_of": as_of.isoformat(),
                     "depth": None, "depth_history": [], **(meta or {})}
        if self.company not in self.world.store.entities:
            self.world.upsert_entity(type="company", external_key={"system": "ipo", "id": ipo_id},
                                     attrs={"ipo_id": ipo_id}, source=self._src("agent:Intake", "Intake"))

    @classmethod
    def resume(cls, run_id: str, ipo_id: str, as_of: date, log_dir: Path) -> "CaseFile":
        """Reopen a run's store after a crash; replayed writes are no-ops."""
        saved = log_dir / ".world" / f"case-{run_id}.json"
        started = None
        if saved.exists():
            events = json.loads(saved.read_text()).get("events", [])
            if events:
                started = datetime.fromisoformat(events[0]["at"].replace("Z", "+00:00"))
        return cls(run_id, ipo_id, as_of, log_dir=log_dir, started_at=started)

    def _src(self, ref: str, agent: str) -> dict:
        return {"kind": _kind(ref), "at": self.started_at, "by": agent, "runId": self.run_id,
                "ref": ref}

    # -- writes ------------------------------------------------------------

    def add_entity(self, type_: str, name: str, *, source: str, agent: str,
                   attrs: dict | None = None, key: tuple[str, str] | None = None) -> str:
        """A person or firm named in the case; returns its (possibly merged) id."""
        ent = self.world.upsert_entity(
            type=type_, name=name, attrs=attrs, source=self._src(source, agent),
            external_key={"system": key[0], "id": key[1]} if key else None)
        return ent["id"]

    def link(self, src_id: str, type_: str, dst_id: str, *, source: str, agent: str,
             valid_from: str | None = None, attrs: dict | None = None) -> dict:
        return self.world.relate(from_=src_id, type=type_, to=dst_id, attrs=attrs,
                                 valid_from=valid_from, source=self._src(source, agent))

    def _assert(self, predicate: str, value: dict, *, source: str, agent: str,
                confidence: float = 0.5) -> dict:
        return self.world.assert_fact(about=self.company, predicate=predicate, value=value,
                                      confidence=confidence, source=self._src(source, agent))

    def add_fact(self, field_: str, fy: str, value: float | None, *, unit: str, page: int | None,
                 verified: bool, method: str, agent: str, source: str | None = None) -> dict:
        return self._assert(f"fact:{field_}:{fy}", {
            "field": field_, "fy": fy, "value": value, "unit": unit, "page": page,
            "verified": verified, "method": method},
            source=source or (f"rhp:p{page}" if page else "agent:" + agent), agent=agent)

    def set_verification(self, statement: str, fy: str, *, passed: bool, attempts: int,
                         agent: str, detail: str = "") -> dict:
        return self._assert(f"verification:{statement}:{fy}", {
            "statement": statement, "fy": fy, "passed": passed, "attempts": attempts,
            "detail": detail}, source=f"agent:{agent}", agent=agent)

    def add_flag(self, flag_id: str, severity: str, *, value: dict, evidence: list[str],
                 confidence: float, agent: str, text: str = "") -> dict:
        if severity not in SEVERITY:
            raise ValueError(f"severity must be one of {list(SEVERITY)}")
        current = self.flag(flag_id) or {}
        return self._assert(f"flag:{flag_id}", {
            "id": flag_id, "severity": severity, "value": value, "evidence": evidence,
            "confidence": confidence, "text": text, "skeptic": current.get("skeptic")},
            source=evidence[0] if evidence else f"agent:{agent}", agent=agent,
            confidence=confidence)

    def rule_on_flag(self, flag_id: str, ruling: str, reason: str, evidence: list[str], *,
                     agent: str = "Skeptic", severity: str | None = None) -> dict:
        flag = self.flag(flag_id)
        if flag is None:
            raise KeyError(f"no flag {flag_id}")
        updated = {**flag, "skeptic": {"ruling": ruling, "reason": reason, "evidence": evidence}}
        if severity:
            updated["severity"] = severity
        return self._assert(f"flag:{flag_id}", updated,
                            source=evidence[0] if evidence else f"agent:{agent}", agent=agent,
                            confidence=flag["confidence"])

    def add_question(self, text: str, *, agent: str, source: str | None = None) -> dict:
        return self._assert(f"question:{text[:80]}", {"text": text},
                            source=source or f"agent:{agent}", agent=agent)

    def add_not_checked(self, item: str, reason: str, *, agent: str) -> dict:
        return self._assert(f"not_checked:{item}", {"item": item, "reason": reason},
                            source=f"agent:{agent}", agent=agent)

    def set_score(self, score: dict, *, agent: str = "Scorer") -> dict:
        return self._assert("score", score, source=f"agent:{agent}", agent=agent)

    def set_depth(self, depth: str, reason: str) -> None:
        if self.meta["depth"] != depth:
            self.meta["depth_history"].append({"from": self.meta["depth"], "to": depth,
                                               "reason": reason})
            self.meta["depth"] = depth

    # -- reads -------------------------------------------------------------

    def _current(self, prefix: str) -> list[dict]:
        facts = [f for f in self.world.store.facts.values()
                 if f["about"] == self.company and f["predicate"].startswith(prefix)
                 and not f.get("supersededBy")]
        return sorted(facts, key=lambda f: f["predicate"])

    def facts(self, *, verified_only: bool = False) -> list[dict]:
        out = [{**f["value"], "source": f["source"]["ref"], "agent": f["source"]["by"]}
               for f in self._current("fact:")]
        return [f for f in out if f["verified"]] if verified_only else out

    def fact(self, field_: str, fy: str) -> dict | None:
        hits = self._current(f"fact:{field_}:{fy}")
        return hits[0]["value"] if hits else None

    def flags(self) -> list[dict]:
        return [{**f["value"], "agent": f["source"]["by"]} for f in self._current("flag:")]

    def flag(self, flag_id: str) -> dict | None:
        hits = [f for f in self._current(f"flag:{flag_id}") if f["predicate"] == f"flag:{flag_id}"]
        return hits[0]["value"] if hits else None

    def entities(self, type_: str | None = None) -> list[dict]:
        return [e for e in self.world.view("graph").query(type=type_) if e["type"] != "company"]

    def neighbours(self, entity_id: str, hops: int = 2,
                   edge_types: set[str] | None = None) -> dict[str, int]:
        """Entity ids within `hops` open edges of `entity_id`, with their distance."""
        adj: dict[str, set[str]] = {}
        for rel in self.world.store.relationships.values():
            if rel.get("validTo") or (edge_types and rel["type"] not in edge_types):
                continue
            adj.setdefault(rel["from"], set()).add(rel["to"])
            adj.setdefault(rel["to"], set()).add(rel["from"])
        start = self.world.store.resolve_alias(entity_id)
        seen, queue = {start: 0}, deque([start])
        while queue:
            node = queue.popleft()
            if seen[node] < hops:
                for nxt in adj.get(node, ()):
                    if nxt not in seen:
                        seen[nxt] = seen[node] + 1
                        queue.append(nxt)
        seen.pop(start)
        return seen

    def context(self, intent: str, *, mentions: tuple[str, ...] | list[str] = (),
                kinds: tuple[str, ...] = ("entity", "fact", "flag"),
                budget_chars: int = 6000) -> ContextPayload:
        """Select, prioritise and truncate for one intent: records that mention a
        requested term first, then flags by severity, then verified facts."""
        terms = [m.lower() for m in mentions]
        candidates: list[tuple[str, dict, str]] = []
        if "entity" in kinds:
            candidates += [("entity", {"type": e["type"], "name": e.get("name"), "id": e["id"]},
                            e["source"].get("ref", "")) for e in self.entities()]
        if "fact" in kinds:
            candidates += [("fact", {k: v for k, v in f.items() if k not in ("agent", "source")},
                            f["source"]) for f in self.facts()]
        if "flag" in kinds:
            candidates += [("flag", f, (f.get("evidence") or [""])[0]) for f in self.flags()]
        if "question" in kinds:
            candidates += [("question", f["value"], f["source"]["ref"])
                           for f in self._current("question:")]
        scored = []
        for kind, data, src in candidates:
            text = json.dumps(data, default=str).lower()
            hit = [t for t in terms if t in text]
            sev = SEVERITY.get(str(data.get("severity")), 0)
            score = 2.0 * len(hit) + 0.5 * sev + (0.1 if data.get("verified") else 0)
            reason = ("mentions " + ", ".join(hit) if hit else
                      f"severity {data['severity']}" if sev else
                      "verified fact" if data.get("verified") else "background")
            scored.append(ContextItem(kind, data, src, reason, score))
        scored.sort(key=lambda it: it.relevance, reverse=True)
        payload = ContextPayload(intent, "task_focused" if terms else "comprehensive")
        used = 0
        for it in scored:
            size = len(json.dumps(it.data, default=str))
            if used + size > budget_chars:
                payload.truncated = True
                break
            payload.items.append(it)
            used += size
        return payload

    def case_file(self) -> dict[str, Any]:
        """The run's evidence record, as saved to output/<run_id>/case_file.json."""
        rels = [r for r in self.world.store.relationships.values() if not r.get("validTo")]
        score = self._current("score")
        return {
            "meta": self.meta,
            "entities": [{"id": e["id"], "type": e["type"], "name": e.get("name"),
                          "aliases": e.get("aliases", []), "source": e["source"].get("ref"),
                          "agent": e["source"].get("by")} for e in self.entities()],
            "edges": [{"src": r["from"], "dst": r["to"], "type": r["type"],
                       "source": r["source"].get("ref"), "agent": r["source"].get("by")}
                      for r in sorted(rels, key=lambda r: r["id"])],
            "facts": self.facts(),
            "verification": [f["value"] for f in self._current("verification:")],
            "flags": self.flags(),
            "open_questions": [f["value"]["text"] for f in self._current("question:")],
            "not_checked": [f["value"] for f in self._current("not_checked:")],
            "merge_queue": [p for p in self.world.merge_queue() if p["status"] == "pending"],
            "score": score[0]["value"] if score else None,
            "writes": len(self.world.audit()),
        }
