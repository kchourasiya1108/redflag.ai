"""RedFlag entry point.

    python run.py universe prices labels   # run offline stages in order
    python run.py all                      # every offline stage
    python run.py check [stage ...]        # print stage gates
    python run.py universe --limit 5       # quick probe on a few rows
    python run.py sebi-review export       # SEBI links for a person to review
    python run.py sebi-review import       # read the reviewed CSV back
    python run.py explain-label NSE-2024-X # show how one label was computed
    python run.py zetaris render|load-pg|register|check   # Zetaris setup
    python run.py investigate NSE-2023-SYNOPTICS [--sample]  # one run, JSON to stdout
    python run.py llm-baseline             # single-prompt Nemotron baseline on the test split
    python run.py serve                    # FastAPI on 0.0.0.0:$PORT
    python run.py upcoming                 # load open/upcoming SME IPOs and their RHPs
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from pipeline.common.db import connect
from pipeline.common.http import Fetcher
from pipeline.common.log import StageLog
from agent.config import settings
from pipeline import STAGES, stage_module
from pipeline.base import StageContext


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run.py")
    parser.add_argument("stages", nargs="+",
                        help=f"'all', 'check', or any of: {', '.join(STAGES)}")
    parser.add_argument("--limit", type=int, help="process at most N rows")
    parser.add_argument("--sample", action="store_true",
                        help="investigate in sample mode (no network, recorded replies)")
    args = parser.parse_args(argv)
    cfg = settings()
    # serve and investigate never open the archive here: connect() would create an
    # empty data/archive.db on a fresh clone and hide the sample database.
    if args.stages[0] == "serve":
        import os
        import uvicorn
        uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
        return 0

    if args.stages[0] == "investigate":
        import json
        from agent.main import RunError, investigate
        try:
            out = investigate(cfg, args.stages[1], sample_mode=True if args.sample else None)
        except RunError as e:           # the same structured error POST /run returns
            print(json.dumps({"status": "error", "error": {
                "type": e.type, "message": str(e), "recoverable": e.recoverable}}, indent=2))
            return 1
        print(json.dumps(out, indent=2, default=str))
        return 0

    conn = connect(cfg.archive_db)

    if args.stages[0] == "sebi-review":
        from pipeline import sebi
        path = cfg.data / "review" / "sebi_entities.csv"
        if args.stages[1:] == ["export"]:
            print(f"wrote {sebi.export_review(conn, path)} rows to {path}")
        elif args.stages[1:] == ["import"]:
            print(f"updated {sebi.import_review(conn, path)} reviewed rows from {path}")
        elif args.stages[1:] == ["model"]:
            from agent.llm import Budget, gateway_from_env
            from pipeline import sebi_review
            gateway = gateway_from_env(cfg.data)
            gateway.budget = Budget(max_calls=2000, max_tokens=50_000_000)
            from os import environ
            workers = int(environ.get("SEBI_REVIEW_WORKERS", "3"))
            print(sebi_review.model_review(conn, lambda: connect(cfg.archive_db), gateway,
                                           cfg.cache, limit=args.limit, workers=workers))
        else:
            parser.error("use: sebi-review export|import|model")
        return 0

    if args.stages[0] == "llm-baseline":
        from agent.llm import Budget, gateway_from_env
        from pipeline.llm_baseline import run_baseline
        gateway = gateway_from_env(cfg.data)
        gateway.budget = Budget(max_calls=2000, max_tokens=50_000_000)
        run_baseline(conn, gateway, cfg.cache / "pages", cfg.data / "backtest" / "llm_baseline.json")
        return 0

    if args.stages[0] == "upcoming":
        from pipeline.upcoming import refresh
        summary = refresh(cfg, Fetcher(cfg.http_min_interval_s, cfg.http_timeout_s))
        print(f"{len(summary['found'])} open or upcoming SME IPO(s) ready; "
              f"{len(summary['errors'])} error(s)")
        return 0 if not summary["errors"] else 1

    if args.stages[0] == "zetaris":
        from pipeline import zetaris_setup
        from agent.tools_zetaris import MockZetaris, ZetarisRestClient
        action = args.stages[1:] or ["render"]
        if action == ["render"]:
            print("\n\n".join(s.masked + ";" for s in zetaris_setup.statements()))
        elif action == ["load-pg"]:
            zetaris_setup.load_postgres(cfg.data / "publish" / "postgres")
        elif action == ["register"]:
            zetaris_setup.register(ZetarisRestClient())
        elif action == ["check"]:
            ok = zetaris_setup.check(ZetarisRestClient(), MockZetaris(cfg.archive_db))
            return 0 if ok else 1
        else:
            parser.error("use: zetaris render|load-pg|register|check")
        return 0

    if args.stages[0] == "audit":
        from pipeline import audit
        print(audit.render(audit.run_audit(conn, cfg)))
        return 0

    if args.stages[0] == "explain-label":
        from pipeline import labels
        print("\n".join(labels.explain(conn, i) for i in args.stages[1:]))
        return 0

    if args.stages[0] == "check":
        names = args.stages[1:] or list(STAGES)
        gates = [stage_module(n).gate(conn, cfg) for n in names]
        print("\n".join(g.render() for g in gates))
        return 0 if all(g.passed for g in gates) else 1

    names = list(STAGES) if args.stages == ["all"] else args.stages
    fetcher = Fetcher(cfg.http_min_interval_s, cfg.http_timeout_s)
    try:
        for name in names:
            module = stage_module(name)
            log = StageLog(name, cfg.logs, cfg.failures_csv)
            started = time.monotonic()
            log.info("starting")
            summary = module.run(StageContext(cfg, conn, fetcher, log, args.limit))
            log.info(f"done in {time.monotonic() - started:.1f}s "
                     f"({log.failure_count} failures)", **(summary or {}))
            print(module.gate(conn, cfg).render())
    finally:
        fetcher.close()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
