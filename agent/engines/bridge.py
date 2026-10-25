"""JSON-lines bridge that lets the official TypeScript conformance runners
test RedFlag's Python engines (agent/engines/conformance/<engine>/index.ts).

    python -m agent.engines.bridge <engine> <op>

One request object arrives on stdin. Pure functions answer with one line,
`{"ok": true, "result": ...}` or `{"ok": false, "error": "..."}`. Ops that
need the caller's model (a Markovian run) write `{"op": "generate", "args":
[...]}` lines while they work and read one `{"ok": ..., "result" | "error"}`
reply line per request, then finish with the result line. Nothing else may be
printed to stdout.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _remote(op: str) -> Callable[..., Any]:
    """A function whose calls are answered by the process on the other end."""
    def call(*args):
        _send({"op": op, "args": list(args)})
        reply = json.loads(sys.stdin.readline() or "null")
        if not reply or not reply.get("ok"):
            raise RuntimeError((reply or {}).get("error", f"{op}: no reply"))
        return reply.get("result")
    return call


def markovian(op: str, req: dict) -> Any:
    from agent.engines import markovian as m
    if op == "validate":
        return m.resolve_config(req.get("chunkConfig")).to_wire()
    if op == "modelTotals":
        return m.model_totals(req["n"], req.get("constants"))
    if op == "runEfficiency":
        cfg = m.ChunkConfig(**{m._WIRE_KEYS[k]: v for k, v in req["config"].items()
                              if k in m._WIRE_KEYS})
        return m.run_efficiency(req["chunks"], cfg)
    if op == "run":
        generate = _remote("generate")

        def generator(prompt, attachments=None, system=None, on_stream=None, abort=None):
            return generate(prompt, attachments or [], system) or {}

        engine = m.Markovian(req.get("chunkConfig"))
        opts = req.get("options") or {}
        run = engine.run(opts.get("goal") or opts.get("prompt") or "",
                         generator=generator, mode=opts.get("mode", "ARCHITECT"),
                         attachments=opts.get("attachments"),
                         memory_context=opts.get("memoryContext", ""),
                         reflect=opts.get("reflect", True) is not False)
        return run.to_wire()
    raise ValueError(f"markovian: unknown op {op!r}")


ENGINES = {"markovian": markovian}


def main(argv: list[str]) -> int:
    engine, op = argv
    req = json.loads(sys.stdin.readline() or "{}")
    try:
        result = ENGINES[engine](op, req)
    except Exception as e:
        _send({"op": "result", "ok": False, "error": str(e), "type": type(e).__name__})
        return 0
    _send({"op": "result", "ok": True, "result": result})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
