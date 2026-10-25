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


def _world_call(world, method: str, args: list) -> Any:
    """One World Model call in the reference's (camelCase) shape."""
    if method == "upsertEntity":
        a = args[0]
        return world.upsert_entity(type=a.get("type"), source=a.get("source"),
                                   external_key=a.get("externalKey"), name=a.get("name"),
                                   attrs=a.get("attrs"), state=a.get("state", "active"))
    if method == "upsertContext":
        a = args[0]
        return world.upsert_context(type=a.get("type"), source=a.get("source"),
                                    external_key=a.get("externalKey"), attrs=a.get("attrs"),
                                    parent=a.get("parent"))
    if method == "relate":
        a = args[0]
        return world.relate(from_=a["from"], type=a["type"], to=a["to"], source=a.get("source"),
                            context=a.get("context"), attrs=a.get("attrs"),
                            valid_from=a.get("validFrom"), valid_to=a.get("validTo"))
    if method == "assertFact":
        a = args[0]
        return world.assert_fact(about=a["about"], predicate=a["predicate"], value=a["value"],
                                 source=a.get("source"), confidence=a.get("confidence", 0.5),
                                 context=a.get("context"))
    if method == "ingest":
        return world.ingest(args[0])
    if method == "approveMerge":
        return world.approve_merge(*args)
    if method == "rejectMerge":
        return world.reject_merge(*args)
    if method == "stateShapeCanonical":
        return world.state_shape_canonical()
    if method == "stateShape":
        return world.state_shape()
    if method in ("audit", "events", "mergeQueue"):
        return {"audit": world.audit, "events": world.events, "mergeQueue": world.merge_queue}[method]()
    if method == "graph.neighborsOf":
        opts = args[1] if len(args) > 1 and args[1] else {}
        return world.view("graph").neighbors_of(args[0], via=opts.get("via"),
                                                in_context=opts.get("inContext"), at=opts.get("at"))
    if method == "graph.incoming":
        opts = args[1] if len(args) > 1 and args[1] else {}
        return world.view("graph").incoming(args[0], via=opts.get("via"))
    if method == "graph.entity":
        return world.view("graph").entity(args[0])
    if method == "graph.query":
        return world.view("graph").query(type=(args[0] or {}).get("type"))
    if method == "graph.traverse":
        o = args[1]
        return world.view("graph").traverse(args[0], via=o["via"], direction=o["direction"],
                                            max_depth=o.get("maxDepth", 5))
    if method == "facts.about":
        return world.view("facts").about(args[0])
    if method == "snapshot.entityAt":
        return world.view("snapshot").entity_at(args[0], args[1])
    raise ValueError(f"world-model: unknown method {method!r}")


def world_model(op: str, req: dict) -> Any:
    from datetime import datetime

    from agent.engines import world as w
    if op == "entityIdFromName":
        return w.entity_id_from_name(*req["args"])
    if op == "normalizeName":
        return w.normalize_name(*req["args"])
    if op == "decide":
        return w.decide(*req["args"])
    if op == "replay":
        opts = req.get("options") or {}
        at = opts.get("clockAt")
        clock = (lambda: datetime.fromisoformat(at.replace("Z", "+00:00"))) if at else None
        world = w.WorldModel(opts.get("namespace", "bridge"), clock=clock,
                             conflict_policy=opts.get("conflictPolicy", "most-recent-wins"),
                             source_priority=opts.get("sourcePriority"))
        for method, args in req.get("ops", []):
            _world_call(world, method, args)
        method, args = req["call"]
        return _world_call(world, method, args)
    raise ValueError(f"world-model: unknown op {op!r}")


ENGINES = {"markovian": markovian, "world-model": world_model}


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
