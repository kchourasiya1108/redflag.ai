"""Register RedFlag's sources in a hosted Zetaris instance, and check them.

Nothing is installed locally: this sends Lightning SQL to the instance named by
ZETARIS_REST_URL, one statement per call. Two routes for the archive:

- files (default): the Parquet files from `run.py publish` are uploaded to a
  bucket (RF_FILES_BASE, e.g. s3a://my-bucket/redflag/parquet) and registered
  as Lightning file-store tables.
- postgres: `run.py zetaris load-pg` loads the CSVs into one Postgres
  database per source (<db>_archive, <db>_live, <db>_prices, where <db> is
  the database in RF_PG_JDBC_URL), tables in `public`: Zetaris's table
  discovery reads only `public` and ignores currentSchema. Zetaris registers
  each database over JDBC (RF_PG_JDBC_URL, RF_PG_USER, RF_PG_PASSWORD).
  Daily prices come from <db>_prices, so this route needs no bucket.

Syntax follows the Zetaris Lightning SQL command reference
(zetaris/getting-started, docs/guides/zetaris-lightning-sql-commands.md).
Secrets are read from the environment when statements are sent and are
masked whenever statements are printed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from pipeline.publish import PG_DATABASES, TABLES

LIVE_TABLES = ["sebi_orders", "sebi_order_entity"]
_SECRET_KEYS = {"password", "awsSecretAccessKey", "awsAccessKeyId"}


@dataclass
class Statement:
    sql: str
    masked: str


def _q(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _options(opts: dict[str, str]) -> tuple[str, str]:
    real = ",\n  ".join(f"{k} {_q(v)}" for k, v in opts.items())
    masked = ",\n  ".join(f"{k} {_q('***' if k in _SECRET_KEYS else v)}"
                          for k, v in opts.items())
    return f"(\n  {real}\n)", f"(\n  {masked}\n)"


def _stmt(head: str, opts: dict[str, str] | None = None) -> Statement:
    if not opts:
        return Statement(head, head)
    real, masked = _options(opts)
    return Statement(f"{head} OPTIONS {real}", f"{head} OPTIONS {masked}")


def statements(env: dict[str, str] | None = None) -> list[Statement]:
    env = dict(os.environ if env is None else env)
    route = env.get("ZETARIS_ARCHIVE_ROUTE", "files")
    aws = {k: env[e] for k, e in (("awsAccessKeyId", "AWS_ACCESS_KEY_ID"),
                                 ("awsSecretAccessKey", "AWS_SECRET_ACCESS_KEY"))
           if env.get(e)}
    if env.get("RF_S3_ENDPOINT"):
        aws["s3Endpoint"] = env["RF_S3_ENDPOINT"]
    out: list[Statement] = []
    if route == "postgres":
        creds = {"username": _need(env, "RF_PG_USER"), "password": _need(env, "RF_PG_PASSWORD")}
        for db in PG_DATABASES:
            source = f"RF_{db.upper()}"
            out.append(_stmt(f'CREATE DATASOURCE {source} DESCRIBE BY "RedFlag-{db}-Postgres"',
                             {"jdbcdriver": "org.postgresql.Driver",
                              "jdbcurl": pg_jdbc_url(env, db),
                              **creds, "fetchsize": "1000"}))
            out.append(_stmt(f"REGISTER DATASOURCE TABLES FROM {source}"))
            # Pick up columns added to tables that were already registered.
            out.append(_stmt(f"REFRESH DATASOURCE TABLES FROM {source}"))
    elif route == "files":
        base = _need(env, "RF_FILES_BASE").rstrip("/")
        out.append(_stmt('CREATE LIGHTNING DATABASE RF_ARCHIVE DESCRIBE BY "RedFlag-archive"'))
        for table in TABLES:
            if table != "prices":
                out.append(_stmt(f"CREATE LIGHTNING FILESTORE TABLE {table} FROM RF_ARCHIVE "
                                 f"FORMAT PARQUET", {"path": f"{base}/{table}.parquet", **aws}))
        out.append(_stmt('CREATE LIGHTNING DATABASE RF_LIVE DESCRIBE BY "RedFlag-fresh-lookups"'))
        live = env.get("RF_LIVE_BASE", f"{base}/live").rstrip("/")
        for table in LIVE_TABLES:
            out.append(_stmt(f"CREATE LIGHTNING FILESTORE TABLE {table} FROM RF_LIVE "
                             f"FORMAT PARQUET", {"path": f"{live}/{table}.parquet", **aws}))
        # Daily prices are a partitioned Parquet file store on this route.
        prices = env.get("RF_PRICES_PATH") or f"{base}/prices"
        out.append(_stmt('CREATE LIGHTNING DATABASE RF_PRICES DESCRIBE BY "NSE-Emerge-daily-bhavcopy"'))
        out.append(_stmt("CREATE LIGHTNING FILESTORE TABLE prices FROM RF_PRICES FORMAT PARQUET",
                         {"path": prices, **aws}))
    else:
        raise ValueError("ZETARIS_ARCHIVE_ROUTE must be 'files' or 'postgres'")
    return out


def _pg_parts(env: dict[str, str]) -> tuple[str, str, str]:
    """(server, database, query) from RF_PG_JDBC_URL = jdbc:postgresql://host:port/db?query."""
    jdbc = _need(env, "RF_PG_JDBC_URL")
    if not jdbc.startswith("jdbc:postgresql://"):
        raise ValueError("RF_PG_JDBC_URL must look like jdbc:postgresql://host:5432/db")
    url, _, query = jdbc[len("jdbc:postgresql://"):].partition("?")
    server, _, base = url.partition("/")
    return server, base or "postgres", query


def pg_jdbc_url(env: dict[str, str], db: str | None = None) -> str:
    """The JDBC URL of <base>_<db> (or of the base database when db is None)."""
    server, base, query = _pg_parts(env)
    name = f"{base}_{db}" if db else base
    return f"jdbc:postgresql://{server}/{name}" + (f"?{query}" if query else "")


def pg_conninfo(env: dict[str, str] | None = None, db: str | None = None) -> str:
    """libpq conninfo for the same database Zetaris reaches over JDBC.

    When Zetaris reaches Postgres by a Docker network name (RF_PG_JDBC_URL =
    jdbc:postgresql://redflag-pg:5432/...), RF_PG_LOCAL_SERVER (host:port) is
    where this machine reaches the same server, for loading.
    """
    env = dict(os.environ if env is None else env)
    from psycopg.conninfo import make_conninfo
    url = pg_jdbc_url(env, db)[len("jdbc:"):].split("?", 1)[0]
    if local := env.get("RF_PG_LOCAL_SERVER"):
        server = _pg_parts(env)[0]
        url = url.replace(f"//{server}/", f"//{local}/", 1)
    return make_conninfo(url, user=_need(env, "RF_PG_USER"), password=_need(env, "RF_PG_PASSWORD"))


def load_postgres(folder, env: dict[str, str] | None = None, *, log=print) -> dict[str, int]:
    """Create each <base>_<db> database if missing, run schema-<db>.sql, then COPY
    every CSV that load-<db>.sql names."""
    import re

    import psycopg
    env = dict(os.environ if env is None else env)
    _, base, _ = _pg_parts(env)
    with psycopg.connect(pg_conninfo(env), autocommit=True) as admin:
        have = {r[0] for r in admin.execute("SELECT datname FROM pg_database")}
        for db in PG_DATABASES:
            if f"{base}_{db}" not in have:
                admin.execute(f'CREATE DATABASE "{base}_{db}"')
                log(f"ok   created database {base}_{db}")
    rows = {}
    for db in PG_DATABASES:
        with psycopg.connect(pg_conninfo(env, db)) as conn:
            conn.execute((folder / f"schema-{db}.sql").read_text())
            for line in (folder / f"load-{db}.sql").read_text().splitlines():
                m = re.match(r"\\copy (\w+) FROM '([^']+)'", line)
                if not m:
                    continue
                table, csv_name = m.groups()
                with conn.cursor() as cur:
                    with (folder / csv_name).open("rb") as f, cur.copy(
                            f"COPY {table} FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
                        while chunk := f.read(1 << 20):
                            cp.write(chunk)
                    n = cur.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                rows[f"{base}_{db}.{table}"] = n
                log(f"ok   {base}_{db}.{table}: {n} rows")
    return rows


def _need(env: dict[str, str], key: str) -> str:
    if not env.get(key):
        raise ValueError(f"{key} must be set (see .env.example)")
    return env[key]


def register(client, *, log=print) -> int:
    """Send every statement; an 'already exists' error is not a failure."""
    sent = 0
    for st in statements():
        try:
            client.query(st.sql, 1)
            log(f"ok   {st.masked.splitlines()[0]}")
            sent += 1
        except Exception as e:
            msg = str(e)
            if "exist" in msg.lower():
                log(f"skip {st.masked.splitlines()[0]} (already registered)")
            else:
                log(f"FAIL {st.masked.splitlines()[0]}: {msg[:200]}")
                raise
    return sent


def check(live_client, mock_client, *, log=print) -> bool:
    """Ping Zetaris and compare row counts with the local archive, table by table."""
    columns, rows, _ = live_client.query("SELECT 1", 1)
    log(f"ping: {rows}")
    ok = True
    from pipeline.publish import PG_DATABASES
    for source, table in [("RF_ARCHIVE", t) for t in PG_DATABASES["archive"]] + \
                         [("RF_PRICES", t) for t in PG_DATABASES["prices"]]:
        sql = f"SELECT count(*) AS n FROM {source}.{table}"
        local = mock_client.query(sql, 1)[1][0][0]
        try:
            remote = int(float(live_client.query(sql, 1)[1][0][0]))
        except Exception as e:
            log(f"FAIL {source}.{table}: {str(e)[:160]}")
            ok = False
            continue
        same = remote == local
        ok &= same
        log(f"{'ok  ' if same else 'DIFF'} {source}.{table}: zetaris={remote} local={local}")
    return ok
