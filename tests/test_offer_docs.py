"""Cover-page extraction from offer documents (layouts seen in real RHPs)."""
from __future__ import annotations

import pytest

from redflag.pipeline.offer_docs import extract, firms

STACKED = """LISTING
The Equity Shares are proposed to be listed on the SME Platform of BSE Limited.
BOOK RUNNING LEAD MANAGER
Name and Logo Contact Person Email and Telephone
KHAMBATTA SECURITIES LIMITED
Mr. Chandan Mishra Email: ipo@khambattasecurities.com
REGISTRAR TO THE ISSUE
Name and Logo Contact Person Email and Telephone
MAASHITLA SECURITIES PRIVATE LIMITED
Mr. Mukul Agrawal Email: ipo@maashitla.com
BID/ ISSUE PROGRAMME
DETAILS OF ISSUE TO PUBLIC
Fresh Issue Upto 21,00,000 Equity Shares aggregating to Rs. [] Lakhs.
"""

TWO_COLUMN = """in consultation with the Lead Manager) as stated under Basis for Issue Price
LEAD MANAGER TO THE ISSUE REGISTRAR TO THE ISSUE
ARYAMAN FINANCIAL SERVICES LIMITED
60, Khatau Building, Ground Floor, Mumbai
U67190DL1993PLC052085 BIGSHARE SERVICES PRIVATE LIMITED
Tel. No.: +91 22 6263 8200
ISSUE PROGRAMME
"""

MM_PAGE = "MARKET MAKER\nThe Market Maker to the Issue, in this case being Spread X Securities Private Limited.\n"


def test_stacked_cover():
    got = extract([STACKED, MM_PAGE], "Icon Facilitators Limited")
    assert got["lead_managers"] == ["KHAMBATTA SECURITIES LIMITED"]
    assert got["registrar"] == "MAASHITLA SECURITIES PRIVATE LIMITED"
    assert got["market_maker"] == "Spread X Securities Private Limited"
    assert got["issue_shares"] == 2100000


def test_two_column_cover_ignores_prose_and_cin():
    got = extract([TWO_COLUMN], "CFF Fluid Control Limited")
    assert got["lead_managers"] == ["ARYAMAN FINANCIAL SERVICES LIMITED"]
    assert got["registrar"] == "BIGSHARE SERVICES PRIVATE LIMITED"


def test_banker_fallback_uses_register_names():
    cover = "Some cover without a header naming Hem Securities Limited as manager."
    got = extract([cover], bankers=["HEM SECURITIES LTD."])
    assert got["lead_managers"] == ["HEM SECURITIES LTD."]


def test_firms_skips_banks_and_the_company_itself():
    text = "Monitoring Agency CARE Ratings Limited. HDFC Bank Limited. Icon Facilitators Limited"
    assert firms(text, "Icon Facilitators Limited") == []


def test_bse_scrip_match_uses_listing_window_and_name():
    from redflag.pipeline.bse_universe import match_scrip
    ipo = {"company": "Icon Facilitators Limited", "close_date": "2025-06-26"}
    scrips = [
        {"symbol": "544420", "name": "ICON FACILITATORS LTD", "names": ["ICON FACILITATORS LTD"],
         "isin": "INE0X", "first_date": "2025-07-01"},
        {"symbol": "544421", "name": "ICONIC WEAR LTD", "names": ["ICONIC WEAR LTD"],
         "isin": "INE0Y", "first_date": "2025-07-01"},
        {"symbol": "500001", "name": "ICON FACILITATORS LTD", "names": ["ICON FACILITATORS LTD"],
         "isin": "INE0Z", "first_date": "2024-01-01"},
    ]
    scrip, score = match_scrip(ipo, scrips)
    assert scrip["symbol"] == "544420" and score > 0.9
    assert match_scrip({"company": "Nothing Alike Limited", "close_date": "2025-06-26"},
                       scrips)[0] is None


def test_bse_row_marks_price_basis():
    from redflag.pipeline.bse_universe import build_row
    ipo = {"open_date": "2025-06-24", "close_date": "2025-06-26", "company": "Icon Facilitators Limited",
           "issue_type": "Book Building", "price_low": 85.0, "price_high": 91.0, "face_value": 10.0,
           "offer_doc_url": "https://www.bsesme.com/download/1/x.pdf", "bse_ipo_id": "BSE-2025-ICON"}
    facts = {"lead_managers": ["KHAMBATTA SECURITIES LIMITED"], "registrar": "MAASHITLA",
             "market_maker": None, "issue_shares": 2100000, "fresh_shares": 2100000,
             "ofs_shares": None, "issue_type": "fresh"}
    row = build_row(ipo, {"symbol": "544420", "isin": "INE0X", "first_date": "2025-07-01"}, facts, None)
    assert row["ipo_id"] == "BSE-2025-544420" and row["issue_price"] == 91.0
    assert row["issue_price_basis"] == "band_upper" and row["issue_size_cr"] == round(2100000 * 91 / 1e7, 4)
    assert row["listing_date"] == "2025-07-01" and row["exchange"] == "BSE SME"


def test_follow_on_offer_is_detected():
    from redflag.pipeline.bse_universe import prior_listing
    scrips = [{"symbol": "543920", "name": "CFF FLUID CONTROL LIMITED", "isin": None,
               "first_date": "2023-06-12"}]
    assert prior_listing({"company": "CFF FLUID CONTROL LIMITED", "open_date": "2025-07-09"},
                         scrips)["symbol"] == "543920"
    assert prior_listing({"company": "CFF FLUID CONTROL LIMITED", "open_date": "2023-06-01"},
                         scrips) is None


def test_bse_match_handles_abbreviations_renames_and_late_listings():
    from redflag.pipeline.bse_universe import match_scrip
    meson = [{"symbol": "543982", "name": "MESON", "names": ["MESON"], "isin": None,
              "first_date": "2023-09-26"}]
    assert match_scrip({"company": "MESON VALVES INDIA LIMITED", "close_date": "2023-09-12"},
                       meson)[0]["symbol"] == "543982"
    renamed = [{"symbol": "544341", "name": "LANDMARK GLOBAL LEARNING LIMIT",
                "names": ["LANDMARK GLOBAL LEARNING LIMIT", "LANDMARK IMMIGRATION CONSULTAN"],
                "isin": None, "first_date": "2025-01-28"}]
    assert match_scrip({"company": "LANDMARK IMMIGRATION CONSULTANTS LIMITED",
                        "close_date": "2025-01-20"}, renamed)[0]["symbol"] == "544341"
    late = [{"symbol": "544417", "name": "Aten Papers & Foam Limited",
             "names": ["Aten Papers & Foam Limited"], "isin": None, "first_date": "2025-06-20"}]
    assert match_scrip({"company": "ATEN PAPERS AND FOAM LIMITED", "close_date": "2025-03-26"},
                       late)[0]["symbol"] == "544417"


def test_fixed_price_read_from_cover():
    from redflag.pipeline.offer_docs import issue_price
    assert issue_price("Issue of 10,00,000 Equity Shares at a price of ₹ 40/- per Equity Share") == 40.0


def test_cin_and_nic_section():
    from redflag.pipeline.offer_docs import cin, nic_section
    c = cin(["ICON FACILITATORS LIMITED\nCorporate Identification Number: U93000DL2013PLC258273"])
    assert c == "U93000DL2013PLC258273"
    assert nic_section(c) == ("93000", "R Arts, entertainment and recreation")
    assert nic_section("U29301HR2023PLC115705")[1] == "C Manufacturing"


def test_lot_size_from_document_text():
    from redflag.pipeline.offer_docs import lot_size
    assert lot_size(["The Market lot and Trading lot for the Equity Share is 1,000 and in multiples"]) == 1000
    assert lot_size(["Bid Lot 1200 Equity Shares and in multiples of 1200 Equity Shares thereafter"]) == 1200
    assert lot_size(["Bid Lot [●] Equity Shares and in multiples of [●] Equity Shares"]) is None


def test_registrar_with_bracketed_word():
    from redflag.pipeline.offer_docs import firms
    assert firms("REGISTRAR Purva Sharegistry (India) Private Limited Tel: 022") == \
        ["Purva Sharegistry (India) Private Limited"]


def test_pick_lead_managers_dedupes_and_prefers_registered():
    from redflag.pipeline.bse_universe import pick_lead_managers
    register = {"SHRENI SHARES LIMITED": "INM1", "GYR CAPITAL ADVISORS PRIVATE LIMITED": "INM2",
                "HEM SECURITIES LIMITED": "INM3", "SHARE INDIA CAPITAL SERVICES PVT LTD": "INM4"}
    assert pick_lead_managers(["SHRENI SHARES LIMITED", "Shreni Shares Private Limited"],
                              register) == ["SHRENI SHARES LIMITED"]
    assert pick_lead_managers(["GYR CAPITAL ADVISORS PRIVATE LIMITED",
                               "Alpha Numero Services Private Limited"],
                              register) == ["GYR CAPITAL ADVISORS PRIVATE LIMITED"]
    assert pick_lead_managers(["HEM SECURITIES LIMITED",
                               "SHARE INDIA CAPITAL SERVICES PRIVATE LIMITED"], register) == \
        ["HEM SECURITIES LIMITED", "SHARE INDIA CAPITAL SERVICES PRIVATE LIMITED"]


def test_fused_private_limited_normalises():
    from redflag.common.names import normalise
    assert normalise("Link Intime India PrivateLimited") == normalise("Link Intime India Private Limited")


def test_auditor_name_frn_and_change():
    from redflag.pipeline.offer_docs import auditor
    page = ("GENERAL INFORMATION\nStatutory and Peer Review Auditor\nM/s. PIYUSH KOTHARI & ASSOCIATES\n"
            "Chartered Accountants\nAddress: 208, Hemkoot Building, Ahmedabad\n"
            "Firm Registration No.: 140711W\nPeer Review Certificate No.: 016589\n"
            "Change in Auditors during the last three (3) years\n"
            "There has been no change in our Company's auditors in the last three (3) years.\n")
    got = auditor(["cover", page])
    assert got["name"] == "PIYUSH KOTHARI & ASSOCIATES"
    assert (got["frn"], got["peer_review_no"], got["changed_3y"], got["page"]) == ("140711W", "016589", 0, 2)


def test_auditor_change_detected_from_two_firms():
    from redflag.pipeline.offer_docs import auditor
    page = ("Statutory Auditor\nM/s. Dokania S. Kumar & Co, Chartered Accountants\n"
            "Change in Auditors during the last three (3) years\nName of the Auditor "
            "R.T Yadava & Co, Chartered Accountants M/s. Dokania S. Kumar & Co, Chartered Accountants\n")
    got = auditor([page])
    assert got["name"] == "Dokania S. Kumar & Co" and got["changed_3y"] == 1


def test_resolver_containment_and_manual_alias(ctx):
    from redflag.pipeline.universe import Resolver
    register = {"BIGSHARE SERVICES PVT. LTD.": "INR000001385",
                "PURVA SHAREGISTRY (INDIA)  P LTD (BUSICOMP SOFTWARE CONSULTANTS": "INR000001112",
                "MUFG INTIME INDIA PRIVATE LIMITED": "INR000004058"}
    r = Resolver(ctx, register, kind="registrar", table="registrars", id_col="registrar_id",
                 alias_table="registrar_alias",
                 manual={"Link Intime India Private Limited": "MUFG INTIME INDIA PRIVATE LIMITED"})
    big = r.resolve("Bigshare Services Private Limited")
    assert r.resolve("Rajiv Sharma Bigshare Services Private Limited") == big
    assert r.resolve("Big Share Services Priavte Limited") == big
    purva = r.resolve("Purva Sharegistry (India) Private Limited")
    assert ctx.conn.execute("SELECT sebi_reg_no FROM registrars WHERE registrar_id=?",
                            (purva,)).fetchone()[0] == "INR000001112"
    assert r.resolve("Link Intime India Private Limited") == r.resolve("MUFG INTIME INDIA PRIVATE LIMITED")


@pytest.mark.parametrize("page, name, frn", [
    ("Statutory Auditor\nM/s. Singhi & Co LLP, Chartered Accountants\nFRN: 302049E", "Singhi & Co LLP", "302049E"),
    ("Statutory Auditor/ The Statutory Auditors of our Company being M/s. Jagdish & Harish, "
     "Chartered Accountants (F.R.N: 120028W).", "Jagdish & Harish", "120028W"),
    ("Statutory Auditor/ Peer Review Auditor Statutory and peer review auditor of our Company, "
     "namely, M/s Gnanoba & Bhat, Chartered Accountants, having FRN: 000939S", "Gnanoba & Bhat", "000939S"),
    ("Statutory Auditor The Statutory Auditors of our Company having a valid Peer Review "
     "certificate in our case being M/s. Rajendra Prasad, Chartered Accountants", "Rajendra Prasad", None),
])
def test_auditor_name_variants(page, name, frn):
    from redflag.pipeline.offer_docs import auditor
    got = auditor([page])
    assert (got["name"], got["frn"]) == (name, frn)


@pytest.mark.parametrize("page, name", [
    ("Statutory Auditor The Statutory Auditors of our Company, being M/s Vijay Darji and Associates, "
     "Chartered Accountants holding a valid peer review certificate", "Vijay Darji and Associates"),
    ("Statutory Auditor of our Company. The Restated Financial Statements has been provided by a Peer "
     "Reviewed Chartered Accountant, M/s. A Y & Co., Chartered Accountants (having Peer Review)", "A Y & Co"),
])
def test_auditor_and_connector_and_descriptive_words(page, name):
    from redflag.pipeline.offer_docs import auditor
    assert auditor([page])["name"] == name
