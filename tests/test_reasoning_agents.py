"""Skeptic, corroboration, citation checks and the Reporter summary, with a fake model."""
from __future__ import annotations

import time
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agent.agents.base import RunContext
from agent.agents.citations import CitationVerifier, value_on_page
from agent.agents.reporter import plain_summary
from agent.agents.skeptic import Skeptic
from agent.case_file import CaseFile
from agent.llm import parse_structured
from agent.trace import Tracer


class FakeGateway:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def generate(self, prompt, system=None, *, schema=None, route="", purpose="", **kw):
        self.calls.append((route, purpose))
        return SimpleNamespace(parsed=schema.model_validate(self.answers.pop(0)))


def ctx_with(tmp_path, gateway, pages):
    case = CaseFile("r", "NSE-2025-X", date(2025, 3, 10))
    case.meta["depth"] = "STANDARD"
    ctx = RunContext("r", "NSE-2025-X", SimpleNamespace(logs=tmp_path, data=tmp_path),
                     Tracer("r", tmp_path), None, "test", False, time.perf_counter() + 600,
                     gateway=gateway, as_of=date(2025, 3, 10), case=case)
    ctx.scratch["pages"] = pages
    return ctx


PAGES = ["cover"] * 5 + ["Our order book stands at Rs 120 crore as of March 2025."] + ["x"] * 4


def test_ruling_without_evidence_stands_and_cited_dismissal_lowers(tmp_path):
    gw = FakeGateway([{"ruling": "dismissed", "reason": "order book", "evidence": []},
                      {"ruling": "dismissed", "reason": "order book explains it",
                       "evidence": ["rhp:p6", "rhp:p99"]}])
    ctx = ctx_with(tmp_path, gw, PAGES)
    ctx.case.add_flag("F2", "ELEVATED", value={}, evidence=["rhp:p45"], confidence=0.7,
                      agent="t", text="profit spike")
    ctx.case.add_flag("F1", "ELEVATED", value={}, evidence=["rhp:p46"], confidence=0.7,
                      agent="t", text="cash")
    Skeptic().run(ctx)
    f2, f1 = ctx.case.flag("F2"), ctx.case.flag("F1")
    assert {f2["skeptic"]["ruling"], f1["skeptic"]["ruling"]} == {"upheld", "dismissed"}
    dismissed = f2 if f2["skeptic"]["ruling"] == "dismissed" else f1
    assert dismissed["severity"] == "LOW" and dismissed["skeptic"]["evidence"] == ["rhp:p6"]
    assert all(route == "reason" for route, _ in gw.calls)


def test_lone_high_flag_is_held_at_elevated(tmp_path):
    ctx = ctx_with(tmp_path, None, PAGES)
    ctx.case.add_flag("F10", "HIGH", value={}, evidence=["zq:1"], confidence=0.8, agent="t",
                      text="banker")
    Skeptic().run(ctx)
    assert ctx.case.flag("F10")["severity"] == "ELEVATED"
    ctx.case.add_flag("F10", "HIGH", value={}, evidence=["zq:1"], confidence=0.8, agent="t",
                      text="banker")
    ctx.case.add_flag("F1", "ELEVATED", value={}, evidence=["rhp:p2"], confidence=0.7,
                      agent="t", text="cash")
    Skeptic().run(ctx)
    assert ctx.case.flag("F10")["severity"] == "HIGH"


def test_citation_verifier_withdraws_claims_with_missing_evidence(tmp_path):
    ctx = ctx_with(tmp_path, None, PAGES)
    ctx.case.add_flag("F2", "ELEVATED", value={}, evidence=["rhp:p400"], confidence=0.7,
                      agent="t", text="spike")
    res = CitationVerifier().run(ctx)
    assert res.data["withdrawn"] == ["F2"] and ctx.case.flag("F2")["severity"] == "LOW"
    assert value_on_page(4.3236, "Profit for the year 432.36 236.02")       # lakh on the page
    assert not value_on_page(4.3236, "Profit for the year 999.99")


def test_summary_must_not_invent_numbers_or_advise():
    out = {"rating": "ELEVATED", "p_adverse": 0.12, "reasons": [{"text": "PAT rose 2.1x"}],
           "skeptic_notes": [], "cleared_checks": [], "questions": [], "not_checked": []}
    good = FakeGateway([{"summary": "Rated ELEVATED; profit rose 2.1x before the IPO."}])
    assert plain_summary(good, out).startswith("Rated ELEVATED")
    assert plain_summary(FakeGateway([{"summary": "Profit rose 7.5x."}]), out) is None
    assert plain_summary(FakeGateway([{"summary": "You should avoid this IPO."}]), out) is None


class Tiny(BaseModel):
    ruling: str


def test_structured_reply_after_inline_reasoning():
    text = 'The user wants {a summary}. Let me think about {"x": 1} ... Final: {"ruling": "upheld"}'
    assert parse_structured(text, Tiny).ruling == "upheld"


def test_summary_numbers_must_match_exactly():
    out = {"rating": "LOW", "p_adverse": 0.12, "reasons": [{"text": "value 17.53"}],
           "skeptic_notes": [], "cleared_checks": [], "questions": [], "not_checked": []}
    assert plain_summary(FakeGateway([{"summary": "Profit rose 7.5x."}]), out) is None
    assert plain_summary(FakeGateway([{"summary": "A 12% chance; 17.53 recorded."}]), out)


def test_uncited_flag_is_withdrawn(tmp_path):
    ctx = ctx_with(tmp_path, None, PAGES)
    ctx.case.add_flag("F1", "ELEVATED", value={}, evidence=[], confidence=0.7, agent="t",
                      text="no citation")
    assert CitationVerifier().run(ctx).data["withdrawn"] == ["F1"]


def test_summary_accepts_grouped_numbers():
    out = {"rating": "LOW", "p_adverse": 0.05, "reasons": [{"text": "revenue 1234.5"}],
           "skeptic_notes": [], "cleared_checks": [], "questions": [], "not_checked": []}
    assert plain_summary(FakeGateway([{"summary": "Revenue was ₹1,234.5 cr."}]), out)
