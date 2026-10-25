"""The deterministic statement parser (Extractor strategy 1) and the Verifier's identities."""
from __future__ import annotations

from datetime import date

import pytest

from agent.config import ROOT
from agent.rhp import Period, find_periods, find_unit, load_pages, parse_number, parse_statements
from agent.verify import check_cf, check_pl, close, verify

PL_PAGE = """RESTATED STATEMENT OF PROFIT & LOSS
(Amount in Lakhs, Unless Otherwise Stated)
PARTICULARS Annexure AS AT 31-Mar-24 31-Mar-23 31-Mar-22
(a) Revenue From Operations 22 3,465.46 5,092.32 2,207.81
(b) Other Income 23 1.35 4.75 3.62
2 Total Income (1a+1b) 3,466.81 5,097.07 2,211.43
(d) Finance Cost 27 156.89 130.57 165.19
(e) Depreciation 28 339.10 387.68 232.13
4 Total Expenditure 3(a) to 3(f) 2,820.85 4,510.62 1,991.36
5 Profit/(Loss) Before Tax (2-4) 645.96 586.45 220.07
Exceptional Items - - -
7 Profit/(Loss) for the Year (5-6) 525.36 432.36 182.06
""" + "1.00 " * 40


def test_units_periods_and_numbers():
    assert find_unit("(Amount in Lakhs, Unless Otherwise Stated)") == "lakh"
    assert find_unit("(₹ in Millions)") == "million"
    assert find_unit("Rs. in lacs") == "lakh"
    assert [p.label for p in find_periods("As at March 31, 2024 March\r\n31, 2023 2021-22")] == \
        ["FY24", "FY23", "FY22"]
    assert [p.label for p in find_periods("for the period from 1st April 2023 to 31st Dec 2023")] \
        == ["S2023-12-31"]                      # a period start is never a period end
    assert (parse_number("(375.01)"), parse_number("1,436.51"), parse_number("-")) == \
        (-375.01, 1436.51, 0.0)
    assert Period(date(2024, 3, 31)).is_full_year and not Period(date(2023, 12, 31)).is_full_year


def test_parser_reads_a_profit_and_loss_page_in_crore():
    st = parse_statements([PL_PAGE])["pl"]
    assert st.unit == "lakh" and [p.label for p in st.periods] == ["FY24", "FY23", "FY22"]
    assert st.values["revenue"] == [34.6546, 50.9232, 22.0781]
    assert st.values["pat"][0] == pytest.approx(5.2536)
    assert all(c.passed for c in verify(st)), [c.failed for c in verify(st)]


def test_identities_catch_bad_numbers():
    good = {"revenue": [10.0], "other_income": [1.0], "total_income": [11.0],
            "total_expenses": [8.0], "pbt": [3.0], "pat": [2.2]}
    assert check_pl(good, 0)[1] == []
    assert check_pl({**good, "total_income": [15.0]}, 0)[1]
    assert check_pl({**good, "pat": [9.0]}, 0)[1]                 # tax of -6 is implausible
    cf = {"cfo": [5.0], "cfi": [-3.0], "cff": [-1.0], "opening_cash": [2.0], "closing_cash": [3.0]}
    assert check_cf(cf, 0)[1] == [] and check_cf({**cf, "closing_cash": [9.0]}, 0)[1]
    assert close(100.0, 101.5) and not close(100.0, 103.0) and close(0.01, 0.05)


def test_demo_rhp_parses_and_verifies():
    import sqlite3
    db = ROOT / "data" / "archive.db"
    if not db.exists():
        pytest.skip("archive.db not built")
    sha = sqlite3.connect(db).execute(
        "SELECT sha256 FROM docs WHERE ipo_id = 'NSE-2023-SYNOPTICS'").fetchone()
    pages = load_pages(ROOT / "data" / "cache" / "pages", sha[0]) if sha else []
    if not pages:
        pytest.skip("demo RHP pages not cached")
    st = parse_statements(pages)
    pl = st["pl"]
    fy22 = [p.label for p in pl.periods].index("FY22")
    assert pl.values["revenue"][fy22] == pytest.approx(50.9232)        # 5,092.32 lakh, p.45
    assert pl.values["pat"][fy22] == pytest.approx(4.3236)
    assert verify(pl)[fy22].passed
