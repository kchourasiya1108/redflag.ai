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
from agent.engines.markovian import ChunkConfig, Markovian, model_totals, parse_markers
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


def test_markovian_windows_carry_table_state_and_save_tokens(tmp_path):
    prompts, chunks = [], []
    state = "COMPLETED: balance sheet rows\nCONTEXT: unit lakh, columns FY24 FY23"

    def generator(prompt, attachments=None, system=None, on_stream=None, abort=None):
        prompts.append(prompt)
        if system:                                   # the reflection pass
            return {"text": "ratify"}
        return {"text": f"rows from part {len(prompts)}\n[STATE_CHECKPOINT]\n{state}"}

    m = Markovian(ChunkConfig(chunk_size=4000, carryover_tokens=640),
                  history_path=tmp_path / "m.jsonl")
    run = m.run("extract the balance sheet", generator=generator, mode="EXTRACT",
                windows=["p1 " * 50, "p2 " * 50, "p3 " * 50], on_chunk=chunks.append)
    assert run.status == "completed" and len(run.chunks) == 3      # ends with the windows
    assert "[ATTACHMENT part 2 of 3]" in prompts[1] and "p2 p2" in prompts[1]
    assert "unit lakh" in prompts[1] and "unit lakh" in prompts[2]   # carryover reached
    assert [c.cascade_level for c in chunks] == ["marker"] * 3
    assert "[STATE_CHECKPOINT]" not in run.output
    assert run.chunk_config.carryover_tokens == 640 and run.token_label == "estimated"
    assert run.reflection == "ratify"
    assert (tmp_path / "m.jsonl").exists()


def test_markovian_config_rejects_out_of_range_values():
    from agent.engines.markovian import ConfigValidationError, resolve_config
    with pytest.raises(ConfigValidationError):
        resolve_config({"chunkSize": 128})
    with pytest.raises(ConfigValidationError):          # framing + carryover + output
        resolve_config(ChunkConfig(chunk_size=2000, carryover_tokens=800))
    assert resolve_config({"overlapTokens": 64}).overlap_tokens == 64


def test_markovian_carryover_survives_model_outage():
    from agent.engines.markovian import next_carryover, resolve_config

    def broken(*a, **k):
        raise RuntimeError("down")
    state, level = next_carryover(None, "", "All figures in ₹ lakhs unless stated.",
                                  resolve_config(), broken)
    assert level == "heuristic" and "lakhs" in state


def test_markovian_measured_tokens_and_abort():
    calls = []

    def generator(prompt, *a):
        calls.append(prompt)
        return {"text": "part [STATE_CHECKPOINT] COMPLETED: one part of the job",
                "metadata": {"usage": {"output_tokens": 42}}}
    run = Markovian(ChunkConfig(max_chunks=3)).run("job", generator=generator, reflect=False)
    assert run.status == "max-chunks" and run.token_label == "measured"
    assert [c.tokens for c in run.chunks] == [42, 42, 42]
    stopped = Markovian().run("job", generator=generator, aborted=lambda: len(calls) >= 4)
    assert stopped.status == "aborted" and len(stopped.chunks) == 1


def test_split_markers():
    p = parse_markers("rows\n[STATE_CHECKPOINT] {\"a\":1}")
    assert (p.cleaned, p.state, p.done) == ("rows", '{"a":1}', False)
    assert model_totals(20)["markovianTotal"] == 34_000


def test_world_model_resolves_names_and_closes_superseded_edges():
    src = {"kind": "rhp", "at": "2025-03-01T00:00:00Z"}
    wm = WorldModel("t")
    a = wm.upsert_entity(type="banker", name="Hem Securities Limited", source=src)
    b = wm.upsert_entity(type="banker", name="Hem Securities Limited.", source=src)
    assert a["id"] == b["id"]                                    # auto-merged alias
    wm.upsert_entity(type="banker", name="Hem Securities Ltd", source=src)
    assert [p["status"] for p in wm.merge_queue()] == []         # 0.69: a new entity
    wm.upsert_entity(type="director", name="Ramesh Kumar Jain", source=src)
    wm.upsert_entity(type="director", name="Ramesh Kumr Jain", source=src)   # 0.94
    wm.upsert_entity(type="director", name="Ramesh K. Jainn", source=src)
    assert all(p["status"] == "pending" for p in wm.merge_queue())
    with pytest.raises(ValueError, match="provenance"):
        wm.upsert_entity(type="banker", name="No Source", source=None)
    wm.relate(from_="x", type="auditor_of", to="old", source=src, valid_from="2024-01-01")
    wm.relate(from_="x", type="auditor_of", to="new", source=src, valid_from="2025-01-01")
    open_edges = [r for r in wm.store.relationships.values() if not r.get("validTo")]
    assert [r["to"] for r in open_edges] == ["new"]


def test_case_file_facts_flags_graph_and_resume(tmp_path):
    from agent.case_file import CaseFile
    as_of = date(2025, 3, 10)
    cf = CaseFile("r1", "NSE-2025-X", as_of, log_dir=tmp_path)
    banker = cf.add_entity("banker", "Hem Securities Limited", source="rhp:p3", agent="Extractor")
    director = cf.add_entity("person", "Ramesh Jain", source="rhp:p40", agent="Extractor")
    cf.link(banker, "lead_manager_of", cf.company, source="rhp:p3", agent="Extractor")
    cf.link(director, "director_of", cf.company, source="rhp:p40", agent="Extractor")
    cf.link(director, "named_in_order", "sebi:ORD-1", source="sebi:ORD-1", agent="Watchlist")
    assert cf.neighbours(banker, hops=2) == {cf.company: 1, director: 2}

    cf.add_fact("pat", "FY24", 4.0, unit="cr", page=187, verified=False, method="table",
                agent="Extractor")
    cf.add_fact("pat", "FY24", 4.21, unit="cr", page=187, verified=True, method="table",
                agent="Verifier")
    assert cf.fact("pat", "FY24")["value"] == 4.21 and len(cf.facts()) == 1   # superseded
    cf.add_flag("F2", "HIGH", value={"jump": 2.4}, evidence=["rhp:p201"], confidence=0.8,
                agent="Specialist", text="profit spike")
    cf.add_flag("F10", "LOW", value={}, evidence=["zq:ab"], confidence=0.5, agent="Banker")
    cf.rule_on_flag("F2", "downgraded", "order book explains it", ["rhp:p96"],
                    severity="ELEVATED")
    assert cf.flag("F2")["severity"] == "ELEVATED" and cf.flag("F10")["severity"] == "LOW"

    ctx = cf.context("skeptic on F2", mentions=["order book"])
    assert ctx.items[0].data["id"] == "F2" and ctx.strategy == "task_focused"

    doc = cf.case_file()
    assert doc["meta"]["as_of"] == "2025-03-10" and len(doc["flags"]) == 2
    assert {e["type"] for e in doc["entities"]} == {"banker", "person"}

    again = CaseFile.resume("r1", "NSE-2025-X", as_of, tmp_path)
    writes = len(again.world.audit())
    again.add_fact("pat", "FY24", 4.21, unit="cr", page=187, verified=True, method="table",
                   agent="Verifier")
    assert again.case_file()["facts"] == doc["facts"]
    assert len(again.world.store.facts) == len(cf.world.store.facts)        # replay no-op
    assert len(again.world.audit()) == writes                              # nothing new written


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
    # Zetaris in Docker reaches "db"; this machine loads through the published port.
    local = zetaris_setup.pg_conninfo({**env, "RF_PG_LOCAL_SERVER": "127.0.0.1:5432"}, "archive")
    assert "host=127.0.0.1" in local and "port=5432" in local and "dbname=rf_archive" in local
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
    assert routes_from_env({}) == {"extract": "meterless/fast", "reason": "meterless/deep",
                                   "backup": "nvidia/nemotron-3.5-bolt-e4b"}

    seen = []
    gw = _gateway(tmp_path, [_provider("meterless", ["plan</think>done"], seen)])
    gw.routes = routes_from_env({"MODEL_ROUTE_REASON": "nvidia/nemotron-3-ultra-550b-a55b"})
    res = gw.generate("hi", route="reason", purpose="t")
    assert seen[0][1] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert res.route == "nvidia/nemotron-3-ultra-550b-a55b" and res.text == "done"


def test_gateway_never_starts_a_call_after_the_run_deadline(tmp_path):
    import time
    seen = []
    gw = _gateway(tmp_path, [_provider("meterless", ["late"], seen)])
    gw.deadline = time.perf_counter() - 1
    with pytest.raises(BudgetExceeded):
        gw.generate("hi", purpose="t")
    assert seen == []


def test_recording_copies_cached_replies_into_the_replay_set(tmp_path):
    gw = _gateway(tmp_path, [_provider("meterless", ["first"])])
    gw.generate("hi", purpose="t")                       # cached, not recorded
    rec = _gateway(tmp_path, [_provider("meterless", [])], record_replay=True)
    rec.generate("hi", purpose="t")                      # served from cache
    assert len(list((tmp_path / "replay").glob("*.json"))) >= 1
