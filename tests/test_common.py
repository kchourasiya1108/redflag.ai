from __future__ import annotations

import httpx
import pytest

from redflag.common import names
from redflag.common.db import upsert
from redflag.common.http import Fetcher, FetchError


def test_normalise_unifies_legal_suffixes():
    assert (names.normalise("First Overseas Capital Limited")
            == names.normalise("FIRST OVERSEAS CAPITAL LTD."))
    assert (names.normalise("Hem Securities Ltd.(Formerly Hem Financial Services Ltd)")
            == "hem securities ltd")


@pytest.mark.parametrize("text, expected", [
    ("Hem Securities Limited", ["Hem Securities Limited"]),
    ("Beeline Capital Advisors Private Limited and Hem Securities Limited",
     ["Beeline Capital Advisors Private Limited", "Hem Securities Limited"]),
    ("D & A Financial Services Pvt. Ltd., Shreni Shares Limited",
     ["D & A Financial Services Pvt. Ltd.", "Shreni Shares Limited"]),
    ("", []),
])
def test_split_names_only_after_legal_suffix(text, expected):
    assert names.split_names(text) == expected


def test_similarity_bands():
    assert names.similarity("Narnolia Financial Services Limited",
                            "NARNOLIA FINANCIAL SERVICES LTD") == 1.0
    assert names.similarity("Narnolia Financial Services Limited",
                            "Aryaman Financial Services Limited") < names.MERGE_AT


def test_upsert_updates_non_key_columns(conn):
    conn.execute("INSERT INTO bankers VALUES (1, 'A Ltd', NULL, 'universe')")
    upsert(conn, "banker_alias", [{"alias": "a ltd", "banker_id": 1,
                                   "match_type": "self", "similarity": 1.0}], ("alias",))
    upsert(conn, "banker_alias", [{"alias": "a ltd", "banker_id": 1,
                                   "match_type": "manual", "similarity": 0.9}], ("alias",))
    row = conn.execute("SELECT match_type, similarity FROM banker_alias").fetchone()
    assert tuple(row) == ("manual", 0.9)


def _fetcher(handler) -> Fetcher:
    return Fetcher(0, 5, retries=1, transport=httpx.MockTransport(handler))


def test_fetcher_caches_and_never_refetches(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, content=b"body")

    f, dest = _fetcher(handler), tmp_path / "x.csv"
    assert f.get("https://example.test/x", dest) == b"body"
    assert f.get("https://example.test/x", dest) == b"body"
    assert len(calls) == 1


def test_fetcher_remembers_404_unless_told_not_to(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    f = _fetcher(handler)
    assert f.get("https://example.test/a", tmp_path / "a") is None
    assert f.get("https://example.test/a", tmp_path / "a") is None
    assert len(calls) == 1
    f.get("https://example.test/b", tmp_path / "b", remember_missing=False)
    f.get("https://example.test/b", tmp_path / "b", remember_missing=False)
    assert len(calls) == 3


def test_fetcher_retries_then_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    f = _fetcher(lambda r: httpx.Response(503))
    with pytest.raises(FetchError):
        f.get("https://example.test/down", tmp_path / "down")


def test_dotenv_strips_trailing_comments_but_keeps_quoted_and_inline_hashes(tmp_path, monkeypatch):
    from redflag.config import load_dotenv
    env = tmp_path / ".env"
    env.write_text("\n".join([
        "# a comment line",
        "RF_T_PLAIN=s3a://bucket/parquet   # upload here",
        "RF_T_TAB=value\t# tab comment",
        "RF_T_HASH=http://host:8088/connect#k=abc",
        "RF_T_QUOTED=\"keep # this\"   # but not this",
        "RF_T_SINGLE='x y'",
        "RF_T_EMPTY=",
    ]))
    for k in ("PLAIN", "TAB", "HASH", "QUOTED", "SINGLE", "EMPTY"):
        monkeypatch.delenv(f"RF_T_{k}", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["RF_T_PLAIN"] == "s3a://bucket/parquet"
    assert os.environ["RF_T_TAB"] == "value"
    assert os.environ["RF_T_HASH"] == "http://host:8088/connect#k=abc"
    assert os.environ["RF_T_QUOTED"] == "keep # this"
    assert os.environ["RF_T_SINGLE"] == "x y"
    assert os.environ["RF_T_EMPTY"] == ""
