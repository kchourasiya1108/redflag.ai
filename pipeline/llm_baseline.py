"""The single-prompt LLM baseline (project brief): one Nemotron call per test IPO.

The model reads only its RHP (the cover, the summary profit and loss, and the
basis for the issue price) and returns a probability that the IPO ends
badly. It is scored on the same time-split test IPOs and with the same
metrics as the scorer (pipeline/train.py), into data/backtest/llm_baseline.json.

Caveat stored with the results: the model may have seen news about some of
these IPOs in its training data, which can only flatter the baseline.
"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
from pydantic import BaseModel, Field

from agent.rhp import load_pages, section_pages, statement_pages
from pipeline.train import evaluate, load_dataset, split  # noqa: F401 (train rows unused)

SYSTEM = "You are an analyst rating the risk of Indian SME IPOs from their prospectus alone."


class Rating(BaseModel):
    p_adverse: float = Field(ge=0, le=1)
    reason: str = Field(default="", max_length=400)


def rhp_excerpt(pages: list[str], budget: int = 6000) -> str:
    picks = [1, 2] + statement_pages(pages, "pl")[:1] + section_pages(
        pages, r"basis\s+for\s+(?:the\s+)?(?:issue|offer)\s+price", window=2)
    seen, out = set(), []
    for p in picks:
        if p and p <= len(pages) and p not in seen:
            seen.add(p)
            out.append(f"[page {p}]\n{pages[p - 1][:budget // 4]}")
    return "\n\n".join(out)[:budget]


def rate(gateway, cache, ipo: dict, sha: str | None) -> float | None:
    pages = load_pages(cache, sha) if sha else []
    if not pages:
        return None
    prompt = (f"Prospectus excerpts of an SME IPO opening on {ipo['as_of']}:\n\n"
              f"{rhp_excerpt(pages)}\n\nWhat is the probability (0 to 1) that within six months "
              "of listing the share falls more than 40% below the issue price, is suspended, or "
              "the company is named in a SEBI order? Reply with JSON: p_adverse, reason.")
    try:
        return gateway.generate(prompt, SYSTEM, schema=Rating, route="extract",
                                purpose=f"llm_baseline:{ipo['ipo_id']}").parsed.p_adverse
    except Exception:
        return None


def run_baseline(conn, gateway, cache, out_path, *, workers: int = 2, log=print) -> dict:
    train, test = split(load_dataset(conn))
    docs = {r["ipo_id"]: r["sha256"] for r in conn.execute(
        "SELECT ipo_id, sha256 FROM docs WHERE doc_type = 'rhp' AND status = 'ok'")}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        ps = list(pool.map(lambda r: rate(gateway, cache, r, docs.get(r["ipo_id"])), test))
    rated = [(r, p) for r, p in zip(test, ps) if p is not None]
    # The scorer's own HIGH cut-off, and the scorer re-scored on exactly the same
    # IPOs, so the two rows compare like with like.
    import json as _json
    from agent.scoring import score
    scorer = _json.loads((out_path.parent.parent / "model" / "scorer.json").read_text())
    high_at = scorer["bands"]["high_at"]
    y = np.array([r["y"] for r, _ in rated])
    p = np.array([p for _, p in rated])
    s = np.array([score(r, scorer).p_adverse or scorer["bands"]["low_below"] for r, _ in rated])
    ok = len(rated) > 0 and 0 < y.sum() < len(y)
    result = {"trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "test_ipos": len(test), "rated": len(rated), "high_at": high_at,
              "metrics": evaluate(y, p, high_at) if ok else None,
              "scorer_on_same_ipos": evaluate(y, s, high_at) if ok else None,
              "caveat": "Nemotron may have seen news about these IPOs in training; this can "
                        "only flatter the baseline."}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    log(f"LLM baseline rated {len(rated)}/{len(test)} test IPOs: {result['metrics']}")
    return result
