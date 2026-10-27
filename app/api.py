"""The web app's API (/api/*), used by the frontend in web/.

Runs start in the background and stream their trace as server-sent events,
so the browser shows each agent's step while a live run (about three
minutes) is under way. Everything here reads what a run already wrote
(output/<run_id>/, logs/trace-<run_id>.jsonl) or the archive; the run itself
is the same `investigate` that POST /run and `run.py investigate` call.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from agent import uploads
from agent.config import settings
from agent.main import RunError, investigate
from agent.rhp import load_pages
from agent.scoring import explain, load_scorer

router = APIRouter(prefix="/api")


def _output() -> Path:
    return settings().data.parent / "output"        # where the Reporter writes (reporter.py)


# One live run at a time (Zetaris, Nightshift and the shared run memory are
# single resources); sample runs are offline and can overlap.
_live = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-run")
_sample = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sample-run")
_runs: dict[str, dict] = {}
_lock = threading.Lock()
LIVE = threading.Lock()     # held by every live run, here and in POST /run (app/main.py)


def _db() -> sqlite3.Connection:
    cfg = settings()
    path = cfg.archive_db if cfg.archive_db.exists() else cfg.data / "sample" / "archive_sample.db"
    if not path.exists():
        raise HTTPException(503, "no archive: run the offline pipeline or add data/sample")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sample_ids() -> set[str]:
    path = settings().data / "sample" / "archive_sample.db"
    if not path.exists():
        return set()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return {r[0] for r in conn.execute("SELECT ipo_id FROM ipos")}


def _recorded_ids() -> set[str]:
    """The demo IPOs: recorded model replies let sample mode reproduce their live runs."""
    from pipeline.publish import DEFAULT_SAMPLE
    return {i.strip() for i in os.environ.get("SAMPLE_IPOS", DEFAULT_SAMPLE).split(",")}


# -- catalogue ---------------------------------------------------------------

@router.get("/ipos")
def ipos(q: str = "", limit: int = 50, recorded: bool = False) -> dict:
    """The IPO catalogue from the archive, with each one's outcome for hindsight."""
    like = f"%{q.strip().lower()}%"
    with _db() as conn:
        rows = conn.execute(
            """SELECT i.ipo_id, i.company, i.symbol, i.exchange, i.open_date, i.issue_price,
                      i.banker_raw, i.industry,
                      EXISTS(SELECT 1 FROM docs d WHERE d.ipo_id = i.ipo_id
                             AND d.doc_type = 'rhp' AND d.status = 'ok') AS has_rhp,
                      l.ret_6m, l.adverse
               FROM ipos i LEFT JOIN labels l ON l.ipo_id = i.ipo_id
               WHERE lower(i.company) LIKE :q OR lower(i.symbol) LIKE :q
                     OR lower(i.ipo_id) LIKE :q OR lower(coalesce(i.banker_raw, '')) LIKE :q
               ORDER BY i.open_date DESC LIMIT :n""",
            {"q": like, "n": 100_000 if recorded else max(1, min(limit, 1000))}).fetchall()
        total = conn.execute("SELECT count(*) FROM ipos").fetchone()[0]
    sample, demos = _sample_ids(), _recorded_ids()
    items = [{**dict(r), "has_rhp": bool(r["has_rhp"]), "in_sample": r["ipo_id"] in sample,
              "recorded": r["ipo_id"] in demos} for r in rows]
    return {"total": total, "items": [i for i in items if i["recorded"]] if recorded else items}


@router.get("/ipos/{ipo_id}")
def ipo(ipo_id: str) -> dict:
    if uploads.is_upload(ipo_id):
        return _upload_detail(_upload(ipo_id))
    with _db() as conn:
        row = conn.execute("SELECT * FROM ipos WHERE ipo_id = ?", (ipo_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"unknown IPO {ipo_id}")
        label = conn.execute("SELECT * FROM labels WHERE ipo_id = ?", (ipo_id,)).fetchone()
        docs = [dict(d) for d in conn.execute(
            "SELECT doc_type, pages, text_pages, status, url FROM docs WHERE ipo_id = ?",
            (ipo_id,))]
    keep = ("ipo_id", "company", "exchange", "symbol", "industry", "issue_type", "issue_price",
            "face_value", "lot_size", "open_date", "close_date", "listing_date", "issue_size_cr",
            "banker_raw", "registrar_raw", "market_maker_raw", "rhp_url", "final_subscription_x")
    meta = {k: row[k] for k in keep if k in row.keys()}
    return {"ipo": meta, "docs": docs, "in_sample": ipo_id in _sample_ids(),
            "recorded": ipo_id in _recorded_ids(),
            "outcome": dict(label) if label else None,
            "runs": [r for r in _history() if r["ipo_id"] == ipo_id]}


@router.get("/ipos/{ipo_id}/pages/{page}")
def rhp_page(ipo_id: str, page: int) -> dict:
    """One page of the IPO's RHP text, for the evidence viewer (rhp:pN)."""
    cfg = settings()
    if uploads.is_upload(ipo_id):
        pages = load_pages(cfg.cache / "pages", _upload(ipo_id)["sha256"])
        if not 1 <= page <= len(pages):
            raise HTTPException(404, f"the RHP has {len(pages)} pages")
        return {"ipo_id": ipo_id, "page": page, "pages": len(pages), "text": pages[page - 1]}
    paths = [cfg.archive_db, cfg.data / "sample" / "archive_sample.db"]
    for db in (p for p in paths if p.exists()):
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            hit = conn.execute("SELECT sha256 FROM docs WHERE ipo_id = ? AND doc_type = 'rhp' "
                               "AND status = 'ok'", (ipo_id,)).fetchone()
        if not hit:
            continue
        for folder in (cfg.cache / "pages", cfg.data / "sample" / "pages"):
            pages = load_pages(folder, hit[0])
            if pages:
                if not 1 <= page <= len(pages):
                    raise HTTPException(404, f"the RHP has {len(pages)} pages")
                return {"ipo_id": ipo_id, "page": page, "pages": len(pages),
                        "text": pages[page - 1]}
    raise HTTPException(404, "the RHP text is not available here")


@router.get("/sebi/{order_id}")
def sebi_order(order_id: str) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT order_id, order_date, order_type, title, url, pdf_url "
                           "FROM sebi_orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"unknown SEBI order {order_id}")
        named = [dict(r) for r in conn.execute(
            "SELECT entity_name, entity_role, finding, action, period FROM sebi_order_entity "
            "WHERE order_id = ? LIMIT 25", (order_id,))]
    return {**dict(row), "named": named}


# -- uploaded RHPs (upcoming IPOs) ---------------------------------------------

def _upload(upload_id: str) -> dict:
    record = uploads.load(settings(), upload_id)
    if record is None:
        raise HTTPException(404, f"unknown upload {upload_id}")
    return record


def _upload_view(record: dict) -> dict:
    from agent.prefill import LABEL
    sources = {k: LABEL.get(v, v) for k, v in (record.get("field_sources") or {}).items()}
    return {**record, "missing": uploads.missing(record), "required": list(uploads.REQUIRED),
            "exchanges": list(uploads.EXCHANGES), "field_sources": sources}


def _upload_detail(record: dict) -> dict:
    """An upload in the shape of GET /ipos/{id}, so the IPO page can show it."""
    f = record["fields"]
    meta = {"ipo_id": record["upload_id"], "company": f.get("company") or record["filename"],
            "exchange": f.get("exchange"), "symbol": "", "issue_price": f.get("issue_price"),
            "lot_size": f.get("lot_size"), "open_date": f.get("open_date"),
            "banker_raw": ", ".join(f.get("lead_managers") or []) or None,
            "registrar_raw": f.get("registrar"), "market_maker_raw": f.get("market_maker"),
            "issue_type": record["detected"].get("issue_type"), "rhp_url": None,
            "final_subscription_x": (record.get("source") or {}).get("subscription_x")}
    return {"ipo": meta, "docs": [{"doc_type": "rhp", "pages": record["pages"],
                                   "text_pages": record["text_pages"], "status": record["status"],
                                   "url": ""}],
            "in_sample": False, "recorded": False, "outcome": None, "uploaded": True,
            "upload": _upload_view(record),
            "runs": [r for r in _history() if r["ipo_id"] == record["upload_id"]]}


def _prefill_tools():
    """(fetcher, gateway) for filling in an upload: the exchanges' sites and Nightshift."""
    from agent.llm import gateway_from_env
    from pipeline.common.http import Fetcher
    cfg = settings()
    gateway = gateway_from_env(cfg.data)
    return Fetcher(cfg.http_min_interval_s, 15), gateway if gateway.providers else None


def _create_upload(body: bytes, name: str) -> dict:
    """Store the PDF and fill in its details from the archive, the exchange, the cover and AI."""
    fetcher, gateway = _prefill_tools()
    try:
        return uploads.create(settings(), body, name, fetcher=fetcher, gateway=gateway)
    finally:
        if fetcher is not None:
            fetcher.close()


@router.post("/uploads", status_code=201)
async def create_upload(request: Request) -> dict:
    """The RHP PDF as the request body (Content-Type: application/pdf)."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "send the RHP PDF as the request body")
    name = unquote(request.headers.get("x-filename", "rhp.pdf"))
    try:
        record = await asyncio.to_thread(_create_upload, body, name)
    except uploads.UploadError as e:
        raise HTTPException(422, str(e)) from e
    return _upload_view(record)


@router.get("/uploads")
def list_uploads() -> dict:
    return {"items": [_upload_view(r) for r in uploads.list_all(settings())]}


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str) -> dict:
    return _upload_view(_upload(upload_id))


@router.patch("/uploads/{upload_id}")
def edit_upload(upload_id: str, changes: dict) -> dict:
    _upload(upload_id)
    try:
        return _upload_view(uploads.update(settings(), upload_id, changes))
    except uploads.UploadError as e:
        raise HTTPException(422, str(e)) from e


# -- open and upcoming IPOs (pipeline/upcoming.py) -----------------------------

_refresh = {"running": False, "error": None}


@router.get("/upcoming")
def upcoming() -> dict:
    """Exchange-listed open and upcoming SME IPOs whose RHPs are loaded."""
    from pipeline.upcoming import last_refresh
    cfg = settings()
    items = [_upload_view(r) for r in uploads.list_all(cfg) if r.get("source")]
    today = time.strftime("%Y-%m-%d")
    items = [i for i in items if (i["source"].get("close_date") or "9999") >= today]
    items.sort(key=lambda i: i["fields"].get("open_date") or "9999")
    return {"items": items, "last": last_refresh(cfg), "refreshing": _refresh["running"],
            "error": _refresh["error"]}


@router.post("/upcoming/refresh", status_code=202)
def refresh_upcoming() -> dict:
    with _lock:
        if _refresh["running"]:
            return {"refreshing": True}
        _refresh.update(running=True, error=None)

    def work():
        from pipeline.common.http import Fetcher
        from pipeline.upcoming import refresh
        try:
            refresh(settings(), Fetcher(), log=lambda *_: None)
        except Exception as e:      # shown on the page
            _refresh["error"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            _refresh["running"] = False
    threading.Thread(target=work, name="upcoming-refresh", daemon=True).start()
    return {"refreshing": True}


# -- runs --------------------------------------------------------------------

class StartRun(BaseModel):
    ipo_id: str
    sample_mode: bool = False
    auto: bool = False          # live when Zetaris and Nightshift answer, else offline


@router.post("/runs", status_code=202)
def start_run(req: StartRun) -> dict:
    if uploads.is_upload(req.ipo_id) and (gaps := uploads.missing(_upload(req.ipo_id))):
        raise HTTPException(422, f"confirm these details first: {', '.join(gaps)}")
    if req.auto:
        st = status()
        live = bool(st["zetaris"].get("ok") and st["meterless"].get("ok"))
        req = StartRun(ipo_id=req.ipo_id, sample_mode=not live)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    with _lock:
        busy = [r for r in _runs.values() if r["status"] == "running" and not r["sample_mode"]]
        if not req.sample_mode and (busy or LIVE.locked()):
            where = busy[0]["run_id"] if busy else "POST /run"
            raise HTTPException(409, f"a live run is already in progress ({where}); "
                                     "wait for it or use sample mode")
        _runs[run_id] = {"run_id": run_id, "ipo_id": req.ipo_id, "sample_mode": req.sample_mode,
                         "status": "running", "started_at": time.time(), "error": None}
    (_sample if req.sample_mode else _live).submit(_execute, run_id, req.ipo_id, req.sample_mode)
    return {"run_id": run_id, "status": "running"}


def _execute(run_id: str, ipo_id: str, sample_mode: bool) -> None:
    state = _runs[run_id]
    if not sample_mode:
        LIVE.acquire()
    try:
        result = investigate(settings(), ipo_id, run_id=run_id, sample_mode=sample_mode)
        state.update(status=result["status"], finished_at=time.time())
    except RunError as e:
        state.update(status="error", finished_at=time.time(),
                     error={"type": e.type, "message": str(e), "recoverable": e.recoverable})
    except Exception as e:      # shown in the browser, never a silent crash
        state.update(status="error", finished_at=time.time(),
                     error={"type": type(e).__name__, "message": str(e)[:300],
                            "recoverable": True})
    finally:
        if not sample_mode:
            LIVE.release()


def _history() -> list[dict]:
    out = []
    for path in _output().glob("*/result.json"):
        try:
            r = json.loads(path.read_text())
            o = r["output"]
            out.append({"run_id": r["run_id"], "ipo_id": o["ipo"]["ipo_id"],
                        "company": o["ipo"]["company"], "status": r["status"],
                        "rating": o["rating"], "p_adverse": o["p_adverse"], "depth": o["depth"],
                        "data_source": o["data_source"],
                        "sample_mode": "sample" in o["data_source"],
                        "execution_time_seconds": r["execution_time_seconds"],
                        "finished_at": path.stat().st_mtime,
                        "recorded": r["run_id"].startswith("rec-")})
        except (OSError, ValueError, KeyError, TypeError):
            continue            # one unreadable or older-format run never hides the rest
    return sorted(out, key=lambda r: -r["finished_at"])


@router.get("/runs")
def runs() -> dict:
    done = _history()
    seen = {r["run_id"] for r in done}
    active = [{**r, "company": None} for r in _runs.values()
              if r["run_id"] not in seen and r["status"] in ("running", "error")]
    return {"active": sorted(active, key=lambda r: -r["started_at"]), "runs": done}


def _result_path(run_id: str) -> Path:
    if not run_id.replace("-", "").isalnum():
        raise HTTPException(400, "bad run id")
    return _output() / run_id / "result.json"


@router.get("/runs/{run_id}")
def run_status(run_id: str) -> dict:
    path = _result_path(run_id)
    state = _runs.get(run_id)
    if path.exists():
        result = json.loads(path.read_text())
        return {"run_id": run_id, "status": result["status"], "result": result,
                **({"started_at": state["started_at"]} if state else {})}
    if state:
        return {**state, "result": None}
    raise HTTPException(404, f"unknown run {run_id}")


@router.get("/runs/{run_id}/case")
def run_case(run_id: str) -> dict:
    path = _result_path(run_id).with_name("case_file.json")
    if not path.exists():
        raise HTTPException(404, "no case file yet")
    return json.loads(path.read_text())


@router.get("/runs/{run_id}/report.md", response_class=PlainTextResponse)
def run_report(run_id: str) -> str:
    path = _result_path(run_id).with_name("report.md")
    if not path.exists():
        raise HTTPException(404, "no report yet")
    return path.read_text()


@router.get("/runs/{run_id}/explain")
def run_explain(run_id: str) -> dict:
    """How each feature moved the probability (the Scorer's own arithmetic)."""
    case = run_case(run_id)
    scorer = load_scorer(settings().data / "model" / "scorer.json")
    score = case.get("score") or {}         # None when the run stopped before the Scorer
    features = score.get("features") or {}
    return {"intercept": scorer["intercept"], "bands": scorer["bands"],
            "version": scorer["version"], "p_adverse": score.get("p_adverse"),
            "features": explain(features, scorer)}


@router.get("/runs/{run_id}/lead-manager")
def run_lead_manager(run_id: str) -> dict:
    """The lead manager's earlier SME IPOs with a known six-month result before the bid,
    for the report's track-record chart (the same rule as the F10 check)."""
    result = json.loads(_result_path(run_id).read_text())
    o = result["output"]
    ipo_id, as_of = o["ipo"]["ipo_id"], o["as_of"]
    with _db() as conn:
        if uploads.is_upload(ipo_id):
            names = [n.strip() for n in (o["ipo"].get("banker_raw") or "").split(",") if n.strip()]
            rows = [dict(r) for r in conn.execute(
                "SELECT b.banker_id, b.canonical_name, a.alias FROM bankers b "
                "LEFT JOIN banker_alias a ON a.banker_id = b.banker_id")]
            ids = [h["banker_id"] for n in names if (h := uploads._match(n, rows, "canonical_name"))]
        else:
            ids = [r[0] for r in conn.execute("SELECT banker_id FROM ipo_banker WHERE ipo_id = ? "
                                              "ORDER BY position", (ipo_id,))]
        if not ids:
            return {"banker": None, "ipos": [], "base_rate": None}
        banker = conn.execute("SELECT canonical_name FROM bankers WHERE banker_id = ?",
                              (ids[0],)).fetchone()[0]
        ipos = [dict(r) for r in conn.execute(
            """SELECT i.company, i.listing_date, l.ret_6m, l.adverse, l.outcome_known_on
               FROM ipo_banker ib JOIN ipos i ON i.ipo_id = ib.ipo_id
               JOIN labels l ON l.ipo_id = ib.ipo_id
               WHERE ib.banker_id = ? AND l.outcome_known_on < ? AND ib.ipo_id <> ?
               ORDER BY i.listing_date""", (ids[0], as_of, ipo_id))]
        base = conn.execute("SELECT avg(adverse) FROM labels WHERE outcome_known_on < ?",
                            (as_of,)).fetchone()[0]
    return {"banker": banker, "ipos": ipos, "base_rate": base}


@router.get("/runs/{run_id}/trace")
def run_trace(run_id: str) -> list[dict]:
    _result_path(run_id)
    path = settings().logs / f"trace-{run_id}.jsonl"
    if not path.exists():
        raise HTTPException(404, "no trace for this run")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    """The run's trace as server-sent events, then `done` with the final status.

    Each event's id is the byte offset after it, so a reconnecting browser
    (Last-Event-ID) resumes where it left off instead of replaying the trace.
    """
    result = _result_path(run_id)
    path = settings().logs / f"trace-{run_id}.jsonl"
    try:
        start = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        start = 0

    def finished() -> str | None:
        state = _runs.get(run_id)
        if state is not None:
            return None if state["status"] == "running" else state["status"]
        if result.exists():                     # a run from the CLI, POST /run or before a restart
            try:
                return json.loads(result.read_text())["status"]
            except (OSError, ValueError, KeyError):
                return None
        if path.exists() and time.time() - path.stat().st_mtime < 60:
            return None                         # still writing its trace elsewhere
        return "unknown"

    async def stream():
        offset, idle = start, 0.0
        while True:
            if path.exists():
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                complete = chunk[:chunk.rfind(b"\n") + 1]    # never a half-written line
                for line in complete.splitlines(keepends=True):
                    offset += len(line)
                    if line.strip():
                        yield f"id: {offset}\ndata: {line.decode().strip()}\n\n"
                if complete:
                    idle = 0.0
            status = finished()
            if status is not None:
                if path.exists() and path.stat().st_size > offset:
                    continue
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return
            await asyncio.sleep(0.25)       # async: an open stream holds no worker thread
            idle += 0.25
            if idle >= 15:          # keep proxies from closing a quiet stream
                idle = 0.0
                yield ": keep-alive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -- backtest and status -----------------------------------------------------

@router.get("/backtest")
def backtest() -> dict:
    cfg = settings()
    folder = cfg.data / "backtest"
    out = {}
    for name in ("results", "llm_baseline"):
        path = folder / f"{name}.json"
        out[name] = json.loads(path.read_text()) if path.exists() else None
    scorer = cfg.data / "model" / "scorer.json"
    if scorer.exists():
        s = json.loads(scorer.read_text())
        out["scorer"] = {"version": s["version"], "bands": s["bands"], "features": s["features"],
                         "trained_on": s.get("trained_on")}
    return out


_status_cache: dict = {}


@router.get("/status")
def status(probe: bool = True) -> dict:
    """What is configured and, when probing, whether Zetaris and Nightshift answer."""
    if probe and _status_cache.get("at", 0) > time.time() - 15:
        return _status_cache["value"]
    cfg = settings()
    out = {"sample_mode_default": os.environ.get("SAMPLE_MODE", "false").lower() == "true",
           "archive": cfg.archive_db.exists(),
           "scorer": (cfg.data / "model" / "scorer.json").exists(),
           "zetaris": {"configured": bool(os.environ.get("ZETARIS_REST_URL")
                                          and os.environ.get("ZETARIS_API_KEY"))},
           "meterless": {"configured": bool(os.environ.get("METERLESS_BASE_URL"))}}
    if probe:
        out["zetaris"].update(_probe_zetaris() if out["zetaris"]["configured"] else {})
        out["meterless"].update(_probe_meterless() if out["meterless"]["configured"] else {})
        _status_cache.update(at=time.time(), value=out)
    return out


def _probe_zetaris() -> dict:
    from agent.tools_zetaris import ZetarisError, ZetarisRestClient
    t0 = time.perf_counter()
    try:
        client = ZetarisRestClient(timeout_s=4.0, retries=0)
        client.query("SELECT 1", 1)
        return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000)}
    except (ZetarisError, Exception) as e:      # any failure is "down", shown as is
        return {"ok": False, "error": str(e)[:160]}


def _probe_meterless() -> dict:
    from agent.llm import providers_from_env
    t0 = time.perf_counter()
    try:
        provider = providers_from_env()[0]
        provider._http.timeout = 3.0
        models = provider.models()
        roles = {k[len("MODEL_ROUTE_"):].lower(): v for k, v in os.environ.items()
                 if k.startswith("MODEL_ROUTE_") and v}
        return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000),
                "models": len(models), "nemotron": sorted(m for m in models if "nemotron" in m),
                "roles": roles}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
