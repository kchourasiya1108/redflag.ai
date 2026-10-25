"""Stage 09: train the scorer and backtest it on a time split.

Older IPOs train the model and newer ones test it. A training IPO is used
only if its outcome was known before the first test bid date, so the test
period never leaks into training. The banker-only rule is the baseline the
model must beat (the single-prompt LLM baseline joins with the model gateway).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

from pipeline.base import Gate, StageContext

FEATURES = ["f_banker_crash", "f_banker_low_history", "f_banker_median_ret"]
TEST_SHARE = 0.3
RULE_HIGH_AT = 0.5        # data guide F10: banker crash rate >= 0.5 is HIGH
VERSION = "v0-banker"


def load_dataset(conn) -> list[dict]:
    feats: dict[str, dict] = defaultdict(dict)
    for r in conn.execute("SELECT ipo_id, as_of, name, value FROM features"):
        feats[r["ipo_id"]][r["name"]] = r["value"]
        feats[r["ipo_id"]]["as_of"] = r["as_of"]
    rows = []
    for r in conn.execute("SELECT ipo_id, adverse, outcome_known_on FROM labels "
                          "WHERE adverse IS NOT NULL"):
        if r["ipo_id"] in feats:
            rows.append(feats[r["ipo_id"]] | {"ipo_id": r["ipo_id"], "y": r["adverse"],
                                               "known_on": r["outcome_known_on"]})
    return sorted(rows, key=lambda r: r["as_of"])


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    cut = int(len(rows) * (1 - TEST_SHARE))
    test = rows[cut:]
    first_test = test[0]["as_of"] if test else "9999"
    train = [r for r in rows[:cut] if r["known_on"] < first_test]
    return train, test


def matrix(rows: list[dict], fill: dict[str, float]) -> np.ndarray:
    cols = []
    for f in FEATURES:
        vals = [r.get(f) for r in rows]
        cols.append([fill[f] if v is None else v for v in vals])
        cols.append([float(v is None) for v in vals])      # missing indicator
    return np.array(cols, dtype=float).T


def bands(base_rate: float) -> dict[str, float]:
    """LOW below the average SME IPO's risk; HIGH at twice it or more."""
    return {"low_below": round(base_rate, 4), "high_at": round(min(2 * base_rate, 0.9), 4)}


def evaluate(y: np.ndarray, p: np.ndarray, high_at: float) -> dict:
    high = p >= high_at
    pos, neg = y == 1, y == 0
    return {
        "n": int(len(y)), "adverse": int(pos.sum()),
        "auc": float(roc_auc_score(y, p)) if 0 < pos.sum() < len(y) else None,
        "recall_at_high": float(high[pos].mean()) if pos.any() else None,
        "false_alarm_rate": float(high[neg].mean()) if neg.any() else None,
        "brier": float(brier_score_loss(y, np.clip(p, 0, 1))),
    }


def run(ctx: StageContext) -> dict:
    rows = load_dataset(ctx.conn)
    train, test = split(rows)
    if len(train) < 30 or len({r["y"] for r in train}) < 2:
        ctx.log.failure("train", "too_little_data", f"{len(train)} training rows")
        return {"train": len(train), "test": len(test)}
    fill = {f: float(np.nanmedian([r[f] for r in train if r.get(f) is not None] or [0]))
            for f in FEATURES}
    y_train = np.array([r["y"] for r in train])
    y_test = np.array([r["y"] for r in test])
    model = LogisticRegression(C=1.0, max_iter=1000).fit(matrix(train, fill), y_train)
    p_test = model.predict_proba(matrix(test, fill))[:, 1]
    base_rate = float(y_train.mean())
    cut = bands(base_rate)

    rule_score = np.array([r.get("f_banker_crash") or base_rate for r in test])
    results = {
        "version": VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "split": {"train": len(train), "test": len(test),
                  "train_from": train[0]["as_of"], "train_to": train[-1]["as_of"],
                  "test_from": test[0]["as_of"], "test_to": test[-1]["as_of"]},
        "base_rate_train": base_rate,
        "model": evaluate(y_test, p_test, cut["high_at"]),
        "baseline_banker_rule": evaluate(y_test, rule_score, RULE_HIGH_AT),
    }
    names = [n for f in FEATURES for n in (f, f"{f}__missing")]
    scorer = {"version": VERSION, "features": FEATURES, "columns": names,
              "coef": model.coef_[0].round(6).tolist(),
              "intercept": round(float(model.intercept_[0]), 6),
              "fill": fill, "bands": cut, "calibration": "logistic (Platt); refit in v2",
              "trained_on": results["split"]}
    model_dir, backtest_dir = ctx.settings.data / "model", ctx.settings.data / "backtest"
    model_dir.mkdir(parents=True, exist_ok=True)
    backtest_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "scorer.json").write_text(json.dumps(scorer, indent=2))
    (backtest_dir / "results.json").write_text(json.dumps(results, indent=2))
    ctx.log.info(f"model AUC {results['model']['auc']} vs banker rule "
                 f"{results['baseline_banker_rule']['auc']} on {len(test)} test IPOs",
                 model=results["model"], baseline=results["baseline_banker_rule"])
    return results["split"]


def gate(conn, settings) -> Gate:
    g = Gate("train")
    path = settings.data / "backtest" / "results.json"
    if not path.exists():
        g.add("backtest results", False, "run the train stage")
        return g
    r = json.loads(path.read_text())
    m, b = r["model"], r["baseline_banker_rule"]
    g.add("backtest results", True, f"{r['split']['train']} train / {r['split']['test']} test")
    g.add("beats banker-only baseline (AUC)",
          (m["auc"] or 0) >= (b["auc"] or 0), f"model {m['auc']} vs rule {b['auc']}")
    return g
