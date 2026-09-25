from __future__ import annotations

import sqlite3
from pathlib import Path
from datetime import datetime, timezone


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")


SCHEMA = r"""
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 1000;


-- ============================================================
-- 1. METADATA
-- ============================================================

CREATE TABLE metadata (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ============================================================
-- 2. DATA SOURCES
-- Herkunft externer Daten
-- ============================================================

CREATE TABLE data_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    source_type TEXT,
    url         TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ============================================================
-- 3. SECURITY
-- Zentrale Identität eines beobachteten Wertpapiers
-- Kein komplexer Instrument-/Listing-Master.
-- ============================================================

CREATE TABLE security (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,

    symbol      TEXT,
    isin        TEXT,
    wkn         TEXT,

    name        TEXT NOT NULL,

    exchange    TEXT,
    currency    TEXT,
    country     TEXT,

    asset_type  TEXT NOT NULL DEFAULT 'stock',

    sector      TEXT,
    industry    TEXT,

    active      INTEGER NOT NULL DEFAULT 1,

    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_security_isin
ON security(isin)
WHERE isin IS NOT NULL;

CREATE INDEX idx_security_symbol
ON security(symbol);

CREATE INDEX idx_security_name
ON security(name);

CREATE INDEX idx_security_active
ON security(active);


-- ============================================================
-- 4. IMPORTS
-- Importhistorie, z.B. Parqet
-- ============================================================

CREATE TABLE imports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    import_type         TEXT NOT NULL,
    source              TEXT,

    file_name           TEXT,

    started_at          TEXT,
    completed_at        TEXT,

    records_total       INTEGER,
    records_imported    INTEGER,
    records_failed      INTEGER,

    status              TEXT,
    notes               TEXT,

    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ============================================================
-- 5. TRANSACTIONS
-- Vollständige Portfoliohistorie
-- ============================================================

CREATE TABLE transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id         INTEGER NOT NULL,

    transaction_type    TEXT NOT NULL,
    transaction_date    TEXT NOT NULL,

    shares              REAL,
    price               REAL,
    amount              REAL,

    fees                REAL DEFAULT 0,
    taxes               REAL DEFAULT 0,

    currency            TEXT,

    broker              TEXT,
    external_id         TEXT,

    notes               TEXT,

    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE
);

CREATE INDEX idx_transactions_security_date
ON transactions(security_id, transaction_date);

CREATE INDEX idx_transactions_date
ON transactions(transaction_date);

CREATE UNIQUE INDEX idx_transactions_external_id
ON transactions(external_id)
WHERE external_id IS NOT NULL;


-- ============================================================
-- 6. POSITIONS
-- Aktueller Portfoliozustand
-- Genau eine Position pro Security.
-- ============================================================

CREATE TABLE positions (
    security_id             INTEGER PRIMARY KEY,

    shares                  REAL NOT NULL DEFAULT 0,

    avg_cost                REAL,
    remaining_cost_basis    REAL,

    currency                TEXT,

    invested_amount         REAL,
    realized_gain           REAL DEFAULT 0,

    first_transaction_at    TEXT,
    last_transaction_at     TEXT,

    transaction_count       INTEGER NOT NULL DEFAULT 0,

    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE
);

CREATE INDEX idx_positions_shares
ON positions(shares);


-- ============================================================
-- 7. WATCHLIST
-- Swing-Kandidaten
-- Genau ein Datensatz je Security.
-- ============================================================

CREATE TABLE watchlist (
    security_id         INTEGER PRIMARY KEY,

    status              TEXT NOT NULL DEFAULT 'WATCH',

    priority            INTEGER,

    entry_reason        TEXT,
    thesis              TEXT,

    target_entry        REAL,

    notes               TEXT,

    added_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE
);

CREATE INDEX idx_watchlist_status
ON watchlist(status);

CREATE INDEX idx_watchlist_priority
ON watchlist(priority);


-- ============================================================
-- 8. MARKET SNAPSHOT
-- Aktueller / letzter bekannter Marktstatus.
-- Genau eine Zeile pro Security.
-- ============================================================

CREATE TABLE market_snapshot (
    security_id             INTEGER PRIMARY KEY,

    as_of_at                TEXT,

    price                   REAL,
    previous_close          REAL,

    open                    REAL,
    high                    REAL,
    low                     REAL,

    volume                  REAL,

    market_cap              REAL,
    shares_outstanding      REAL,

    currency                TEXT,

    source_id               INTEGER,

    fetched_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (source_id)
        REFERENCES data_sources(id)
);


-- ============================================================
-- 9. MARKET DATA
-- Tageshistorie OHLCV.
-- Hauptbasis für technische Swing-Kennzahlen.
-- ============================================================

CREATE TABLE market_data (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id         INTEGER NOT NULL,

    trade_date          TEXT NOT NULL,

    open                REAL,
    high                REAL,
    low                 REAL,
    close               REAL,
    adjusted_close      REAL,

    volume              REAL,

    currency            TEXT,

    source_id           INTEGER,

    fetched_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (source_id)
        REFERENCES data_sources(id),

    UNIQUE (
        security_id,
        trade_date,
        source_id
    )
);

CREATE INDEX idx_market_data_security_date
ON market_data(security_id, trade_date DESC);

CREATE INDEX idx_market_data_date
ON market_data(trade_date);


-- ============================================================
-- 10. FUNDAMENTALS
-- Jahres-, Quartals- und optional TTM-Daten.
-- Rohdaten, keine unnötig berechneten Kennzahlen.
-- ============================================================

CREATE TABLE fundamentals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id             INTEGER NOT NULL,

    period_end              TEXT NOT NULL,
    period_type             TEXT NOT NULL,

    fiscal_year             INTEGER,
    fiscal_quarter          INTEGER,

    filing_date             TEXT,

    currency                TEXT,

    revenue                 REAL,
    gross_profit            REAL,

    operating_income        REAL,
    ebit                    REAL,
    ebitda                  REAL,

    net_income              REAL,

    eps_basic               REAL,
    eps_diluted             REAL,

    operating_cash_flow     REAL,
    capex                   REAL,
    free_cash_flow          REAL,

    cash                    REAL,
    total_debt              REAL,

    total_assets            REAL,
    total_liabilities       REAL,
    total_equity            REAL,

    shares_outstanding      REAL,

    source_id               INTEGER,

    fetched_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (source_id)
        REFERENCES data_sources(id),

    UNIQUE (
        security_id,
        period_end,
        period_type,
        source_id
    )
);

CREATE INDEX idx_fundamentals_security_period
ON fundamentals(
    security_id,
    period_end DESC
);

CREATE INDEX idx_fundamentals_period_type
ON fundamentals(period_type);


-- ============================================================
-- 11. ESTIMATES
-- Analystenschätzungen als Snapshots.
-- Damit lassen sich Revisionen selbst berechnen.
-- ============================================================

CREATE TABLE estimates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id         INTEGER NOT NULL,

    metric              TEXT NOT NULL,

    period_end          TEXT NOT NULL,
    period_type         TEXT,

    as_of_date          TEXT NOT NULL,

    estimate_mean       REAL,
    estimate_median     REAL,
    estimate_high       REAL,
    estimate_low        REAL,

    analyst_count       INTEGER,

    currency            TEXT,

    source_id           INTEGER,

    fetched_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (source_id)
        REFERENCES data_sources(id),

    UNIQUE (
        security_id,
        metric,
        period_end,
        as_of_date,
        source_id
    )
);

CREATE INDEX idx_estimates_security_metric
ON estimates(
    security_id,
    metric,
    period_end,
    as_of_date DESC
);


-- ============================================================
-- 12. RATINGS
-- Analystenkonsens als historische Snapshots.
-- ============================================================

CREATE TABLE ratings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id         INTEGER NOT NULL,

    as_of_date          TEXT NOT NULL,

    strong_buy          INTEGER,
    buy                 INTEGER,
    hold                INTEGER,
    sell                INTEGER,
    strong_sell         INTEGER,

    analyst_count       INTEGER,

    source_id           INTEGER,

    fetched_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (source_id)
        REFERENCES data_sources(id),

    UNIQUE (
        security_id,
        as_of_date,
        source_id
    )
);

CREATE INDEX idx_ratings_security_date
ON ratings(security_id, as_of_date DESC);


-- ============================================================
-- 13. PRICE TARGETS
-- Analystenkursziele als Snapshots.
-- ============================================================

CREATE TABLE price_targets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id         INTEGER NOT NULL,

    as_of_date          TEXT NOT NULL,

    target_mean         REAL,
    target_median       REAL,
    target_high         REAL,
    target_low          REAL,

    analyst_count       INTEGER,

    currency            TEXT,

    source_id           INTEGER,

    fetched_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (source_id)
        REFERENCES data_sources(id),

    UNIQUE (
        security_id,
        as_of_date,
        source_id
    )
);

CREATE INDEX idx_price_targets_security_date
ON price_targets(security_id, as_of_date DESC);


-- ============================================================
-- 14. EVENTS
-- Dauerhafte kursrelevante Ereignisse.
-- ============================================================

CREATE TABLE events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id         INTEGER NOT NULL,

    event_type          TEXT NOT NULL,
    event_date          TEXT NOT NULL,

    period_end          TEXT,

    title               TEXT,

    actual_value        REAL,
    estimated_value     REAL,
    surprise_percent    REAL,

    currency            TEXT,

    notes               TEXT,

    source_id           INTEGER,

    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (source_id)
        REFERENCES data_sources(id)
);

CREATE INDEX idx_events_security_date
ON events(security_id, event_date DESC);

CREATE INDEX idx_events_type_date
ON events(event_type, event_date);


-- ============================================================
-- 15. NEWS
-- Kurzlebiger Cache.
-- Normale News werden später regelmäßig gelöscht.
-- Material News können dauerhaft erhalten bleiben.
-- ============================================================

CREATE TABLE news (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id         INTEGER NOT NULL,

    published_at        TEXT NOT NULL,

    title               TEXT NOT NULL,
    summary             TEXT,

    url                 TEXT,

    publisher           TEXT,
    category            TEXT,

    sentiment           REAL,

    is_material         INTEGER NOT NULL DEFAULT 0,

    expires_at          TEXT,

    source_id           INTEGER,

    fetched_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (source_id)
        REFERENCES data_sources(id)
);

CREATE UNIQUE INDEX idx_news_url
ON news(url)
WHERE url IS NOT NULL;

CREATE INDEX idx_news_security_date
ON news(security_id, published_at DESC);

CREATE INDEX idx_news_expiry
ON news(expires_at);

CREATE INDEX idx_news_material
ON news(is_material);


-- ============================================================
-- 16. DECISIONS
-- Historie konkreter Tradingentscheidungen.
-- ============================================================

CREATE TABLE decisions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id             INTEGER NOT NULL,

    decision_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    status                  TEXT NOT NULL,
    confidence              REAL,

    price_at_decision       REAL,

    fundamental_score       REAL,
    valuation_score         REAL,
    momentum_score          REAL,
    balance_sheet_score     REAL,
    risk_score              REAL,
    portfolio_fit_score     REAL,

    reason                  TEXT,
    next_action             TEXT,

    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE
);

CREATE INDEX idx_decisions_security_date
ON decisions(security_id, decision_at DESC);

CREATE INDEX idx_decisions_status
ON decisions(status);


-- ============================================================
-- 17. ANALYSIS HISTORY
-- Längere Agentenanalysen / Reviews.
-- ============================================================

CREATE TABLE analysis_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    security_id         INTEGER NOT NULL,

    analysis_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    analysis_type       TEXT,

    summary             TEXT,
    full_analysis       TEXT,

    model               TEXT,

    decision_id         INTEGER,

    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    FOREIGN KEY (decision_id)
        REFERENCES decisions(id)
        ON DELETE SET NULL
);

CREATE INDEX idx_analysis_security_date
ON analysis_history(security_id, analysis_at DESC);


-- ============================================================
-- 18. USEFUL VIEWS
-- ============================================================

CREATE VIEW v_active_positions AS
SELECT
    s.id AS security_id,
    s.symbol,
    s.isin,
    s.wkn,
    s.name,
    s.exchange,
    s.currency AS security_currency,
    p.currency AS cost_basis_currency,

    p.shares,
    p.avg_cost,
    p.remaining_cost_basis,
    p.realized_gain,
    p.last_transaction_at

FROM positions p

JOIN security s
    ON s.id = p.security_id

WHERE p.shares > 0;


CREATE VIEW v_watchlist AS
SELECT
    s.id AS security_id,
    s.symbol,
    s.isin,
    s.wkn,
    s.name,
    s.exchange,
    s.currency,
    s.country,
    s.sector,
    s.industry,

    w.status,
    w.priority,
    w.target_entry,
    w.entry_reason,
    w.thesis,
    w.added_at,

    ms.price,
    ms.previous_close,
    ms.volume,
    ms.market_cap,
    ms.as_of_at AS market_as_of

FROM watchlist w

JOIN security s
    ON s.id = w.security_id

LEFT JOIN market_snapshot ms
    ON ms.security_id = s.id;


CREATE VIEW v_portfolio_market AS
SELECT
    s.id AS security_id,
    s.symbol,
    s.name,
    s.isin,

    p.shares,
    p.avg_cost,
    p.remaining_cost_basis,
    p.currency AS cost_basis_currency,
    p.realized_gain,

    ms.price AS market_price_native,
    ms.currency AS market_price_currency,
    ms.previous_close,
    ms.market_cap,
    ms.as_of_at,

    CASE WHEN p.shares > 0 AND ms.price IS NOT NULL
              AND upper(p.currency) = upper(ms.currency)
         THEN p.shares * ms.price ELSE NULL END AS market_value,
    CASE WHEN p.shares > 0 AND ms.price IS NOT NULL
              AND upper(p.currency) = upper(ms.currency)
         THEN p.currency ELSE NULL END AS market_value_currency,
    CASE WHEN ms.price IS NULL THEN 'MARKET_PRICE_UNAVAILABLE'
         WHEN p.currency IS NULL OR ms.currency IS NULL THEN 'CURRENCY_UNAVAILABLE'
         WHEN upper(p.currency) <> upper(ms.currency) THEN 'CURRENCY_MISMATCH'
         ELSE 'AVAILABLE' END AS valuation_status

FROM positions p

JOIN security s
    ON s.id = p.security_id

LEFT JOIN market_snapshot ms
    ON ms.security_id = s.id

WHERE p.shares > 0;


-- ============================================================
-- 18. STRATEGY ASSIGNMENTS
-- Initial model: one time-aware strategy assignment per security interval.
-- Future versions may attach assignments to position lots when simultaneous
-- strategies for one security must be represented.
-- ============================================================

CREATE TABLE strategy_assignment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id     INTEGER NOT NULL,
    strategy_type   TEXT NOT NULL
        CHECK (strategy_type IN ('long_term', 'swing', 'tactical', 'unknown')),
    effective_from  TEXT NOT NULL,
    effective_to    TEXT,
    source          TEXT,
    rationale       TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CHECK (effective_to IS NULL OR effective_to >= effective_from),

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,

    UNIQUE (security_id, effective_from)
);

CREATE INDEX idx_strategy_assignment_security_dates
ON strategy_assignment(security_id, effective_from, effective_to);

CREATE TRIGGER trg_strategy_assignment_no_overlap_insert
BEFORE INSERT ON strategy_assignment
WHEN EXISTS (
    SELECT 1
    FROM strategy_assignment existing
    WHERE existing.security_id = NEW.security_id
      AND COALESCE(existing.effective_to, '9999-12-31') >= NEW.effective_from
      AND COALESCE(NEW.effective_to, '9999-12-31') >= existing.effective_from
)
BEGIN
    SELECT RAISE(ABORT, 'strategy assignment overlaps an existing interval');
END;

CREATE TRIGGER trg_strategy_assignment_no_overlap_update
BEFORE UPDATE OF security_id, effective_from, effective_to ON strategy_assignment
WHEN EXISTS (
    SELECT 1
    FROM strategy_assignment existing
    WHERE existing.security_id = NEW.security_id
      AND existing.id <> OLD.id
      AND COALESCE(existing.effective_to, '9999-12-31') >= NEW.effective_from
      AND COALESCE(NEW.effective_to, '9999-12-31') >= existing.effective_from
)
BEGIN
    SELECT RAISE(ABORT, 'strategy assignment overlaps an existing interval');
END;

-- ============================================================
-- 18a. CANDIDATE PROMOTION AUDIT
-- Promotion evaluation is read-only.  This relation records only explicit
-- approvals; it is not a replacement for generic watchlist membership.
-- ============================================================

CREATE TABLE candidate_promotion (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id     INTEGER NOT NULL,
    evaluated_at    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('watching', 'ready', 'promoted', 'rejected', 'deferred')),
    source          TEXT NOT NULL CHECK (length(trim(source)) > 0),
    rationale       TEXT,
    details_json    TEXT,
    approved_at     TEXT,
    approved_by     TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (security_id) REFERENCES security(id) ON DELETE CASCADE
);

CREATE INDEX idx_candidate_promotion_security_evaluated
ON candidate_promotion(security_id, evaluated_at DESC, id DESC);


-- ============================================================
-- 18b. FX RATES
-- ECB convention: 1 EUR = N quote currency units.
-- ============================================================

CREATE TABLE fx_rates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rate_date       TEXT NOT NULL,
    base_currency   TEXT NOT NULL CHECK (base_currency GLOB '[A-Z][A-Z][A-Z]'),
    quote_currency  TEXT NOT NULL CHECK (quote_currency GLOB '[A-Z][A-Z][A-Z]'),
    rate            REAL NOT NULL CHECK (rate > 0),
    source          TEXT NOT NULL CHECK (length(trim(source)) > 0),
    fetched_at      TEXT NOT NULL,
    CHECK (base_currency = 'EUR'),
    CHECK (base_currency <> quote_currency),
    UNIQUE (rate_date, base_currency, quote_currency, source)
);

CREATE INDEX idx_fx_rates_lookup
ON fx_rates(base_currency, quote_currency, source, rate_date DESC);


-- ============================================================
-- 18c. SWING CAMPAIGN LIFECYCLE
-- Explicit campaign identity and manually recorded lifecycle events.
-- Events never imply automated trading execution.
-- ============================================================

CREATE TABLE swing_campaign (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id             INTEGER NOT NULL,
    strategy_assignment_id  INTEGER NOT NULL,
    opened_at               TEXT NOT NULL,
    original_quantity       REAL NOT NULL CHECK (original_quantity > 0),
    reference_avg_cost      REAL CHECK (reference_avg_cost IS NULL OR reference_avg_cost > 0),
    reference_currency      TEXT CHECK (reference_currency IS NULL OR reference_currency GLOB '[A-Z][A-Z][A-Z]'),
    status                  TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    closed_at               TEXT,
    source                  TEXT NOT NULL CHECK (length(trim(source)) > 0),
    rationale               TEXT,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((reference_avg_cost IS NULL) = (reference_currency IS NULL)),
    CHECK ((status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL)),
    CHECK (closed_at IS NULL OR closed_at >= opened_at),
    FOREIGN KEY (security_id) REFERENCES security(id) ON DELETE CASCADE,
    FOREIGN KEY (strategy_assignment_id) REFERENCES strategy_assignment(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_swing_campaign_one_open_per_security
ON swing_campaign(security_id) WHERE status = 'open';

CREATE INDEX idx_swing_campaign_security_status
ON swing_campaign(security_id, status, opened_at DESC);

CREATE TRIGGER trg_swing_campaign_assignment_insert
BEFORE INSERT ON swing_campaign
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_assignment sa
    WHERE sa.id = NEW.strategy_assignment_id
      AND sa.security_id = NEW.security_id
      AND sa.strategy_type = 'swing'
)
BEGIN
    SELECT RAISE(ABORT, 'campaign requires a matching swing strategy assignment');
END;

CREATE TRIGGER trg_swing_campaign_assignment_update
BEFORE UPDATE OF security_id, strategy_assignment_id ON swing_campaign
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_assignment sa
    WHERE sa.id = NEW.strategy_assignment_id
      AND sa.security_id = NEW.security_id
      AND sa.strategy_type = 'swing'
)
BEGIN
    SELECT RAISE(ABORT, 'campaign requires a matching swing strategy assignment');
END;

CREATE TABLE swing_campaign_event (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         INTEGER NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN (
        'baseline', 'add', 'tp1_signal', 'tp1_execution', 'tp2_signal',
        'tp2_execution', 'manual_reduction', 'stop_execution', 'close'
    )),
    event_at            TEXT NOT NULL,
    quantity            REAL CHECK (quantity IS NULL OR quantity > 0),
    price               REAL CHECK (price IS NULL OR price > 0),
    currency            TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    transaction_id      INTEGER,
    source              TEXT NOT NULL CHECK (length(trim(source)) > 0),
    external_event_id   TEXT,
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (price IS NULL OR currency IS NOT NULL),
    FOREIGN KEY (campaign_id) REFERENCES swing_campaign(id) ON DELETE RESTRICT,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_swing_campaign_event_external_id
ON swing_campaign_event(external_event_id) WHERE external_event_id IS NOT NULL;

CREATE UNIQUE INDEX idx_swing_campaign_event_transaction
ON swing_campaign_event(transaction_id) WHERE transaction_id IS NOT NULL;

CREATE INDEX idx_swing_campaign_event_campaign_date
ON swing_campaign_event(campaign_id, event_at, id);

CREATE TRIGGER trg_swing_campaign_event_transaction_insert
BEFORE INSERT ON swing_campaign_event
WHEN NEW.transaction_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM swing_campaign campaign
    JOIN transactions transaction_row ON transaction_row.id = NEW.transaction_id
    WHERE campaign.id = NEW.campaign_id
      AND campaign.security_id = transaction_row.security_id
)
BEGIN
    SELECT RAISE(ABORT, 'linked transaction must belong to the campaign security');
END;

CREATE TRIGGER trg_swing_campaign_event_transaction_update
BEFORE UPDATE OF campaign_id, transaction_id ON swing_campaign_event
WHEN NEW.transaction_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM swing_campaign campaign
    JOIN transactions transaction_row ON transaction_row.id = NEW.transaction_id
    WHERE campaign.id = NEW.campaign_id
      AND campaign.security_id = transaction_row.security_id
)
BEGIN
    SELECT RAISE(ABORT, 'linked transaction must belong to the campaign security');
END;


-- ============================================================
-- INITIAL METADATA
-- ============================================================

INSERT INTO metadata(key, value)
VALUES ('schema_version', '2.0');

INSERT INTO metadata(key, value)
VALUES ('strategy_assignment_schema_version', '1');

INSERT INTO metadata(key, value)
VALUES ('candidate_promotion_schema_version', '1');

INSERT INTO metadata(key, value)
VALUES ('fx_rates_schema_version', '1');

INSERT INTO metadata(key, value)
VALUES ('swing_campaign_schema_version', '1');

INSERT INTO metadata(key, value)
VALUES ('database_type', 'swing_trading');

INSERT INTO metadata(key, value)
VALUES ('market_data_interval', '1day');

"""


DEFAULT_SOURCES = [
    ("FMP", "market_data", "https://financialmodelingprep.com"),
    ("Yahoo Finance", "web", "https://finance.yahoo.com"),
    ("SEC EDGAR", "regulatory", "https://www.sec.gov"),
    ("Company IR", "primary", None),
    ("Exchange", "primary", None),
    ("Web", "web", None),
]


def remove_database_files() -> None:
    candidates = [
        DB_PATH,
        Path(str(DB_PATH) + "-wal"),
        Path(str(DB_PATH) + "-shm"),
    ]

    for path in candidates:
        if path.exists():
            print(f"Deleting: {path}")
            path.unlink()


def create_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"Creating: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))

    try:
        conn.executescript(SCHEMA)

        conn.executemany(
            """
            INSERT INTO data_sources(
                name,
                source_type,
                url
            )
            VALUES (?, ?, ?)
            """,
            DEFAULT_SOURCES,
        )

        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO metadata(key, value)
            VALUES ('created_at', ?)
            """,
            (now,),
        )

        conn.commit()

    finally:
        conn.close()


def validate_database() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    try:
        integrity = conn.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        journal_mode = conn.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0]

        foreign_keys = conn.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0]

        tables = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        views = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'view'
            ORDER BY name
            """
        ).fetchall()

        print()
        print("===============================================")
        print(" Trading DB v2 Validation")
        print("===============================================")
        print(f"Database       : {DB_PATH}")
        print(f"Integrity      : {integrity}")
        print(f"Journal mode   : {journal_mode}")
        print(f"Foreign keys   : {bool(foreign_keys)}")
        print(f"Tables         : {len(tables)}")
        print(f"Views          : {len(views)}")
        print()

        print("Tables:")
        for row in tables:
            print(f"  - {row['name']}")

        print()
        print("Views:")
        for row in views:
            print(f"  - {row['name']}")

        print()
        print("Data Sources:")

        sources = conn.execute(
            """
            SELECT id, name, source_type
            FROM data_sources
            ORDER BY id
            """
        ).fetchall()

        for row in sources:
            print(
                f"  {row['id']:>2} | "
                f"{row['name']:<20} | "
                f"{row['source_type']}"
            )

        print()
        print("===============================================")

        if integrity != "ok":
            raise RuntimeError(
                f"SQLite integrity check failed: {integrity}"
            )

    finally:
        conn.close()


def main() -> None:
    print()
    print("===============================================")
    print(" Trading DB v2 RESET")
    print("===============================================")
    print()
    print("WARNING:")
    print(f"Existing database will be deleted:")
    print(DB_PATH)
    print()

    confirm = input(
        "Type RESET to continue: "
    ).strip()

    if confirm != "RESET":
        print("Cancelled.")
        return

    print()

    remove_database_files()
    create_database()
    validate_database()

    print()
    print("Trading DB v2 created successfully.")


if __name__ == "__main__":
    main()
