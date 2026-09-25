"""Deliberate additive migration for Phase-3B.1 Swing campaign lifecycle."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from swing_lifecycle import FEATURE_VERSION, FEATURE_VERSION_KEY


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS swing_campaign (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id             INTEGER NOT NULL,
    strategy_assignment_id  INTEGER NOT NULL,
    opened_at               TEXT NOT NULL,
    original_quantity       REAL NOT NULL CHECK (original_quantity > 0),
    reference_avg_cost      REAL CHECK (reference_avg_cost IS NULL OR reference_avg_cost > 0),
    reference_currency      TEXT CHECK (
        reference_currency IS NULL
        OR reference_currency GLOB '[A-Z][A-Z][A-Z]'
    ),
    status                  TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    closed_at               TEXT,
    source                  TEXT NOT NULL CHECK (length(trim(source)) > 0),
    rationale               TEXT,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CHECK ((reference_avg_cost IS NULL) = (reference_currency IS NULL)),
    CHECK (
        (status = 'open' AND closed_at IS NULL)
        OR (status = 'closed' AND closed_at IS NOT NULL)
    ),
    CHECK (closed_at IS NULL OR closed_at >= opened_at),

    FOREIGN KEY (security_id)
        REFERENCES security(id)
        ON DELETE CASCADE,
    FOREIGN KEY (strategy_assignment_id)
        REFERENCES strategy_assignment(id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_swing_campaign_one_open_per_security
ON swing_campaign(security_id)
WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_swing_campaign_security_status
ON swing_campaign(security_id, status, opened_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_swing_campaign_assignment_insert
BEFORE INSERT ON swing_campaign
WHEN NOT EXISTS (
    SELECT 1
    FROM strategy_assignment sa
    WHERE sa.id = NEW.strategy_assignment_id
      AND sa.security_id = NEW.security_id
      AND sa.strategy_type = 'swing'
)
BEGIN
    SELECT RAISE(ABORT, 'campaign requires a matching swing strategy assignment');
END;

CREATE TRIGGER IF NOT EXISTS trg_swing_campaign_assignment_update
BEFORE UPDATE OF security_id, strategy_assignment_id ON swing_campaign
WHEN NOT EXISTS (
    SELECT 1
    FROM strategy_assignment sa
    WHERE sa.id = NEW.strategy_assignment_id
      AND sa.security_id = NEW.security_id
      AND sa.strategy_type = 'swing'
)
BEGIN
    SELECT RAISE(ABORT, 'campaign requires a matching swing strategy assignment');
END;

CREATE TABLE IF NOT EXISTS swing_campaign_event (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         INTEGER NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN (
        'baseline', 'add', 'tp1_signal', 'tp1_execution', 'tp2_signal',
        'tp2_execution', 'manual_reduction', 'stop_execution', 'close'
    )),
    event_at            TEXT NOT NULL,
    quantity            REAL CHECK (quantity IS NULL OR quantity > 0),
    price               REAL CHECK (price IS NULL OR price > 0),
    currency            TEXT CHECK (
        currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'
    ),
    transaction_id      INTEGER,
    source              TEXT NOT NULL CHECK (length(trim(source)) > 0),
    external_event_id   TEXT,
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CHECK (price IS NULL OR currency IS NOT NULL),
    FOREIGN KEY (campaign_id)
        REFERENCES swing_campaign(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (transaction_id)
        REFERENCES transactions(id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_swing_campaign_event_external_id
ON swing_campaign_event(external_event_id)
WHERE external_event_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_swing_campaign_event_transaction
ON swing_campaign_event(transaction_id)
WHERE transaction_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_swing_campaign_event_campaign_date
ON swing_campaign_event(campaign_id, event_at, id);

CREATE TRIGGER IF NOT EXISTS trg_swing_campaign_event_transaction_insert
BEFORE INSERT ON swing_campaign_event
WHEN NEW.transaction_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM swing_campaign campaign
    JOIN transactions transaction_row ON transaction_row.id = NEW.transaction_id
    WHERE campaign.id = NEW.campaign_id
      AND campaign.security_id = transaction_row.security_id
)
BEGIN
    SELECT RAISE(ABORT, 'linked transaction must belong to the campaign security');
END;

CREATE TRIGGER IF NOT EXISTS trg_swing_campaign_event_transaction_update
BEFORE UPDATE OF campaign_id, transaction_id ON swing_campaign_event
WHEN NEW.transaction_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM swing_campaign campaign
    JOIN transactions transaction_row ON transaction_row.id = NEW.transaction_id
    WHERE campaign.id = NEW.campaign_id
      AND campaign.security_id = transaction_row.security_id
)
BEGIN
    SELECT RAISE(ABORT, 'linked transaction must belong to the campaign security');
END;
"""


def inspect_migration(conn: sqlite3.Connection) -> dict:
    schema_row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    if schema_row is None or schema_row[0] != "2.0":
        raise RuntimeError("Swing lifecycle migration requires schema_version 2.0")
    assignment_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'strategy_assignment'"
    ).fetchone()
    assignment_marker = conn.execute(
        "SELECT value FROM metadata WHERE key = 'strategy_assignment_schema_version'"
    ).fetchone()
    if assignment_table is None or assignment_marker is None or assignment_marker[0] != "1":
        raise RuntimeError(
            "Swing lifecycle migration requires strategy_assignment_schema_version 1"
        )
    tables = {
        row[0]
        for row in conn.execute(
            """SELECT name FROM sqlite_master WHERE type = 'table'
               AND name IN ('swing_campaign', 'swing_campaign_event')"""
        )
    }
    feature = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (FEATURE_VERSION_KEY,)
    ).fetchone()
    return {
        "campaign_table_exists": "swing_campaign" in tables,
        "event_table_exists": "swing_campaign_event" in tables,
        "feature_version": feature[0] if feature is not None else None,
        "target_feature_version": FEATURE_VERSION,
    }


def apply_migration(conn: sqlite3.Connection) -> dict:
    before = inspect_migration(conn)
    conn.execute("PRAGMA foreign_keys = ON")
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
    if not path.exists():
        raise FileNotFoundError(f"Trading DB not found: {path}")
    if write:
        conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    else:
        conn = sqlite3.connect(f"file:///{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Swing campaign lifecycle (dry-run by default)"
    )
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--write", action="store_true", help="apply the migration")
    args = parser.parse_args()
    conn = _connect(args.db_path, write=args.write)
    try:
        print(apply_migration(conn) if args.write else {"dry_run": True, **inspect_migration(conn)})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
