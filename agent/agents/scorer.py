"""Scorer: the backtest's own feature function and model (no LLM)."""
from __future__ import annotations

from dataclasses import asdict

from agent.agents.base import Agent, AgentResult, RunContext
from agent.features import compute_features
from agent.scoring import load_scorer, score


class Scorer(Agent):
    name = "Scorer"
    role = "Applies the backtested, calibrated model to compute_features (no LLM)"

    def run(self, ctx: RunContext) -> AgentResult:
        facts = ctx.scratch.get("facts")            # CaseFileFacts once extraction ran
        # Same feature function as the backtest. The Data Port caches each
        # template call per run, so the lookups the earlier agents made are
        # reused, not sent to Zetaris again. With no parties recorded (a run
        # cut short before Names), the archive's parties are used, as in the backtest.
        feats = compute_features(ctx.ipo_id, ctx.as_of, ctx.port, facts, named=ctx.named or None)
        ctx.features |= feats
        result = score(feats, load_scorer(ctx.cfg.data / "model" / "scorer.json"))
        ctx.case.set_score({**asdict(result), "features": {
            k: v for k, v in feats.items() if k.startswith("f_")}})
        return AgentResult(f"{result.band} (p={result.p_adverse}, model {result.version})",
                           target="Evaluator", confidence=result.p_adverse)
