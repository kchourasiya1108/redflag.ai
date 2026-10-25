"""Meterless gateway and engines, Zetaris setup, scoring and the HTTP API.

No network: model and Zetaris endpoints are stand-ins (httpx.MockTransport).
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from pydantic import BaseModel

from pipeline import zetaris_setup
from agent.engines.hmem import HMem
from agent.engines.markovian import ChunkConfig, Markovian, split_markers
from agent.engines.world import WorldModel
from agent.llm import (BudgetExceeded, Budget, Gateway, MalformedOutput, Provider,
                                 ReplayMiss, parse_structured)
from agent.scoring import INSUFFICIENT, score


class Row(BaseModel):
    field: str
    value: float


def _provider(name, replies, seen=None, status=200):
    replies = list(replies)

    def handler(request):
        body = json.loads(request.content)
        if seen is not None:
            seen.append((name, body["model"], request.headers.get("meterless-app")))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={
            "model": f"{name}-served", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"message": {"content": replies.pop(0)}}]})
    return Provider(name, f"http://{name}.test/v1", "tok",
                    headers={"meterless-app": "redflag"} if name == "meterless" else None,
                    transport=httpx.MockTransport(handler))


def _gateway(tmp_path, providers, **kw):
    return Gateway(providers, tmp_path / "cache", tmp_path / "replay", **kw)


def test_gateway_routes_through_meterless_and_caches(tmp_path):
    seen = []
    gw = _gateway(tmp_path, [_provider("meterless", ["hello"], seen)])
    first = gw.generate("hi", route="meterless/fast", purpose="t")
    again = gw.generate("hi", route="meterless/fast", purpose="t")
    assert first.text == again.text == "hello"
    assert seen == [("meterless", "meterless/fast", "redflag")]
    assert again.cached and first.served_model == "meterless-served"


def test_nightshift_is_the_only_provider(tmp_path):
    from agent.llm import GatewayError, providers_from_env
    env = {"METERLESS_BASE_URL": "http://127.0.0.1:8088/v1", "NVIDIA_API_KEY": "nvapi-x"}
    assert [p.name for p in providers_from_env(env)] == ["meterless"]
    assert providers_from_env({"NVIDIA_API_KEY": "nvapi-x"}) == []

    events = []
    gw = _gateway(tmp_path, [_provider("meterless", [], status=503)], on_call=events.append)
    with pytest.raises(GatewayError, match="every provider failed"):
        gw.generate("hi", purpose="t")
    assert sum(e["action"] == "model.retry_or_fallback" for e in events) == 2


def test_structured_output_with_one_repair(tmp_path):
    gw = _gateway(tmp_path, [_provider("meterless", ["not json", '{"field":"pat","value":4.2}'])])
    res = gw.generate("extract", schema=Row, purpose="t")
    assert res.parsed == Row(field="pat", value=4.2)


def test_structured_output_gives_up_after_repair(tmp_path):
    gw = _gateway(tmp_path, [_provider("meterless", ["nope", "still nope"])])
    with pytest.raises(MalformedOutput):
        gw.generate("extract", schema=Row, purpose="t")


def test_parse_structured_tolerates_fences():
    assert parse_structured('```json\n{"field":"x","value":1}\n```', Row).value == 1


def test_sample_mode_replays_or_misses(tmp_path):
    live = _gateway(tmp_path, [_provider("meterless", ["recorded"])], record_replay=True)
    live.generate("q", purpose="t")
    offline = Gateway([], tmp_path / "other-cache", tmp_path / "replay", sample_mode=True)
    assert offline.generate("q", purpose="t").replayed
    with pytest.raises(ReplayMiss):
        offline.generate("never recorded", purpose="t")


def test_budget_stops_calls(tmp_path):
    gw = _gateway(tmp_path, [_provider("meterless", ["a", "b"])], budget=Budget(max_calls=1))
    gw.generate("one", purpose="t")
    with pytest.raises(BudgetExceeded):
        gw.generate("two", purpose="t")


def test_markovian_carries_state_and_saves_tokens(tmp_path):
    prompts = []

    def generator(prompt, attachments=None, system=None, on_stream=None, abort=None):
        prompts.append(prompt)
        n = len(prompts)
        tail = '[TASK_COMPLETE]' if n == 3 else '[STATE_CHECKPOINT] {"unit":"lakh"}'
        return {"text": f"rows from part {n}\n{tail}"}

    chunks = []
    run = Markovian(generator, ChunkConfig(chunk_size=1000, max_chunks=5),
                    history_path=tmp_path / "m.jsonl").run(
        "extract the balance sheet", ["p1 " * 50, "p2 " * 50, "p3 " * 50],
        on_chunk=chunks.append)
    assert run.completed and run.stop_reason == "final_marker"
    assert '{"unit":"lakh"}' in prompts[1]           # carryover reached chunk 2
    assert "[STATE_CHECKPOINT]" not in chunks[0].content
    assert run.total_tokens_saved > 0
    assert (tmp_path / "m.jsonl").exists()


def test_markovian_carryover_survives_model_outage():
    def broken(*a, **k):
        raise RuntimeError("down")
    m = Markovian(lambda *a, **k: {"text": ""}, compressor=broken)
    state, method = m.carryover(None, "All figures in ₹ lakhs unless stated", "")
    assert method == "heuristic" and "lakhs" in state


def test_split_markers():
    content, state, final = split_markers("rows\n[STATE_CHECKPOINT] {\"a\":1}")
    assert (content, state, final) == ("rows", '{"a":1}', False)


def test_world_model_idempotent_and_graph(tmp_path):
    wm = WorldModel("r1", tmp_path / "world.jsonl")
    a = wm.ingest("entity", {"type": "banker", "name": "Hem Securities"},
                  source="rhp:p3", agent_id="Extractor")
    assert wm.ingest("entity", {"type": "banker", "name": "Hem Securities"},
                     source="rhp:p3", agent_id="Extractor") is a
    wm.ingest("edge", {"src": "co", "dst": "banker", "type": "lead_manager_of"},
              source="rhp:p3", agent_id="Extractor")
    wm.ingest("edge", {"src": "banker", "dst": "order", "type": "named_in_order"},
              source="sebi:1", agent_id="Watchlist")
    assert wm.neighbours("co", hops=2) == {"banker": 1, "order": 2}
    resumed = WorldModel.resume("r1", tmp_path / "world.jsonl")
    assert set(resumed.records) == set(wm.records)


def test_world_model_context_prioritises_mentions():
    wm = WorldModel("r1")
    wm.ingest("fact", {"field": "pat", "fiscal_year": "FY24", "value": 4.2},
              source="rhp:p100", agent_id="Extractor")
    wm.ingest("flag", {"flag_id": "F2", "severity": "HIGH", "text": "profit spike"},
              source="rhp:p100", agent_id="Specialist")
    ctx = wm.context("skeptic on F2", mentions=["profit spike"])
    assert ctx.items[0].record.data["flag_id"] == "F2"
    assert ctx.strategy == "task_focused"


def test_hmem_never_returns_memories_from_later_runs(tmp_path):
    mem = HMem(tmp_path / "m.db")
    mem.add("Banker 17 RHPs put restated financials in Annexure IV",
            as_of=date(2024, 6, 1), tags=["extraction"], entities=["ent:banker:17"])
    mem.add("Banker 17 RHPs scan badly", as_of=date(2025, 6, 1), entities=["ent:banker:17"])
    got = mem.retrieve("restated financials banker 17", as_of=date(2025, 1, 1),
                       entities=["ent:banker:17"])
    assert [g.memory.as_of for g in got] == ["2024-06-01"]
    assert HMem(tmp_path / "off.db", enabled=False).retrieve("x", as_of=date(2030, 1, 1)) == []


def test_hmem_mining_falls_back_without_a_model(tmp_path):
    mem = HMem(tmp_path / "m.db")
    def broken(prompt):
        raise RuntimeError("rate limited")
    [m] = mem.mine("verifier", "balance sheet passed on retry", as_of=date(2025, 1, 1),
                   extractor=broken)
    assert m.layer == "short_term"


def test_zetaris_statements_mask_secrets():
    env = {"ZETARIS_ARCHIVE_ROUTE": "postgres", "RF_PG_JDBC_URL": "jdbc:postgresql://db:5432/rf",
           "RF_PG_USER": "rf", "RF_PG_PASSWORD": "s3cret-pass", "RF_FILES_BASE": "s3a://b/rf"}
    stmts = zetaris_setup.statements(env)
    assert any('jdbcurl "jdbc:postgresql://db:5432/rf_archive"' in s.sql for s in stmts)
    assert any("s3cret-pass" in s.sql for s in stmts)
    assert not any("s3cret-pass" in s.masked for s in stmts)
    assert any("RF_PRICES" in s.sql and "/rf_prices" in s.sql for s in stmts)
    assert zetaris_setup.pg_jdbc_url({**env, "RF_PG_JDBC_URL": "jdbc:postgresql://h:1/x?ssl=true"},
                                     "live") == "jdbc:postgresql://h:1/x_live?ssl=true"
    assert not any("FILESTORE" in s.sql for s in stmts)       # no bucket on this route
    assert zetaris_setup.statements({k: v for k, v in env.items() if k != "RF_FILES_BASE"})
    info = zetaris_setup.pg_conninfo(env, "archive")
    assert "host=db" in info and "dbname=rf_archive" in info and "user=rf" in info
    files = zetaris_setup.statements({"RF_FILES_BASE": "s3a://b/rf"})
    # Zetaris accepts only letters, digits, _ . - and , in a description.
    import re
    for st in stmts + files:
        m = re.search(r'DESCRIBE BY "([^"]*)"', st.sql)
        assert m is None or re.fullmatch(r"[A-Za-z0-9_.,-]+", m.group(1)), st.sql
    assert any('path "s3a://b/rf/ipos.parquet"' in s.sql for s in files)
    assert files[-1].sql.startswith("CREATE LIGHTNING FILESTORE TABLE prices FROM RF_PRICES")


def test_score_bands_and_insufficient_data():
    scorer = {"version": "t", "features": ["f_banker_crash"], "coef": [4.0, 0.0],
              "intercept": -2.0, "fill": {"f_banker_crash": 0.3},
              "bands": {"low_below": 0.3, "high_at": 0.6}}
    assert score({"f_banker_crash": 0.9}, scorer).band == "HIGH"
    assert score({"f_banker_crash": 0.0}, scorer).band == "LOW"
    assert score({"f_banker_crash": None}, scorer).band == INSUFFICIENT


def test_api_health_and_unknown_ipo(cfg, conn, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    monkeypatch.setattr(main, "settings", lambda: cfg)
    for key in ("ZETARIS_REST_URL", "ZETARIS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main.app)
    assert client.get("/health").json()["archive_db"] is True
    resp = client.post("/run", json={"input": {"ipo_id": "NSE-2099-NOPE"}})
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "UnknownIPO"


def test_gateway_sends_images_to_the_vision_model(tmp_path):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"model": body["model"], "usage": {},
                                         "choices": [{"message": {"content": '{"field":"x","value":1}'}}]})
    p = Provider("meterless", "http://ns.test/v1", "k", transport=httpx.MockTransport(handler))
    gw = Gateway([p], tmp_path / "c", tmp_path / "r", vision_model="vendor/vision-model")
    res = gw.generate("read the table", schema=Row, images=[b"\x89PNG fake"], purpose="t")
    assert res.parsed.value == 1
    body = seen[0]
    assert body["model"] == "vendor/vision-model"
    parts = body["messages"][-1]["content"]
    assert parts[0]["type"] == "text" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    with pytest.raises(Exception):
        Gateway([p], tmp_path / "c2", tmp_path / "r").generate("x", images=[b"img"])


def test_meterless_contract_url_token_and_scope_header():
    from agent.llm import meterless_token, normalise_base_url, providers_from_env
    for pasted in ("127.0.0.1:8088", "http://127.0.0.1:8088/v1/", "http://127.0.0.1:8088/healthz",
                   "http://127.0.0.1:8088/v1/models", "http://127.0.0.1:8088/v1/chat/completions"):
        assert normalise_base_url(pasted) == "http://127.0.0.1:8088/v1"
    assert meterless_token({"METERLESS_RUNTIME_TOKEN": "rt-local"}) is None
    assert meterless_token({"METERLESS_TOKEN": "abc"}) == "abc"

    seen = {}
    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    [p] = providers_from_env({"METERLESS_BASE_URL": "127.0.0.1:8088/v1",
                              "METERLESS_RUNTIME_TOKEN": "tok"}, transport=httpx.MockTransport(handler))
    p.chat([{"role": "user", "content": "hi"}], "meterless/auto", 16)
    assert seen["authorization"] == "Bearer tok" and seen["meterless-scope"] == "user"
    assert seen["meterless-app"] == "redflag"


def test_roles_resolve_to_pinned_models_and_reasoning_is_stripped(tmp_path):
    from agent.llm import routes_from_env, strip_reasoning
    assert strip_reasoning("thinking...</think>\n{\"a\": 1}") == '{"a": 1}'
    assert strip_reasoning("<think>x</think>ok") == "ok"
    assert strip_reasoning("plain") == "plain"
    assert routes_from_env({}) == {"extract": "meterless/fast", "reason": "meterless/deep"}

    seen = []
    gw = _gateway(tmp_path, [_provider("meterless", ["plan</think>done"], seen)])
    gw.routes = routes_from_env({"MODEL_ROUTE_REASON": "nvidia/nemotron-3-ultra-550b-a55b"})
    res = gw.generate("hi", route="reason", purpose="t")
    assert seen[0][1] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert res.route == "nvidia/nemotron-3-ultra-550b-a55b" and res.text == "done"
