"""Meterless H-MEM Agent Engine (minimum set), ported to Python: run memory.

Implements MemoryStoreService and MemoryRetrievalService plus event mining
(contract: Meterless H-MEM Agent Engine, sections 4-8, 13). Dreaming and
sleep are left out, as the guide recommends for a short build.

RedFlag rules on top of the reference:
- every memory carries the `as_of` of the run that wrote it, and retrieval
  only returns memories from runs with an earlier as_of (the data guide's
  time rule), so lessons never leak backwards in a backtest;
- memory shapes prompts and plans only; it is never a scorer input;
- the backtest switches memory off entirely (`enabled=False`).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

LAYER_WEIGHT = {"long_term": 1.0, "working": 0.8, "short_term": 0.6}
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "in", "on", "for", "and", "or", "to", "is", "was",
         "with", "by", "at", "from", "this", "that", "it", "as", "be"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, content TEXT NOT NULL, type TEXT NOT NULL,
    layer TEXT NOT NULL, tags TEXT NOT NULL, entities TEXT NOT NULL,
    domain TEXT, confidence REAL NOT NULL, source TEXT,
    agent_id TEXT, run_id TEXT, task_id TEXT, as_of TEXT NOT NULL,
    created_at TEXT NOT NULL, last_accessed TEXT, access_count INTEGER NOT NULL DEFAULT 0,
    supersedes TEXT
);
"""


@dataclass
class Memory:
    id: str
    content: str
    type: str          # factual | procedural | general
    layer: str         # short_term | working | long_term
    tags: list[str]
    entities: list[str]
    domain: str
    confidence: float
    source: str
    agent_id: str
    run_id: str
    task_id: str
    as_of: str
    created_at: str
    access_count: int = 0


@dataclass
class Retrieved:
    memory: Memory
    score: float
    why: str


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


class HMem:
    def __init__(self, path: Path, *, enabled: bool = True,
                 on_event: Callable[[dict], None] | None = None):
        self.enabled = enabled
        self.on_event = on_event
        path.parent.mkdir(parents=True, exist_ok=True)
        # Agents may read memory from worker threads; one lock serialises access.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.executescript(SCHEMA)

    # -- MemoryStoreService --------------------------------------------------

    def add(self, *args, **kwargs):
        with self._lock:
            return self._add(*args, **kwargs)

    def _add(self, content: str, *, as_of: date, type: str = "procedural",
            layer: str = "working", tags: list[str] | None = None,
            entities: list[str] | None = None, domain: str = "", confidence: float = 0.7,
            source: str = "", agent_id: str = "", run_id: str = "",
            task_id: str = "") -> Memory | None:
        if not self.enabled:
            return None
        content = content.strip()
        dup = self._db.execute("SELECT id FROM memories WHERE content=? AND as_of=?",
                               (content, as_of.isoformat())).fetchone()
        if dup:
            return self.get(dup["id"])
        m = Memory(uuid.uuid4().hex[:16], content, type, layer, sorted(tags or []),
                   sorted(entities or []), domain, confidence, source, agent_id, run_id,
                   task_id, as_of.isoformat(),
                   datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._db.execute(
            "INSERT INTO memories (id, content, type, layer, tags, entities, domain, "
            "confidence, source, agent_id, run_id, task_id, as_of, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.id, m.content, m.type, m.layer, json.dumps(m.tags), json.dumps(m.entities),
             m.domain, m.confidence, m.source, m.agent_id, m.run_id, m.task_id, m.as_of,
             m.created_at))
        self._db.commit()
        self._emit("hmem.add", id=m.id, layer=layer, tags=m.tags, as_of=m.as_of)
        return m

    def get(self, *args, **kwargs):
        with self._lock:
            return self._get(*args, **kwargs)

    def _get(self, memory_id: str) -> Memory | None:
        r = self._db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._row(r) if r else None

    # -- MemoryRetrievalService -----------------------------------------------

    def retrieve(self, *args, **kwargs):
        with self._lock:
            return self._retrieve(*args, **kwargs)

    def _retrieve(self, query: str, *, as_of: date, tags: list[str] | None = None,
                 entities: list[str] | None = None, top_n: int = 5,
                 threshold: float = 0.15) -> list[Retrieved]:
        """Hybrid ranking over memories written by runs with an earlier as_of."""
        if not self.enabled:
            return []
        q_words, q_tags, q_ents = _words(query), set(tags or []), set(entities or [])
        out = []
        for r in self._db.execute("SELECT * FROM memories WHERE as_of < ?",
                                  (as_of.isoformat(),)):
            m = self._row(r)
            words = _words(m.content)
            kw = len(q_words & words) / math.sqrt(len(q_words) * len(words) or 1)
            tag = len(q_tags & set(m.tags)) / (len(q_tags) or 1) if q_tags else 0.0
            ent = 1.0 if q_ents & set(m.entities) else 0.0
            age_days = (as_of - date.fromisoformat(m.as_of)).days
            recency = math.exp(-age_days / 365)
            score = (0.5 * kw + 0.25 * tag + 0.25 * ent) * LAYER_WEIGHT[m.layer] \
                * (0.7 + 0.3 * recency) * m.confidence
            if score >= threshold:
                why = ", ".join(x for x, on in (("keywords", kw > 0), ("tags", tag > 0),
                                                ("entity", ent > 0)) if on)
                out.append(Retrieved(m, round(score, 4), why))
        out.sort(key=lambda x: x.score, reverse=True)
        out = out[:top_n]
        for x in out:
            self._db.execute("UPDATE memories SET access_count = access_count + 1, "
                             "last_accessed = ? WHERE id = ?",
                             (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              x.memory.id))
        self._db.commit()
        self._emit("hmem.retrieve", query=query[:120], as_of=as_of.isoformat(),
                   returned=[{"id": x.memory.id, "score": x.score, "why": x.why} for x in out],
                   strategy="task_focused" if q_tags or q_ents else "minimal")
        return out

    def context_block(self, retrieved: list[Retrieved]) -> str:
        if not retrieved:
            return ""
        lines = ["# Lessons from earlier runs (as of earlier dates)"]
        lines += [f"- {x.memory.content}" for x in retrieved]
        return "\n".join(lines)

    # -- MemoryMiningService ------------------------------------------------

    def mine(self, event: str, summary: str, *, as_of: date, extractor=None,
             tags: list[str] | None = None, entities: list[str] | None = None,
             agent_id: str = "", run_id: str = "") -> list[Memory]:
        """Turn a runtime event into memories.

        `extractor(prompt) -> list[str]` asks a model for lessons; with no
        extractor, or if it fails, a concise direct summary is stored as
        short-term memory (the reference's no-model fallback).
        """
        lessons, layer = [], "working"
        if extractor is not None:
            try:
                lessons = [s for s in extractor(
                    f"Event: {event}\n{summary}\n\nList at most 3 reusable lessons for "
                    "future runs, as a JSON array of short strings.") if s.strip()]
            except Exception:
                lessons = []
        if not lessons:
            lessons, layer = [f"{event}: {summary[:300]}"], "short_term"
        return [m for m in (self.add(text, as_of=as_of, layer=layer, tags=tags,
                                     entities=entities, source=event, agent_id=agent_id,
                                     run_id=run_id) for text in lessons) if m]

    # -- internals -------------------------------------------------------------

    def _row(self, r: sqlite3.Row) -> Memory:
        return Memory(r["id"], r["content"], r["type"], r["layer"], json.loads(r["tags"]),
                      json.loads(r["entities"]), r["domain"] or "", r["confidence"],
                      r["source"] or "", r["agent_id"] or "", r["run_id"] or "",
                      r["task_id"] or "", r["as_of"], r["created_at"], r["access_count"])

    def _emit(self, action: str, **detail) -> None:
        if self.on_event:
            self.on_event({"engine": "hmem", "action": action, **detail})
