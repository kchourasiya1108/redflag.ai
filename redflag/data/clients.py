"""Data Port clients: live Zetaris over REST, and a SQLite stand-in.

Both serve the same qualified names (RF_ARCHIVE, RF_LIVE, RF_PRICES), so a
template's SQL text is identical in live and sample mode.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

SOURCES = ("RF_ARCHIVE", "RF_LIVE", "RF_PRICES")


class ZetarisError(RuntimeError):
    pass


class MockZetaris:
    """archive.db attached under every Zetaris source name.

    In the live layout RF_LIVE and RF_PRICES are separate sources; offline
    they are all views of archive.db.
    """
    name = "mock-sqlite"

    def __init__(self, archive: Path):
        self._conn = sqlite3.connect("file::memory:", uri=True, check_same_thread=False)
        for source in SOURCES:
            self._conn.execute(f"ATTACH DATABASE ? AS {source}",
                               (f"file:{archive.resolve()}?mode=ro",))

    def query(self, sql: str, limit: int) -> tuple[list[str], list[list[Any]], float]:
        t0 = time.perf_counter()
        cur = self._conn.execute(sql)
        rows = [list(r) for r in cur.fetchmany(limit)]
        return [d[0] for d in cur.description], rows, (time.perf_counter() - t0) * 1000


class ZetarisRestClient:
    """POST {ZETARIS_REST_URL}/sql-editor/sqls/run, one statement per call."""
    name = "zetaris-rest"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 org_id: str | None = None, timeout_s: float = 20.0, retries: int = 1,
                 transport: httpx.BaseTransport | None = None):
        self.base = (base_url or os.environ.get("ZETARIS_REST_URL", "")).rstrip("/")
        self._key = api_key or os.environ.get("ZETARIS_API_KEY")
        self.org = org_id or os.environ.get("ZETARIS_ORG_ID", "")
        if not self.base or not self._key:
            raise ZetarisError("ZETARIS_REST_URL and ZETARIS_API_KEY must be set")
        self.retries = retries
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def query(self, sql: str, limit: int) -> tuple[list[str], list[list[Any]], float]:
        last: Exception | None = None
        for _ in range(self.retries + 1):
            # A fresh queryId per attempt: Zetaris rejects a reused one.
            body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor",
                    "limit": limit}
            headers = {"Authorization": f"Bearer {self._key}", "X-Org-ID": self.org,
                       "X-Request-ID": str(uuid.uuid4())}
            t0 = time.perf_counter()
            try:
                resp = self._http.post(f"{self.base}/sql-editor/sqls/run", json=body,
                                       headers=headers)
                if resp.status_code >= 400:
                    raise ZetarisError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                out = resp.json()
                cols = out.get("headers") or out.get("columns") or []
                columns = [c.get("name") if isinstance(c, dict) else str(c) for c in cols]
                return columns, out.get("data") or [], (time.perf_counter() - t0) * 1000
            except (httpx.HTTPError, ZetarisError, ValueError) as e:
                last = e
        raise ZetarisError(f"Zetaris query failed: {last}")
