# redflag.ai
An agent that gives retail investors an independent, evidence-backed risk rating for Indian SME IPOs before they apply

Design: [docs/DESIGN.md](docs/DESIGN.md).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

## Offline pipeline

Each stage is idempotent (downloads are cached under `data/raw/`, and a stage
skips work already done) and logs bad rows to `data/failures.csv` instead of
stopping. Progress goes to stdout and `logs/pipeline-<stage>.jsonl`.

```bash
.venv/bin/python run.py universe          # 01: NSE Emerge IPO list + banker master
.venv/bin/python run.py prices labels     # 02-03: daily prices, 6-month labels
.venv/bin/python run.py sebi              # 04: SEBI orders naming our companies/bankers
.venv/bin/python run.py rhp               # 05: RHP PDFs + page-text cache (large)
.venv/bin/python run.py check             # print every stage gate
.venv/bin/python run.py universe --limit 5  # quick probe
```

SEBI links are proposals until a person confirms them:

```bash
.venv/bin/python run.py sebi-review export   # data/review/sebi_entities.csv
# set reviewed to 1 (correct) or -1 (wrong) for each row, then:
.venv/bin/python run.py sebi-review import
.venv/bin/python run.py labels               # re-label with Label B
```

| Stage | Source | Output tables |
|---|---|---|
| universe | NSE `public-past-issues` and `ipo-detail` APIs; SEBI merchant banker register | `ipos`, `bankers`, `banker_alias`, `ipo_banker` |
| prices | NSE Emerge daily bhavcopy (`smeDDMMYY.csv`) | `prices`, `trading_days`, `corp_actions` |
| labels | `prices`, reviewed SEBI links | `labels` |
| sebi | SEBI Enforcement → Orders search, order PDFs | `sebi_orders`, `sebi_order_entity`, `entities`, `entity_alias` |
| rhp | NSE RHP zips | `docs`, `data/cache/pages/` |

Data comes only from public exchange and regulator pages. Downloads respect
a per-host delay (`HTTP_MIN_INTERVAL_S`), and every file is fetched once.
BSE SME issues are not covered yet.

Then compute features, train the v0 (banker-only) model and publish:

```bash
.venv/bin/python run.py features train publish
```

## Zetaris (data layer)

Nothing is installed locally: RedFlag talks to a hosted Zetaris instance over
its REST API (`ZETARIS_REST_URL`, `ZETARIS_API_KEY`, `ZETARIS_ORG_ID`).

1. `run.py publish` writes `data/publish/parquet/` (Parquet per table, prices
   split by year) and `data/publish/postgres/` (CSVs plus `schema-<db>.sql` and `load-<db>.sql` for the `redflag_archive`, `redflag_live` and `redflag_prices` databases; `python run.py zetaris load-pg` loads them).
2. Upload the Parquet folder to the bucket in `RF_FILES_BASE` (files route), or
   load the Postgres files into a hosted Postgres (`ZETARIS_ARCHIVE_ROUTE=postgres`).
3. Register and verify:

```bash
.venv/bin/python run.py zetaris render     # print the Lightning SQL (secrets masked)
.venv/bin/python run.py zetaris register   # send it to Zetaris
.venv/bin/python run.py zetaris check      # row counts: Zetaris vs local archive
```

Agents read data only through named templates in
[redflag/data/queries.yaml](redflag/data/queries.yaml), via the Data Port and its
Leakage Guard. Without Zetaris credentials the same SQL runs on a SQLite stand-in.

## Meterless (model and agent engines)

- **Nightshift runtime:** every model call goes through one gateway
  ([redflag/llm/gateway.py](redflag/llm/gateway.py)) to `METERLESS_BASE_URL`, with
  roles pinned to Nemotron models via `MODEL_ROUTE_*` and no direct NVIDIA
  fallback (Nightshift holds the NVIDIA key), a disk cache, recorded replies for sample mode, and a run budget.
- **Engines** (Python ports of the Meterless reference contracts):
  [Markovian](redflag/engines/markovian.py) for long RHP sections,
  [World Model](redflag/engines/world.py) for the shared Case File, and
  [H-MEM](redflag/engines/hmem.py) for lessons across runs, gated by `as_of`.

## API (FastAPI)

```bash
.venv/bin/python run.py serve              # 0.0.0.0:$PORT (default 8000)
curl -s localhost:8000/health
curl -s -X POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"input": {"ipo_id": "NSE-2023-SYNOPTICS"}}'
.venv/bin/python run.py investigate NSE-2023-SYNOPTICS   # same, from the CLI
```

Each run writes `logs/trace-<run_id>.jsonl`. `SAMPLE_MODE=true` serves the
committed sample database and recorded model replies with no network.

## Tests

```bash
.venv/bin/python -m pytest -q
```
