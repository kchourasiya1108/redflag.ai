"""Meterless Markovian Agent Engine, ported to Python for RedFlag.

Runs one long task as a chain of bounded chunks. Each chunk sees only the
goal, a compressed carryover from the previous chunk, and its own input
window, so cost per step stays flat instead of growing with the chain
(contract: Meterless MARKOVIAN Agent Engine, sections 3-9).

RedFlag's use: reading one RHP section page-window by page-window. The
carryover holds table state (unit, fiscal-year columns, open table), and each
chunk's rows are handed to the caller, never carried forward.

The model is reached only through the injected generator.
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE = "[STATE_CHECKPOINT]"
FINAL = "[TASK_COMPLETE]"
_STATE_ALIASES = (STATE, "@@@STATE@@@", "[STATE]", "---STATE---")
_FINAL_ALIASES = (FINAL, "@@@FINAL@@@", "[FINAL]", "---FINAL---")

Generator = Callable[..., dict]


@dataclass
class ChunkConfig:
    chunk_size: int = 6000          # generation + input budget per chunk, tokens
    max_chunks: int = 12
    carryover_tokens: int = 512

    def clamp(self) -> "ChunkConfig":
        self.chunk_size = min(max(self.chunk_size, 1000), 128_000)
        self.carryover_tokens = min(max(self.carryover_tokens, 128), 32_768)
        self.max_chunks = max(self.max_chunks, 1)
        return self


@dataclass
class ChunkInfo:
    id: int
    tokens: int
    carryover_in: str
    content: str
    carryover_out: str
    carryover_method: str


@dataclass
class MarkovianRun:
    id: str
    run_id: str
    agent_id: str
    mode: str
    prompt: str
    chunks: list[ChunkInfo] = field(default_factory=list)
    total_tokens_used: int = 0
    total_tokens_saved: int = 0
    efficiency_percent: float = 0.0
    duration_ms: float = 0.0
    completed: bool = False
    stop_reason: str = ""


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass
class ModeTemplate:
    """First-chunk and continuation prompts for one task class."""
    first: str
    continuation: str


MODES: dict[str, ModeTemplate] = {
    "EXTRACT": ModeTemplate(
        first=("{memory}Task: {goal}\n\nYou will read the input in parts. This is part "
               "{n} of {total}.\n\nINPUT:\n{input}\n\nReturn the rows found in this part "
               "as instructed. Then write " + STATE + " followed by a short JSON object "
               "holding only what the next part needs to continue (unit, column order, "
               "open table, partial row). On the last part write " + FINAL + " instead."),
        continuation=("Task (continued): {goal_short}\n\nState from the previous part:\n"
                      "{carryover}\n\nThis is part {n} of {total}.\n\nINPUT:\n{input}\n\n"
                      "Return the rows found in this part. End with " + STATE + " and the "
                      "updated state JSON, or " + FINAL + " on the last part.")),
    "RESEARCH": ModeTemplate(
        first=("{memory}Task: {goal}\n\nPart {n} of {total}:\n{input}\n\nRecord findings "
               "as '>> ' lines with page citations. End with " + STATE + " and 3-5 key "
               "points to carry forward, or " + FINAL + " when done."),
        continuation=("Task (continued): {goal_short}\n\nCarried forward:\n{carryover}\n\n"
                      "Part {n} of {total}:\n{input}\n\nContinue. End with " + STATE +
                      " and updated points, or " + FINAL + ".")),
}


class Markovian:
    def __init__(self, generator: Generator, config: ChunkConfig | None = None, *,
                 compressor: Generator | None = None, history_path: Path | None = None,
                 on_event: Callable[[dict], None] | None = None):
        self.generator = generator
        self.config = (config or ChunkConfig()).clamp()
        self.compressor = compressor
        self.history_path = history_path
        self.on_event = on_event

    def run(self, goal: str, inputs: list[str], *, mode: str = "EXTRACT",
            system: str | None = None, memory_context: str = "",
            run_id: str = "", agent_id: str = "",
            on_chunk: Callable[[ChunkInfo], None] | None = None) -> MarkovianRun:
        """Process `inputs` (pre-sized windows) as a bounded chain."""
        tmpl = MODES[mode]
        run = MarkovianRun(id=uuid.uuid4().hex[:12], run_id=run_id, agent_id=agent_id,
                           mode=mode, prompt=goal[:200])
        started, carry, history_tokens = time.perf_counter(), "", 0
        total = min(len(inputs), self.config.max_chunks)
        self._emit("initializing", run, total=total)
        for n, window in enumerate(inputs[:total], 1):
            self._emit("generating", run, chunk=n)
            if n == 1:
                prompt = tmpl.first.format(memory=(memory_context + "\n\n") if memory_context
                                           else "", goal=goal, n=n, total=total, input=window)
            else:
                prompt = tmpl.continuation.format(goal_short=goal[:200], carryover=carry,
                                                  n=n, total=total, input=window)
            reply = self.generator(prompt, None, system, None, None)["text"]
            content, explicit_state, final = split_markers(reply)
            self._emit("compressing", run, chunk=n)
            new_carry, method = self.carryover(explicit_state, content, carry)
            tokens = estimate_tokens(prompt) + estimate_tokens(reply)
            standard = history_tokens + tokens          # cost if history were resent
            run.total_tokens_used += tokens
            run.total_tokens_saved += max(standard - tokens, 0)
            history_tokens += tokens
            info = ChunkInfo(n, tokens, carry, content, new_carry, method)
            run.chunks.append(info)
            if on_chunk:
                on_chunk(info)
            carry = new_carry
            if final:
                run.stop_reason = "final_marker"
                break
        else:
            run.stop_reason = ("inputs_done" if total == len(inputs)
                               else "max_chunks")
        run.completed = run.stop_reason in ("final_marker", "inputs_done")
        spent = run.total_tokens_used + run.total_tokens_saved
        run.efficiency_percent = round(100 * run.total_tokens_saved / spent, 1) if spent else 0.0
        run.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        self._emit("complete" if run.completed else "error", run)
        self._persist(run)
        return run

    def carryover(self, explicit: str | None, content: str, previous: str) -> tuple[str, str]:
        """Compression cascade, in the reference's strict order."""
        budget_chars = self.config.carryover_tokens * 4
        if explicit and len(explicit.strip()) >= 2:
            state = sanitize(explicit)
            if len(state) <= budget_chars:
                return state, "explicit"
        if self.compressor is not None:
            try:
                prompt = ("Compress this working state to the 3-5 points needed to "
                          f"continue. Keep units and column order.\n\nPrevious state:\n"
                          f"{previous}\n\nLatest output:\n{content[:4000]}")
                text = self.compressor(prompt, None, None, None, None)["text"].strip()
                if text:
                    return text[:budget_chars], "llm"
            except Exception:
                pass    # fall through: carryover must survive a model outage
        points = re.findall(r"(?im)^.*\b(unit|in (?:₹|rs\.?|rupees)|lakhs?|crores?|"
                            r"therefore|key insight|status)\b.*$", content)
        if points:
            return "; ".join(p.strip() for p in points[:5])[:budget_chars], "heuristic"
        words = (previous + " " + content).split()
        return " ".join(words[-(budget_chars // 6):]), "truncate"

    def _emit(self, phase: str, run: MarkovianRun, **detail) -> None:
        if self.on_event:
            self.on_event({"engine": "markovian", "phase": phase, "markovian_id": run.id,
                           "run_id": run.run_id, "agent_id": run.agent_id,
                           "tokens_used": run.total_tokens_used,
                           "tokens_saved": run.total_tokens_saved, **detail})

    def _persist(self, run: MarkovianRun) -> None:
        if self.history_path:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            record = asdict(run)
            for c in record["chunks"]:
                c["content"] = c["content"][:500]      # keep the log small
            with self.history_path.open("a") as f:
                f.write(json.dumps(record) + "\n")


def split_markers(reply: str) -> tuple[str, str | None, bool]:
    """(content without control text, state text after the marker, is_final)."""
    final = any(m in reply for m in _FINAL_ALIASES)
    state = None
    for marker in _STATE_ALIASES:
        if marker in reply:
            reply, state = reply.split(marker, 1)
            break
    for marker in _FINAL_ALIASES:
        reply = reply.replace(marker, "")
        if state:
            state = state.replace(marker, "")
    return reply.strip(), state.strip() if state else None, final


def sanitize(state: str) -> str:
    return re.sub(r"\s+", " ", state.replace("```json", "").replace("```", "")).strip()


def projected_curve(config: ChunkConfig, steps: int = 10) -> list[dict]:
    """Projected cost per step, standard vs Markovian (reference section 5.1)."""
    return [{"step": k,
             "standard": (k - 1) * config.chunk_size + config.chunk_size,
             "markovian": config.carryover_tokens + config.chunk_size}
            for k in range(1, steps + 1)]
