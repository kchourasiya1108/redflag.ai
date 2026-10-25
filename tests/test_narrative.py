"""Narrative sections through the Markovian engine (F4, F9), with a fake model."""
from __future__ import annotations

import json
from datetime import date

import pytest

from agent.narrative import insider_discount, objects_summary, read_section, section_windows, SECTIONS
from agent.specialists import f4_insider_discount, f9_use_of_money

AS_OF = date(2025, 3, 10)


def test_insider_discount_uses_only_the_18_months_before_as_of():
    rows = [{"date": "2016-04-01", "shares": 10000, "issue_price": 10, "nature": "MoA"},
            {"date": "2024-06-01", "shares": 100000, "issue_price": 20, "consideration": "cash"},
            {"date": "2024-09-01", "shares": 100000, "issue_price": 40, "consideration": "cash"},
            {"date": "2024-10-01", "shares": 500000, "issue_price": 0, "nature": "Bonus issue"},
            {"date": "2025-04-01", "shares": 1, "issue_price": 1, "consideration": "cash"}]
    s = insider_discount(rows, 150.0, AS_OF)
    assert s["weighted_price"] == 30.0 and s["insider_discount"] == 5.0 and s["bonus_in_window"]
    assert f4_insider_discount(s).severity == "HIGH"
    assert f4_insider_discount(insider_discount(rows[2:3], 130.0, AS_OF)).severity == "ELEVATED"
    assert f4_insider_discount(insider_discount(rows[2:3], 100.0, AS_OF)).severity == "LOW"


def test_objects_summary_and_gcp_cap():
    data = {"unit": "lakhs", "gross_proceeds": "2,000.00", "rows": [
        {"kind": "working_capital", "amount": "1,200"}, {"kind": "gcp", "amount": 290},
        {"kind": "issue_expenses", "amount": 110}]}
    s = objects_summary(data)
    assert s["gross_cr"] == 20.0 and s["gcp_cr"] == pytest.approx(2.9)
    assert s["gcp_cap_cr"] == pytest.approx(3.0) and s["wc_share"] == pytest.approx(0.6)
    assert f9_use_of_money(s, [67]).severity == "ELEVATED"          # near the cap, WC 60%
    over = objects_summary({**data, "rows": [{"kind": "gcp", "amount": 400}]})
    assert f9_use_of_money(over, [67]).severity == "HIGH"           # above min(15%, 10 cr)


class FakeGateway:
    """Answers each Markovian chunk with the rows on that page, plus a state block."""
    def __init__(self, by_page):
        self.by_page, self.prompts = by_page, []

    def engine_generator(self, route, purpose):
        def gen(prompt, attachments=None, system=None, on_stream=None, abort=None):
            self.prompts.append(prompt)
            page = int(prompt.split("[page ")[-1].split("]")[0])
            return {"text": json.dumps({"rows": self.by_page.get(page, [])}) +
                    "\n[STATE_CHECKPOINT]\nCOMPLETED: rows read\nCONTEXT: unit Rs, columns date shares price"}
        return gen


def test_capital_history_is_read_window_by_window_with_carryover(tmp_path):
    pages = ["intro " * 50] * 3 + ["SECTION VI CAPITAL STRUCTURE\nHistory of Equity Share Capital "
             + "1 " * 40, "continued table " + "2 " * 40] + ["other " * 50] * 10
    fake = FakeGateway({4: [{"date": "2024-06-01", "shares": 100, "issue_price": 20}],
                        5: [{"date": "2024-09-01", "shares": 100, "issue_price": 40}]})
    assert section_windows(pages, SECTIONS["capital"])[0][0] == 4
    res = read_section(fake, pages, "capital", history=tmp_path / "m.jsonl")
    assert [r["issue_price"] for r in res.data["rows"][:2]] == [20, 40]
    assert "[CARRYOVER STATE]" in fake.prompts[1] and "unit Rs" in fake.prompts[1]
    assert res.run.status == "completed" and (tmp_path / "m.jsonl").exists()
