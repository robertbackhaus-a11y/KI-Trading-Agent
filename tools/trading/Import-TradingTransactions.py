# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Import transactions from a canonical Trading CSV or a source export (Parqet), including strategy and Swing-campaign initialization.

Without ``--write`` this is a complete dry run: the whole import is simulated on an in-memory copy of the database and the planned end
state is shown; the real database is only read.  ``--write`` makes a backup first and applies everything in one transaction.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import import_workflow
from transaction_import import (
    DEFAULT_DB_PATH,
    ImportOptions,
    TransactionImportError,
    apply_campaign_reconciliation,
    apply_import_plan,
    build_import_plan,
    get_adapter,
    parse_key_value_options,
    preview_import,
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


def _print_state(result: dict[str, Any], *, label: str) -> None:
    summary = result["summary"]
    print(f"{label}: {summary['validation_status']}")
    print("  " + ", ".join(f"{key}={summary[key]}" for key in (
        "records_total", "inserted", "duplicates", "historical_skipped", "historical_inserted", "conflicts", "failed")))
    print("  " + ", ".join(f"{key}={summary[key]}" for key in (
        "new_securities", "new_positions", "strategy_assignments_created", "strategy_required", "campaigns_created",
        "campaigns_reconciled", "campaign_initialization_required")))
    for security in result["created_securities"]:
        print(f"  new security: {security['symbol'] or security['isin']} ({security['name']})")
    for assignment in result["strategy_assignments"]:
        if assignment["action"] in {"created", "required", "conflict", "unclear"}:
            print(f"  strategy {assignment['action']}: {assignment['symbol']} {assignment.get('strategy') or assignment.get('requested') or ''} {assignment.get('effective_from') or ''}".rstrip())
    for campaign in result["campaigns_created"]:
        print(f"  campaign created: {campaign['symbol']} opened {campaign['opened_at']} quantity {campaign['original_quantity']:g} ({campaign['start_basis']})")
    for item in result["open_items"]:
        print(f"  OPEN {item['code']}: {item.get('symbol') or '-'} {item.get('detail') or ''}".rstrip())


def _print_workflow(result: dict[str, Any]) -> None:
    """The difference to the database before the import and the reconciliation of the file against the database after it (``import_workflow``)."""
    diff, closing = result.get("diff"), result.get("closing")
    if diff:
        print("Difference to the database:")
        print(f"  new securities: {len(diff['new_securities'])}, master data supplemented: {len(diff['masterdata_updates'])}, master data conflicts: {len(diff['masterdata_conflicts'])}")
        print(f"  new transactions: {diff['new_transactions']['count']} {diff['new_transactions']['by_type'] or ''}".rstrip() + f", already in the database: {diff['existing_transactions']}"
              f", historical skipped: {diff['historical_skipped']}, rows with a problem: {diff['problem_row_count']}")
        for item in diff.get("identifier_aliases", []):
            print(f"  identifier alias {item['identifier']} -> {item['symbol'] or item['security']}: {item['status']}" + (f" ({item['reason']})" if item["reason"] else ""))
        for item in diff["masterdata_updates"]:
            print(f"  master data: {item['symbol']} {item['field']} <- {item['to']}")
        for item in diff["masterdata_conflicts"]:
            print(f"  MASTER DATA CONFLICT: {item['symbol']} {item['field']}: database {item.get('database')} / file {item.get('file')} - {item['reason']}")
        for change in diff["position_changes"]:
            print(f"  position {change['kind']}: {change['symbol']} {change['shares_before']:g} -> {change['shares_after']:g}")
    if closing:
        counts, value, fvd = closing["counts"], closing["portfolio_value"], closing["file_vs_database"]
        print(f"Reconciliation file <-> database: {closing['status']}")
        print(f"  securities {counts['securities']}, transactions {counts['transactions']}, held positions {counts['positions_held']}, file rows {counts['file_rows']} ({counts['file_securities']} securities)")
        if value.get("total_market_value_eur") is not None:
            print(f"  portfolio value {value['total_market_value_eur']:.2f} EUR ({value['status']}, {value.get('as_of')})")
        print(f"  share differences {fvd['share_difference_count']}, file rows not in the database {fvd['file_rows_not_in_database_count']}, database rows not in the file "
              f"{fvd['database_rows_not_in_file']}, unresolved {closing['unresolved_count']}, positions differing from their transactions {closing['positions_vs_transactions']['drift_count']}")
        for item in fvd["share_differences"]:
            print(f"  DIFFERENCE {item['security']}: file {item['file_net_shares']:g}, database {item['database_shares']:g}" + (f" - {item['note']}" if item.get("note") else ""))
        print(f"  integrity_check {closing['integrity_check']}, foreign_key_check {closing['foreign_key_check'] or 'ok'}")


def _print_human(result: dict[str, Any], *, write: bool) -> None:
    if not write:
        counts = result["counts"]
        print(f"Preview ({result['format']}): {result['source_file']} ({result['source_row_count']} rows) - nothing is written")
        for name in ("duplicate", "new", "new_historical", "conflict", "unknown_security", "invalid"):
            print(f"{name}: {counts[name]}")
        for row in result["rows"]:
            if row["classification"] != "DUPLICATE":
                print(f"row {row['source_row_number']}: {row['classification']} - {row['classification_reason']}")
        _print_state(result["planned"], label="Planned end state")
        _print_campaigns(result["planned"].get("lifecycle", []), applied_label="would reconcile")
        _print_workflow(result)
        return
    print("Transaction import completed" if result["would_write"] else "No changes to write")
    print(f"Inserted: {len(result['inserted_transaction_ids'])}")
    if result.get("backup_path"):
        print(f"Backup: {result['backup_path']}")
    _print_state(result, label="End state")
    _print_campaigns(result.get("lifecycle", []), applied_label="reconciled")
    _print_workflow(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--format", choices=("canonical", "parqet"), help="source format of --csv")
    parser.add_argument("--csv", type=Path, help="CSV file to import")
    parser.add_argument("--reconcile-campaigns", action="store_true", help="Reconcile already-imported post-campaign trades with open Swing campaigns (no CSV; preview by default, --write applies)")
    parser.add_argument("--db-path", "--db", dest="db_path", type=Path, default=DEFAULT_DB_PATH, help="Trading SQLite DB path")
    parser.add_argument("--write", action="store_true", help="Apply the import (backup first); the default is a complete dry run")
    parser.add_argument("--include-historical", action="store_true", help="Also import rows that predate the latest persisted transaction")
    parser.add_argument("--create-securities", action="store_true", help="Create securities that are not in the database yet (needs name and isin or symbol in the file)")
    parser.add_argument("--strategy", action="append", default=[], metavar="SYMBOL=swing|long_term", help="Assign a strategy to a held security without an active assignment (repeatable)")
    parser.add_argument("--campaign-opened-at", action="append", default=[], metavar="SYMBOL=YYYY-MM-DD", help="Start date of the Swing campaign when it cannot be derived (repeatable)")
    parser.add_argument("--accept-identifier-alias", action="append", default=[], metavar="IDENTIFIER=SYMBOL", dest="accept_alias",
                        help="Declare that an identifier (ISIN) of the file IS an existing security (an ADR / GDR or another listing of the same instrument). Needs the same WKN in the file and in the "
                             "security master; stored as a source symbol, so later imports resolve it without a master data conflict (repeatable)")
    parser.add_argument("--no-campaign-start", action="store_true", help="Do not derive and start a Swing campaign automatically (an explicit --campaign-opened-at still starts one)")
    parser.add_argument("--json", action="store_true", help="Emit deterministic JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.reconcile_campaigns == (args.csv is not None):
        parser.error("use exactly one of --csv or --reconcile-campaigns")
    if args.reconcile_campaigns and (args.include_historical or args.create_securities or args.strategy or args.campaign_opened_at or args.accept_alias):
        parser.error("--reconcile-campaigns takes no import options")
    if args.csv is not None and args.format is None:
        parser.error("--format canonical|parqet is required with --csv")
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
        # One flow for both modes (import_workflow): check = complete dry run + difference + reconciliation of the simulated end state; write = backup, one transaction, then the
        # reconciliation of the file against the database.
        common = dict(fmt=args.format, create_securities=args.create_securities, strategies=args.strategy, campaign_opened_at=args.campaign_opened_at,
                      include_historical=args.include_historical, accept_aliases=args.accept_alias, start_campaigns=not args.no_campaign_start)
        if not args.write:
            result = import_workflow.check(args.db_path, args.csv, **common)
            print(json.dumps(result, sort_keys=True)) if args.json else _print_human(result, write=False)
            return 0
        result = import_workflow.run(args.db_path, args.csv, **common)
        print(json.dumps(result, sort_keys=True)) if args.json else _print_human(result, write=True)
        return 0
    except (OSError, sqlite3.Error, TransactionImportError) as exc:
        failure = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(failure, sort_keys=True))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
