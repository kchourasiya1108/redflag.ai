"""Leakage guard and Data Port: the tests that keep the backtest honest."""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from agent.tools_zetaris import (DataPort, LeakageError, MockZetaris, TruncatedResult,
                                 ZetarisError, ZetarisRestClient, bind, load_catalogue)
from agent.features import adverse_as_of, compute_features

AS_OF = date(2025, 3, 10)


def _ipo(conn, ipo_id, open_date, banker_id=1):
    conn.execute("INSERT OR IGNORE INTO bankers VALUES (?, ?, NULL, 'universe')",
                 (banker_id, f"Banker {banker_id} Ltd"))
    conn.execute("INSERT INTO ipos (ipo_id, company, exchange, symbol, issue_price, "
                 "open_date, close_date, listing_date, status, banker_id) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (ipo_id, ipo_id, "NSE Emerge", ipo_id, 100, open_date, open_date,
                  open_date, "listed", banker_id))
    conn.execute("INSERT INTO ipo_banker VALUES (?, ?, 0)", (ipo_id, banker_id))


def _label(conn, ipo_id, known_on, crashed=0, sebi_date=None, ret=0.0):
    conn.execute("INSERT INTO labels (ipo_id, listing_date, issue_price, outcome_known_on, "
                 "adj_factor, ret_6m, crashed, suspended, stale_close, sebi_action, "
                 "sebi_action_date, adverse) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (ipo_id, "2024-01-01", 100, known_on, 1.0, ret, crashed, 0, 0,
                  int(sebi_date is not None), sebi_date,
                  int(bool(crashed) or sebi_date is not None)))


@pytest.fixture
def archive(conn, cfg):
    _ipo(conn, "JUDGED", "2025-03-10")
    _ipo(conn, "X", "2024-06-01")
    _label(conn, "X", "2025-01-05", crashed=1, ret=-0.6)        # known before as_of
    _ipo(conn, "Z", "2024-11-01")
    _label(conn, "Z", "2025-05-20", crashed=1, ret=-0.7)        # known after as_of
    _ipo(conn, "LATE_SEBI", "2024-03-01")
    _label(conn, "LATE_SEBI", "2024-09-01", sebi_date="2025-06-01")  # order after as_of
    conn.commit()
    return MockZetaris(cfg.archive_db)


def test_track_record_excludes_outcomes_known_after_as_of(archive):
    port = DataPort(archive, AS_OF)
    rows = port.run("banker_track_record", banker_id=1, as_of=AS_OF).rows
    assert {r["ipo_id"] for r in rows} == {"X", "LATE_SEBI"}   # "IPO Z" is excluded


def test_later_sebi_order_does_not_make_a_past_ipo_adverse(archive):
    port = DataPort(archive, AS_OF)
    late = next(r for r in port.run("banker_track_record", banker_id=1, as_of=AS_OF).rows
                if r["ipo_id"] == "LATE_SEBI")
    assert adverse_as_of(late, AS_OF) is False
    assert adverse_as_of(late, date(2025, 7, 1)) is True


def test_banker_features_use_only_the_past(archive):
    feats = compute_features("JUDGED", AS_OF, DataPort(archive, AS_OF))
    assert feats["f_banker_n"] == 2
    assert feats["f_banker_low_history"] == 1.0
    # base rate as of AS_OF: X adverse, LATE_SEBI not yet -> 1/2
    assert feats["base_rate"] == pytest.approx(0.5)
    assert feats["f_banker_crash"] == pytest.approx((1 + 5 * 0.5) / (2 + 5))


def test_as_of_must_match_the_run(archive):
    port = DataPort(archive, AS_OF)
    with pytest.raises(LeakageError):
        port.run("banker_track_record", banker_id=1, as_of=date(2026, 1, 1))


def test_guard_drops_rows_a_bad_template_lets_through(archive):
    catalogue = load_catalogue()
    bad = catalogue["banker_track_record"]
    leaky = type(bad)(**{**bad.__dict__, "sql": bad.sql.replace(
        "AND l.outcome_known_on < :as_of", "AND :as_of IS NOT NULL")})
    port = DataPort(archive, AS_OF, catalogue={"banker_track_record": leaky})
    result = port.run("banker_track_record", banker_id=1, as_of=AS_OF)
    assert result.dropped_by_guard == 1
    assert all(r["outcome_known_on"] < AS_OF for r in result.rows)


def test_template_without_time_rule_is_rejected(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text("bad:\n  params: {}\n  columns: {a: str}\n  sql: SELECT 1 AS a\n")
    with pytest.raises(LeakageError):
        load_catalogue(path)


def test_offline_only_template_is_refused_in_a_live_run(archive):
    with pytest.raises(LeakageError):
        DataPort(archive, AS_OF).run("labelled_ipos")


@pytest.mark.parametrize("value", ["1; DROP TABLE ipos", "x' OR '1'='1"])
def test_bind_rejects_injection(value):
    t = load_catalogue()["ipo_bankers"]
    with pytest.raises(ValueError):
        bind(t, {"ipo_id": value})


def test_truncated_result_raises(archive):
    catalogue = load_catalogue()
    t = catalogue["outcomes_known_before"]
    tiny = type(t)(**{**t.__dict__, "max_rows": 1})
    port = DataPort(archive, AS_OF, catalogue={"outcomes_known_before": tiny})
    with pytest.raises(TruncatedResult):
        port.run("outcomes_known_before", as_of=AS_OF)


def test_rest_client_types_string_values_and_sends_fresh_query_ids():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["queryId"])
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json={
            "headers": [{"name": "ipo_id"}, {"name": "crashed"}, {"name": "suspended"},
                        {"name": "sebi_action"}, {"name": "sebi_action_date"},
                        {"name": "outcome_known_on"}],
            "data": [["X", "1.0E0", "0", "0", None, "2025-01-05"]]})

    client = ZetarisRestClient("http://z.test/api/v1.0", "k", "1",
                               transport=httpx.MockTransport(handler))
    rows = DataPort(client, AS_OF).run("outcomes_known_before", as_of=AS_OF).rows
    assert rows == [{"ipo_id": "X", "crashed": 1, "suspended": 0, "sebi_action": 0,
                     "sebi_action_date": None, "outcome_known_on": date(2025, 1, 5)}]
    DataPort(client, AS_OF).run("outcomes_known_before", as_of=AS_OF)
    assert len(set(seen)) == 2


@pytest.mark.parametrize("status, calls", [(400, 1), (401, 1), (429, 2), (503, 2)])
def test_rest_client_retries_only_transient_errors(status, calls):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["queryId"])
        return httpx.Response(status, text="nope")

    client = ZetarisRestClient("http://z.test/api/v1.0", "k", "1", retries=1, backoff_s=0,
                               transport=httpx.MockTransport(handler))
    with pytest.raises(ZetarisError, match=f"HTTP {status}"):
        client.query("SELECT 1", 1)
    assert len(seen) == calls and len(set(seen)) == calls
