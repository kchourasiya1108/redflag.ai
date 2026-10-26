# redflag.ai
An agent that gives retail investors an independent, evidence-backed risk rating for Indian SME IPOs before they apply

Design: [docs/DESIGN.md](docs/DESIGN.md).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python smoke_test.py --full     # Zetaris + Meterless, every line PASS
```

Services (Zetaris and Postgres on the Linux PC, Nightshift on this Mac) are
set up as in [docs/SETUP.md](docs/SETUP.md).

Layout (the Setup Guide's starter layout): `agent/` is the online agent.
`agent/llm.py` is the Power layer, `agent/tools_zetaris.py` the Data layer,
`agent/main.py` the agent loop, and `agent/engines/` the Meterless engines.
`pipeline/` holds the offline stages, `app/` the FastAPI app, and `prompts/`
the prompt files.

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

Zetaris runs in Docker on the Linux PC and serves the archive from Postgres
as three Lightning sources: `RF_ARCHIVE`, `RF_LIVE` and `RF_PRICES`, one
database each, 24 tables. Every row an agent reads in a live run comes
through it, and every `data.query` trace event says `source: zetaris-rest`.

```bash
.venv/bin/python run.py publish            # data/publish/postgres/ (and Parquet for the files route)
.venv/bin/python run.py zetaris load-pg    # load redflag_archive | live | prices
.venv/bin/python run.py zetaris register   # CREATE DATASOURCE + REGISTER/REFRESH TABLES
.venv/bin/python run.py zetaris check      # row counts: Zetaris == archive.db
```

Agents read data only through named templates in
[agent/queries.yaml](agent/queries.yaml), via the Data Port and its Leakage
Guard. Without Zetaris credentials, the same SQL runs on a SQLite stand-in.

## Meterless (Power layer and engines)

- **Nightshift is the only model provider.** Every call goes through
  [agent/llm.py](agent/llm.py), which follows provider contract v4 (bearer
  token, `meterless-app`, `meterless-scope: user`, `GET /v1/models`).
  - Two roles are pinned to Nemotron: `extract` uses
    `nvidia/nemotron-3.5-lightning-30b-a3b` and `reason` uses
    `nvidia/nemotron-3-ultra-550b-a55b`.
  - Each call is traced with the model that served it.
  - There is no direct NVIDIA call, because Nightshift holds the NVIDIA key.
- **Engines.** These meet the Build rule:

| Engine | What it does in RedFlag | Code | Feature | Evidence |
|---|---|---|---|---|
| **Markovian** | Reads the RHP's share capital history and objects of the issue window by window, carrying table state rather than history | [agent/engines/markovian.py](agent/engines/markovian.py), [agent/narrative.py](agent/narrative.py) | F4 insider discount and F9 use of money on the report card | Official suite **8/8**: [docs/evidence/markovian-conformance.txt](docs/evidence/markovian-conformance.txt); per-run `logs/<run>/markovian.jsonl` |
| **World Model** | The Case File: every entity, edge, fact and flag, with provenance (agent, run, `rhp:`/`zq:`/`sebi:` evidence) | [agent/engines/world.py](agent/engines/world.py), [agent/case_file.py](agent/case_file.py) | `output/<run>/case_file.json`; the Skeptic's and Reporter's context | Official suite **8/8**: [docs/evidence/world-model-conformance.txt](docs/evidence/world-model-conformance.txt) |
| H-MEM | Lessons across runs (a banker's RHP layout, earlier Skeptic rulings), gated by `as_of`, never a score input | [agent/engines/hmem.py](agent/engines/hmem.py) | Narrative reader and Skeptic context | Unit tests |

The official conformance suites run against the Python engines through
TypeScript adapters:

```bash
.venv/bin/python -m agent.engines.conformance.run          # clones Meterless @1742dec, runs the suites
```

- **SEBI review.** Nemotron reviews every proposed order-to-entity link, and
  its verdict is final (`run.py sebi-review model`). The model and its
  reason are stored on each row.

## Checking an upcoming IPO

Two ways in, both on the home page:

- **Upload the RHP.** Drop the PDF; the cover is read without a model
  (company, bid date, price, exchange, lead managers, registrar, market maker,
  auditor) and the investor confirms or fills in the rest. Book-built RHPs
  print the price as [●], so the top of the price band is entered by hand.
- **Pick from the list.** "Open and upcoming SME IPOs" lists issues found on
  NSE Emerge (the upcoming-issues API, SME series) and BSE SME (offer
  documents with an RHP but no allotment yet), with their RHPs already loaded
  and NSE's dates, price band and lead managers pre-filled.

```bash
.venv/bin/python run.py upcoming     # or the Refresh button; RHPs land in data/uploads/
```

Either way the IPO becomes an upload record (`agent/uploads.py`, id `UP-…`)
and the usual check runs: the IPO's own facts come from the RHP, while the
lead manager's and auditor's history, SEBI orders and base rates still come
from Zetaris. An upcoming IPO is judged as of today; one whose bid has opened,
as of its bid date. With Zetaris down, "Offline check" runs the same agents
against the local archive without model calls.

## Web app

A React frontend (`web/`, Vite + TypeScript) served by the FastAPI app,
written for investors rather than developers. Search the 693 archived IPOs,
check one (a fresh live check, or an instant replay of saved data under
"Check options"), follow plain-language progress, then read the report:
the risk rating as odds out of 100, the concerns with why each matters and
how it held up when challenged, questions to ask before bidding, what looked
fine and what could not be checked. Every cited prospectus page, SEBI order
and data lookup opens in a side panel. Financials open with an at-a-glance
view (sales, profit, cash from operations); "Track record" explains the
backtest in plain terms. Agents, Zetaris and Nemotron details, the trace and
the full metrics sit behind "Technical details" and "For analysts"
disclosures.

```bash
cd web && npm install && npm run build && cd ..   # once, and after frontend changes
.venv/bin/python run.py serve                     # then open http://localhost:8000
# frontend development with hot reload (proxies /api to :8000):
cd web && npm run dev                             # http://localhost:5173
```

One live run at a time (Zetaris, Nightshift and the run memory are shared);
sample runs can overlap. The browser API is under `/api` (`app/api.py`):
`GET /api/ipos`, `POST /api/runs`, `GET /api/runs/{id}/events` (the trace as
server-sent events), `/case`, `/explain`, `/trace`, `/report.md`,
`GET /api/ipos/{id}/pages/{n}`, `GET /api/sebi/{order}`, `GET /api/backtest`
and `GET /api/status` (probes Zetaris and Nightshift).

## API (FastAPI)

```bash
.venv/bin/python run.py serve              # 0.0.0.0:$PORT (default 8000)
curl -s localhost:8000/health
curl -s -X POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"input": {"ipo_id": "NSE-2023-SYNOPTICS"}, "options": {"sample_mode": true}}'
.venv/bin/python run.py investigate NSE-2023-SYNOPTICS [--sample]   # same, from the CLI
```

A live run takes about 4 to 6 minutes, because Nemotron calls take
13–30 s each and Nightshift sometimes returns `502 Load failed`. Sample
mode takes about 0.6 s.

Zetaris runs on a second machine. If it is unreachable, the Zetaris client
retries with back-off (never past the run's time limit), then the run
returns a recoverable `DataSourceUnavailable` error (HTTP 503 from
`POST /run`).

Each run writes:
- `logs/trace-<run_id>.jsonl`, with every agent step, transition, Zetaris
  query and model call;
- `output/<run_id>/report.md`, `case_file.json` and `result.json` (the full
  response, which the web app lists as run history);
- the per-engine logs under `logs/<run_id>/`.

`SAMPLE_MODE=true` serves the committed sample database and the recorded
Nemotron replies for the demo IPOs, with no network:
- BSE-2024-544168 Varyaa Creations
- NSE-2023-SYNOPTICS Synoptics Technologies
- BSE-2024-544310 Yash Highvoltage

## Backtest

`run.py extract features train` uses a time split: 390 training IPOs and
205 test IPOs.

| On the 205 test IPOs | AUC | Recall at HIGH | False alarms |
|---|---|---|---|
| **v2 scorer** (banker + watchlist + RHP numbers) | **0.662** | **36%** | 15.6% |
| v0 banker-only model | 0.603 | 29% | 8.2% |
| banker rule | 0.576 | 0% | 0% |

The single-prompt Nemotron baseline (`run.py llm-baseline`) scores AUC 0.503
on the 189 test IPOs with RHP text, against 0.673 for the v2 scorer on the
same IPOs. It catches 72% of adverse IPOs at HIGH, but only by flagging 77%
of the fine ones.

See [docs/DESIGN.md](docs/DESIGN.md#backtest-results) for the details.

## Tests

```bash
.venv/bin/python -m pytest -q              # 153 tests, no network
.venv/bin/python smoke_test.py --full      # live: Zetaris + Meterless
```
