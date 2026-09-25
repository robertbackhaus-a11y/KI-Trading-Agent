"""Deliberate, additive migration for the Phase-3A strategy assignment model.

The established database-wide schema version remains ``2.0`` because existing
tools validate it exactly. This migration records its own additive feature
version in ``metadata.strategy_assignment_schema_version`` instead. It never
runs automatically and requires ``--write``.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
FEATURE_VERSION_KEY = "strategy_assignment_schema_version"
FEATURE_VERSION = "1"


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS strategy_assignment (
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

CREATE INDEX IF NOT EXISTS idx_strategy_assignment_security_dates
ON strategy_assignment(security_id, effective_from, effective_to);

CREATE TRIGGER IF NOT EXISTS trg_strategy_assignment_no_overlap_insert
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

CREATE TRIGGER IF NOT EXISTS trg_strategy_assignment_no_overlap_update
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
"""


def inspect_migration(conn: sqlite3.Connection) -> dict:
    schema_row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    if schema_row is None or schema_row[0] != "2.0":
        raise RuntimeError("strategy assignment migration requires schema_version 2.0")
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'strategy_assignment'"
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
            + """
            INSERT INTO metadata(key, value, updated_at)
            VALUES ('strategy_assignment_schema_version', '1', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            ;
            COMMIT;
            """
        )
    except Exception:
        conn.rollback()
        raise
    return {"before": before, "after": inspect_migration(conn)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate strategy assignments (dry-run by default)")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--write", action="store_true", help="apply the migration")
    args = parser.parse_args()
    if not args.db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {args.db_path}")

    conn = sqlite3.connect(str(args.db_path), timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if args.write:
            print(apply_migration(conn))
        else:
            print({"dry_run": True, **inspect_migration(conn)})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
