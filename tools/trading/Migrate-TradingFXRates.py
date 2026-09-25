"""Deliberate additive migration for Phase-3A.5 ECB FX rates."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
FEATURE_VERSION_KEY = "fx_rates_schema_version"
FEATURE_VERSION = "1"

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS fx_rates (
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

CREATE INDEX IF NOT EXISTS idx_fx_rates_lookup
ON fx_rates(base_currency, quote_currency, source, rate_date DESC);

DROP VIEW IF EXISTS v_active_positions;
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
JOIN security s ON s.id = p.security_id
WHERE p.shares > 0;

DROP VIEW IF EXISTS v_portfolio_market;
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
    CASE
        WHEN p.shares > 0
         AND ms.price IS NOT NULL
         AND upper(p.currency) = upper(ms.currency)
        THEN p.shares * ms.price
        ELSE NULL
    END AS market_value,
    CASE
        WHEN p.shares > 0
         AND ms.price IS NOT NULL
         AND upper(p.currency) = upper(ms.currency)
        THEN p.currency
        ELSE NULL
    END AS market_value_currency,
    CASE
        WHEN ms.price IS NULL THEN 'MARKET_PRICE_UNAVAILABLE'
        WHEN p.currency IS NULL OR ms.currency IS NULL THEN 'CURRENCY_UNAVAILABLE'
        WHEN upper(p.currency) <> upper(ms.currency) THEN 'CURRENCY_MISMATCH'
        ELSE 'AVAILABLE'
    END AS valuation_status
FROM positions p
JOIN security s ON s.id = p.security_id
LEFT JOIN market_snapshot ms ON ms.security_id = s.id
WHERE p.shares > 0;
"""


def inspect_migration(conn: sqlite3.Connection) -> dict:
    schema_row = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if schema_row is None or schema_row[0] != "2.0":
        raise RuntimeError("FX migration requires schema_version 2.0")
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fx_rates'"
    ).fetchone() is not None
    feature_row = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (FEATURE_VERSION_KEY,)
    ).fetchone()
    return {"table_exists": table_exists, "feature_version": feature_row[0] if feature_row else None, "target_feature_version": FEATURE_VERSION}


def apply_migration(conn: sqlite3.Connection) -> dict:
    before = inspect_migration(conn)
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;\n" + MIGRATION_SQL + """
            INSERT INTO metadata(key, value, updated_at)
            VALUES ('fx_rates_schema_version', '1', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
            COMMIT;
            """
        )
    except Exception:
        conn.rollback()
        raise
    return {"before": before, "after": inspect_migration(conn)}


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        return sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    return sqlite3.connect(f"file:///{path.as_posix()}?mode=ro", uri=True, timeout=10.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate ECB FX rates (dry-run by default)")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if not args.db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {args.db_path}")
    conn = _connect(args.db_path, write=args.write)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        print(apply_migration(conn) if args.write else {"dry_run": True, **inspect_migration(conn)})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
