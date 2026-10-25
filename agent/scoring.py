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
    x = []
    transform = scorer.get("transform", {})
    for f in scorer["features"]:
        v = features.get(f)
        value = scorer["fill"][f] if v is None else float(v)
        t = transform.get(f)
        if t:                       # the training stage's clip + standardisation
            value = (min(max(value, t["clip"][0]), t["clip"][1]) - t["mean"]) / t["sd"]
        x += [value, float(v is None)]
    z = scorer["intercept"] + sum(w * xi for w, xi in zip(scorer["coef"], x))
    p = 1 / (1 + math.exp(-z))
    b = scorer["bands"]
    band = "HIGH" if p >= b["high_at"] else "LOW" if p < b["low_below"] else "ELEVATED"
    return Score(band, round(p, 4), scorer["version"], [])
