"""Deliberate additive migration for Phase-3B.5d portfolio capital state."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
FEATURE_VERSION_KEY = "portfolio_capital_state_schema_version"
FEATURE_VERSION = "1"

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS portfolio_capital_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of           TEXT NOT NULL,
    base_currency   TEXT NOT NULL CHECK (base_currency GLOB '[A-Z][A-Z][A-Z]'),
    cash_available  REAL,
    buying_power    REAL,
    source          TEXT NOT NULL CHECK (length(trim(source)) > 0),
    quality         TEXT NOT NULL
        CHECK (quality IN ('available', 'partial', 'unavailable', 'stale')),
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_portfolio_capital_state_as_of
ON portfolio_capital_state(as_of DESC, id DESC);
"""


def inspect_migration(conn: sqlite3.Connection) -> dict:
    schema_row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    if schema_row is None or schema_row[0] != "2.0":
        raise RuntimeError("capital state migration requires schema_version 2.0")
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'portfolio_capital_state'"
    ).fetchone() is not None
    feature_row = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (FEATURE_VERSION_KEY,)
    ).fetchone()
    return {
        "table_exists": table_exists,
        "feature_version": feature_row[0] if feature_row is not None else None,
        "target_feature_version": FEATURE_VERSION,
    }


def apply_migration(conn: sqlite3.Connection) -> dict:
    before = inspect_migration(conn)
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + MIGRATION_SQL
            + f"""
            INSERT INTO metadata(key, value, updated_at)
            VALUES ('{FEATURE_VERSION_KEY}', '{FEATURE_VERSION}', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at;
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
    conn = sqlite3.connect(f"file:///{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.execute("PRAGMA query_only = ON")
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate portfolio capital state (dry-run by default)")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--write", action="store_true", help="apply the migration")
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
