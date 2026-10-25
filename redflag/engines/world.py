"""Meterless World Model Agent Engine, ported to Python: RedFlag's Case File.

The single shared state of one run. Agents write through `ingest` and read
through `context`; they never hand raw state to each other (contract:
Meterless WORLD MODEL Agent Engine, sections 4, 6, 10, 12).

Records are entities, edges, facts, flags, questions and notes. Each id is a
content hash of its identity fields, so ingesting the same record twice is a
no-op and an interrupted run resumes cleanly. Every write is appended to a
JSONL log with the agent and run that made it.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Fields that identify a record of each kind; everything else may change.
IDENTITY = {
    "entity": ("type", "name"),
    "edge": ("src", "dst", "type"),
    "fact": ("field", "fiscal_year"),
    "flag": ("flag_id",),
    "question": ("text",),
    "note": ("agent_id", "text"),
}


@dataclass
class Record:
    kind: str
    data: dict[str, Any]
    source: str                    # evidence ref: rhp:p12, zq:<sha>, sebi:<id>
    agent_id: str
    run_id: str
    id: str = ""
    version: int = 1
    written_at: str = ""

    def __post_init__(self) -> None:
        if self.kind not in IDENTITY:
            raise ValueError(f"unknown record kind {self.kind!r}")
        if not self.id:
            key = [self.kind] + [str(self.data.get(f, "")).strip().lower()
                                 for f in IDENTITY[self.kind]]
            self.id = f"{self.kind}:" + hashlib.sha256("|".join(key).encode()).hexdigest()[:12]
        self.written_at = self.written_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds")


@dataclass
class ContextItem:
    record: Record
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
            r = it.record
            lines.append(f"- [{r.kind}] {json.dumps(r.data, default=str)} (source {r.source})")
        return "\n".join(lines)


class WorldModel:
    def __init__(self, run_id: str, log_path: Path | None = None):
        self.run_id = run_id
        self.log_path = log_path
        self.records: dict[str, Record] = {}

    # -- writes ------------------------------------------------------------

    def ingest(self, kind: str, data: dict, *, source: str, agent_id: str) -> Record:
        """Idempotent write: same identity and data -> nothing changes."""
        rec = Record(kind, dict(data), source, agent_id, self.run_id)
        old = self.records.get(rec.id)
        if old is not None:
            if old.data == rec.data and old.source == rec.source:
                return old
            rec.version = old.version + 1
        self.records[rec.id] = rec
        self._log("ingest", rec)
        return rec

    def ingest_many(self, kind: str, rows: Iterable[dict], *, source_field: str = "source",
                    agent_id: str) -> list[Record]:
        return [self.ingest(kind, {k: v for k, v in r.items() if k != source_field},
                            source=r.get(source_field, ""), agent_id=agent_id) for r in rows]

    # -- reads -------------------------------------------------------------

    def all(self, kind: str, **match) -> list[Record]:
        return [r for r in self.records.values() if r.kind == kind
                and all(r.data.get(k) == v for k, v in match.items())]

    def entity(self, type_: str, name: str) -> Record | None:
        hits = self.all("entity", type=type_, name=name)
        return hits[0] if hits else None

    def neighbours(self, entity_id: str, hops: int = 2,
                   edge_types: set[str] | None = None) -> dict[str, int]:
        """Entity ids reachable within `hops` edges, with their distance."""
        adj: dict[str, set[str]] = {}
        for e in self.all("edge"):
            if edge_types and e.data["type"] not in edge_types:
                continue
            adj.setdefault(e.data["src"], set()).add(e.data["dst"])
            adj.setdefault(e.data["dst"], set()).add(e.data["src"])
        seen, queue = {entity_id: 0}, deque([entity_id])
        while queue:
            node = queue.popleft()
            if seen[node] >= hops:
                continue
            for nxt in adj.get(node, ()):
                if nxt not in seen:
                    seen[nxt] = seen[node] + 1
                    queue.append(nxt)
        seen.pop(entity_id)
        return seen

    def context(self, intent: str, *, kinds: tuple[str, ...] = ("entity", "fact", "flag"),
                mentions: Iterable[str] = (), budget_chars: int = 6000) -> ContextPayload:
        """Select, prioritise and truncate records for one intent.

        Priority: records that mention a requested term, then flags by
        severity, then the most recent. Strategy label as in the reference.
        """
        terms = [m.lower() for m in mentions]
        scored = []
        for r in self.records.values():
            if r.kind not in kinds:
                continue
            text = json.dumps(r.data, default=str).lower()
            hit = sum(t in text for t in terms)
            sev = {"HIGH": 3, "ELEVATED": 2, "LOW": 1}.get(str(r.data.get("severity")), 0)
            score = 2.0 * hit + 0.5 * sev + (0.1 if r.data.get("verified") else 0)
            reason = ("mentions " + ", ".join(t for t in terms if t in text) if hit
                      else f"severity {r.data['severity']}" if sev else "recent")
            scored.append(ContextItem(r, reason, score))
        scored.sort(key=lambda it: (it.relevance, it.record.written_at), reverse=True)
        payload = ContextPayload(intent, "task_focused" if terms else "comprehensive")
        used = 0
        for it in scored:
            size = len(json.dumps(it.record.data, default=str))
            if used + size > budget_chars:
                payload.truncated = True
                break
            payload.items.append(it)
            used += size
        return payload

    # -- persistence -------------------------------------------------------

    def snapshot(self) -> dict:
        out: dict[str, list] = {k: [] for k in IDENTITY}
        for r in self.records.values():
            out[r.kind].append({"id": r.id, **r.data, "source": r.source,
                                "agent": r.agent_id, "version": r.version})
        return out

    @classmethod
    def resume(cls, run_id: str, log_path: Path) -> "WorldModel":
        """Rebuild state from the write log after a crash."""
        wm = cls(run_id, log_path=None)
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                e = json.loads(line)
                rec = Record(e["kind"], e["data"], e["source"], e["agent_id"],
                             e["run_id"], e["id"], e["version"], e["written_at"])
                wm.records[rec.id] = rec
        wm.log_path = log_path
        return wm

    def _log(self, action: str, rec: Record) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"action": action, "id": rec.id, "kind": rec.kind,
                                "data": rec.data, "source": rec.source,
                                "agent_id": rec.agent_id, "run_id": rec.run_id,
                                "version": rec.version, "written_at": rec.written_at},
                               default=str) + "\n")
