"""One investigation run, shared by run.py and the FastAPI app.

v0 path: Intake -> Banker track record -> Scorer -> Reporter, all through the
Data Port (Zetaris live, or the SQLite stand-in) and the trained scorer.
Extraction, the Skeptic and the Planner's escalation plug in here as they land.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict

from redflag.common.trace import Tracer
from redflag.config import Settings
from redflag.data.clients import MockZetaris, ZetarisRestClient
from redflag.data.port import DataPort
from redflag.features import compute_features
from redflag.scoring import load_scorer, score

DISCLAIMER = ("RedFlag gives a risk rating with evidence, not investment advice. It never "
              "says apply, avoid, buy or sell. Links to regulatory orders mean an entity "
              "was named in that order, not that this company did anything wrong.")
AGENTS = [
    {"name": "Intake", "role": "Identifies the IPO and fixes as_of to its bid opening date"},
    {"name": "BankerTrackRecord", "role": "Looks up the lead manager's past SME IPO outcomes "
                                          "known before as_of, through Zetaris"},
    {"name": "Scorer", "role": "Applies the backtested model to the features (no LLM)"},
    {"name": "Reporter", "role": "Writes the report card from the evidence"},
]


class RunError(RuntimeError):
    def __init__(self, type_: str, message: str, recoverable: bool):
        super().__init__(message)
        self.type, self.recoverable = type_, recoverable


def data_client(cfg: Settings, sample_mode: bool):
    if sample_mode:
        return MockZetaris(cfg.data / "sample" / "archive_sample.db"), "sample (mock-sqlite)"
    if os.environ.get("ZETARIS_REST_URL") and os.environ.get("ZETARIS_API_KEY"):
        return ZetarisRestClient(), "zetaris"
    return MockZetaris(cfg.archive_db), "local archive (mock-sqlite; Zetaris not configured)"


def investigate(cfg: Settings, ipo_id: str, *, run_id: str | None = None,
                sample_mode: bool | None = None) -> dict:
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    sample_mode = (os.environ.get("SAMPLE_MODE", "false").lower() == "true"
                   if sample_mode is None else sample_mode)
    started = time.perf_counter()
    tracer = Tracer(run_id, cfg.logs)
    client, source = data_client(cfg, sample_mode)
    queries: list[dict] = []

    def on_query(result):
        queries.append({"template": result.template, "params": result.params,
                        "rows": len(result.rows), "ref": result.ref,
                        "source": result.source, "guard_dropped": result.dropped_by_guard})
        tracer.event("DataPort", "data.query", f"{result.template} -> {len(result.rows)} rows",
                     input_summary=str(result.params), ref=result.ref, source=result.source)

    tracer.event("Intake", "start", f"investigating {ipo_id}", input_summary=ipo_id,
                 data_source=source, sample_mode=sample_mode)
    meta_rows = DataPort(client, None, on_query=on_query).run("ipo_meta", ipo_id=ipo_id).rows
    if not meta_rows:
        tracer.event("Intake", "error", f"unknown IPO {ipo_id}", status="error")
        raise RunError("UnknownIPO", f"{ipo_id} is not in the archive", recoverable=False)
    meta = meta_rows[0]
    as_of = meta["open_date"]
    tracer.event("Intake", "delegate_task", f"as_of fixed to {as_of}",
                 target_agent="BankerTrackRecord")

    port = DataPort(client, as_of, on_query=on_query)
    feats = compute_features(ipo_id, as_of, port)
    tracer.event("BankerTrackRecord", "compute_features",
                 f"banker crash rate {fmt(feats['f_banker_crash'])} over "
                 f"{fmt(feats['f_banker_n'], 0)} past IPOs (base rate {fmt(feats['base_rate'])})",
                 target_agent="Scorer", features=feats)

    scorer = load_scorer(cfg.data / "model" / "scorer.json")
    result = score(feats, scorer)
    tracer.event("Scorer", "score", f"{result.band} (p={result.p_adverse})",
                 confidence=result.p_adverse, target_agent="Reporter", **asdict(result))

    reasons, not_checked = [], [
        "RHP financials (F1-F9): extraction not yet wired in",
        "SEBI watchlist links (F11): pending human review of order links",
        "DRHP drift, anchors, market maker, ratings (F12-F14)"]
    track = [q for q in queries if q["template"] == "banker_track_record"]
    if feats["f_banker_crash"] is not None:
        reasons.append({
            "flag": "F10", "title": "Lead manager track record",
            "text": (f"{meta['banker_raw']}: {fmt(feats['f_banker_n'], 0)} earlier SME IPOs "
                     f"with a known 6-month outcome before {as_of}; adjusted share that "
                     f"ended badly {fmt(feats['f_banker_crash'])} against an average of "
                     f"{fmt(feats['base_rate'])}."
                     + (" Thin history: fewer than 5 known outcomes."
                        if feats["f_banker_low_history"] else "")),
            "evidence": [q["ref"] for q in track]})
    output = {"ipo": {k: str(v) for k, v in meta.items()}, "as_of": as_of.isoformat(),
              "rating": result.band, "p_adverse": result.p_adverse,
              "model_version": result.version, "reasons": reasons,
              "skeptic_notes": [], "cleared_checks": [], "questions": [],
              "not_checked": not_checked, "data_source": source, "disclaimer": DISCLAIMER}
    tracer.event("Reporter", "final_synthesis", f"rating {result.band} with {len(reasons)} reasons")
    return {"run_id": run_id, "status": "success", "output": output, "agents": AGENTS,
            "trace_id": f"trace-{run_id}", "log_file": str(tracer.path),
            "execution_time_seconds": round(time.perf_counter() - started, 2)}


def fmt(value, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}" if digits else str(int(value))
