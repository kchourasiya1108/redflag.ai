from __future__ import annotations

from datetime import date, timedelta

import pytest

from redflag.pipeline import labels, prices, sebi, universe

DETAIL = {
    "metaInfo": {"isin": "INE2O2Y01016", "industry": "Gems", "listingDate": "2026-10-12"},
    "demandGraph": {"noOfTimesIssueSubscribed": "2.61"},
    "issueInfo": {"dataList": [
        {"title": "Issue Period", "value": "05-Oct-2026 to 07-Oct-2026"},
        {"title": "Issue Size", "value": '"Initial Public Offering of upto 42,67,200 '
                                         'fresh equity shares (Including market maker '
                                         'portion of 2,14,400 equity shares)"'},
        {"title": "Lot Size", "value": "1600 Equity Shares"},
        {"title": "Face Value", "value": "Rs.10 per Equity Share"},
        {"title": "Book Running Lead Managers",
         "value": "Affinity Global Capital Market Private Limited"},
        {"title": "Name of the Registrar", "value": "Cameo Corporate Service Limited"},
        {"title": "Red Herring Prospectus",
         "value": "https://nsearchives.nseindia.com/content/ipo/RHP_RKFAL.zip"},
    ]},
}
ISSUE = {"companyName": "R.K. Fashion Accessories Limited", "symbol": "RKFAL",
         "ipoStartDate": "05-OCT-2026", "ipoEndDate": "07-OCT-2026",
         "issuePrice": " 82", "priceRange": "Rs.77 to Rs.82", "securityType": "SME"}


def test_parse_issue_reads_nse_detail():
    row = universe.parse_issue(ISSUE, DETAIL)
    assert row["ipo_id"] == "NSE-2026-RKFAL"
    assert (row["open_date"], row["close_date"], row["listing_date"]) == (
        "2026-10-05", "2026-10-07", "2026-10-12")
    assert row["issue_shares"] == 4267200 and row["issue_type"] == "fresh"
    assert row["issue_size_cr"] == pytest.approx(4267200 * 82 / 1e7, abs=1e-3)
    assert (row["lot_size"], row["face_value"], row["final_subscription_x"]) == (1600, 10, 2.61)
    assert row["banker_raw"] == "Affinity Global Capital Market Private Limited"
    assert row["rhp_url"].endswith("RHP_RKFAL.zip")


@pytest.mark.parametrize("text, total, fresh, ofs, kind", [
    ("Fresh issue of 33,80,000 Equity Shares and OFS of 15,00,000 Equity shares",
     4880000, 3380000, 1500000, "fresh+ofs"),
    ("Initial Public offer of 45,80,000 Equity Shares", 4580000, 4580000, None, "fresh"),
    ("Offer for Sale of upto 12,00,000 Equity Shares", 1200000, None, 1200000, "ofs"),
])
def test_parse_issue_size(text, total, fresh, ofs, kind):
    got = universe.parse_issue_size(text)
    assert (got["total"], got["fresh"], got["ofs"], got["type"]) == (total, fresh, ofs, kind)


@pytest.mark.parametrize("q", ['"', "'"])
def test_parse_register_handles_both_quote_styles(q):
    page = (f"<div class={q}title{q}><span>Name</span></div>"
            f"<div class={q}value varun-text{q}><span>HEM SECURITIES LTD.</span></div>"
            f"<div class={q}title{q}><span>Registration No.</span></div>"
            f"<div class={q}value{q}><span>INM000010981</span></div>")
    assert universe.parse_register(page) == {"HEM SECURITIES LTD.": "INM000010981"}


BHAV = """MARKET,SERIES,SYMBOL,SECURITY,PREV_CL_PR,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,NET_TRDVAL,NET_TRDQTY,CORP_IND,HI_52_WK,LO_52_WK
N,SM,AATMAJ,AATMAJ HEALTHCARE LIMITED,        17.60,        18.00,        18.05,        17.05,        17.05,                   314100.00,18000, ,        41.50,        16.00
N,EQ,IGNORED,NOT SME,1,1,1,1,1,1,1, ,1,1
"""


def test_parse_bhavcopy_keeps_sme_series_only():
    rows = prices.parse_bhavcopy(BHAV, date(2025, 3, 10))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AATMAJ" and rows[0]["close"] == 17.05
    assert rows[0]["prev_close"] == 17.60 and rows[0]["volume"] == 18000


def _calendar(n: int, start=date(2024, 1, 1)) -> list[str]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _seed(conn, cal, closes: dict[int, float], symbol="ABC", issue_price=100.0,
          prev_close_overrides: dict[int, float] | None = None):
    conn.executemany("INSERT INTO trading_days VALUES ('NSE', ?)", [(d,) for d in cal])
    conn.execute(
        "INSERT INTO ipos (ipo_id, company, exchange, symbol, issue_price, open_date, "
        "close_date, listing_date, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"NSE-2024-{symbol}", f"{symbol} Ltd", "NSE Emerge", symbol, issue_price,
         cal[0], cal[0], cal[2], "listed"))
    prev = None
    for i, close in sorted(closes.items()):
        reported = (prev_close_overrides or {}).get(i, prev)
        conn.execute("INSERT INTO prices (exchange, symbol, series, date, close, "
                     "prev_close, volume) VALUES ('NSE', ?, 'SM', ?, ?, ?, 1000)",
                     (symbol, cal[i], close, reported))
        prev = close
    return conn.execute("SELECT * FROM ipos WHERE symbol=?", (symbol,)).fetchone()


def test_label_crash_uses_close_at_126_trading_days(conn):
    cal = _calendar(200)
    closes = {i: 100.0 for i in range(2, 128)} | {128: 55.0}
    ipo = _seed(conn, cal, closes)
    label = labels.compute_label(conn, cal, ipo, None)
    assert label["outcome_known_on"] == cal[2 + 126]
    assert label["ret_6m"] == pytest.approx(-0.45)
    assert label["crashed"] == 1 and label["adverse"] == 1 and label["suspended"] == 0


def test_label_adjusts_for_bonus(conn):
    cal = _calendar(200)
    # 1:1 bonus on day 50: the price halves but each IPO share is now two shares.
    closes = {i: 100.0 for i in range(2, 50)} | {i: 50.0 for i in range(50, 129)}
    ipo = _seed(conn, cal, closes)
    kind, ratio = prices.parse_action("BONUS 1:1")
    conn.execute("INSERT INTO corp_actions VALUES ('NSE', 'ABC', ?, ?, ?, 'test')",
                 (cal[50], kind, ratio))
    label = labels.compute_label(conn, cal, ipo, None)
    assert label["adj_factor"] == pytest.approx(2.0)
    assert label["ret_6m"] == pytest.approx(0.0)
    assert label["crashed"] == 0


@pytest.mark.parametrize("subject, expected", [
    ("BONUS 1:1", ("bonus", 0.5)),
    ("BONUS 3:1", ("bonus", 0.25)),
    ("BONUS 4:5", ("bonus", round(5 / 9, 6))),
    ("FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 10/- PER SHARE TO RE 1/- PER SHARE",
     ("split", 0.1)),
    ("DIVIDEND - RE 0.45 PER SHARE", None),
])
def test_parse_action(subject, expected):
    assert prices.parse_action(subject) == expected


def test_label_marks_untraded_stock_suspended(conn):
    cal = _calendar(200)
    ipo = _seed(conn, cal, {i: 100.0 for i in range(2, 60)})
    label = labels.compute_label(conn, cal, ipo, None)
    assert label["suspended"] == 1 and label["adverse"] == 1
    assert label["stale_close"] == 1


def test_label_is_none_until_outcome_known(conn):
    cal = _calendar(100)
    ipo = _seed(conn, cal, {i: 100.0 for i in range(2, 100)})
    assert labels.compute_label(conn, cal, ipo, None) is None


def test_label_b_sets_adverse(conn):
    cal = _calendar(200)
    ipo = _seed(conn, cal, {i: 100.0 for i in range(2, 129)})
    label = labels.compute_label(conn, cal, ipo, "2024-09-01")
    assert label["sebi_action"] == 1 and label["adverse"] == 1 and label["crashed"] == 0


LISTING = """<tr><th>Date</th></tr><tr role='row' class='odd'><td>May 06, 2025</td><td>
<a href='https://www.sebi.gov.in/enforcement/orders/may-2025/interim-order-in-the-matter-of-synoptics-technologies-limited_93822.html'
target="_blank" title="x" class='points'>Interim Order in the matter of Synoptics Technologies Limited
</a></td></tr>"""


def test_parse_sebi_listing():
    [order] = sebi.parse_listing(LISTING)
    assert order["order_id"] == "sebi-93822"
    assert order["order_date"] == "2025-05-06"
    assert order["order_type"] == "interim"
    assert order["title"] == "Interim Order in the matter of Synoptics Technologies Limited"


def test_match_title_needs_whole_words():
    ents = [{"entity_id": "e1", "type": "company", "name": "Synoptics Technologies Ltd",
             "query": "synoptics technologies"},
            {"entity_id": "e2", "type": "company", "name": "Opti Ltd", "query": "opti"}]
    hits = sebi.match_title("Interim Order in the matter of Synoptics Technologies Limited", ents)
    assert [h["entity_id"] for h in hits] == ["e1"]


def test_bse_bonus_inferred_from_price_gap(conn):
    from redflag.pipeline.bse_prices import infer_corp_actions
    conn.execute("INSERT INTO ipos (ipo_id, company, exchange, symbol, status) "
                 "VALUES ('BSE-2023-1', 'A Ltd', 'BSE SME', '543937', 'listed')")
    closes = [355.0, 356.0, 355.3, 177.65, 178.0, 180.0, 179.0, 181.0]
    for i, c in enumerate(closes):
        conn.execute("INSERT INTO prices (exchange, symbol, series, date, open, close, volume) "
                     "VALUES ('BSE', '543937', 'M', ?, ?, ?, 100)", (f"2023-12-{i + 1:02d}", c, c))
    [action] = infer_corp_actions(conn)
    assert action["ex_date"] == "2023-12-04" and action["ratio"] == 0.5
    assert action["source"] == "bse_price_gap"


def test_label_relative_to_index(conn):
    cal = _calendar(200)
    ipo = _seed(conn, cal, {i: 100.0 for i in range(2, 128)} | {128: 50.0})
    conn.executemany("INSERT INTO index_prices VALUES ('NIFTY SME EMERGE', ?, ?)",
                     [(cal[2], 1000.0), (cal[128], 700.0)])
    label = labels.compute_label(conn, cal, ipo, None)
    assert label["ret_6m"] == pytest.approx(-0.5)
    assert label["index_ret_6m"] == pytest.approx(-0.3)
    assert label["crashed"] == 1 and label["crashed_vs_index"] == 0   # 0.5/0.7-1 = -28.6%
