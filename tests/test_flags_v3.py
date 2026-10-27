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
    assert calls == ["extract", "reason"] and "trying reason" in events[0]["error"]
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


def test_a_model_that_failed_is_tried_last_from_then_on(tmp_path):
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
    assert calls == ["extract", "reason", "reason", "reason"]   # later calls start on the model that answered


def test_f1_cannot_be_dismissed_on_its_own_signal():
    from agent.agents.skeptic import Skeptic
    assert Skeptic.self_explained("F1", "Negative CFO is explained by working capital absorption from expansion")
    assert Skeptic.self_explained("F1", "Trade receivable days rose from 12 to 39 days")
    assert not Skeptic.self_explained("F1", "Receivables were collected in the following quarter (page 88)")
    assert not Skeptic.self_explained("F2", "Working capital rose with revenue")


def test_when_both_nemotron_lanes_fail_the_backup_model_answers(tmp_path):
    from agent.llm import Gateway, GatewayError
    gw = Gateway(providers=[], cache_dir=tmp_path / "c", replay_dir=tmp_path / "r",
                 routes={"extract": "fast", "reason": "deep", "backup": "bolt"})
    calls = []
    gw._is_cached = lambda *a, **k: False

    def fake(prompt, system=None, *, route="", **k):
        calls.append(route)
        if route != "backup":
            raise GatewayError("502")
        return SimpleNamespace(served_model="bolt")
    gw._generate = fake
    assert gw.generate("hi", route="reason", purpose="t").served_model == "bolt"
    assert calls == ["reason", "extract", "backup"]          # the reasoning role falls back too
    gw.generate("hi", route="reason", purpose="t")
    gw.generate("hi", route="extract", purpose="t")
    assert calls[-2:] == ["backup", "backup"]                # broken lanes are skipped


def test_a_timed_out_model_is_not_waited_on_twice(tmp_path):
    import httpx
    import pytest
    from agent.llm import Gateway, GatewayError
    tries = []

    class Slow:
        name = "meterless"

        def chat(self, *a, **k):
            tries.append(1)
            raise httpx.ReadTimeout("timed out")
    gw = Gateway(providers=[Slow()], cache_dir=tmp_path / "c", replay_dir=tmp_path / "r")
    with pytest.raises(GatewayError):
        gw.generate("hi", route="meterless/auto", purpose="t")
    assert len(tries) == 1


def test_a_row_whose_numbers_sit_on_their_own_line_is_read():
    from agent.rhp import parse_statement
    text = """RESTATED STATEMENT OF PROFIT AND LOSS
(₹ in Million)
Particulars June 30, 2026 March 31, 2026
Revenue from operations 100.00 400.00
e) Depreciation and amortisation
expense
33.69 126.01
f) Other expenses 10.00 40.00
"""
    st = parse_statement(text, "pl", 91)
    assert st.values["depreciation"] == [3.369, 12.601]


def test_a_printed_tax_line_closes_a_loss_year():
    from agent.verify import check_pl
    ran, failed = check_pl({"pbt": [-0.29], "tax": [-0.17], "pat": [-0.12]}, 0)
    assert ran == ["PAT = PBT - tax"] and not failed


def test_cash_flows_that_close_on_the_printed_net_change_verify():
    from agent.verify import check_cf
    v = {"cfo": [8.035], "cfi": [-3.597], "cff": [-2.621], "net_change": [1.817],
         "opening_cash": [3.865], "closing_cash": [4.682]}
    assert check_cf(v, 0)[1] == []


def test_concentration_is_read_from_risk_factors_and_self_checking_tables():
    from agent.extract import top_shares
    risk = "We derived 73.54% and 71.33%, 67.44% and 67.43% of our revenue from our top ten customers for the periods."
    table = """Particulars % of revenue from operations (%)
Largest
Customer 792.88 19.97 2,584.60 20.40
Top 5
Customers 2,096.83 52.81 7,019.20 55.41
Top 10
Customers 2,920.12 73.54 9,036.03 71.33
"""
    got = {r["field"]: (r["value"], r["verified"]) for r in top_shares([risk, table])}
    assert got == {"biz.top1_customers_pct": (19.97, True), "biz.top5_customers_pct": (52.81, True),
                   "biz.top10_customers_pct": (73.54, True)}


def test_out_of_order_concentration_shares_are_not_trusted():
    from agent.extract import top_shares
    pages = ["Top 5 suppliers accounted for 26.64% of purchases. Top 10 suppliers accounted for 5.96% of purchases."] * 2
    assert not any(r["verified"] for r in top_shares(pages))


def test_layout_rows_with_labels_after_the_numbers_are_read():
    from agent.rhp import parse_statement
    text = """CONSOLIDATED STATEMENT OF PROFIT & LOSS AS RESTATED
(₹ in Lakhs)
Particulars June 30, 2026 March 31, 2026
I 26 4,565.18 9,133.83 Revenue From Operation
II 27 12.26 45.02 Other Income
III 4,577.44 9,178.85 Total Income (I + II)
Total Expenses (IV) 3,747.72 7,524.67
IX 829.73 1,654.18 Profit before tax (VII-VIII)
Total Tax Expenses 213.05 417.58
XI Profit ( Loss) for the period from continuing operations (IX-X) 616.67 1,236.61
XII Profit (Loss) for the period from discontinuing operations - -
"""
    st = parse_statement(text, "pl", 308)
    assert st.values["revenue"] == [45.6518, 91.3383] and st.values["total_income"][0] == 45.7744
    assert st.values["pat"] == [6.1667, 12.3661]


def test_no_model_is_retried_and_a_plain_route_falls_back_to_every_role(tmp_path):
    from agent.llm import Gateway, GatewayError
    gw = Gateway(providers=[], cache_dir=tmp_path / "c", replay_dir=tmp_path / "r",
                 routes={"extract": "fast", "reason": "deep", "backup": "bolt"})
    gw._is_cached = lambda *a, **k: False
    calls = []

    def fake(prompt, system=None, *, route="", attempts=2, **k):
        calls.append((route, attempts))
        if route != "backup":
            raise GatewayError("502")
        return SimpleNamespace(served_model="bolt")
    gw._generate = fake
    assert gw.generate("hi", route="meterless/auto", purpose="t").served_model == "bolt"
    assert calls == [("meterless/auto", 1), ("extract", 1), ("reason", 1), ("backup", 1)]


def test_roles_pinned_to_one_model_are_tried_once(tmp_path):
    from agent.llm import Gateway, GatewayError
    gw = Gateway(providers=[], cache_dir=tmp_path / "c", replay_dir=tmp_path / "r",
                 routes={"extract": "same", "reason": "same", "backup": "bolt"})
    gw._is_cached = lambda *a, **k: False
    calls = []

    def fake(prompt, system=None, *, route="", **k):
        calls.append(route)
        if route != "backup":
            raise GatewayError("502")
        return SimpleNamespace(served_model="bolt")
    gw._generate = fake
    gw.generate("hi", route="extract", purpose="t")
    assert calls == ["extract", "backup"]


def test_f11_cannot_be_dismissed_on_the_reports_own_caveat():
    from agent.agents.skeptic import Skeptic
    assert Skeptic.self_explained("F11", "The flag states that being named in a SEBI order is not a finding against the company.")
    assert not Skeptic.self_explained("F11", "The RHP shows the named Rikhav Securities is a different entity (page 210).")


def test_a_cached_backup_answer_does_not_stop_nemotron_being_asked(tmp_path):
    from agent.llm import Gateway
    gw = Gateway(providers=[], cache_dir=tmp_path / "c", replay_dir=tmp_path / "r",
                 routes={"extract": "fast", "reason": "deep", "backup": "bolt"})
    gw._is_cached = lambda prompt, system, schema, role, images=None: role == "backup"
    calls = []
    gw._generate = lambda prompt, system=None, *, route="", **k: (calls.append(route), SimpleNamespace(served_model=route))[1]
    assert gw.generate("hi", route="extract", purpose="t").served_model == "extract"
    assert calls == ["extract"]


def test_rows_printed_one_per_line_or_cut_off_are_still_read():
    from agent.narrative import _chunk_data, objects_summary
    lines = ('{"date": "2025-10-28", "nature": "Private Placement", "shares_allotted": 113637, "issue_price": 88.0}\n'
             '{"date": "2025-10-28", "nature": "Private Placement", "no_of_shares": 113636, "issue_price": 88.0}')
    assert [r["shares"] for r in _chunk_data(lines)["rows"]] == [113637, 113636]
    cut = ('{"unit": "million", "rows": [{"object": "Repayment of certain borrowings", "kind": "Repayment", '
           '"amount": 1250.00}, {"object": "General corporate purposes", "amount": 300.0}, {"object": "Capi')
    data = _chunk_data(cut)
    assert data["unit"] == "million" and len(data["rows"]) == 2
    assert objects_summary(data)["by_kind_cr"] == {"loan_repayment": 125.0, "gcp": 30.0}


def test_issue_pe_comes_from_the_price_when_the_rhp_prints_a_placeholder():
    from agent.narrative import peer_valuation
    data = {"issue_eps": 7.64, "peers": [{"name": "BMW Industries Limited", "pe": 14.98},
                                         {"name": "Shiv Aum Steels Limited", "pe": 98.54}]}
    v = peer_valuation(data, "Bombay Coated and Special Steels Limited", issue_price=72.0)
    assert v["issue_pe"] == 9.42 and abs(v["peer_median_pe"] - 56.76) < 1e-9


def test_years_under_one_march_heading_and_shifted_draft_columns():
    from agent.rhp import find_periods
    from agent.specialists import _Facts, f12_drift
    got = [p.end.isoformat() for p in find_periods("September 30, 2025 As at 31st March 2025 2024 2023")]
    assert got == ["2025-09-30", "2025-03-31", "2024-03-31", "2023-03-31"]
    rhp = _Facts([{"field": "pl.pbt", "fiscal_year": "FY25", "value": 5.36, "verified": 1},
                  {"field": "pl.pbt", "fiscal_year": "FY24", "value": 4.08, "verified": 1}])
    shifted = [{"field": "pl.pbt", "fiscal_year": "FY25", "value": 4.08, "page": 1, "verified": 1}]
    assert f12_drift(rhp, shifted) is None


def test_a_windowed_read_covers_every_page_even_after_the_model_says_done():
    from agent.engines.markovian import ChunkConfig, Markovian
    seen = []

    def gen(prompt, attachments=None, system_prompt=None, on_stream=None, abort_signal=None):
        seen.append(attachments[-1]["name"])
        return {"text": '{"rows": []} [TASK_COMPLETE]'}
    run = Markovian(ChunkConfig(chunk_size=8000, max_chunks=6, carryover_tokens=640), generator=gen).run(
        "read the table", mode="EXTRACT", reflect=False, windows=["p1", "p2", "p3"])
    assert seen == ["part 1 of 3", "part 2 of 3", "part 3 of 3"] and run.status == "completed"


def test_the_peer_table_is_read_in_code():
    from agent.narrative import peer_table
    page = """5. Comparison of Accounting Ratio with Industry Peers:
Name of the
Company
Current
Market Price
(₹)
Face Value (₹) Basic EPS (₹) P/E (x) times RoNW(%) Net Asset
Value per
share (₹)
Yash
Highvoltage
Limited
NA 5 5.45 [•] 32.51% 19.03
Peer Group
Crompton
Greaves Power
and Industrial
Solutions
Limited
732.70 2 9.33 78.53 47.29% 19.76
Hitachi Energy
India Limited
11,970 2 38.64 309.78 13% 320.86
Notes:
a) The figures for our company are as certified by the Statutory Auditor."""
    got = peer_table([page], [1], "Yash Highvoltage Limited")
    assert got["issue_eps"] == 5.45
    assert [(p["name"], p["pe"]) for p in got["peers"]] == [
        ("Crompton Greaves Power and Industrial Solutions Limited", 78.53), ("Hitachi Energy India Limited", 309.78)]
    prose = "There is only 1 peer competitor, i.e. Allied Digital Services Limited whose PE as on March 31, 2022 is 9.82"
    assert peer_table([prose], [1], "Synoptics Technologies Limited")["peers"][0]["pe"] == 9.82
    kpi = page.replace("Hitachi Energy\nIndia Limited", "EBITDA Margin (%)")
    assert [p["name"] for p in peer_table([kpi], [1], "Yash Highvoltage Limited")["peers"]] == [
        "Crompton Greaves Power and Industrial Solutions Limited"]
