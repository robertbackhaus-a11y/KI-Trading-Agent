"""Preview or safely import incremental Parqet CSV transactions."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from parqet_import import DEFAULT_DB_PATH, ParqetImportError, apply_import_plan, build_import_plan


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"Trading DB not found: {path}")
    conn = sqlite3.connect(f"file:///{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _connect_write(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"Trading DB not found: {path}")
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _print_human(result: dict[str, Any], *, write: bool) -> None:
    if write:
        print("Parqet incremental import completed" if result["written"] else "No approved rows to import")
        print(f"Inserted: {len(result['inserted_transaction_ids'])}")
        if result.get("backup_path"):
            print(f"Backup: {result['backup_path']}")
        return
    counts = result["counts"]
    print(f"Preview: {result['source_file']} ({result['source_row_count']} rows)")
    for name in ("duplicate", "new", "new_historical", "conflict", "unknown_security", "invalid"):
        print(f"{name}: {counts[name]}")
    for row in result["rows"]:
        if row["classification"] != "DUPLICATE":
            print(f"row {row['source_row_number']}: {row['classification']} — {row['classification_reason']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="Parqet semicolon CSV export")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Trading SQLite DB path")
    parser.add_argument("--write", action="store_true", help="Apply the exact previewed plan; preview is default")
    parser.add_argument("--include-historical", action="store_true", help="Also import explicitly classified NEW_HISTORICAL rows (requires --write)")
    parser.add_argument("--json", action="store_true", help="Emit deterministic JSON")
    args = parser.parse_args(argv)
    if args.include_historical and not args.write:
        parser.error("--include-historical requires --write")
    try:
        if not args.write:
            conn = _connect_read_only(args.db)
            try:
                plan = build_import_plan(conn, args.csv)
                result = plan.primitive()
            finally:
                conn.close()
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                _print_human(result, write=False)
            return 0
        conn = _connect_write(args.db)
        try:
            plan = build_import_plan(conn, args.csv)
            result = apply_import_plan(conn, plan, expected_plan_token=plan.plan_token, include_historical=args.include_historical, create_backup=True)
        finally:
            conn.close()
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            _print_human(result, write=True)
        return 0
    except (OSError, sqlite3.Error, ParqetImportError) as exc:
        failure = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(failure, sort_keys=True))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
