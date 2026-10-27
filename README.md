# redflag.ai

An agent that gives retail investors an independent, evidence-backed risk
rating for an Indian SME IPO (NSE Emerge or BSE SME) before they bid.

Fourteen named agents read the prospectus (RHP), check the lead manager's
track record and SEBI orders known on the bid date, argue the company's side,
and score the issue with a backtested model. Every claim in the report points
back to an RHP page, a Zetaris query row or a SEBI order.

- **Data layer:** Zetaris (every row an agent reads in a live run).
- **AI provider:** Meterless Nightshift (the only model provider), serving
  NVIDIA Nemotron models.
- **Meterless Engines:** Markovian and World Model (both pass the official
  Meterless conformance suites, 8/8), plus H-MEM.

Contents: [Quick start](#quick-start-no-credentials-sample-mode) ·
[Requirements](#requirements) · [Full setup](#full-setup-live-mode) ·
[Usage](#usage) · [Examples](#input-and-output-examples) ·
[Agents and traces](#agents-and-traces) · [Sponsor technology](#sponsor-technology) ·
[Tests](#tests) · [Layout](#repository-layout) · [Limitations](#limitations)

---

## Quick start (no credentials, sample mode)

`SAMPLE_MODE=true` runs the real agents on a committed SQLite sample of the
archive and recorded Nemotron replies, so it needs no Zetaris, no Nightshift,
no network and no keys. It reproduces three demo IPOs:

| IPO id | Company |
|---|---|
| `NSE-2023-SYNOPTICS` | Synoptics Technologies |
| `BSE-2024-544168` | Varyaa Creations |
| `BSE-2024-544310` | Yash Highvoltage |

### With Docker (recommended)

```bash
git clone https://github.com/kchourasiya1108/redflag.ai
cd redflag.ai
docker build -t redflag .
docker run --rm -p 8000:8000 -e SAMPLE_MODE=true redflag
```

In another terminal:

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/run -H 'Content-Type: application/json' \
  -d @input_examples/example_1.json
```

Open <http://localhost:8000> for the web app.

### Without Docker

Needs Python 3.12+ (tested on 3.12 and 3.14) and, for the web app only,
Node.js 20+.

```bash
git clone https://github.com/kchourasiya1108/redflag.ai
cd redflag.ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # leave the placeholders as they are

SAMPLE_MODE=true python run.py investigate NSE-2023-SYNOPTICS --sample   # one run, JSON to stdout

# the API and web app
(cd web && npm ci && npm run build)        # once; skip it if you only need the API
SAMPLE_MODE=true python run.py serve       # http://localhost:8000
```

A sample run takes under a second. It still runs every agent: the Planner
picks a depth, the Extractor parses the RHP pages, the Skeptic challenges the
flags, the Scorer applies the model and the Reporter writes the report. Only
the data source (SQLite instead of Zetaris) and the model replies (recorded
instead of live) change. The trace says which (`data_source: sample
(mock-sqlite)`). An IPO without recorded replies runs without model calls
and lists the sections it could not check.

---

## Requirements

| Need | For | Notes |
|---|---|---|
| Python 3.12+ | everything | `pip install -r requirements.txt` (FastAPI, httpx, pydantic, numpy, scikit-learn, pypdf, pypdfium2, pyarrow, psycopg) |
| Node.js 20+ | the web app build | `web/` (React, Vite, TypeScript). Not needed for the API or CLI |
| Docker | the container, and Zetaris | optional for RedFlag itself |
| Zetaris | live mode: the data layer | the hackathon's `zetaris-platform` Docker bundle |
| Meterless Nightshift | live mode: the AI provider | desktop app, with NVIDIA added as a provider |
| Postgres 16 | live mode: the archive Zetaris serves | one Docker container (below) |

No GPU is needed. Nemotron models are reached through Nightshift's hosted
NVIDIA endpoints. The NVIDIA API key lives in Nightshift's Providers tab and
never in this project.

---

## Full setup (live mode)

Live mode reads data through Zetaris and calls Nemotron through Nightshift.
The official instructions for both are in the Participant Setup Guide
(Resource Center on HackOS). These are the steps we used on one Mac
(Apple M4, macOS 15).

### 1. Install RedFlag

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
(cd web && npm ci && npm run build)
```

All settings come from environment variables in `.env`. `.env` is git-ignored
and `.env.example` holds placeholders only.

### 2. Zetaris

1. Start Zetaris from the hackathon bundle: `docker login`, `./preflight.sh`,
   set the admin account in the bundle's `.env`, `docker compose pull &&
   docker compose up -d`, then `./verify.sh` (9 of 9 checks). The web UI is
   at <http://localhost:3000>, the REST API at <http://localhost:8888/api/v1.0>.
2. In the Zetaris UI (Management → API Tokens), create an API key and set in
   `.env`:

   ```
   ZETARIS_REST_URL=http://localhost:8888/api/v1.0
   ZETARIS_API_KEY=<your key>
   ZETARIS_ORG_ID=1
   ```

### 3. Postgres for the archive

Zetaris serves the archive from a Postgres container on the Zetaris Docker
network (`zetaris_default`). Zetaris reaches it as `redflag-pg`; your machine
loads it through `127.0.0.1`.

1. In `.env` (password: letters and digits only):

   ```
   ZETARIS_ARCHIVE_ROUTE=postgres
   RF_PG_JDBC_URL=jdbc:postgresql://redflag-pg:5432/redflag
   RF_PG_LOCAL_SERVER=127.0.0.1:5432
   RF_PG_USER=redflag
   RF_PG_PASSWORD=<password>
   ```

2. Start it (reads the password from `.env` without printing it):

   ```bash
   set -a; . ./.env; set +a
   POSTGRES_PASSWORD="$RF_PG_PASSWORD" docker run -d --name redflag-pg \
     --restart unless-stopped --network zetaris_default \
     -e POSTGRES_DB=redflag -e POSTGRES_USER=redflag -e POSTGRES_PASSWORD \
     -p 127.0.0.1:5432:5432 -v redflag-pgdata:/var/lib/postgresql/data postgres:16-alpine
   ```

3. Build the archive, load it and register it in Zetaris. The offline
   pipeline downloads public exchange and SEBI data (cached under
   `data/raw/`, each file fetched once, with a per-host delay); a full build
   takes hours, so `--limit N` is there for a quick probe.

   ```bash
   python run.py all                     # universe, prices, labels, sebi, rhp, features, train, publish
   python run.py zetaris load-pg         # creates redflag_archive | live | prices and loads them
   python run.py zetaris register        # CREATE DATASOURCE + REGISTER/REFRESH TABLES (RF_ARCHIVE, RF_LIVE, RF_PRICES)
   python run.py zetaris check           # row counts: Zetaris == archive.db
   ```

### 4. Meterless Nightshift

1. Install Nightshift from <https://meterless.ai> (on an M1–M4 Mac, the
   M1+ edition), finish the first-run prompts and leave it running.
2. Get an `nvapi-…` key at <https://build.nvidia.com/models>. In Nightshift,
   open **Setup → Providers → NVIDIA**, paste the key and save. The key stays
   in Nightshift.
3. In Nightshift, open **Console** and copy the Runtime URL and token into
   `.env`:

   ```
   METERLESS_BASE_URL=http://127.0.0.1:8088/v1
   METERLESS_RUNTIME_TOKEN=<token from the Console>
   MODEL_ROUTE_EXTRACT=nvidia/nemotron-3.5-lightning-30b-a3b
   MODEL_ROUTE_REASON=nvidia/nemotron-3-ultra-550b-a55b
   ```

   Both model ids must appear in `GET /v1/models`. Without the two
   `MODEL_ROUTE_*` lines the roles use `meterless/fast` and `meterless/deep`,
   and Nightshift's lanes choose the model.

### 5. Verify

```bash
python smoke_test.py --detect     # find the Nightshift runtime, then run every check
python smoke_test.py --full       # also compare every Zetaris table's row count with archive.db
```

Setup is done when every line says PASS. The `Meterless: extract` and
`Meterless: reason` lines show which Nemotron model served each role.

### Running the container against live services

Inside a container `localhost` is the container itself. On Docker Desktop,
point `.env` at the host:

```
ZETARIS_REST_URL=http://host.docker.internal:8888/api/v1.0
METERLESS_BASE_URL=http://host.docker.internal:8088/v1
```

```bash
docker run --rm -p 8000:8000 --env-file .env redflag
```

---

## Usage

### HTTP API

```bash
python run.py serve                        # 0.0.0.0:$PORT (default 8000)
```

`POST /run` takes structured JSON and returns structured JSON:

```bash
curl -s -X POST localhost:8000/run -H 'Content-Type: application/json' \
  -d '{"input": {"ipo_id": "NSE-2023-SYNOPTICS"}, "options": {"sample_mode": true}}'
```

| Field | Meaning |
|---|---|
| `input.ipo_id` | an IPO in the archive (`NSE-<year>-<symbol>` or `BSE-<year>-<scrip>`), or an uploaded RHP (`UP-…`) |
| `options.sample_mode` | `true`: sample data and recorded replies; `false`: Zetaris and Nightshift; omitted: the `SAMPLE_MODE` variable |
| `run_id` | optional; generated when missing |

The response holds `status` (`success`, `partial` or `error`), the rating
(`LOW`, `ELEVATED` or `HIGH`), the chance of a bad outcome, each reason with
its evidence, the Skeptic's notes, cleared and unchecked items, questions to
ask, the list of agents that ran, the trace file and the execution time.
Failures return a structured error with an HTTP code, never a stack trace:
`UnknownIPO` (404), `DataSourceUnavailable` (503, recoverable), `Busy` (409:
one live run at a time).

Other endpoints: `GET /health` (what is configured) and the web app's API
under `/api`: `GET /api/ipos`, `POST /api/uploads` (an RHP PDF),
`POST /api/runs`, `GET /api/runs/{id}/events` (the trace as server-sent
events), `/api/runs/{id}/trace`, `/case`, `/report.md`, `GET /api/status`
(probes Zetaris and Nightshift). FastAPI's docs are at `/docs`.

### CLI

```bash
python run.py investigate NSE-2023-SYNOPTICS --sample   # sample mode
python run.py investigate NSE-2023-SYNOPTICS            # live: Zetaris + Nightshift
python run.py upcoming                                  # load open and upcoming SME IPOs and their RHPs
python run.py check                                     # offline pipeline stage gates
```

### Web app

<http://localhost:8000> after `python run.py serve`. It is written for
investors: upload an RHP (the only input; issue details are filled in from
the archive, the exchange and the cover page) or pick an open or upcoming
SME IPO, and get the report card: the rating, each concern with its source
page and whether it survived the Skeptic, and questions to ask before
bidding. Agents and traces stay in the API.

A live run takes about 4–6 minutes (Nemotron calls take 13–30 s each).

---

## Input and output examples

`input_examples/` holds three `POST /run` bodies; `output_examples/` holds
what the agent returned for each, its full trace and its report:

| Input | IPO | Rating | Chance of a bad outcome |
|---|---|---|---|
| [example_1.json](input_examples/example_1.json) | Synoptics Technologies (NSE Emerge, 2023) | ELEVATED | 28% |
| [example_2.json](input_examples/example_2.json) | Varyaa Creations (BSE SME, 2024) | HIGH | 48% |
| [example_3.json](input_examples/example_3.json) | Yash Highvoltage (BSE SME, 2024) | LOW | 3% |

For each `example_N`: `output_examples/example_N.json` (the response),
`example_N.trace.jsonl` (every agent step, handoff, Zetaris query and model
call) and `example_N.report.md` (the investor report).

The outputs are generated, never stored and replayed. Regenerate them with:

```bash
python scripts/make_examples.py      # POSTs each input to the app and saves what comes back
```

and call the API with any of them:

```bash
for i in 1 2 3; do
  curl -s -X POST localhost:8000/run -H 'Content-Type: application/json' \
    -d @input_examples/example_$i.json | python -m json.tool | head -20
done
```

---

## Agents and traces

| Agent | Role |
|---|---|
| Orchestrator | Runs the state machine; logs every transition |
| Intake | Identifies the IPO and fixes `as_of` to its bid opening date |
| Names | Records the issuer, lead managers, auditor, market maker and registrar |
| Planner | Chooses LIGHT, STANDARD or FORENSIC depth and escalates mid-run |
| BankerTrackRecord | The lead managers' earlier SME IPO outcomes known before `as_of` (Zetaris) |
| Watchlist | Matches every named party against SEBI orders dated before `as_of` (Zetaris) |
| Extractor | Restated financials from the RHP: a deterministic table pass, then Nemotron for gaps |
| NarrativeReader | Share capital history, peers, objects of the issue (Markovian engine) |
| Specialists | The checks F1–F14 (paper profits, profit spike, insider discount, concentration, …) |
| Evaluator | Checks coverage; sends gaps back to the Planner at most twice |
| Skeptic | Argues the innocent explanation of each ELEVATED or HIGH flag, citing RHP pages (Nemotron) |
| Scorer | The backtested, calibrated model (no LLM) |
| CitationVerifier | Re-checks every cited page, query row and SEBI order; drops what it cannot find |
| Reporter | Writes the report card and `case_file.json` (Nemotron for the plain-language summary) |

Every run writes:

- `logs/trace-<run_id>.jsonl` (also streamed to stdout): one JSON line per
  step with `agent_name`, `action`, `target_agent`, `status`, `retry_count`
  and, for model calls, the route and the model that served it. Zetaris
  queries appear as `data.query` events with the template, row count and
  source (`zetaris-rest` live, `mock-sqlite` in sample mode). Planner
  escalations, Evaluator loops, Skeptic downgrades and model fallbacks are
  separate events.
- `logs/<run_id>/markovian.jsonl` and the other engine logs.
- `output/<run_id>/report.md`, `case_file.json`, `result.json`.

`logs/` and `output/` are created at runtime and git-ignored; the three
traces in `output_examples/` are copies from real runs.

---

## Sponsor technology

### Zetaris: the data layer

- In a live run every row an agent reads comes through the Zetaris REST API
  (`agent/tools_zetaris.py`), and each `data.query` trace event records
  `source: zetaris-rest`.
- The archive (SME IPOs from 2023 to 2026, lead managers, auditors, market
  makers, SEBI orders, labels, daily prices) is served from Postgres as three
  Lightning sources, `RF_ARCHIVE`, `RF_LIVE` and `RF_PRICES` (24 tables).
- Agents never write SQL. They call named, parameterised templates in
  [agent/queries.yaml](agent/queries.yaml) through the Data Port, whose
  Leakage Guard enforces each template's time rule (for example, a lead
  manager's past IPO counts only if its 6-month outcome was known before this
  IPO's bid date). The backtest runs the same templates.
- Setup is scripted (`run.py zetaris load-pg | register | check`). The client
  has timeouts and retries; when Zetaris is down a run returns a recoverable
  `DataSourceUnavailable`, and `SAMPLE_MODE=true` runs the same SQL on SQLite.

### Meterless Engines

| Engine | What it does in RedFlag | Feature it powers | Test |
|---|---|---|---|
| **Markovian** ([agent/engines/markovian.py](agent/engines/markovian.py)) | Reads long RHP sections window by window, carrying a small state (unit, fiscal-year columns, open table, partial row) instead of the whole history | The insider-discount check (F4), use of the money (F9), the listed-peer table | Official suite **8/8**: [docs/evidence/markovian-conformance.txt](docs/evidence/markovian-conformance.txt) |
| **World Model** ([agent/engines/world.py](agent/engines/world.py), [agent/case_file.py](agent/case_file.py)) | The Case File every agent shares: entities, edges, facts and flags, each with the agent, run and evidence that produced it | The Skeptic's and Reporter's context, the SEBI network search, `case_file.json` | Official suite **8/8**: [docs/evidence/world-model-conformance.txt](docs/evidence/world-model-conformance.txt) |
| H-MEM ([agent/engines/hmem.py](agent/engines/hmem.py)) | Lessons across runs, retrieved only if written before the bid date; never a score input | Narrative Reader and Skeptic context | unit tests |

Run the official Meterless conformance suites against the Python engines
(clones Meterless at the pinned commit; needs Node.js and git):

```bash
python -m agent.engines.conformance.run
```

### Meterless Nightshift: the AI provider

Every model call goes through [agent/llm.py](agent/llm.py) to
`METERLESS_BASE_URL` under provider contract v4 (bearer runtime token,
`meterless-app: redflag`, `meterless-scope: user`); `GET /v1/models` is
checked before a run. The engines get the same gateway, so their calls are
budgeted, cached and traced too. There is no other model provider.

### NVIDIA

Two roles are pinned to Nemotron through Nightshift: `extract`
(`nvidia/nemotron-3.5-lightning-30b-a3b`: statement rows, cover page, SEBI
link review) and `reason` (`nvidia/nemotron-3-ultra-550b-a55b`: Skeptic
rulings, the Reporter's summary). If one lane fails, the call moves to the
other Nemotron model, then to `nvidia/nemotron-3.5-bolt-e4b`; each switch is
a `model.retry_or_fallback` trace event naming the model that answered.

---

## Tests

```bash
python -m pytest -q                       # 194 tests, no network, no credentials
python -m agent.engines.conformance.run   # official Meterless suites for Markovian and World Model
python smoke_test.py --full               # live: Zetaris and Nightshift
```

CI (`.github/workflows/tests.yml`) runs the tests, the web build and the three
sample-mode runs on every push.

---

## Repository layout

```
agent/                 the online agent
  agents/              the named agents (Planner, Skeptic, Reporter, …)
  engines/             Meterless engines (markovian.py, world.py, hmem.py) and conformance/
  llm.py               Nightshift gateway (provider contract v4, fallbacks, budget, trace)
  tools_zetaris.py     Zetaris REST client, Data Port, Leakage Guard
  queries.yaml         the named query templates
  main.py              the agent loop
app/                   FastAPI: POST /run, /health, /api/* for the web app
pipeline/              offline stages: universe, prices, labels, sebi, rhp, features, train, publish
web/                   React web app (Vite, TypeScript)
data/sample/           the SQLite sample archive and RHP pages for sample mode
data/replay/           recorded Nightshift replies for sample mode
data/model/scorer.json the trained, calibrated scorer
input_examples/        three POST /run inputs
output_examples/       their outputs, traces and reports
scripts/               make_examples.py, prepare_submission.sh
tests/                 unit and integration tests
docs/evidence/         Meterless conformance suite output
run.py                 entry point (CLI and `serve`)
smoke_test.py          live checks for Zetaris and Nightshift
Dockerfile             the API and web app in one image
```

Downloaded and generated data (`data/raw/`, `data/cache/`, `data/archive.db`,
`data/publish/`, `logs/`, `output/`) is git-ignored and rebuilt by the
pipeline.

---

## Limitations

- **Sample mode covers three IPOs fully.** Other IPOs in the sample database
  run, but without recorded model replies the model-dependent sections are
  listed as not checked. Live mode needs Zetaris and Nightshift.
- **The archive is built offline.** A full pipeline build downloads several
  GB of public exchange and SEBI documents and takes hours; it is not part
  of a judge's setup. Training data is not committed: `run.py train`
  rebuilds the scorer and backtest from the archive, and the web app's track
  record section is empty until it has run.
- **Modest predictive power.** On held-out IPOs (June 2025–April 2026) the
  scorer's AUC is 0.67, against 0.57 for a banker-only rule. It flags risk;
  it does not predict prices.
- **Model reliability.** Nightshift sometimes returns `502 Load failed`;
  fallbacks keep the run going but a section can still end up unchecked,
  and the report says so.
- **Coverage.** NSE Emerge and BSE SME only; RHPs that are scanned images
  read poorly.
- **Not investment advice.** RedFlag is a research aid, not a
  SEBI-registered adviser, and it is not production-ready.
