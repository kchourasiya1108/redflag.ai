"""SQLite access for archive.db."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA.read_text())
    _migrate(conn)
    return conn


# Columns added after a table was first created: (table, column, type).
_ADDED_COLUMNS = [("ipos", "issue_price_basis", "TEXT"), ("ipos", "cin", "TEXT"),
                  ("ipos", "nic_code", "TEXT"), ("ipos", "nic_section", "TEXT"),
                  ("ipos", "lot_size_source", "TEXT"), ("securities", "first_lot", "INTEGER"),
                  ("securities", "first_lot_date", "TEXT"), ("ipos", "registrar_id", "INTEGER"),
                  ("labels", "index_name", "TEXT"), ("labels", "index_ret_6m", "REAL"),
                  ("labels", "crashed_vs_index", "INTEGER")]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in _ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()


def upsert(conn: sqlite3.Connection, table: str,
           rows: Iterable[Mapping], key: tuple[str, ...]) -> int:
    """Insert rows, updating the non-key columns of rows that already exist."""
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    updates = [c for c in cols if c not in key]
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT ({', '.join(key)}) DO ")
    sql += ("UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in updates)
            if updates else "NOTHING")
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)
