"""Add the auditable corporate-action relation (Phase 4B.3: stock splits)."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
FEATURE_VERSION_KEY = "corporate_action_schema_version"
FEATURE_VERSION = "1"

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS corporate_action (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id         INTEGER NOT NULL,
    action_type         TEXT NOT NULL CHECK (action_type IN ('STOCK_SPLIT')),
    effective_date      TEXT NOT NULL,
    ratio_numerator     REAL NOT NULL CHECK (ratio_numerator > 0),
    ratio_denominator   REAL NOT NULL CHECK (ratio_denominator > 0),
    source              TEXT NOT NULL CHECK (length(trim(source)) > 0),
    source_reference     TEXT,
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (security_id) REFERENCES security(id) ON DELETE CASCADE,
    UNIQUE (security_id, action_type, effective_date, ratio_numerator, ratio_denominator)
);
CREATE INDEX IF NOT EXISTS idx_corporate_action_security_effective
ON corporate_action(security_id, effective_date);
"""


def inspect_migration(conn: sqlite3.Connection) -> dict:
    schema = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if schema is None or schema[0] != "2.0":
        raise RuntimeError("corporate action migration requires schema_version 2.0")
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corporate_action'"
    ).fetchone()
    version = conn.execute("SELECT value FROM metadata WHERE key=?", (FEATURE_VERSION_KEY,)).fetchone()
    row_count = None
    if table is not None:
        row_count = conn.execute("SELECT COUNT(*) FROM corporate_action").fetchone()[0]
    return {
        "table_exists": table is not None,
        "row_count": row_count,
        "feature_version": version[0] if version else None,
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
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
        COMMIT;
        """
        )
    except Exception:
        conn.rollback()
        raise
    return {"before": before, "after": inspect_migration(conn)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate corporate-action (stock split) relation (dry-run by default)"
    )
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if not args.db_path.is_file():
        raise FileNotFoundError(f"Trading DB not found: {args.db_path}")
    if args.write:
        conn = sqlite3.connect(args.db_path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        conn = sqlite3.connect(f"file:///{args.db_path.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    try:
        print(apply_migration(conn) if args.write else {"dry_run": True, **inspect_migration(conn)})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
