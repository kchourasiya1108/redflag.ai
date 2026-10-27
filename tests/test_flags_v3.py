"""Data guide checks added on 27 Oct 2026: F5 top-N, F8 re-pricing, F12, F13 anchors, F14, F19."""
from datetime import date
from types import SimpleNamespace

from agent.narrative import reprice_peers
from agent.specialists import (_Facts as ListFacts, f5_concentration, f12_drift, f14_rating,
                               f19_offer_for_sale)
from pipeline.anchors import parse_report


def facts(rows):
    return ListFacts([{"field": f, "fiscal_year": fy, "value": v, "page": 7, "verified": True}
                      for f, fy, v in rows])


def test_f5_rates_the_strongest_dependence_customers_or_suppliers():
    got = f5_concentration(facts([("biz.top10_customers_pct", "", 65.0),
                                  ("biz.top5_suppliers_pct", "", 85.0)]))
    assert got.severity == "HIGH" and "top 5 suppliers" in got.text.lower()
    assert f5_concentration(facts([("biz.top10_customers_pct", "", 40.0)])).severity == "LOW"
    assert f5_concentration(facts([])) is None


def test_f19_offer_for_sale_against_the_2025_cap():
    split = {"issue_shares": 1000, "ofs_shares": 250}
    assert f19_offer_for_sale(split, date(2025, 6, 1)).severity == "HIGH"       # above 20%
    assert f19_offer_for_sale(split, date(2024, 6, 1)).severity == "LOW"        # no cap then
    assert f19_offer_for_sale({"issue_shares": 1000, "ofs_shares": 0}, date(2025, 6, 1)).severity == "LOW"
    assert f19_offer_for_sale({}, date(2025, 6, 1)) is None


def test_f12_flags_restated_figures_that_moved_between_drafts():
    rhp = facts([("pl.revenue", "FY24", 112.0), ("pl.pat", "FY24", 10.0)])
    drhp = [{"field": "pl.revenue", "fiscal_year": "FY24", "value": 100.0, "page": 3, "verified": 1},
            {"field": "pl.pat", "fiscal_year": "FY24", "value": 10.0, "page": 3, "verified": 1}]
    got = f12_drift(rhp, drhp)
    assert got.severity == "HIGH" and got.value["field"] == "pl.revenue" and got.value["compared"] == 2
    same = f12_drift(rhp, [dict(drhp[1])])
    assert same.severity == "LOW"
    assert f12_drift(rhp, [dict(drhp[0], verified=0)]) is None      # unverified drafts are not compared


def test_f14_reads_rating_actions_from_the_prospectus():
    assert f14_rating(["CARE Ratings downgraded our bank facilities rating to CARE BB in 2024."]).severity == "ELEVATED"
    assert f14_rating(["CRISIL has placed the rating in Issuer Not Cooperating category."]).severity == "ELEVATED"
    assert f14_rating(["ICRA has assigned a rating of [ICRA]D to our term loans."]).severity == "HIGH"
    assert f14_rating(["CRISIL has assigned CRISIL BBB/Stable rating to our facilities."]).severity == "LOW"
    assert f14_rating(["Nothing about ratings here."]) is None


def test_anchor_report_rows_are_parsed():
    text = ("Sr. No. Name of Anchor Investor\n1. Kingsman Wealth Fund PCC- KIF 340,800 35.32% 110/-\n"
            "2. Saint Capital Fund 91,200 9.45% 110/-\n Total 9,64,800 100.00%\n")
    rows = parse_report(text)
    assert [r["name"] for r in rows] == ["Kingsman Wealth Fund PCC- KIF", "Saint Capital Fund"]
    assert rows[0]["shares"] == 340800 and rows[0]["pct"] == 35.32


class Port:
    def run(self, template, **p):
        if template == "market_security_names":
            return SimpleNamespace(rows=[{"symbol": "PEERONE", "isin": "INE1", "name": "Peer One Limited"}])
        if template == "market_close_before":
            assert p["as_of"] == date(2025, 3, 1)
            return SimpleNamespace(rows=[{"date": date(2025, 2, 28), "close": 300.0}])
        raise AssertionError(template)


def test_f8_reprices_listed_peers_on_the_bid_date():
    valuation = {"issue_pe": 30.0, "peer_median_pe": 20.0,
                 "peers": [{"name": "Peer One Ltd", "pe": 20.0, "eps": 10.0, "page": 9},
                           {"name": "Unlisted Two", "pe": 18.0, "eps": 2.0, "page": 9}]}
    got = reprice_peers(valuation, Port(), date(2025, 3, 1))
    assert got["repriced"] == 1 and got["peers"][0]["pe"] == 30.0 and got["peers"][0]["pe_printed"] == 20.0
    assert got["peers"][1]["pe"] == 18.0                    # no NSE match: printed P/E kept
    assert got["peer_median_pe"] == 24.0 and abs(got["premium"] - 1.25) < 1e-9


def test_a_failing_extract_model_falls_back_to_the_reasoning_role(tmp_path):
    import pytest
    from agent.llm import Gateway, GatewayError
    gw = Gateway(providers=[], cache_dir=tmp_path / "c", replay_dir=tmp_path / "r",
                 routes={"extract": "fast", "reason": "deep"})
    calls, events = [], []
    gw.on_call = events.append

    def fake(prompt, system=None, *, route="", **k):
        calls.append(route)
        if route == "extract":
            raise GatewayError("HTTP 502 empty response")
        return SimpleNamespace(text="ok", served_model="deep")
    gw._generate = fake
    assert gw.generate("hi", route="extract", purpose="t").served_model == "deep"
    assert calls == ["extract", "reason"] and "retrying on reason" in events[0]["error"]
    gw.sample_mode = True                      # replays never fall back
    with pytest.raises(GatewayError):
        gw.generate("hi", route="extract", purpose="t")


def test_a_cached_fallback_answer_is_reused_without_retrying_the_failing_model(tmp_path):
    from agent.llm import Gateway
    gw = Gateway(providers=[], cache_dir=tmp_path / "c", replay_dir=tmp_path / "r",
                 routes={"extract": "fast", "reason": "deep"})
    calls = []
    gw._is_cached = lambda prompt, system, schema, role, images=None: role == "reason"
    gw._generate = lambda prompt, system=None, *, route="", **k: (calls.append(route), SimpleNamespace(served_model="deep"))[1]
    assert gw.generate("hi", route="extract", purpose="t").served_model == "deep"
    assert calls == ["reason"]                  # the failing model is not tried again


def test_a_role_that_keeps_failing_goes_straight_to_its_fallback(tmp_path):
    from agent.llm import Gateway, GatewayError
    gw = Gateway(providers=[], cache_dir=tmp_path / "c", replay_dir=tmp_path / "r",
                 routes={"extract": "fast", "reason": "deep"})
    calls = []
    gw._is_cached = lambda *a, **k: False

    def fake(prompt, system=None, *, route="", **k):
        calls.append(route)
        if route == "extract":
            raise GatewayError("502")
        return SimpleNamespace(served_model="deep")
    gw._generate = fake
    for _ in range(3):
        gw.generate("hi", route="extract", purpose="t")
    assert calls == ["extract", "reason", "extract", "reason", "reason"]   # third call skips the fast model


def test_f1_cannot_be_dismissed_on_its_own_signal():
    from agent.agents.skeptic import Skeptic
    assert Skeptic.self_explained("F1", "Negative CFO is explained by working capital absorption from expansion")
    assert Skeptic.self_explained("F1", "Trade receivable days rose from 12 to 39 days")
    assert not Skeptic.self_explained("F1", "Receivables were collected in the following quarter (page 88)")
    assert not Skeptic.self_explained("F2", "Working capital rose with revenue")
