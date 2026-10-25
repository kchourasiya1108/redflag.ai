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


def test_review_fixes_missing_price_total_rows_and_braces():
    from agent.narrative import _rows_from
    rows = [{"date": "2024-09-01", "shares": 100, "issue_price": None, "nature": "share swap"}]
    s = insider_discount(rows, 100.0, AS_OF)
    assert not s["bonus_in_window"] and s["recent_allotments"] == 0
    data = {"unit": "lakh", "rows": [{"object": "Working capital", "kind": "working_capital",
                                      "amount": 600}, {"object": "GCP", "kind": "gcp", "amount": 100},
                                     {"object": "Total", "kind": "other", "amount": 700}]}
    assert objects_summary(data)["gross_cr"] == 7.0
    reply = 'I see {unit} in lakh here... {"rows": [{"date": "2024-01-01"}]}\n[STATE_CHECKPOINT]'
    assert _rows_from(reply)["rows"] == [{"date": "2024-01-01"}]


def test_peer_valuation_and_f8():
    from agent.narrative import peer_valuation
    from agent.specialists import f8_overvalued
    data = {"issue_pe": "38.35", "peers": [
        {"name": "Synoptics Technologies Limited", "pe": 38.35, "page": 76},
        {"name": "Allied Digital Services Limited", "pe": "9.82", "page": 76}]}
    s = peer_valuation(data, "Synoptics Technologies Limited")
    assert s["peer_median_pe"] == 9.82 and round(s["premium"], 2) == 3.91 and s["pages"] == [76]
    assert f8_overvalued(s).severity == "HIGH"
    assert peer_valuation({"issue_pe": 20, "peers": []})["premium"] is None


def test_rpt_share_and_f6():
    from agent.narrative import rpt_share
    from agent.specialists import f6_related_parties
    data = {"unit": "lakhs", "rows": [
        {"party": "A Pvt Ltd", "nature": "sale", "fiscal_year": "2024-03-31", "amount": "900"},
        {"party": "B", "nature": "purchase", "fiscal_year": "2024-03-31", "amount": 300},
        {"party": "C", "nature": "remuneration", "fiscal_year": "2024-03-31", "amount": 50},
        {"party": "A Pvt Ltd", "nature": "sale", "fiscal_year": "2023-03-31", "amount": 100}]}
    s = rpt_share(data, {"FY24": 40.0, "FY23": 30.0})
    assert s["fiscal_year"] == "FY24" and s["rpt_cr"] == 12.0 and s["share"] == 0.3
    assert f6_related_parties(s).severity == "ELEVATED"


def test_gcp_cap_follows_the_rules_at_the_bid_date():
    from agent.narrative import gcp_cap, printed_gross
    assert gcp_cap(35.076, date(2023, 6, 30)) == pytest.approx(8.769)      # 25% before Mar 2025
    assert gcp_cap(100.0, date(2025, 6, 1)) == 10.0 and gcp_cap(20.0, date(2025, 6, 1)) == 3.0
    page = "(Rs. in Lakhs)\nGross Proceeds from the Fresh Issue 3507.60\nNet Proceeds 3457.60"
    assert printed_gross([page], [1]) == pytest.approx(35.076)
    data = {"unit": "lakh", "gross_proceeds": "3008", "rows": [
        {"object": "Working capital", "kind": "working_capital", "amount": 1757.6},
        {"object": "General Corporate Purpose", "kind": "gcp", "amount": 670}]}
    s = objects_summary(data, date(2023, 6, 30), printed_gross([page], [1]))
    assert s["gross_cr"] == pytest.approx(35.076) and f9_use_of_money(s, [67]).severity != "HIGH"


def test_f8_uses_the_issuers_row_and_f4_clears_without_allotments():
    from agent.narrative import peer_valuation
    s = peer_valuation({"peers": [{"name": "Synoptics Technologies Limited", "pe": 38.35},
                                  {"name": "Allied Digital Services Limited", "pe": 9.82}]},
                       "Synoptics Technologies Limited")
    assert s["issue_pe"] == 38.35 and round(s["premium"], 1) == 3.9
    none = insider_discount([{"date": "2016-01-01", "shares": 1, "issue_price": 10}], 100.0, AS_OF)
    assert f4_insider_discount(none).severity == "LOW"
