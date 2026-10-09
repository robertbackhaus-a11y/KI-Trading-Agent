"""Preview or safely import incremental Parqet CSV transactions."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from parqet_import import (
    DEFAULT_DB_PATH,
    ParqetImportError,
    apply_campaign_reconciliation,
    apply_import_plan,
    build_import_plan,
    reconcile_campaign_transactions,
)


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


def _print_campaigns(campaigns: list[dict[str, Any]], *, applied_label: str) -> None:
    for item in campaigns:
        print(f"campaign {item.get('campaign_id')} (security {item['security_id']}): {item.get('status', 'n/a')}")
        for done in item.get("reconciled_transactions", []):
            print(f"  {applied_label}: transaction {done['transaction_id']} -> {done['event_type']} {done['quantity']:g}")
        print(f"  skipped_pre_campaign={item.get('skipped_pre_campaign', 0)} skipped_already_processed={item.get('skipped_already_processed', 0)}")
        for reason in item.get("ambiguous", []):
            print(f"  ambiguous: {reason}")
        for review in item.get("manual_review_required", []):
            print(f"  manual_review_required: transaction {review['transaction_id']}: {review['reason']}")
        print(f"  position={item.get('position_quantity')} campaign_expected={item.get('campaign_expected_quantity')} delta_after_reconciliation={item.get('delta_after_reconciliation')}")
        for flag in item.get("flags", []):
            print(f"  flag: {flag}")


def _print_human(result: dict[str, Any], *, write: bool) -> None:
    if write:
        print("Parqet incremental import completed" if result["written"] else "No approved rows to import")
        print(f"Inserted: {len(result['inserted_transaction_ids'])}")
        if result.get("backup_path"):
            print(f"Backup: {result['backup_path']}")
        _print_campaigns(result.get("lifecycle", []), applied_label="reconciled")
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
    parser.add_argument("--csv", type=Path, help="Parqet semicolon CSV export")
    parser.add_argument("--reconcile-campaigns", action="store_true", help="Reconcile already-imported post-campaign trades with open Swing campaigns (no CSV; preview by default, --write applies)")
    parser.add_argument("--db-path", "--db", dest="db_path", type=Path, default=DEFAULT_DB_PATH, help="Trading SQLite DB path")
    parser.add_argument("--write", action="store_true", help="Apply the exact previewed plan; preview is default")
    parser.add_argument("--include-historical", action="store_true", help="Also import explicitly classified NEW_HISTORICAL rows (requires --write)")
    parser.add_argument("--json", action="store_true", help="Emit deterministic JSON")
    args = parser.parse_args(argv)
    if args.include_historical and not args.write:
        parser.error("--include-historical requires --write")
    if args.reconcile_campaigns == (args.csv is not None):
        parser.error("use exactly one of --csv or --reconcile-campaigns")
    if args.reconcile_campaigns and args.include_historical:
        parser.error("--include-historical applies to --csv imports only")
    try:
        if args.reconcile_campaigns:
            if not args.write:
                conn = _connect_read_only(args.db_path)
                try:
                    result = {"written": False, "dry_run": True, "campaigns": reconcile_campaign_transactions(conn, write=False)}
                finally:
                    conn.close()
            else:
                conn = _connect_write(args.db_path)
                try:
                    result = apply_campaign_reconciliation(conn, create_backup=True)
                finally:
                    conn.close()
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                print("Campaign reconciliation " + ("applied" if result["written"] else "preview (nothing written)"))
                if result.get("backup_path"):
                    print(f"Backup: {result['backup_path']}")
                _print_campaigns(result["campaigns"], applied_label="reconciled" if result["written"] else "would reconcile")
            return 0
        if not args.write:
            conn = _connect_read_only(args.db_path)
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
        conn = _connect_write(args.db_path)
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
