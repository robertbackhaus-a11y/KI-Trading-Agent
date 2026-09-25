"""Controlled CLI for Phase-3A security-level strategy assignments.

Mutations are dry-runs unless ``--write`` is supplied. The CLI never creates
the schema itself: run the deliberate strategy-assignment migration first.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
FEATURE_VERSION_KEY = "strategy_assignment_schema_version"
FEATURE_VERSION = "1"
ALLOWED_STRATEGIES = {"long_term", "swing", "tactical", "unknown"}


class AssignmentValidationError(ValueError):
    """Raised when an assignment request is invalid or unsafe to apply."""


class AssignmentSchemaError(RuntimeError):
    """Raised when the explicit Phase-3A migration is not available."""


def _iso_date(value: str, field_name: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise AssignmentValidationError(f"{field_name} must be an ISO date (YYYY-MM-DD)") from exc


def _connect(db_path: Path, *, write: bool) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {db_path}")
    if write:
        conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
    else:
        conn = sqlite3.connect(f"file:///{db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'strategy_assignment'"
    ).fetchone() is not None


def _require_feature(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn):
        raise AssignmentSchemaError(
            "strategy_assignment table is missing; apply Migrate-TradingStrategyAssignments.py --write first"
        )
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (FEATURE_VERSION_KEY,)
    ).fetchone()
    if row is None or row["value"] != FEATURE_VERSION:
        raise AssignmentSchemaError(
            f"metadata.{FEATURE_VERSION_KEY} must equal {FEATURE_VERSION}"
        )


def _security_exists(conn: sqlite3.Connection, security_id: int) -> bool:
    return conn.execute("SELECT 1 FROM security WHERE id = ?", (security_id,)).fetchone() is not None


def _assignment_payload(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "security_id": row["security_id"],
        "symbol": row["symbol"] if "symbol" in row.keys() else None,
        "name": row["name"] if "name" in row.keys() else None,
        "strategy_type": row["strategy_type"],
        "effective_from": row["effective_from"],
        "effective_to": row["effective_to"],
        "source": row["source"],
        "rationale": row["rationale"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_assignments(
    conn: sqlite3.Connection,
    *,
    security_id: Optional[int] = None,
    strategy: Optional[str] = None,
    active_only: bool = False,
) -> list[dict]:
    _require_feature(conn)
    if strategy is not None and strategy not in ALLOWED_STRATEGIES:
        raise AssignmentValidationError(f"invalid strategy: {strategy}")
    query = """
        SELECT sa.id, sa.security_id, s.symbol, s.name, sa.strategy_type,
               sa.effective_from, sa.effective_to, sa.source, sa.rationale,
               sa.created_at, sa.updated_at
        FROM strategy_assignment sa
        JOIN security s ON s.id = sa.security_id
        WHERE 1 = 1
    """
    params: list[object] = []
    if security_id is not None:
        query += " AND sa.security_id = ?"
        params.append(security_id)
    if strategy is not None:
        query += " AND sa.strategy_type = ?"
        params.append(strategy)
    if active_only:
        query += " AND sa.effective_from <= ? AND (sa.effective_to IS NULL OR sa.effective_to >= ?)"
        today = date.today().isoformat()
        params.extend((today, today))
    query += " ORDER BY sa.security_id, sa.effective_from, sa.id"
    return [_assignment_payload(row) for row in conn.execute(query, params).fetchall()]


def _validate_new_assignment(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    strategy: str,
    effective_from: str,
    effective_to: Optional[str],
) -> tuple[str, Optional[str]]:
    _require_feature(conn)
    if not _security_exists(conn, security_id):
        raise AssignmentValidationError(f"security_id {security_id} does not exist")
    if strategy not in ALLOWED_STRATEGIES:
        raise AssignmentValidationError(f"invalid strategy: {strategy}")
    start = _iso_date(effective_from, "effective_from")
    end = _iso_date(effective_to, "effective_to") if effective_to else None
    if end is not None and end < start:
        raise AssignmentValidationError("effective_to must be on or after effective_from")
    overlap = conn.execute(
        """
        SELECT id, effective_from, effective_to
        FROM strategy_assignment
        WHERE security_id = ?
          AND COALESCE(effective_to, '9999-12-31') >= ?
          AND COALESCE(?, '9999-12-31') >= effective_from
        ORDER BY effective_from, id
        """,
        (security_id, start, end),
    ).fetchone()
    if overlap is not None:
        raise AssignmentValidationError(
            "assignment overlaps existing id="
            f"{overlap['id']} ({overlap['effective_from']} to {overlap['effective_to'] or 'open'})"
        )
    return start, end


def set_assignment(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    strategy: str,
    effective_from: str,
    effective_to: Optional[str] = None,
    source: Optional[str] = None,
    rationale: Optional[str] = None,
    write: bool = False,
) -> dict:
    start, end = _validate_new_assignment(
        conn,
        security_id=security_id,
        strategy=strategy,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    payload = {
        "security_id": security_id,
        "strategy_type": strategy,
        "effective_from": start,
        "effective_to": end,
        "source": source,
        "rationale": rationale,
    }
    if not write:
        return {"dry_run": True, "would_insert": payload}

    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """
            INSERT INTO strategy_assignment(
                security_id, strategy_type, effective_from, effective_to, source, rationale
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (security_id, strategy, start, end, source, rationale),
        )
        row = conn.execute(
            """
            SELECT sa.*, s.symbol, s.name
            FROM strategy_assignment sa
            JOIN security s ON s.id = sa.security_id
            WHERE sa.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"dry_run": False, "inserted": _assignment_payload(row)}


def close_assignment(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    effective_to: str,
    write: bool = False,
) -> dict:
    _require_feature(conn)
    if not _security_exists(conn, security_id):
        raise AssignmentValidationError(f"security_id {security_id} does not exist")
    rows = conn.execute(
        """
        SELECT *
        FROM strategy_assignment
        WHERE security_id = ? AND effective_to IS NULL
        ORDER BY effective_from, id
        """,
        (security_id,),
    ).fetchall()
    if not rows:
        raise AssignmentValidationError(f"security_id {security_id} has no open assignment")
    if len(rows) != 1:
        raise AssignmentValidationError(f"security_id {security_id} has multiple open assignments")
    row = rows[0]
    start = _iso_date(row["effective_from"], "existing effective_from")
    end = _iso_date(effective_to, "effective_to")
    if end < start:
        raise AssignmentValidationError("effective_to must be on or after effective_from")
    payload = _assignment_payload(row)
    payload["effective_to"] = end
    if not write:
        return {"dry_run": True, "would_close": payload}

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE strategy_assignment
            SET effective_to = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (end, row["id"]),
        )
        updated = conn.execute(
            """
            SELECT sa.*, s.symbol, s.name
            FROM strategy_assignment sa
            JOIN security s ON s.id = sa.security_id
            WHERE sa.id = ?
            """,
            (row["id"],),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"dry_run": False, "closed": _assignment_payload(updated)}


def validate_assignments(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    try:
        _require_feature(conn)
    except AssignmentSchemaError as exc:
        return [str(exc)]

    invalid_security = conn.execute(
        """
        SELECT sa.id, sa.security_id
        FROM strategy_assignment sa
        LEFT JOIN security s ON s.id = sa.security_id
        WHERE s.id IS NULL
        """
    ).fetchall()
    errors.extend(f"assignment id={row['id']} references missing security_id={row['security_id']}" for row in invalid_security)

    rows = conn.execute(
        "SELECT id, security_id, strategy_type, effective_from, effective_to FROM strategy_assignment"
    ).fetchall()
    for row in rows:
        if row["strategy_type"] not in ALLOWED_STRATEGIES:
            errors.append(f"assignment id={row['id']} has invalid strategy_type={row['strategy_type']}")
        try:
            start = _iso_date(row["effective_from"], "effective_from")
            end = _iso_date(row["effective_to"], "effective_to") if row["effective_to"] else None
            if end is not None and end < start:
                errors.append(f"assignment id={row['id']} has effective_to before effective_from")
        except AssignmentValidationError as exc:
            errors.append(f"assignment id={row['id']}: {exc}")

    overlaps = conn.execute(
        """
        SELECT a.security_id, a.id AS left_id, b.id AS right_id
        FROM strategy_assignment a
        JOIN strategy_assignment b
          ON a.security_id = b.security_id
         AND a.id < b.id
         AND COALESCE(a.effective_to, '9999-12-31') >= b.effective_from
         AND COALESCE(b.effective_to, '9999-12-31') >= a.effective_from
        """
    ).fetchall()
    errors.extend(
        f"security_id={row['security_id']} has overlapping assignments {row['left_id']} and {row['right_id']}"
        for row in overlaps
    )
    duplicate_active = conn.execute(
        """
        SELECT security_id, COUNT(*) AS count
        FROM strategy_assignment
        WHERE effective_to IS NULL
        GROUP BY security_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    errors.extend(
        f"security_id={row['security_id']} has {row['count']} open assignments"
        for row in duplicate_active
    )
    return errors


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage trading strategy assignments")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="override trading DB path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list assignments")
    list_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    list_parser.add_argument("--security-id", type=int)
    list_parser.add_argument("--strategy", choices=sorted(ALLOWED_STRATEGIES))
    list_parser.add_argument("--active-only", action="store_true")

    set_parser = subparsers.add_parser("set", help="create one assignment (dry-run by default)")
    set_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    set_parser.add_argument("--security-id", required=True, type=int)
    set_parser.add_argument("--strategy", required=True, choices=sorted(ALLOWED_STRATEGIES))
    set_parser.add_argument("--effective-from", required=True)
    set_parser.add_argument("--effective-to")
    set_parser.add_argument("--source")
    set_parser.add_argument("--rationale")
    set_parser.add_argument("--write", action="store_true")

    close_parser = subparsers.add_parser("close", help="close one open assignment (dry-run by default)")
    close_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    close_parser.add_argument("--security-id", required=True, type=int)
    close_parser.add_argument("--effective-to", required=True)
    close_parser.add_argument("--write", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="validate assignment integrity")
    validate_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    write = bool(getattr(args, "write", False))
    db_path = getattr(args, "command_db", None) or args.db
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(db_path, write=write)
        if args.command == "list":
            _print(list_assignments(conn, security_id=args.security_id, strategy=args.strategy, active_only=args.active_only))
            return 0
        if args.command == "set":
            _print(set_assignment(conn, security_id=args.security_id, strategy=args.strategy, effective_from=args.effective_from, effective_to=args.effective_to, source=args.source, rationale=args.rationale, write=write))
            return 0
        if args.command == "close":
            _print(close_assignment(conn, security_id=args.security_id, effective_to=args.effective_to, write=write))
            return 0
        errors = validate_assignments(conn)
        _print({"ok": not errors, "errors": errors})
        return 0 if not errors else 1
    except (AssignmentSchemaError, AssignmentValidationError, FileNotFoundError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
