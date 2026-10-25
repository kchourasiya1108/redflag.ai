"""F1-F3 formulas on hand-made verified facts (data guide section 9.1)."""
from __future__ import annotations

import pytest

from agent.specialists import (_Facts, ebitda, f1_paper_profits, f2_profit_spike,
                               f3_eligibility, rhp_features)


def facts(**by_field):
    rows = []
    for field, per_year in by_field.items():
        for fy, value in per_year.items():
            rows.append({"field": field.replace("__", "."), "fiscal_year": fy, "value": value,
                         "page": 45, "verified": True})
    return _Facts(rows)


BASE = dict(pl__pat={"FY24": 4.0, "FY23": 2.0, "FY22": 2.0},
            pl__pbt={"FY24": 5.5, "FY23": 2.8, "FY22": 2.7},
            pl__finance_cost={"FY24": 0.5, "FY23": 0.4, "FY22": 0.4},
            pl__depreciation={"FY24": 0.6, "FY23": 0.5, "FY22": 0.5},
            pl__other_income={"FY24": 0.1, "FY23": 0.1, "FY22": 0.1},
            pl__revenue={"FY24": 40.0, "FY23": 30.0, "FY22": 25.0},
            cf__cfo={"FY24": 3.0, "FY23": 2.5, "FY22": 1.0},
            bs__receivables={"FY24": 8.0, "FY23": 7.0, "FY22": 6.0})


def test_unverified_facts_are_invisible():
    f = _Facts([{"field": "pl.pat", "fiscal_year": "FY24", "value": 4.0, "verified": False}])
    assert f.get("pl.pat", "FY24") is None and f.years() == []


def test_f2_profit_spike_bands():
    assert f2_profit_spike(facts(**BASE)).severity == "ELEVATED"            # 4 / 2 = 2.0x
    assert f2_profit_spike(facts(**{**BASE, "pl__pat": {"FY24": 7.0, "FY23": 2.0, "FY22": 2.0}})
                           ).severity == "HIGH"
    assert f2_profit_spike(facts(**{**BASE, "pl__pat": {"FY24": 2.2, "FY23": 2.0, "FY22": 2.0}})
                           ).severity == "LOW"


def test_f1_paper_profits():
    assert f1_paper_profits(facts(**BASE)).severity == "LOW"                 # CFO/PAT 0.75
    weak = {**BASE, "cf__cfo": {"FY24": 0.8, "FY23": 2.5, "FY22": 1.0}}
    assert f1_paper_profits(facts(**weak)).severity == "ELEVATED"            # 0.2 < 0.3
    neg = {**BASE, "cf__cfo": {"FY24": -1.0, "FY23": -0.5, "FY22": 1.0}}
    found = f1_paper_profits(facts(**neg))
    assert found.severity == "HIGH" and found.value["neg_cfo_years"] == ["FY24", "FY23"]


def test_f3_ebitda_near_the_floor():
    assert ebitda(facts(**BASE), "FY24") == pytest.approx(6.5)
    near = {**BASE, "pl__pbt": {"FY24": 0.1, "FY23": 0.05, "FY22": 0.08},
            "pl__finance_cost": {"FY24": 0.1, "FY23": 0.1, "FY22": 0.1},
            "pl__depreciation": {"FY24": 0.9, "FY23": 0.95, "FY22": 0.92},
            "pl__other_income": {"FY24": 0.0, "FY23": 0.0, "FY22": 0.0}}
    # EBITDA 1.10, 1.10, 1.10: all three years just clear the Rs 1 cr floor
    assert f3_eligibility(facts(**near)).severity == "HIGH"
    assert f3_eligibility(facts(**BASE)).severity == "LOW"


def test_rhp_features():
    feats = rhp_features(facts(**BASE))
    assert feats["f_cfo_pat"] == pytest.approx(0.75) and feats["f_cfo_neg_pat_pos"] == 0.0
    assert feats["f_profit_spike"] == pytest.approx(2.0)
    assert feats["f_ebitda_headroom_min"] == pytest.approx(ebitda(facts(**BASE), "FY22") - 1)
    assert feats["f_recv_rev_gap"] == pytest.approx(8 / 7 - 1 - (40 / 30 - 1))
    assert set(rhp_features(_Facts([])).values()) == {None}


def test_f5_top_customers_needs_a_repeated_value():
    from agent.extract import top_customers
    from agent.specialists import f5_concentration
    once = top_customers(["Our top 10 customers contributed 91.17% of revenue."])
    assert once["value"] == 91.17 and not once["verified"]
    twice = top_customers(["Risk: our top ten customers account for 91.17 % of sales.", "x",
                           "Business: top 10 customers contributed 91.17% in FY24; amount 770.61%"])
    assert twice["verified"] and twice["page"] == 1
    f = _Facts([{**twice, "fiscal_year": None}])
    assert f5_concentration(f).severity == "HIGH" and f.years() == []


def test_auditor_features_are_as_of_and_shrunk():
    from agent.config import ROOT
    from agent.features import auditor_features
    from agent.tools_zetaris import DataPort, MockZetaris
    from datetime import date
    port = DataPort(MockZetaris(ROOT / "data" / "sample" / "archive_sample.db"), date(2025, 1, 1))
    feats = auditor_features(port, "NSE-2023-SYNOPTICS", date(2025, 1, 1), 0.1)
    assert feats["f_auditor_n"] is not None and 0 <= feats["f_auditor_crash"] <= 1
    assert auditor_features(port, "NSE-2099-NONE", date(2025, 1, 1), 0.1)["f_auditor_crash"] is None


def test_top_customers_pattern_edges():
    from agent.extract import top_customers
    same_page = top_customers(["top 10 customers contributed 45.5% ... top 10 customers: 45.5%"])
    assert same_page["value"] == 45.5 and not same_page["verified"]
    assert top_customers(["Our top 10 customers grew 123.45% last year"]) is None
