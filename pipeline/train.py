"""Stage 09: train the scorer and backtest it on a time split.

Older IPOs train the model and newer ones test it. A training IPO is used
only if its outcome was known before the first test bid date, so the test
period never leaks into training. The banker-only rule is the baseline the
model must beat (the single-prompt LLM baseline joins with the model gateway).

v2 chooses among nested feature sets (banker; + watchlist; + RHP numbers) on a
validation slice carved from the newest training years, refits the winner on
the whole training set, and reports the untouched test split once. Each
feature is clipped to its training 1st-99th percentile and standardised; the
scorer stores those transforms so a live run scores exactly the same way.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

from agent.scoring import CORE
from pipeline.base import Gate, StageContext

BANKER = ["f_banker_crash", "f_banker_low_history", "f_banker_median_ret"]
WATCHLIST = ["f_watchlist_hit", "f_watchlist_hop1"]
RHP = ["f_cfo_pat", "f_cfo_neg_pat_pos", "f_recv_rev_gap", "f_profit_spike",
       "f_ebitda_headroom_min", "f_other_income_share", "f_rev_growth"]
PARTIES = ["f_auditor_changed", "f_auditor_crash", "f_top10_customers"]
# Data guide parameters added on 27 Oct 2026: market maker record (F13), customer and
# supplier concentration (F5); the backtest decides whether they earn a place.
PARTIES_V3 = PARTIES + ["f_mm_crash", "f_top5_customers", "f_top10_suppliers", "f_ofs_share",
                         "f_debt_equity", "f_interest_cover", "f_ebitda_margin", "f_ronw",
                         "f_recv_days_change", "f_anchor_bad_rate"]
CANDIDATES = {"v2-banker": BANKER, "v2-banker-watchlist": BANKER + WATCHLIST,
              "v2-banker-watchlist-rhp": BANKER + WATCHLIST + RHP,
              "v2-full": BANKER + WATCHLIST + RHP + PARTIES,
              "v3-full": BANKER + WATCHLIST + RHP + PARTIES_V3}
FEATURES = BANKER          # the banker-only rule and v0 model use these
TEST_SHARE = 0.3
VALIDATION_SHARE = 0.2     # of the training rows, newest first
RULE_HIGH_AT = 0.5        # data guide F10: banker crash rate >= 0.5 is HIGH
C = 0.5


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


def transforms(rows: list[dict], features: list[str]) -> dict:
    """Median fill, 1st-99th percentile clip and standardisation, from training rows only."""
    out = {}
    for f in features:
        vals = np.array([r[f] for r in rows if r.get(f) is not None], dtype=float)
        if len(vals) == 0:
            vals = np.array([0.0])
        lo, hi = np.percentile(vals, [1, 99])
        clipped = np.clip(vals, lo, hi)
        sd = float(clipped.std()) or 1.0
        out[f] = {"fill": float(np.median(vals)), "clip": [float(lo), float(hi)],
                  "mean": float(clipped.mean()), "sd": sd}
    return out


def matrix(rows: list[dict], tf: dict, features: list[str] | None = None) -> np.ndarray:
    cols = []
    for f in features or FEATURES:
        t = tf[f]
        vals = [r.get(f) for r in rows]
        x = [t["fill"] if v is None else min(max(v, t["clip"][0]), t["clip"][1]) for v in vals]
        cols.append([(v - t["mean"]) / t["sd"] for v in x])
        cols.append([float(v is None) for v in vals])      # missing indicator
    return np.array(cols, dtype=float).T


def fit(rows: list[dict], features: list[str]):
    tf = transforms(rows, features)
    model = LogisticRegression(C=C, max_iter=2000).fit(matrix(rows, tf, features),
                                                       np.array([r["y"] for r in rows]))
    return model, tf


def choose(train: list[dict]) -> tuple[str, dict]:
    """Pick the feature set by AUC on the newest training years (never the test split)."""
    cut = int(len(train) * (1 - VALIDATION_SHARE))
    first_val = train[cut]["as_of"]
    inner = [r for r in train[:cut] if r["known_on"] < first_val]
    val = train[cut:]
    scores = {}
    y = np.array([r["y"] for r in val])
    for name, feats in CANDIDATES.items():
        if len({r["y"] for r in inner}) < 2 or len(set(y)) < 2:
            scores[name] = None
            continue
        model, tf = fit(inner, feats)
        scores[name] = float(roc_auc_score(y, model.predict_proba(matrix(val, tf, feats))[:, 1]))
    best = max((n for n in scores if scores[n] is not None), key=lambda n: scores[n],
               default="v2-banker")
    return best, {"validation_auc": scores, "validation": {"n": len(val), "inner": len(inner),
                                                           "from": first_val}}


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
        "reliability": reliability(y, p),
    }


def reliability(y: np.ndarray, p: np.ndarray, bins: int = 5) -> list[dict]:
    """Calibration: predicted against observed rate of adverse outcomes, in equal-count bins
    of predicted probability (the data for a reliability chart, design: Evaluation targets)."""
    order = np.argsort(p)
    out = []
    for part in np.array_split(order, bins):
        if len(part):
            out.append({"n": int(len(part)), "p_mean": round(float(p[part].mean()), 4),
                        "observed": round(float(y[part].mean()), 4),
                        "p_from": round(float(p[part].min()), 4), "p_to": round(float(p[part].max()), 4)})
    return out


def run(ctx: StageContext) -> dict:
    rows = load_dataset(ctx.conn)
    train, test = split(rows)
    if len(train) < 30 or len({r["y"] for r in train}) < 2:
        ctx.log.failure("train", "too_little_data", f"{len(train)} training rows")
        return {"train": len(train), "test": len(test)}
    y_train = np.array([r["y"] for r in train])
    y_test = np.array([r["y"] for r in test])
    version, selection = choose(train)
    features = CANDIDATES[version]
    model, tf = fit(train, features)
    p_test = model.predict_proba(matrix(test, tf, features))[:, 1]
    v0, tf0 = fit(train, BANKER)
    p_v0 = v0.predict_proba(matrix(test, tf0, BANKER))[:, 1]
    base_rate = float(y_train.mean())
    cut = bands(base_rate)

    rule_score = np.array([r.get("f_banker_crash") or base_rate for r in test])
    results = {
        "version": version,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selection": selection,
        "split": {"train": len(train), "test": len(test),
                  "train_from": train[0]["as_of"], "train_to": train[-1]["as_of"],
                  "test_from": test[0]["as_of"], "test_to": test[-1]["as_of"]},
        "base_rate_train": base_rate,
        "model": evaluate(y_test, p_test, cut["high_at"]),
        "baseline_banker_model_v0": evaluate(y_test, p_v0, cut["high_at"]),
        "baseline_banker_rule": evaluate(y_test, rule_score, RULE_HIGH_AT),
        "coverage_test": {f: float(np.mean([r.get(f) is not None for r in test]))
                          for f in features},
        # Share of test IPOs the Scorer would rate INSUFFICIENT DATA (a core feature missing).
        "abstention_rate": float(np.mean([any(r.get(f) is None for f in CORE) for r in test])),
    }
    names = [n for f in features for n in (f, f"{f}__missing")]
    scorer = {"version": version, "features": features, "columns": names,
              "coef": model.coef_[0].round(6).tolist(),
              "intercept": round(float(model.intercept_[0]), 6),
              "fill": {f: tf[f]["fill"] for f in features},
              "transform": {f: {k: tf[f][k] for k in ("clip", "mean", "sd")} for f in features},
              "bands": cut, "calibration": "logistic (Platt) on standardised features",
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
