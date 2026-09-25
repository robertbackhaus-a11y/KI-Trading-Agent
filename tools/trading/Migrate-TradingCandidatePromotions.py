"""Add the auditable Phase-4A candidate-promotion relation."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
FEATURE_VERSION_KEY = "candidate_promotion_schema_version"
FEATURE_VERSION = "1"

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS candidate_promotion (
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
CREATE INDEX IF NOT EXISTS idx_candidate_promotion_security_evaluated
ON candidate_promotion(security_id, evaluated_at DESC, id DESC);
"""


def inspect_migration(conn: sqlite3.Connection) -> dict:
    schema = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if schema is None or schema[0] != "2.0":
        raise RuntimeError("candidate promotion migration requires schema_version 2.0")
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_promotion'").fetchone()
    version = conn.execute("SELECT value FROM metadata WHERE key=?", (FEATURE_VERSION_KEY,)).fetchone()
    return {"table_exists": table is not None, "feature_version": version[0] if version else None, "target_feature_version": FEATURE_VERSION}


def apply_migration(conn: sqlite3.Connection) -> dict:
    before = inspect_migration(conn)
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_SQL + f"""
        INSERT INTO metadata(key, value, updated_at)
        VALUES ('{FEATURE_VERSION_KEY}', '{FEATURE_VERSION}', CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
        COMMIT;
        """)
    except Exception:
        conn.rollback()
        raise
    return {"before": before, "after": inspect_migration(conn)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate candidate-promotion audit relation (dry-run by default)")
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
