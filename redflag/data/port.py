"""The Data Port: named templates, typed binding, and the Leakage Guard.

Agents and `compute_features` call `DataPort.run(template, **params)`; they
never write SQL. The port binds parameters itself (Zetaris takes a SQL
string), checks every time rule against the run's `as_of`, and refuses
results that may have been cut short.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import yaml

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
            canon = json.dumps(self.rows, default=str, sort_keys=True)
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

    def run(self, template: str, **params) -> QueryResult:
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
