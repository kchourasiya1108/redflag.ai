"""Apply data/model/scorer.json to a feature dict (deterministic; no LLM)."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

INSUFFICIENT = "INSUFFICIENT DATA"
CORE = ("f_banker_crash",)       # grows as the model gains features


@dataclass
class Score:
    band: str
    p_adverse: float | None
    version: str
    missing_core: list[str]


def load_scorer(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run: python run.py features train")
    return json.loads(path.read_text())


def score(features: dict, scorer: dict) -> Score:
    missing = [f for f in CORE if features.get(f) is None]
    if missing:
        return Score(INSUFFICIENT, None, scorer["version"], missing)
    x = _design_row(features, scorer)
    z = scorer["intercept"] + sum(w * xi for w, xi in zip(scorer["coef"], x))
    p = 1 / (1 + math.exp(-z))
    b = scorer["bands"]
    band = "HIGH" if p >= b["high_at"] else "LOW" if p < b["low_below"] else "ELEVATED"
    return Score(band, round(p, 4), scorer["version"], [])



def _design_row(features: dict, scorer: dict) -> list[float]:
    """[value, missing] per feature, as the model saw them in training."""
    x = []
    transform = scorer.get("transform", {})
    for f in scorer["features"]:
        v = features.get(f)
        value = scorer["fill"][f] if v is None else float(v)
        t = transform.get(f)
        if t:                       # the training stage's clip + standardisation
            value = (min(max(value, t["clip"][0]), t["clip"][1]) - t["mean"]) / t["sd"]
        x += [value, float(v is None)]
    return x


def explain(features: dict, scorer: dict) -> list[dict]:
    """Each feature's share of the log-odds: its weight times its (standardised) value,
    plus the weight of its missing indicator. Sums with the intercept to z."""
    x, w = _design_row(features, scorer), scorer["coef"]
    return [{"feature": f, "value": features.get(f), "missing": features.get(f) is None,
             "contribution": round(w[2 * i] * x[2 * i] + w[2 * i + 1] * x[2 * i + 1], 4)}
            for i, f in enumerate(scorer["features"])]
