"""Data: Zetaris tools for the agent (the guide's `tools_zetaris.py`).

Clients: live Zetaris over REST, and a SQLite stand-in. Both serve the same
qualified names (RF_ARCHIVE, RF_LIVE, RF_PRICES), so a template's SQL text is
identical in live and sample mode.

The Data Port on top: named templates, typed binding, and the Leakage Guard.
Agents and `compute_features` call `DataPort.run(template, **params)`; they
never write SQL. The port binds parameters itself (Zetaris takes a SQL
string), checks every time rule against the run's `as_of`, and refuses
results that may have been cut short.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

class ZetarisError(RuntimeError):
    pass


LIVE_TABLES = ("sebi_orders", "sebi_order_entity")


class MockZetaris:
    """archive.db attached as RF_ARCHIVE and RF_PRICES; RF_LIVE starts empty.

    As in Zetaris, RF_LIVE holds only rows fresh fetchers found since the
    archive was built (the live Postgres database; empty in sample mode), with
    the archive's table shapes, so a template that UNIONs the archive with
    RF_LIVE never counts an archived row twice.
    """
    name = "mock-sqlite"

    def __init__(self, archive: Path):
        self._conn = sqlite3.connect("file::memory:", uri=True, check_same_thread=False)
        for source in ("RF_ARCHIVE", "RF_PRICES"):
            self._conn.execute(f"ATTACH DATABASE ? AS {source}",
                               (f"file:{archive.resolve()}?mode=ro",))
        self._conn.execute("ATTACH DATABASE ':memory:' AS RF_LIVE")
        for table in LIVE_TABLES:
            self._conn.execute(f"CREATE TABLE RF_LIVE.{table} AS "
                               f"SELECT * FROM RF_ARCHIVE.{table} WHERE 0")


    def query(self, sql: str, limit: int) -> tuple[list[str], list[list[Any]], float]:
        t0 = time.perf_counter()
        cur = self._conn.execute(sql)
        rows = [list(r) for r in cur.fetchmany(limit)]
        return [d[0] for d in cur.description], rows, (time.perf_counter() - t0) * 1000


class ZetarisRestClient:
    """POST {ZETARIS_REST_URL}/sql-editor/sqls/run, one statement per call."""
    name = "zetaris-rest"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 org_id: str | None = None, timeout_s: float = 20.0, retries: int = 3,
                 backoff_s: float = 5.0, transport: httpx.BaseTransport | None = None):
        self.base = (base_url or os.environ.get("ZETARIS_REST_URL", "")).rstrip("/")
        self._key = api_key or os.environ.get("ZETARIS_API_KEY")
        self.org = org_id or os.environ.get("ZETARIS_ORG_ID", "")
        if not self.base or not self._key:
            raise ZetarisError("ZETARIS_REST_URL and ZETARIS_API_KEY must be set")
        self.retries = retries
        self.backoff_s = backoff_s          # doubles per retry: rides out a short LAN drop
        self.timeout_s = timeout_s
        self.deadline: float | None = None  # time.perf_counter(); no retry would end after it
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def query(self, sql: str, limit: int) -> tuple[list[str], list[list[Any]], float]:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt and self.backoff_s:
                pause = self.backoff_s * 2 ** (attempt - 1)
                if (self.deadline is not None
                        and time.perf_counter() + pause + self.timeout_s > self.deadline):
                    break               # no time left for another try
                time.sleep(pause)
            # A fresh queryId per attempt: Zetaris rejects a reused one.
            body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor",
                    "limit": limit}
            headers = {"Authorization": f"Bearer {self._key}", "X-Org-ID": self.org,
                       "X-Request-ID": str(uuid.uuid4())}
            t0 = time.perf_counter()
            try:
                resp = self._http.post(f"{self.base}/sql-editor/sqls/run", json=body,
                                       headers=headers)
            except httpx.HTTPError as e:
                last = e
                continue
            if resp.status_code >= 400:
                last = ZetarisError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code < 500 and resp.status_code != 429:
                    break           # bad SQL, key or org: retrying cannot help
                continue
            try:
                out = resp.json()
            except ValueError as e:
                last = e
                continue
            cols = out.get("headers") or out.get("columns") or []
            columns = [c.get("name") if isinstance(c, dict) else str(c) for c in cols]
            return columns, out.get("data") or [], (time.perf_counter() - t0) * 1000
        raise ZetarisError(f"Zetaris query failed: {last}")


CATALOGUE = Path(__file__).with_name("queries.yaml")
_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


class LeakageError(RuntimeError):
    """A query would let information from after as_of into a run."""


class TruncatedResult(RuntimeError):
    pass


@dataclass(frozen=True)
class Template:
    name: str
    sql: str
    params: dict[str, str]
    columns: dict[str, str]
    max_rows: int
    time_rule: dict | None
    time_free: bool
    offline_only: bool


@dataclass
class QueryResult:
    template: str
    params: dict[str, Any]
    columns: list[str]
    rows: list[dict[str, Any]]
    source: str
    elapsed_ms: float
    dropped_by_guard: int = 0
    sha256: str = field(default="")

    def __post_init__(self) -> None:
        if not self.sha256:
            # Order-independent: the same rows from Zetaris or the stand-in hash alike.
            canon = json.dumps(sorted(json.dumps(r, default=str, sort_keys=True)
                                      for r in self.rows))
            self.sha256 = hashlib.sha256(canon.encode()).hexdigest()[:16]

    @property
    def ref(self) -> str:
        """Evidence reference used in citations (`zq:<sha>`)."""
        return f"zq:{self.sha256}"


class Client(Protocol):
    name: str

    def query(self, sql: str, limit: int) -> tuple[list[str], list[list[Any]], float]:
        ...


def load_catalogue(path: Path = CATALOGUE) -> dict[str, Template]:
    out = {}
    for name, spec in yaml.safe_load(path.read_text()).items():
        rule, free = spec.get("time_rule"), bool(spec.get("time_free"))
        if not rule and not free:
            raise LeakageError(f"template {name!r} has no time_rule and is not time_free")
        if rule and rule["column"] not in spec["columns"]:
            raise LeakageError(f"template {name!r}: time_rule column must be returned")
        if rule and rule.get("op") not in ("<", "<="):
            raise LeakageError(f"template {name!r}: time_rule op must be < or <=")
        out[name] = Template(name, " ".join(spec["sql"].split()), spec.get("params") or {},
                             spec["columns"], int(spec.get("max_rows", 1000)),
                             rule, free, bool(spec.get("offline_only")))
    return out


class DataPort:
    def __init__(self, client: Client, as_of: date | None, *,
                 offline: bool = False, catalogue: dict[str, Template] | None = None,
                 on_query=None):
        self.client = client
        self.as_of = as_of
        self.offline = offline
        self.catalogue = catalogue or load_catalogue()
        self.on_query = on_query        # trace hook: called with each QueryResult
        self._results: dict[tuple, QueryResult] = {}

    def run(self, template: str, **params) -> QueryResult:
        """Run a named template. Identical calls on one port return the first
        result: within one as_of the answer cannot change, so a run never
        sends the same query twice or cites it twice."""
        key = (template, tuple(sorted((k, str(v)) for k, v in params.items())))
        if key in self._results:
            return self._results[key]
        result = self._run(template, params)
        self._results[key] = result
        return result

    def _run(self, template: str, params: dict) -> QueryResult:
        t = self.catalogue.get(template)
        if t is None:
            raise KeyError(f"unknown template {template!r}")
        if t.offline_only and not self.offline:
            raise LeakageError(f"{template!r} is for offline training only")
        if set(params) != set(t.params):
            raise ValueError(f"{template!r} takes {sorted(t.params)}, got {sorted(params)}")
        if "as_of" in t.params:
            if self.as_of is None or params["as_of"] != self.as_of:
                raise LeakageError(f"{template!r}: as_of must be the run's as_of "
                                   f"({self.as_of}), got {params['as_of']}")
        sql = bind(t, params)
        columns, raw_rows, elapsed = self.client.query(sql, t.max_rows + 1)
        if len(raw_rows) > t.max_rows:
            raise TruncatedResult(f"{template!r} returned more than {t.max_rows} rows")
        rows = [{c: convert(v, t.columns.get(c, "str")) for c, v in zip(columns, r)}
                for r in raw_rows]
        rows, dropped = self._guard(t, rows)
        result = QueryResult(template, {k: str(v) for k, v in params.items()},
                             columns, rows, self.client.name, elapsed, dropped)
        if self.on_query:
            self.on_query(result)
        return result

    def _guard(self, t: Template, rows: list[dict]) -> tuple[list[dict], int]:
        """Drop any row the time rule says was not yet known (defence in depth)."""
        if not t.time_rule:
            return rows, 0
        col, op = t.time_rule["column"], t.time_rule["op"]
        kept = [r for r in rows if r[col] is not None
                and (r[col] < self.as_of if op == "<" else r[col] <= self.as_of)]
        return kept, len(rows) - len(kept)


def bind(t: Template, params: dict[str, Any]) -> str:
    """Substitute typed, validated literals for :name placeholders."""
    literals = {k: _literal(k, params[k], kind) for k, kind in t.params.items()}

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in literals:
            raise ValueError(f"{t.name}: placeholder :{name} has no declared param")
        return literals[name]
    return re.sub(r"(?<![:\w]):([A-Za-z_]\w*)", sub, t.sql)


def _literal(name: str, value: Any, kind: str) -> str:
    if kind == "date":
        if not isinstance(value, date):
            raise ValueError(f"{name} must be a date")
        return f"'{value.isoformat()}'"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an int")
        return str(value)
    if kind == "id":
        if not isinstance(value, str) or not _ID.match(value):
            raise ValueError(f"{name} has characters an id cannot contain")
        return f"'{value}'"
    raise ValueError(f"{name}: unsupported param type {kind!r}")


def convert(value: Any, kind: str) -> Any:
    """Zetaris REST returns strings (sometimes in exponent form); type them."""
    if value is None or value == "":
        return None
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    if kind == "date":
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return str(value)
