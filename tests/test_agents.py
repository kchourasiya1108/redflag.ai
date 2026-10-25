"""The agent loop end to end on the sample database, plus the depth policy."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import date

import pytest

from agent.agents.planner import FORENSIC, LIGHT, STANDARD, choose_depth
from agent.config import ROOT
from agent.features import Named, watchlist_links
from agent.main import Orchestrator, RunError, investigate
from agent.tools_zetaris import DataPort, MockZetaris

SAMPLE = ROOT / "data" / "sample" / "archive_sample.db"
DEMO = "NSE-2023-SYNOPTICS"


@pytest.fixture
def sample_cfg(cfg, tmp_path):
    (cfg.data / "sample").mkdir(parents=True, exist_ok=True)
    shutil.copy(SAMPLE, cfg.data / "sample" / "archive_sample.db")
    (cfg.data / "model").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "data" / "model" / "scorer.json", cfg.data / "model" / "scorer.json")
    return cfg


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_sample_run_end_to_end(sample_cfg):
    out = investigate(sample_cfg, DEMO, run_id="run-t1", sample_mode=True)
    assert out["status"] == "success" and out["output"]["rating"]
    assert out["output"]["data_source"].startswith("sample")
    case = json.loads(open(out["case_file"]).read())
    assert case["meta"]["as_of"] == out["output"]["as_of"] and case["writes"] > 0
    assert {e["type"] for e in case["entities"]} >= {"banker", "auditor"}
    events = _events(sample_cfg.logs / "trace-run-t1.jsonl")
    agents = [e["agent_name"] for e in events if e["action"] != "data.query"]
    assert agents.index("Watchlist") < agents.index("Planner") < agents.index("Scorer")
    assert all(e["detail"]["source"] == "mock-sqlite" for e in events if e["action"] == "data.query")
    assert open(out["report"]).read().startswith("# RedFlag report")


def test_unknown_ipo_is_a_structured_error(sample_cfg):
    with pytest.raises(RunError) as err:
        investigate(sample_cfg, "NSE-2099-NOPE", sample_mode=True)
    assert err.value.type == "UnknownIPO" and not err.value.recoverable


def test_confirmed_watchlist_link_flags_and_escalates(sample_cfg):
    db = sample_cfg.data / "sample" / "archive_sample.db"
    meta = DataPort(MockZetaris(db), None).run("ipo_meta", ipo_id=DEMO).rows[0]
    banker = DataPort(MockZetaris(db), None).run("ipo_bankers", ipo_id=DEMO).rows[0]
    with sqlite3.connect(db) as conn:
        hit = conn.execute("""SELECT e.order_id FROM sebi_order_entity e JOIN sebi_orders o
                              USING (order_id) WHERE e.entity_id = ? AND o.order_date < ?""",
                           (f"ent:banker:{banker['banker_id']}",
                            meta["open_date"].isoformat())).fetchone()
        if hit is None:
            pytest.skip("demo banker has no order before its bid date")
        conn.execute("UPDATE sebi_order_entity SET reviewed = 1 WHERE order_id = ?", hit)
    out = investigate(sample_cfg, DEMO, run_id="run-t2", sample_mode=True)
    f11 = [r for r in out["output"]["reasons"] if r["flag"] == "F11"]
    assert f11 and f11[0]["severity"] == "HIGH" and f"sebi:{hit[0]}" in f11[0]["evidence"]
    assert out["output"]["depth"] == FORENSIC


def test_watchlist_matches_typos_but_only_reviewed_rows_confirm(tmp_path):
    db = tmp_path / "s.db"
    shutil.copy(SAMPLE, db)
    with sqlite3.connect(db) as conn:
        name, order = conn.execute("SELECT entity_name, order_id FROM sebi_order_entity "
                                   "WHERE length(entity_name) > 20 LIMIT 1").fetchone()
    typo = name[:5] + name[6:]                              # one letter dropped
    named = [Named("person", typo)]
    as_of = date(2030, 1, 1)
    links = watchlist_links(DataPort(MockZetaris(db), as_of), DEMO, as_of, named)
    assert any(l.order["order_id"] == order for l in links)
    assert not any(l.confirmed for l in links)              # nobody has reviewed it
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sebi_order_entity SET reviewed = 1 WHERE order_id = ?", (order,))
    links = watchlist_links(DataPort(MockZetaris(db), as_of), DEMO, as_of, named)
    assert any(l.confirmed and l.order["order_id"] == order for l in links)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sebi_order_entity SET reviewed = -1 WHERE order_id = ?", (order,))
    links = watchlist_links(DataPort(MockZetaris(db), as_of), DEMO, as_of, named)
    assert not any(l.order["order_id"] == order for l in links)    # rejected by the reviewer


def test_hard_time_limit_returns_partial(sample_cfg, monkeypatch):
    import agent.main as main
    monkeypatch.setattr(main, "HARD_LIMIT_S", -1)
    out = investigate(sample_cfg, DEMO, sample_mode=True)
    assert out["status"] == "partial" and out["output"]["rating"]


@pytest.mark.parametrize("features, flags, discount, depth", [
    ({"f_banker_crash": 0.1, "f_banker_n": 8}, [], None, LIGHT),
    ({"f_banker_crash": 0.1, "f_banker_n": 2}, [], None, STANDARD),
    ({"f_banker_crash": 0.55, "f_banker_n": 8}, [], None, FORENSIC),
    ({"f_banker_crash": 0.1, "f_banker_n": 8, "f_watchlist_hit": 1.0}, [], None, FORENSIC),
    ({"f_banker_crash": 0.1, "f_banker_n": 8},
     [{"id": "F1", "severity": "HIGH"}, {"id": "F2", "severity": "HIGH"}], None, FORENSIC),
    ({"f_banker_crash": 0.1, "f_banker_n": 8}, [], 6.0, FORENSIC),
    ({"f_banker_crash": 0.1, "f_banker_n": 8, "f_watchlist_pending": 1.0}, [], None, STANDARD),
])
def test_depth_policy(features, flags, discount, depth):
    assert choose_depth(features, flags, discount)[0] == depth


def test_archived_ids_decide_and_duplicate_rows_give_one_link(tmp_path):
    db = tmp_path / "s.db"
    shutil.copy(SAMPLE, db)
    with sqlite3.connect(db) as conn:
        name, eid, order = conn.execute(
            "SELECT entity_name, entity_id, order_id FROM sebi_order_entity "
            "WHERE entity_role = 'company' LIMIT 1").fetchone()
        conn.execute("INSERT INTO sebi_order_entity (order_id, entity_name, entity_role, entity_id, "
                     "reviewed) VALUES (?, ?, 'company', ?, 0)",
                     (order, name + " (formerly X)", eid))   # the same party, spelled twice
    as_of = date(2030, 1, 1)
    port = DataPort(MockZetaris(db), as_of)
    same_name_other_id = [Named("company", name, "ent:company:SOMEONE-ELSE")]
    assert watchlist_links(port, DEMO, as_of, same_name_other_id) == []
    same_id = [Named("company", "a different spelling", eid)]
    links = [l for l in watchlist_links(port, DEMO, as_of, same_id) if l.order["order_id"] == order]
    assert len(links) == 1 and links[0].hop == 1
    banker_role_only = [Named("banker", name)]                  # a banker never matches a company row
    assert not any(l.order["order_id"] == order for l in watchlist_links(port, DEMO, as_of,
                                                                          banker_role_only))


def test_data_port_sends_each_query_once_per_run():
    seen = []
    port = DataPort(MockZetaris(SAMPLE), date(2025, 1, 1), on_query=seen.append)
    a = port.run("ipo_bankers", ipo_id=DEMO)
    b = port.run("ipo_bankers", ipo_id=DEMO)
    assert a is b and len(seen) == 1
