"""The web app's API (app/api.py), in sample mode against the bundled archive."""
import shutil
import time

import pytest

from agent.config import ROOT
from tests.test_agents import DEMO, sample_cfg  # noqa: F401  (fixture)


@pytest.fixture
def client(sample_cfg, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient
    import app.api as api
    monkeypatch.setattr(api, "settings", lambda: sample_cfg)
    monkeypatch.setattr(api, "_prefill_tools", lambda: (None, None))     # no network in tests
    shutil.copytree(ROOT / "data" / "sample" / "pages", sample_cfg.data / "sample" / "pages",
                    dirs_exist_ok=True)
    from app.main import app
    return TestClient(app)


def _wait(client, run_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] != "running":
            return state
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_catalogue_search_and_detail(client):
    found = client.get("/api/ipos", params={"q": "synoptics"}).json()
    assert [i["ipo_id"] for i in found["items"]] == [DEMO] and found["items"][0]["in_sample"]
    detail = client.get(f"/api/ipos/{DEMO}").json()
    assert detail["ipo"]["company"].startswith("Synoptics") and detail["docs"]
    assert client.get("/api/ipos/NSE-2099-NOPE").status_code == 404


def test_rhp_page_and_sebi_order(client):
    page = client.get(f"/api/ipos/{DEMO}/pages/45").json()
    assert page["page"] == 45 and page["pages"] > 45 and page["text"]
    assert client.get(f"/api/ipos/{DEMO}/pages/99999").status_code == 404
    order = client.get("/api/sebi/sebi-45441").json()
    assert order["order_date"] == "2019-12-24" and "named" in order


def test_background_run_streams_its_trace_and_is_listed(client):
    run_id = client.post("/api/runs", json={"ipo_id": DEMO, "sample_mode": True}).json()["run_id"]
    with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
        lines = list(stream.iter_lines())
    assert any(line.startswith("data: ") and '"Reporter"' in line for line in lines)
    assert "event: done" in lines
    state = _wait(client, run_id)
    assert state["status"] == "success" and state["result"]["output"]["rating"]
    assert len({a["name"] for a in state["result"]["agents"]}) == len(state["result"]["agents"])
    assert run_id in [r["run_id"] for r in client.get("/api/runs").json()["runs"]]
    explain = client.get(f"/api/runs/{run_id}/explain").json()
    assert explain["p_adverse"] == state["result"]["output"]["p_adverse"]
    assert client.get(f"/api/runs/{run_id}/report.md").text.startswith("# RedFlag report")
    assert client.get(f"/api/runs/{run_id}/trace").json()[0]["action"] == "start"


def test_unknown_ipo_run_reports_a_structured_error(client):
    run_id = client.post("/api/runs", json={"ipo_id": "NSE-2099-NOPE", "sample_mode": True}).json()["run_id"]
    state = _wait(client, run_id)
    assert state["status"] == "error" and state["error"]["type"] == "UnknownIPO"


def test_bad_run_id_and_unknown_api_path(client):
    assert client.get("/api/runs/..%2Fetc").status_code in (400, 404)
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/runs/run-nothere").status_code == 404


def test_events_resume_from_last_event_id(client):
    run_id = client.post("/api/runs", json={"ipo_id": DEMO, "sample_mode": True}).json()["run_id"]
    _wait(client, run_id)
    with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
        lines = list(stream.iter_lines())
    ids = [line[4:] for line in lines if line.startswith("id: ")]
    data = [line for line in lines if line.startswith("data: {\"timestamp")]
    with client.stream("GET", f"/api/runs/{run_id}/events",
                       headers={"Last-Event-ID": ids[4]}) as stream:
        rest = [line for line in stream.iter_lines() if line.startswith("data: {\"timestamp")]
    assert rest == data[5:]


def test_explain_survives_a_run_without_a_score(client, sample_cfg):  # noqa: F811
    import json
    folder = sample_cfg.data.parent / "output" / "run-noscore"
    folder.mkdir(parents=True)
    (folder / "case_file.json").write_text(json.dumps({"score": None}))
    body = client.get("/api/runs/run-noscore/explain").json()
    assert body["p_adverse"] is None and all(f["missing"] for f in body["features"])


def test_get_run_is_not_the_frontend(client):
    assert client.get("/run").status_code == 405 and client.get("/api").status_code == 404
