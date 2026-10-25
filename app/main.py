"""RedFlag HTTP API (FastAPI).

    .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
    # or: .venv/bin/python run.py serve

POST /run takes and returns JSON in the shape the hackathon's Technical
Execution Requirements describe; GET /health reports what is configured.
"""
from __future__ import annotations

import os
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agent.config import settings
from agent.tools_zetaris import LeakageError, TruncatedResult
from agent.main import RunError, investigate

app = FastAPI(title="RedFlag", version="0.1.0",
              description="Evidence-backed risk ratings for Indian SME IPOs")


class RunInput(BaseModel):
    ipo_id: str = Field(..., examples=["NSE-2023-SYNOPTICS"])


class RunOptions(BaseModel):
    sample_mode: bool | None = None


class RunRequest(BaseModel):
    run_id: str | None = None
    input: RunInput
    options: RunOptions = RunOptions()


@app.get("/health")
def health() -> dict:
    cfg = settings()
    return {
        "status": "ok",
        "sample_mode": os.environ.get("SAMPLE_MODE", "false").lower() == "true",
        "archive_db": cfg.archive_db.exists(),
        "sample_db": (cfg.data / "sample" / "archive_sample.db").exists(),
        "scorer": (cfg.data / "model" / "scorer.json").exists(),
        "zetaris_configured": bool(os.environ.get("ZETARIS_REST_URL")
                                   and os.environ.get("ZETARIS_API_KEY")),
        "meterless_configured": bool(os.environ.get("METERLESS_BASE_URL")),
    }


@app.post("/run")
def run(req: RunRequest):
    run_id = req.run_id or f"run-{uuid.uuid4().hex[:8]}"
    try:
        return investigate(settings(), req.input.ipo_id, run_id=run_id,
                           sample_mode=req.options.sample_mode)
    except RunError as e:
        return _error(run_id, e.type, str(e), e.recoverable, 404 if e.type == "UnknownIPO" else 500)
    except FileNotFoundError as e:
        return _error(run_id, "NotReady", str(e), True, 503)
    except (LeakageError, TruncatedResult) as e:
        return _error(run_id, type(e).__name__, str(e), False, 500)
    except Exception as e:      # a structured error, never a silent crash
        return _error(run_id, type(e).__name__, str(e)[:300], True, 502)


def _error(run_id: str, type_: str, message: str, recoverable: bool, code: int):
    return JSONResponse(status_code=code, content={
        "run_id": run_id, "status": "error",
        "error": {"type": type_, "message": message, "recoverable": recoverable},
        "trace_id": f"trace-{run_id}"})
