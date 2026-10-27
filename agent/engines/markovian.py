"""Meterless Markovian Agent Engine, ported to Python for RedFlag.

Runs one long task as a chain of bounded chunks. Each chunk sees only the
goal, a compressed carryover from the previous chunk, and its own input, so
cost per step stays flat instead of growing with the chain.

A faithful port of the reference (github.com/Meterless/Meterless,
engines/markovian, AGENTS.md sections 2-7, 11, 12 and 14): config bounds with
typed errors, the marker protocol, the compression cascade, mode prompts,
the orchestrator loop, reflection that never fails a run, the efficiency
math, and the section 14 accounting rules (numbers labelled `estimated` or
`measured`, `[OVERLAP]` implemented, a mandatory per-run config snapshot).
`agent/engines/conformance/markovian/` runs the official conformance suite
against this module.

RedFlag extension: `run(windows=[...])` feeds one input window per step (an
RHP section page-window by page-window) as that step's attachment, and ends
the run when the windows run out. The carryover then holds table state
(unit, fiscal-year columns, open table), and each chunk's rows go to the
caller through `on_chunk`, never into the carryover.

The model is reached only through the injected generator:
`generator(prompt, attachments, system_prompt, on_stream, abort) -> {"text", ...}`.
"""
from __future__ import annotations

import itertools
import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

STATE = "[STATE_CHECKPOINT]"
FINAL = "[TASK_COMPLETE]"

Generator = Callable[..., dict]
Compressor = Callable[[str, str], str]

FRAMING_TOKENS = 400        # template overhead (section 2.1 engine constant)
OUTPUT_BUDGET = 1200        # per-chunk output budget (section 2.1 engine constant)


class ConfigValidationError(ValueError):
    def __init__(self, message: str):
        super().__init__(f"Markovian config: {message}")


@dataclass
class ChunkConfig:
    chunk_size: int = 8000          # target generation budget per chunk, tokens
    max_chunks: int = 24            # recursion cap
    carryover_tokens: int = 800     # carryover state budget
    overlap_tokens: int = 0         # overlap window; implemented, never ignored

    def to_wire(self) -> dict:
        return {"chunkSize": self.chunk_size, "maxChunks": self.max_chunks,
                "carryoverTokens": self.carryover_tokens, "overlapTokens": self.overlap_tokens}


BOUNDS = {"chunk_size": (1000, 128_000), "max_chunks": (1, 32),
          "carryover_tokens": (128, 32_768), "overlap_tokens": (0, 4096)}
_WIRE_KEYS = {"chunkSize": "chunk_size", "maxChunks": "max_chunks",
              "carryoverTokens": "carryover_tokens", "overlapTokens": "overlap_tokens"}


def resolve_config(partial: ChunkConfig | dict | None = None) -> ChunkConfig:
    """Defaults plus overrides, validated. Out-of-range values raise; nothing is clamped.

    Like the reference, only min/max are enforced: the spec's step sizes are UI
    slider granularity (its own default carryover of 800 is off the 128 grid).
    """
    if isinstance(partial, ChunkConfig):
        cfg = replace(partial)
    else:
        given = {_WIRE_KEYS.get(k, k): v for k, v in (partial or {}).items()}
        unknown = set(given) - {f.name for f in fields(ChunkConfig)}
        if unknown:
            raise ConfigValidationError(f"unknown field(s) {sorted(unknown)}")
        cfg = ChunkConfig(**given)
    for name, (lo, hi) in BOUNDS.items():
        value = getattr(cfg, name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ConfigValidationError(f"{name} must be a number")
        if not lo <= value <= hi:
            raise ConfigValidationError(f"{name}={value} outside [{lo}, {hi}]")
    if cfg.carryover_tokens <= 0:
        raise ConfigValidationError("carryover_tokens must be > 0")
    if cfg.chunk_size < FRAMING_TOKENS + cfg.carryover_tokens + OUTPUT_BUDGET:
        raise ConfigValidationError(
            f"chunk_size={cfg.chunk_size} < framing({FRAMING_TOKENS}) + "
            f"carryover({cfg.carryover_tokens}) + outputBudget({OUTPUT_BUDGET})")
    return cfg


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass
class ChunkInfo:
    id: int             # 1-based step index
    tokens: int         # ceil(chars/4) of the cleaned output, or provider-reported
    carryover: str      # state passed INTO this step
    content: str        # display-safe (marker-cleaned) output
    cascade_level: str = ""   # how the carryover OUT of this step was made


@dataclass
class MarkovianRun:
    id: str
    timestamp: float
    prompt: str
    mode: str
    chunk_config: ChunkConfig           # mandatory per-run snapshot (section 2.3)
    chunks: list[ChunkInfo] = field(default_factory=list)
    total_tokens_used: int = 0
    total_tokens_saved: int = 0
    efficiency_percent: float = 0.0
    duration_ms: float = 0.0
    status: str = "completed"           # completed | max-chunks | errored | aborted
    token_label: str = "estimated"      # estimated | measured
    output: str = ""
    reflection: str | None = None
    error: str | None = None
    run_id: str = ""                    # RedFlag run that owns this chain
    agent_id: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_wire(self) -> dict:
        return {"id": self.id, "timestamp": self.timestamp, "prompt": self.prompt,
                "mode": self.mode, "chunkConfig": self.chunk_config.to_wire(),
                "chunks": [{"id": c.id, "tokens": c.tokens, "carryover": c.carryover,
                            "content": c.content} for c in self.chunks],
                "totalTokensUsed": self.total_tokens_used,
                "totalTokensSaved": self.total_tokens_saved,
                "efficiencyPercent": self.efficiency_percent, "durationMs": self.duration_ms,
                "status": self.status, "tokenLabel": self.token_label, "output": self.output,
                **({"reflection": self.reflection} if self.reflection is not None else {}),
                **({"error": self.error} if self.error is not None else {})}


# -- efficiency math (section 6, docs/efficiency-model.md) --------------------

def run_efficiency(chunks: list, config: ChunkConfig) -> dict:
    """Section 6.3, always against the run's own config snapshot."""
    tokens = [c["tokens"] if isinstance(c, dict) else c.tokens for c in chunks]
    used = sum(tokens)
    standard, prefix = 0, 0
    for t in tokens:
        standard += prefix + t
        prefix += t
    markovian = len(tokens) * config.carryover_tokens + used
    saved = max(0, standard - markovian)
    return {"totalTokensUsed": used, "standardCost": standard, "markovianCost": markovian,
            "tokensSaved": saved,
            "efficiencyPercent": saved / standard * 100 if standard > 0 else 0.0}


DOC_DEFAULTS = {"F": 400, "G": 200, "S": 300, "O": 1200, "C": 800}


def model_totals(n: int, k: dict | None = None) -> dict:
    """The symbolic cost model; model_totals(20) reproduces the doc's 86% example."""
    k = {**DOC_DEFAULTS, **(k or {})}
    naive = n * (k["F"] + k["G"] + k["S"]) + k["O"] * n * (n - 1) / 2
    markovian = n * (k["F"] + k["G"] + k["C"] + k["S"])
    return {"naiveTotal": naive, "markovianTotal": markovian, "savings": naive - markovian,
            "efficiency": (naive - markovian) / naive}


def projection_curve(config: ChunkConfig, steps: int = 10) -> list[dict]:
    """Projected cost per step, standard vs Markovian (section 4.1)."""
    return [{"step": s, "standard": (s - 1) * config.chunk_size + config.chunk_size,
             "markovian": config.carryover_tokens + config.chunk_size}
            for s in range(1, steps + 1)]


# -- marker protocol (section 3) ----------------------------------------------

_STATE_BLOCK = re.compile(r"<CARRYOVER>([\s\S]*?)</CARRYOVER>")
_STATE_TAILS = [re.compile(p) for p in (r"\[STATE_CHECKPOINT\]\s*([\s\S]*)$",
                                        r"@@@STATE@@@\s*([\s\S]*)$",
                                        r"(?<!_)\[STATE\]\s*([\s\S]*)$",
                                        r"---STATE---\s*([\s\S]*)$")]
_FINALS = [re.compile(p) for p in (r"\[TASK_COMPLETE\]", r"@@@FINAL@@@", r"(?<!_)\[FINAL\]",
                                   r"---FINAL---", r"<DONE\s*/?>")]


@dataclass
class Parsed:
    state: str | None
    done: bool
    cleaned: str


def parse_markers(raw: str) -> Parsed:
    blocks = _STATE_BLOCK.findall(raw)
    state = blocks[-1].strip() if blocks else None
    if state is None:
        for pattern in _STATE_TAILS:
            m = pattern.search(raw)
            if m:
                state = m.group(1).strip()
                break
    return Parsed(state, any(p.search(raw) for p in _FINALS), clean_markers(raw))


def clean_markers(raw: str) -> str:
    out = re.sub(r"<CARRYOVER>[\s\S]*?</CARRYOVER>", "", raw)
    out = re.sub(r"<PROGRESS>[\s\S]*?</PROGRESS>", "", out)
    out = re.sub(r"[<\[]NEEDS_TOOL[^\]>]*[\]>]", "", out)
    out = re.sub(r"[<\[]NEEDS_CLARIFICATION[\]>][^\n]*", "", out)
    for tail in (r"\[STATE_CHECKPOINT\][\s\S]*$", r"@@@STATE@@@[\s\S]*$", r"---STATE---[\s\S]*$",
                 r"\n\[STATE\][\s\S]*$"):
        out = re.sub(tail, "", out)
    for marker in (r"\[TASK_COMPLETE\]", r"@@@FINAL@@@", r"---FINAL---", r"<DONE\s*/?>"):
        out = re.sub(marker, "", out)
    out = re.sub(r"\n\[FINAL\][\s\S]*$", "", out)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+$", "", out, flags=re.M)).strip()


# -- carryover cascade (section 4.3) ------------------------------------------

_KEY_PHRASE = re.compile(r"(?:^|\n|\.\s+)((?:Therefore|Key insight|Status|Decision|Decided|Next|"
                         r"Remaining|Completed|Unit|All figures)[^.\n]*)", re.I)


def next_carryover(marker_state: str | None, previous: str, chunk_text: str,
                   config: ChunkConfig, compressor: Compressor | None = None) -> tuple[str, str]:
    """(carryover, level): marker, then LLM compression, then heuristics, then truncation."""
    budget = config.carryover_tokens
    if marker_state is not None:
        sanitized = re.sub(r"[<>\[\]@-]{3,}", "", marker_state).strip()
        if len(sanitized) >= 24 and estimate_tokens(sanitized) <= budget:
            return sanitized, "marker"
    if compressor is not None:
        try:
            prompt = (f"Previous state:\n{previous or '(none)'}\n\nLatest chunk (tail):\n"
                      f"{chunk_text[-2000:]}\n\nCompress into 3-5 critical points as a labeled "
                      "block:\nCOMPLETED: ...\nREMAINING: ...\nDECISIONS: ...\nCONTEXT: ...")
            text = compressor(prompt, "You compress reasoning state. Be terse and lossless "
                                      "on decisions.").strip()
            if len(text) >= 24 and estimate_tokens(text) <= budget:
                return text, "llm"
        except Exception:
            pass    # the chain must survive a model outage
    phrases = [m.group(1).strip() for m in _KEY_PHRASE.finditer(chunk_text)][:5]
    if phrases and estimate_tokens("; ".join(phrases)) <= budget:
        return "; ".join(phrases), "heuristic"
    tail: list[str] = []
    for word in reversed((chunk_text or previous).split()):
        if estimate_tokens(" ".join([word, *tail])) > budget:
            break
        tail.insert(0, word)
    return " ".join(tail), "truncation"


def canonical_block(completed: str, remaining: str, decisions: str, context: str) -> str:
    return (f"COMPLETED: {completed}\nREMAINING: {remaining}\nDECISIONS: {decisions}\n"
            f"CONTEXT: {context}")


# -- prompt builder (section 4.4) ---------------------------------------------

MARKER_RULES = (f"End every non-final chunk with {STATE} followed by a concise state block:\n"
                "COMPLETED: ...\nREMAINING: ...\nDECISIONS: ...\nCONTEXT: ...\n"
                f"End the final chunk with {FINAL}. Exactly one marker per chunk. "
                "Never narrate the markers.")
OPENERS = {
    "ARCHITECT": ("You are executing a long build task in bounded chunks. Analyze the goal, "
                  "produce a plan, and start implementation."),
    "RESEARCH": ("You are executing recursive research in bounded chunks. Log every action as "
                 "'>> action'. Produce multi-file style output where useful and keep a "
                 "research-tree state snapshot."),
    # RedFlag: one RHP section read window by window (rows out, table state carried).
    "EXTRACT": ("You are extracting structured rows from a long document in bounded parts. "
                "Each part arrives as an attachment. Return the rows found in this part as "
                "instructed, with page numbers. Carry only what the next part needs (unit, "
                "column order, open table, partial row) in the state block, never the rows."),
}


def build_prompt(*, mode: str, goal: str, step: int, carryover: str, overlap_text: str,
                 memory_context: str, attachments: list[dict]) -> str:
    parts: list[str] = []
    if step == 1:
        if memory_context:
            parts += [memory_context.strip(), ""]
        parts += [OPENERS[mode], "", f"User Request: {goal}"]
    else:
        parts += [f"Continuing a chunked {mode.lower()} run. Goal (truncated): {goal[:200]}", "",
                  f"[CARRYOVER STATE]\n{carryover}"]
        if overlap_text:
            parts += ["", f"[OVERLAP]\n{overlap_text}"]
        parts += ["", "Continue exactly where the state leaves off. Do not repeat completed work."]
    for a in attachments:
        parts += ["", f"[ATTACHMENT {a['name']}]\n{a['content']}"]
    parts += ["", MARKER_RULES]
    return "\n".join(parts)


def overlap_slice(previous_cleaned: str, config: ChunkConfig) -> str:
    if config.overlap_tokens <= 0 or not previous_cleaned:
        return ""
    return previous_cleaned[-config.overlap_tokens * 4:]


# -- the engine ---------------------------------------------------------------

_RUN_SEQ = itertools.count(1)
_RUN_SEQ_LOCK = threading.Lock()


class Markovian:
    def __init__(self, chunk_config: ChunkConfig | dict | None = None, *,
                 generator: Generator | None = None, compressor: Compressor | None = None,
                 clock: Callable[[], float] | None = None, history_path: Path | None = None,
                 on_event: Callable[[dict], None] | None = None):
        self.config = resolve_config(chunk_config)
        self.generator = generator
        self.compressor = compressor
        self.clock = clock or (lambda: time.time() * 1000)
        self.history_path = history_path
        self.on_event = on_event

    def run(self, goal: str, *, generator: Generator | None = None, mode: str = "ARCHITECT",
            windows: list[str] | None = None, attachments: list[dict] | None = None,
            memory_context: str = "", system: str | None = None, reflect: bool = True,
            stop_when: Callable[[int, str], bool] | None = None,
            aborted: Callable[[], bool] = lambda: False,
            on_chunk: Callable[[ChunkInfo], None] | None = None,
            run_id: str = "", agent_id: str = "") -> MarkovianRun:
        """Run one chain. With `windows`, step i gets window i and the run ends after the last."""
        if not goal:
            raise ValueError("Markovian: run needs a goal")
        if mode not in OPENERS:
            raise ValueError(f"Markovian: unknown mode {mode!r}")
        gen = generator or self.generator
        if gen is None:
            raise ValueError("Markovian: provide a generator")
        cfg = self.config
        with _RUN_SEQ_LOCK:
            seq = next(_RUN_SEQ)
        start = self.clock()
        run = MarkovianRun(id=f"run-{seq}", timestamp=start, prompt=goal[:500], mode=mode,
                           chunk_config=replace(cfg), run_id=run_id, agent_id=agent_id)
        cleaned_parts: list[str] = []
        carryover, previous_cleaned, done = "", "", False
        measured = True
        self._emit("initializing", run)
        try:
            while len(run.chunks) < cfg.max_chunks and not done:
                if aborted():
                    run.status = "aborted"
                    break
                step = len(run.chunks) + 1
                step_attachments = (attachments or []) if step == 1 else []
                if windows is not None:
                    step_attachments = step_attachments + [
                        {"name": f"part {step} of {len(windows)}", "content": windows[step - 1]}]
                self._emit("generating", run, chunk=step)
                prompt = build_prompt(mode=mode, goal=goal, step=step, carryover=carryover,
                                      overlap_text=overlap_slice(previous_cleaned, cfg),
                                      memory_context=memory_context,
                                      attachments=step_attachments)
                reply = gen(prompt, step_attachments, system, None, None)
                text = reply.get("text") or ""
                parsed = parse_markers(text)
                usage = (reply.get("metadata") or {}).get("usage") or {}
                if usage.get("output_tokens"):
                    tokens = int(usage["output_tokens"])
                else:
                    tokens, measured = estimate_tokens(parsed.cleaned), False
                chunk = ChunkInfo(step, tokens, carryover, parsed.cleaned)
                run.chunks.append(chunk)        # paid for: keep it even if we stop now
                cleaned_parts.append(parsed.cleaned)
                previous_cleaned = parsed.cleaned
                if aborted():
                    run.status = "aborted"
                    break
                self._emit("compressing", run, chunk=step)
                carryover, chunk.cascade_level = next_carryover(
                    parsed.state, carryover, text, cfg, self.compressor)
                if on_chunk:
                    on_chunk(chunk)
                # With fixed windows every page is read: a model that calls the table done on
                # a summary page would otherwise skip the pages that hold its rows.
                done = ((windows is not None and step >= len(windows)) or (windows is None and (
                    parsed.done or (stop_when is not None and stop_when(step, carryover)))))
            if not done and run.status == "completed" and len(run.chunks) >= cfg.max_chunks:
                run.status = "max-chunks"
        except Exception as e:      # a failed chunk keeps the partial run (section 11)
            run.status, run.error = "errored", str(e)
            self._emit("error", run, error=str(e)[:200])
        run.token_label = "measured" if measured and run.chunks else "estimated"
        run.output = "\n\n".join(cleaned_parts)
        eff = run_efficiency(run.chunks, run.chunk_config)
        run.total_tokens_used = eff["totalTokensUsed"]
        run.total_tokens_saved = eff["tokensSaved"]
        run.efficiency_percent = eff["efficiencyPercent"]
        if reflect and run.status == "completed" and run.chunks:
            self._emit("reflecting", run)
            try:
                blocks = run.output.count("```") // 2
                prompt = (f"Reflect on this completed run.\nGoal: {goal[:200]}\nFinal state: "
                          f"{carryover}\nOutput (truncated): {run.output[:4000]}\nCode blocks: "
                          f"{blocks}\nVerdict: ratify if the goal is met, refine with "
                          "concrete gaps otherwise.")
                reply = gen(prompt, [], "You are the reflection pass. Be brief and concrete.",
                            None, None)
                run.reflection = clean_markers(reply.get("text") or "")
            except Exception as e:      # reflection never fails the run (sections 7, 11)
                run.reflection = None
                run.error = f"reflection failed (run preserved): {e}"
        run.duration_ms = self.clock() - start
        self._emit("error" if run.status == "errored" else "complete", run)
        self._persist(run)
        return run

    def _emit(self, phase: str, run: MarkovianRun, **detail) -> None:
        if self.on_event:
            self.on_event({"engine": "markovian", "phase": phase, "markovian_id": run.id,
                           "run_id": run.run_id, "agent_id": run.agent_id,
                           "chunks": len(run.chunks), "status": run.status, **detail})

    def _persist(self, run: MarkovianRun) -> None:
        if not self.history_path:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = asdict(run)
        for c in record["chunks"]:
            c["content"] = c["content"][:500]      # keep the log small
        with self.history_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
