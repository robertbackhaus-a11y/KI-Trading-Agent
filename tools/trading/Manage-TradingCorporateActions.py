"""Controlled management CLI for auditable corporate actions (stock splits)."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Optional

from corporate_actions import (
    STOCK_SPLIT,
    CorporateActionError,
    corporate_action_schema_available,
    insert_stock_split,
    load_corporate_actions,
    validate_new_split,
)
from parqet_import import rebuild_position


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")


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


def _action_payload(action) -> dict:
    return {
        "id": action.id,
        "security_id": action.security_id,
        "action_type": action.action_type,
        "effective_date": action.effective_date,
        "ratio_numerator": action.ratio_numerator,
        "ratio_denominator": action.ratio_denominator,
        "ratio": action.ratio,
        "source": action.source,
        "source_reference": action.source_reference,
        "notes": action.notes,
    }


def list_corporate_actions(conn: sqlite3.Connection, *, security_id: Optional[int] = None) -> dict:
    if not corporate_action_schema_available(conn):
        return {
            "schema_available": False,
            "message": "corporate_action table/feature-marker not present; apply "
            "Migrate-TradingCorporateActions.py --write first",
            "actions": [],
        }
    if security_id is not None:
        actions = load_corporate_actions(conn, security_id)
    else:
        ids = [
            int(row["security_id"])
            for row in conn.execute("SELECT DISTINCT security_id FROM corporate_action ORDER BY security_id")
        ]
        actions = [action for sid in ids for action in load_corporate_actions(conn, sid)]
    return {"schema_available": True, "actions": [_action_payload(action) for action in actions]}


def validate_corporate_actions(conn: sqlite3.Connection) -> dict:
    if not corporate_action_schema_available(conn):
        return {"ok": False, "errors": ["corporate_action schema/feature-marker not present"]}
    errors: list[str] = []
    rows = conn.execute("SELECT DISTINCT security_id FROM corporate_action").fetchall()
    for row in rows:
        security_id = int(row["security_id"])
        actions = load_corporate_actions(conn, security_id)
        seen: set[tuple] = set()
        for action in actions:
            key = (action.action_type, action.effective_date, action.ratio_numerator, action.ratio_denominator)
            if key in seen:
                errors.append(f"security_id={security_id}: duplicate corporate action {key}")
            seen.add(key)
            if action.action_type not in (STOCK_SPLIT,):
                errors.append(f"security_id={security_id}: unsupported action_type {action.action_type!r}")
            if action.ratio_numerator <= 0 or action.ratio_denominator <= 0:
                errors.append(f"security_id={security_id}: non-positive ratio in action id={action.id}")
    return {"ok": not errors, "errors": errors}


def add_stock_split(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    effective_date: str,
    ratio_numerator: float,
    ratio_denominator: float,
    source: str,
    source_reference: Optional[str] = None,
    notes: Optional[str] = None,
    write: bool = False,
) -> dict:
    problems = validate_new_split(
        conn,
        security_id=security_id,
        effective_date=effective_date,
        ratio_numerator=ratio_numerator,
        ratio_denominator=ratio_denominator,
    )
    security = conn.execute("SELECT symbol, name FROM security WHERE id = ?", (security_id,)).fetchone()
    would_insert = {
        "security_id": security_id,
        "symbol": security["symbol"] if security else None,
        "name": security["name"] if security else None,
        "action_type": STOCK_SPLIT,
        "effective_date": effective_date,
        "ratio_numerator": ratio_numerator,
        "ratio_denominator": ratio_denominator,
        "source": source,
        "source_reference": source_reference,
        "notes": notes,
    }
    if problems:
        return {"dry_run": not write, "ok": False, "errors": problems, "would_insert": would_insert}
    if not write:
        return {"dry_run": True, "ok": True, "errors": [], "would_insert": would_insert}

    conn.execute("BEGIN IMMEDIATE")
    try:
        action_id = insert_stock_split(
            conn,
            security_id=security_id,
            effective_date=effective_date,
            ratio_numerator=ratio_numerator,
            ratio_denominator=ratio_denominator,
            source=source,
            source_reference=source_reference,
            notes=notes,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"dry_run": False, "ok": True, "errors": [], "inserted_id": action_id}


def _position_snapshot(conn: sqlite3.Connection, security_id: int) -> Optional[dict]:
    row = conn.execute(
        """SELECT security_id, shares, avg_cost, remaining_cost_basis, invested_amount,
                  realized_gain, currency, updated_at
           FROM positions WHERE security_id = ?""",
        (security_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def rebuild_position_command(conn: sqlite3.Connection, *, security_id: int, write: bool = False) -> dict:
    before = _position_snapshot(conn, security_id)
    if not write:
        return {"dry_run": True, "note": "rebuild is a write operation; pass --write to apply", "before": before}
    conn.execute("BEGIN IMMEDIATE")
    try:
        rebuild_position(conn, security_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    after = _position_snapshot(conn, security_id)
    return {"dry_run": False, "before": before, "after": after}


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage auditable corporate actions (stock splits)")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="override trading DB path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list corporate actions")
    list_parser.add_argument("--security-id", type=int)

    validate_parser = subparsers.add_parser("validate", help="validate stored corporate actions")

    add_split_parser = subparsers.add_parser("add-split", help="add one stock split (dry-run by default)")
    add_split_parser.add_argument("--security-id", type=int, required=True)
    add_split_parser.add_argument("--effective-date", required=True)
    add_split_parser.add_argument("--ratio-numerator", type=float, required=True)
    add_split_parser.add_argument("--ratio-denominator", type=float, required=True)
    add_split_parser.add_argument("--source", required=True)
    add_split_parser.add_argument("--source-reference")
    add_split_parser.add_argument("--notes")
    add_split_parser.add_argument("--write", action="store_true")

    rebuild_parser = subparsers.add_parser(
        "rebuild-position", help="rebuild one position via the split-aware canonical path"
    )
    rebuild_parser.add_argument("--security-id", type=int, required=True)
    rebuild_parser.add_argument("--write", action="store_true")

    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    write = bool(getattr(args, "write", False))
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(args.db, write=write)
        if args.command == "list":
            _print(list_corporate_actions(conn, security_id=args.security_id))
            return 0
        if args.command == "validate":
            result = validate_corporate_actions(conn)
            _print(result)
            return 0 if result["ok"] else 1
        if args.command == "add-split":
            result = add_stock_split(
                conn,
                security_id=args.security_id,
                effective_date=args.effective_date,
                ratio_numerator=args.ratio_numerator,
                ratio_denominator=args.ratio_denominator,
                source=args.source,
                source_reference=args.source_reference,
                notes=args.notes,
                write=write,
            )
            _print(result)
            return 0 if result["ok"] else 1
        if args.command == "rebuild-position":
            _print(rebuild_position_command(conn, security_id=args.security_id, write=write))
            return 0
        return 2
    except (CorporateActionError, FileNotFoundError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
