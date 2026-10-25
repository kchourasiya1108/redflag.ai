-- archive.db: the single source of truth for the offline pipeline.
-- Published to Postgres for Zetaris by the publish stage. Money is in ₹ crore,
-- dates are ISO YYYY-MM-DD text. See docs/DESIGN.md, "Archive schema".

CREATE TABLE IF NOT EXISTS ipos (
    ipo_id              TEXT PRIMARY KEY,      -- NSE-2024-SYMBOL
    company             TEXT NOT NULL,
    exchange            TEXT NOT NULL,         -- NSE Emerge | BSE SME
    symbol              TEXT NOT NULL,
    isin                TEXT,
    industry            TEXT,                  -- the exchange's own label (NSE only)
    cin                 TEXT,                  -- from the offer document cover
    nic_code            TEXT,                  -- NIC 2008 code inside the CIN
    nic_section         TEXT,                  -- NIC section, comparable across exchanges
    issue_type          TEXT,                  -- fresh | ofs | fresh+ofs
    issue_shares        INTEGER,
    fresh_shares        INTEGER,
    ofs_shares          INTEGER,
    issue_size_cr       REAL,
    fresh_issue_cr      REAL,
    ofs_cr              REAL,
    price_band_low      REAL,
    price_band_high     REAL,
    issue_price         REAL,
    issue_price_basis   TEXT,                  -- final | fixed | band_upper
    face_value          REAL,
    lot_size            INTEGER,
    lot_size_source     TEXT,                  -- exchange_api | bhavcopy | offer_document
    open_date           TEXT,                  -- the as_of date
    close_date          TEXT,
    listing_date        TEXT,
    listing_open_price  REAL,
    banker_raw          TEXT,
    banker_id           INTEGER REFERENCES bankers(banker_id),
    registrar_raw       TEXT,
    market_maker_raw    TEXT,
    rhp_url             TEXT,
    drhp_url            TEXT,
    final_subscription_x REAL,                 -- context only, never scored
    status              TEXT,                  -- listed | withdrawn | deferred
    source              TEXT,
    fetched_at          TEXT
);
CREATE INDEX IF NOT EXISTS ipos_symbol ON ipos(exchange, symbol);
CREATE INDEX IF NOT EXISTS ipos_banker ON ipos(banker_id);

CREATE TABLE IF NOT EXISTS bankers (
    banker_id       INTEGER PRIMARY KEY,
    canonical_name  TEXT NOT NULL UNIQUE,
    sebi_reg_no     TEXT,
    source          TEXT                       -- sebi_register | universe
);

CREATE TABLE IF NOT EXISTS banker_alias (
    alias       TEXT PRIMARY KEY,              -- normalised name
    banker_id   INTEGER NOT NULL REFERENCES bankers(banker_id),
    match_type  TEXT NOT NULL,                 -- exact | fuzzy | manual | self
    similarity  REAL
);

-- An IPO can have several lead managers; each one's track record counts it.
CREATE TABLE IF NOT EXISTS ipo_banker (
    ipo_id      TEXT NOT NULL REFERENCES ipos(ipo_id),
    banker_id   INTEGER NOT NULL REFERENCES bankers(banker_id),
    position    INTEGER NOT NULL,
    PRIMARY KEY (ipo_id, banker_id)
);

CREATE TABLE IF NOT EXISTS trading_days (
    exchange    TEXT NOT NULL,
    date        TEXT NOT NULL,
    PRIMARY KEY (exchange, date)
);

CREATE TABLE IF NOT EXISTS prices (
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    series      TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    prev_close  REAL,                          -- exchange-adjusted on ex-dates
    volume      INTEGER,
    turnover    REAL,
    corp_ind    TEXT,
    PRIMARY KEY (exchange, symbol, date, series)
);

CREATE TABLE IF NOT EXISTS corp_actions (
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    ex_date     TEXT NOT NULL,
    type        TEXT NOT NULL,                 -- derived | bonus | split
    ratio       REAL NOT NULL,                 -- price multiplier on ex-date
    source      TEXT NOT NULL,
    PRIMARY KEY (exchange, symbol, ex_date, type)
);

CREATE TABLE IF NOT EXISTS labels (
    ipo_id              TEXT PRIMARY KEY REFERENCES ipos(ipo_id),
    listing_date        TEXT NOT NULL,
    issue_price         REAL NOT NULL,
    close_6m            REAL,
    close_6m_date       TEXT,                  -- last traded day used
    outcome_known_on    TEXT NOT NULL,         -- listing + 126 trading days
    adj_factor          REAL NOT NULL,         -- shares per IPO share
    ret_6m              REAL,
    crashed             INTEGER,
    suspended           INTEGER,
    stale_close         INTEGER NOT NULL,      -- last trade long before day 126
    sebi_action         INTEGER NOT NULL DEFAULT 0,
    sebi_action_date    TEXT,
    adverse             INTEGER,
    index_name          TEXT,                  -- benchmark for the relative label
    index_ret_6m        REAL,                  -- benchmark return over the same window
    crashed_vs_index    INTEGER                -- Label A variant: 40%+ worse than the index
);

CREATE TABLE IF NOT EXISTS sebi_orders (
    order_id        TEXT PRIMARY KEY,
    order_date      TEXT NOT NULL,
    order_type      TEXT,                      -- interim | final | settlement | adjudication | other
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    pdf_url         TEXT,
    matched_query   TEXT,
    text_chars      INTEGER,
    fetched_at      TEXT
);

CREATE TABLE IF NOT EXISTS sebi_order_entity (
    order_id        TEXT NOT NULL REFERENCES sebi_orders(order_id),
    entity_name     TEXT NOT NULL,
    entity_role     TEXT NOT NULL,
    entity_id       TEXT,
    finding         TEXT,
    action          TEXT,
    period          TEXT,
    reviewed        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, entity_name, entity_role)
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,          -- ent:<type>:<slug>
    type            TEXT NOT NULL,
    canonical_name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_alias (
    alias           TEXT NOT NULL,
    entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    match_type      TEXT NOT NULL,             -- exact | fuzzy | review | manual
    similarity      REAL,
    PRIMARY KEY (alias, entity_id)
);

CREATE TABLE IF NOT EXISTS entity_edge (
    src_id          TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    type            TEXT NOT NULL,
    source_ref      TEXT NOT NULL,
    valid_from      TEXT NOT NULL,             -- date the link became public
    PRIMARY KEY (src_id, dst_id, type, source_ref)
);

CREATE TABLE IF NOT EXISTS docs (
    ipo_id          TEXT NOT NULL REFERENCES ipos(ipo_id),
    doc_type        TEXT NOT NULL,             -- rhp | drhp
    url             TEXT NOT NULL,
    path            TEXT,
    sha256          TEXT,
    pages           INTEGER,
    text_pages      INTEGER,
    status          TEXT NOT NULL,             -- ok | scanned | missing | error
    PRIMARY KEY (ipo_id, doc_type)
);

CREATE TABLE IF NOT EXISTS facts (
    ipo_id          TEXT NOT NULL REFERENCES ipos(ipo_id),
    field           TEXT NOT NULL,
    fiscal_year     TEXT NOT NULL DEFAULT '',
    value           REAL,
    unit            TEXT,
    page            INTEGER,
    verified        INTEGER NOT NULL DEFAULT 0,
    method          TEXT,
    PRIMARY KEY (ipo_id, field, fiscal_year)
);

-- compute_features output for every labelled IPO, as of its bid date.
-- A cache for training; the live agents recompute features themselves.
CREATE TABLE IF NOT EXISTS features (
    ipo_id          TEXT NOT NULL REFERENCES ipos(ipo_id),
    as_of           TEXT NOT NULL,
    name            TEXT NOT NULL,
    value           REAL,
    PRIMARY KEY (ipo_id, as_of, name)
);

-- One row per traded SME security, from the bhavcopies. BSE IPOs are matched
-- to their scrip code through this table (name + first trading day).
CREATE TABLE IF NOT EXISTS securities (
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,             -- NSE symbol or BSE scrip code
    name            TEXT,
    isin            TEXT,
    ticker          TEXT,
    first_date      TEXT,
    last_date       TEXT,
    first_lot       INTEGER,                   -- board lot on the first UDiFF day seen
    first_lot_date  TEXT,
    PRIMARY KEY (exchange, symbol)
);

-- Every name a security has carried in the bhavcopies: older BSE files use
-- abbreviations ("MESON") and companies get renamed after listing.
CREATE TABLE IF NOT EXISTS security_names (
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    name            TEXT NOT NULL,
    PRIMARY KEY (exchange, symbol, name)
);

-- S13: statutory auditors read from offer documents.
CREATE TABLE IF NOT EXISTS auditors (
    auditor_id      INTEGER PRIMARY KEY,
    canonical_name  TEXT NOT NULL UNIQUE,
    icai_frn        TEXT,                      -- ICAI firm registration number
    peer_review_no  TEXT
);
CREATE TABLE IF NOT EXISTS auditor_alias (
    alias           TEXT PRIMARY KEY,          -- normalised name
    auditor_id      INTEGER NOT NULL REFERENCES auditors(auditor_id)
);
CREATE TABLE IF NOT EXISTS ipo_auditor (
    ipo_id          TEXT PRIMARY KEY REFERENCES ipos(ipo_id),
    auditor_id      INTEGER REFERENCES auditors(auditor_id),
    auditor_raw     TEXT,
    changed_3y      INTEGER,                   -- 1 changed, 0 no change, NULL unknown
    page            INTEGER                    -- RHP page the auditor was read from
);

-- S03: registrar and market maker masters, built like the banker master.
CREATE TABLE IF NOT EXISTS registrars (
    registrar_id    INTEGER PRIMARY KEY,
    canonical_name  TEXT NOT NULL UNIQUE,
    sebi_reg_no     TEXT,                      -- INR... from SEBI's register
    source          TEXT
);
CREATE TABLE IF NOT EXISTS registrar_alias (
    alias           TEXT PRIMARY KEY,
    registrar_id    INTEGER NOT NULL REFERENCES registrars(registrar_id),
    match_type      TEXT NOT NULL,
    similarity      REAL
);
CREATE TABLE IF NOT EXISTS market_makers (
    mm_id           INTEGER PRIMARY KEY,
    canonical_name  TEXT NOT NULL UNIQUE,
    sebi_reg_no     TEXT,                      -- not filled: brokers' register not used
    source          TEXT
);
CREATE TABLE IF NOT EXISTS mm_alias (
    alias           TEXT PRIMARY KEY,
    mm_id           INTEGER NOT NULL REFERENCES market_makers(mm_id),
    match_type      TEXT NOT NULL,
    similarity      REAL
);
CREATE TABLE IF NOT EXISTS ipo_market_maker (
    ipo_id          TEXT PRIMARY KEY REFERENCES ipos(ipo_id),
    mm_id           INTEGER NOT NULL REFERENCES market_makers(mm_id)
);

-- S06: SME market index closes.
CREATE TABLE IF NOT EXISTS index_prices (
    index_name      TEXT NOT NULL,
    date            TEXT NOT NULL,
    close           REAL NOT NULL,
    PRIMARY KEY (index_name, date)
);
