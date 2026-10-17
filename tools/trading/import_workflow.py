# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""One flow for a transaction export (Parqet or the canonical CSV): analyse, compare with the database, show the difference, import, reconcile.

This module adds no import logic of its own.  Planning, security resolution, duplicate handling, master-data supplements, the single write transaction, the position
rebuild (``transaction_import._rebuild_position``), strategy / campaign handling and the backup are ``transaction_import``; this module

* runs the complete dry run (``check``) and the write (``run``) and returns ONE report with the same shape for both,
* condenses the plan into a readable ``diff`` (new securities, supplemented master data, conflicts, new / existing transactions, expected position changes),
* and finishes with a ``closing`` reconciliation of the file against the database (``reconcile_with_source``), run on the database after the write, or on the simulated end state
  of a dry run: security / transaction / position counts, portfolio value, the net shares of the file against the positions, rows of the file that are not in the database,
  unresolved rows, conflicts, a check that every position equals the rebuild of its transactions, ``integrity_check`` and ``foreign_key_check``.

Primary data are ``security`` and ``transactions`` (and the manually kept ``portfolio_capital_state``).  ``positions`` are derived from the transactions and are rebuilt for every
security the import touched; ``swing_campaign_event`` rows are derived through the reconciliation of the existing campaigns.  Prices (``market_snapshot``, ``market_data``) and
``fx_rates`` come from their own collectors and are not part of an import: a new held security is reported as ``MARKET_DATA_MISSING``.  Watchlist, strategy assignments and campaigns
are never changed because a position was imported, except by the existing, explicit import rules (``--strategy``, ``--campaign-opened-at``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

import transaction_import as ti

_EPS = 1e-6
LIST_LIMIT = 25  # entries of a list in the report; the counts are always complete


def _positions(conn: sqlite3.Connection, security_ids: Optional[Iterable[int]] = None) -> dict[int, dict[str, Any]]:
    rows = conn.execute("""SELECT p.security_id, s.symbol, s.name, p.shares, p.avg_cost, p.remaining_cost_basis, p.invested_amount, p.currency
                             FROM positions p JOIN security s ON s.id = p.security_id""").fetchall()
    wanted = None if security_ids is None else {int(i) for i in security_ids}
    return {int(r["security_id"]): {"security_id": int(r["security_id"]), "symbol": r["symbol"], "name": r["name"], "shares": float(r["shares"] or 0.0),
                                    "avg_cost": None if r["avg_cost"] is None else float(r["avg_cost"]), "remaining_cost_basis": float(r["remaining_cost_basis"] or 0.0),
                                    "invested_amount": float(r["invested_amount"] or 0.0), "currency": r["currency"]}
            for r in rows if wanted is None or int(r["security_id"]) in wanted}


def _label(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("name") or row.get("security_id"))


def position_changes(before: dict[int, dict[str, Any]], after: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Positions whose quantity or cost basis differ (new, changed, closed)."""
    out = []
    for security_id in sorted(set(before) | set(after)):
        b, a = before.get(security_id), after.get(security_id)
        shares_b, shares_a = (b or {}).get("shares", 0.0), (a or {}).get("shares", 0.0)
        basis_b, basis_a = (b or {}).get("remaining_cost_basis", 0.0), (a or {}).get("remaining_cost_basis", 0.0)
        if b is not None and a is not None and abs(shares_b - shares_a) < _EPS and abs(basis_b - basis_a) < 0.005:
            continue
        kind = "new" if shares_b < _EPS and shares_a >= _EPS else "closed" if shares_b >= _EPS and shares_a < _EPS else "changed"
        out.append({"security_id": security_id, "symbol": _label(a or b), "kind": kind, "shares_before": shares_b, "shares_after": shares_a,
                    "cost_basis_before": basis_b, "cost_basis_after": basis_a})
    return out


def _diff(plan: ti.ImportPlan, include_historical: bool, planned_or_result: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    records = plan.records
    approved = [r for r in records if r.classification == ti.CLASS_NEW or (include_historical and r.classification == ti.CLASS_NEW_HISTORICAL)]
    by_type: dict[str, int] = {}
    for record in approved:
        by_type[record.transaction_type or "?"] = by_type.get(record.transaction_type or "?", 0) + 1
    problems = [r for r in records if r.classification in (ti.CLASS_CONFLICT, ti.CLASS_UNKNOWN_SECURITY, ti.CLASS_INVALID)]
    skipped = [r for r in records if r.classification == ti.CLASS_NEW_HISTORICAL and not include_historical]
    created = planned_or_result.get("created_securities", [])
    return {
        "classification_counts": plan.counts,
        "new_securities": created,
        "masterdata_updates": list(plan.masterdata_updates),
        "masterdata_conflicts": list(plan.masterdata_conflicts),
        "identifier_aliases": list(plan.identifier_aliases),
        # what the caller can decide on the spot (the Import page): an identifier alias per ISIN conflict, a strategy per newly held position without one
        "alias_candidates": [{**c["alias_candidate"], "security_id": c["security_id"], "name": c.get("name"), "wkn": c.get("wkn"), "database_isin": c.get("database")}
                             for c in plan.masterdata_conflicts if c.get("alias_candidate")],
        "strategy_needed": [{"security_id": i["security_id"], "symbol": i.get("symbol"), "name": i.get("name"), "key": i.get("strategy_key"), "shares": i.get("shares")}
                            for i in planned_or_result.get("open_items", []) if i["code"] == ti.STRATEGY_ASSIGNMENT_REQUIRED and i.get("strategy_key")],
        "new_transactions": {"count": len(approved), "by_type": by_type},
        "existing_transactions": plan.counts[ti.CLASS_DUPLICATE.lower()],
        "historical_skipped": len(skipped),
        "problem_rows": [{"row": r.source_row_number, "classification": r.classification, "reason": r.classification_reason, "identifier": r.identifier, "holding": r.holding}
                         for r in problems[:LIST_LIMIT]],
        "problem_row_count": len(problems),
        "position_changes": changes,
        "open_items": planned_or_result.get("open_items", []),
    }


# ------------------------------------------------------------------------------------------------------------ closing reconciliation
def _rebuilt_positions(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """The positions the existing rebuild path produces from the transactions, calculated on an in-memory copy; the database is not touched."""
    copy = sqlite3.connect(":memory:", isolation_level=None)
    copy.row_factory = sqlite3.Row
    try:
        conn.backup(copy)
        ids = [int(r[0]) for r in copy.execute("SELECT DISTINCT security_id FROM transactions")]
        for security_id in ids:
            ti._rebuild_position(copy, security_id)
        return _positions(copy, ids)
    finally:
        copy.close()


def _portfolio_value(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        import analysis_engine

        context = analysis_engine.build_portfolio_context(connection=conn)
        return {"total_market_value_eur": None if context.total_market_value is None else float(context.total_market_value),
                "status": context.valuation_quality.status.value, "as_of": str(context.evaluation_as_of)[:10]}
    except Exception as exc:  # noqa: BLE001 - the value is informational; its absence is reported, not hidden
        return {"total_market_value_eur": None, "status": "unavailable", "detail": f"{type(exc).__name__}: {exc}"[:200]}


def reconcile_with_source(conn: sqlite3.Connection, csv_path: str | Path, adapter: ti.ImportAdapter | str, *, include_historical: bool = False) -> dict[str, Any]:
    """The file against the database as it is NOW.  Read-only."""
    adapter = ti.get_adapter(adapter) if isinstance(adapter, str) else adapter
    rows = adapter.parse(Path(csv_path))
    valid = [r for r in rows if r.error is None]
    unresolved: list[dict[str, Any]] = [{"row": r.row_number, "reason": r.error} for r in rows if r.error is not None]
    file_net: dict[int, float] = {}
    labels: dict[int, str] = {}
    file_prints: dict[int, set[str]] = {}
    missing: list[dict[str, Any]] = []
    actions: dict[int, list] = {}
    from corporate_actions import cumulative_split_factor, load_corporate_actions

    for row in valid:
        security_id, _, error = ti._resolve_security(conn, row.identifier, wkn=row.wkn, holding=row.name, symbol=row.symbol, isin=row.isin)
        if security_id is None:
            unresolved.append({"row": row.row_number, "reason": error or "unknown security", "identifier": row.identifier, "holding": row.name})
            continue
        labels.setdefault(security_id, row.name or row.identifier or str(security_id))
        digest = ti.economic_fingerprint(security_id=security_id, transaction_type=row.transaction_type, transaction_date=row.transaction_date, shares=float(row.shares or 0.0),
                                         price=float(row.price or 0.0), amount=float(row.amount or 0.0), fees=float(row.fees or 0.0), taxes=float(row.taxes or 0.0),
                                         currency=row.currency, broker=row.broker or "")
        file_prints.setdefault(security_id, set()).add(digest)
        if security_id not in actions:
            actions[security_id] = load_corporate_actions(conn, security_id)
        quantity = float(row.shares or 0.0) * cumulative_split_factor(actions[security_id], row.transaction_date, None)
        if row.transaction_type in ("BUY", "TRANSFERIN"):
            file_net[security_id] = file_net.get(security_id, 0.0) + quantity
        elif row.transaction_type in ("SELL", "TRANSFEROUT"):
            file_net[security_id] = file_net.get(security_id, 0.0) - quantity
        else:
            file_net.setdefault(security_id, 0.0)
        found = [t for t in ti._existing_for_security(conn, security_id) if ti.economic_fingerprint(
            security_id=security_id, transaction_type=t["transaction_type"], transaction_date=t["transaction_date"], shares=float(t["shares"] or 0), price=float(t["price"] or 0),
            amount=float(t["amount"] or 0), fees=float(t["fees"] or 0), taxes=float(t["taxes"] or 0), currency=t["currency"] or "", broker=t["broker"] or "") == digest]
        if not found:
            missing.append({"row": row.row_number, "security": labels[security_id], "date": row.transaction_date, "type": row.transaction_type, "shares": row.shares})

    positions = _positions(conn)
    differences = []
    for security_id in sorted(file_net):
        db_shares = positions.get(security_id, {}).get("shares", 0.0)
        # a security whose file rows are not all in the database (historical rows skipped, conflicts) cannot match; the missing rows say why
        if abs(file_net[security_id] - db_shares) > _EPS:
            item = {"security_id": security_id, "security": labels[security_id], "file_net_shares": file_net[security_id], "database_shares": db_shares,
                    "delta": db_shares - file_net[security_id]}
            if file_net[security_id] < -_EPS:
                item["note"] = "the file sells more shares than it buys; the position model never goes below 0 (the file itself is inconsistent or starts after the purchase)"
            differences.append(item)
    ids = sorted(file_prints)
    in_db_not_file = 0
    if ids:
        marks = ",".join("?" * len(ids))
        for t in conn.execute(f"""SELECT security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker FROM transactions
                                   WHERE external_id LIKE ? AND security_id IN ({marks})""", [adapter.external_prefix + "%", *ids]):
            digest = ti.economic_fingerprint(security_id=int(t["security_id"]), transaction_type=t["transaction_type"], transaction_date=t["transaction_date"], shares=float(t["shares"] or 0),
                                             price=float(t["price"] or 0), amount=float(t["amount"] or 0), fees=float(t["fees"] or 0), taxes=float(t["taxes"] or 0),
                                             currency=t["currency"] or "", broker=t["broker"] or "")
            if digest not in file_prints[int(t["security_id"])]:
                in_db_not_file += 1
    rebuilt = _rebuilt_positions(conn)
    drift = []
    for security_id, expected in rebuilt.items():
        actual = positions.get(security_id)
        if actual is None or abs(actual["shares"] - expected["shares"]) > _EPS or abs(actual["remaining_cost_basis"] - expected["remaining_cost_basis"]) > 0.01 \
                or abs(actual["invested_amount"] - expected["invested_amount"]) > 0.01:
            drift.append({"security_id": security_id, "symbol": _label(expected), "position_shares": None if actual is None else actual["shares"], "rebuilt_shares": expected["shares"]})
    integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    foreign_keys = [tuple(r) for r in conn.execute("PRAGMA foreign_key_check")]
    held = [p for p in positions.values() if p["shares"] > _EPS]
    clean = not (differences or missing or unresolved or drift or in_db_not_file) and integrity == ["ok"] and not foreign_keys
    return {
        "status": "MATCH" if clean else "DIFFERENCES",
        "counts": {"securities": int(conn.execute("SELECT COUNT(*) FROM security").fetchone()[0]), "transactions": int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]),
                   "positions_held": len(held), "file_rows": len(rows), "file_securities": len(file_prints)},
        "portfolio_value": _portfolio_value(conn),
        "file_vs_database": {"share_differences": differences[:LIST_LIMIT], "share_difference_count": len(differences),
                             "file_rows_not_in_database": missing[:LIST_LIMIT], "file_rows_not_in_database_count": len(missing),
                             "database_rows_not_in_file": in_db_not_file},
        "unresolved": unresolved[:LIST_LIMIT], "unresolved_count": len(unresolved),
        "positions_vs_transactions": {"drift": drift[:LIST_LIMIT], "drift_count": len(drift), "checked": len(rebuilt)},
        "integrity_check": integrity, "foreign_key_check": foreign_keys,
    }


# ------------------------------------------------------------------------------------------------------------ the two entry points
MAX_FILE_BYTES = 20 * 1024 * 1024


def compact(result: dict[str, Any]) -> dict[str, Any]:
    """The report for a client with a size limit (the MCP tools): the complete difference and reconciliation, but not the row-by-row plan (the rows with a problem are in ``diff``)."""
    planned = result.get("planned") or result
    return {
        "ok": True, "mode": result["mode"], "format": result.get("format"), "source_file": result.get("source_file"), "source_row_count": result.get("source_row_count"),
        "plan_token": result.get("plan_token"), "written": bool(result.get("written")), "would_write": bool(planned.get("would_write")), "backup_path": result.get("backup_path"),
        "summary": planned.get("summary"), "open_items": planned.get("open_items", []), "blocked_row_numbers": planned.get("blocked_row_numbers", []),
        "strategy_assignments": planned.get("strategy_assignments", []), "campaigns_created": planned.get("campaigns_created", []),
        "diff": result.get("diff"), "closing": result.get("closing"),
    }


def validate_source_file(file_path: Any) -> Path:
    """A client-supplied path must be an existing, reasonably small CSV file (nothing else is ever read by the import tools)."""
    path = Path(str(file_path))
    if path.suffix.lower() != ".csv":
        raise ti.TransactionImportError("only a .csv file can be imported")
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ti.TransactionImportError("the file is larger than 20 MB")
    return path

def _options(create_securities: bool, strategies: Iterable[str], campaign_opened_at: Iterable[str], accept_aliases: Iterable[str] = (), start_campaigns: bool = True,
             include_historical: bool = False) -> ti.ImportOptions:
    return ti.ImportOptions(create_securities=create_securities, strategies=ti.parse_key_value_options(strategies, kind="strategy"),
                            campaign_opened_at=ti.parse_key_value_options(campaign_opened_at, kind="date"), accept_aliases=ti.parse_key_value_options(accept_aliases, kind="alias"),
                            start_campaigns=bool(start_campaigns), include_historical=bool(include_historical))


def check(db_path: str | Path, csv_path: str | Path, *, fmt: str = "parqet", create_securities: bool = False, strategies: Iterable[str] = (),
          campaign_opened_at: Iterable[str] = (), include_historical: bool = False, accept_aliases: Iterable[str] = (), start_campaigns: bool = True) -> dict[str, Any]:
    """The complete dry run: the real database is opened read-only, the import is simulated on an in-memory copy.  Nothing is written."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Trading DB not found: {path}")
    conn = sqlite3.connect(f"file:///{path.resolve().as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        adapter = ti.get_adapter(fmt)
        options = _options(create_securities, strategies, campaign_opened_at, accept_aliases, start_campaigns, include_historical)
        before = _positions(conn)
        captured: dict[str, Any] = {}

        def inspect(copy: sqlite3.Connection, planned: dict[str, Any]) -> None:
            captured["after"] = _positions(copy)
            captured["closing"] = reconcile_with_source(copy, csv_path, adapter, include_historical=include_historical)

        result = ti.preview_import(conn, csv_path, adapter=adapter, options=options, include_historical=include_historical, inspect=inspect)
        plan = ti.build_import_plan(conn, csv_path, adapter=adapter, options=options)
        if plan.plan_token != result["plan_token"] or _positions(conn) != before:  # a writer changed the database while the check ran: the report would mix two states
            raise ti.TransactionImportError("the database changed during the check; check again")
        changes = position_changes(before, captured.get("after", before))
        # the closing reconciliation of a dry run describes the SIMULATED end state; a file row that stays out (historical, conflict) shows up as a difference
        return {**result, "mode": "check", "diff": _diff(plan, include_historical, result["planned"], changes), "closing": captured.get("closing")}
    finally:
        conn.close()


def run(db_path: str | Path, csv_path: str | Path, *, fmt: str = "parqet", create_securities: bool = False, strategies: Iterable[str] = (),
        campaign_opened_at: Iterable[str] = (), include_historical: bool = False, expected_plan_token: Optional[str] = None, accept_aliases: Iterable[str] = (), start_campaigns: bool = True) -> dict[str, Any]:
    """The write: backup, then everything in ONE transaction (securities, master data, transactions, positions, strategy / campaign rules); any error rolls everything back.
    ``expected_plan_token`` (from ``check``) makes the write fail when the file or the database changed since the check."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Trading DB not found: {path}")
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        adapter = ti.get_adapter(fmt)
        options = _options(create_securities, strategies, campaign_opened_at, accept_aliases, start_campaigns, include_historical)
        plan = ti.build_import_plan(conn, csv_path, adapter=adapter, options=options)
        if expected_plan_token is not None and expected_plan_token != plan.plan_token:
            raise ti.TransactionImportError("import plan is stale; check again before writing")
        before = _positions(conn)
        result = ti.apply_import_plan(conn, plan, expected_plan_token=plan.plan_token, include_historical=include_historical, create_backup=True)
        after = _positions(conn)
        closing = reconcile_with_source(conn, csv_path, adapter, include_historical=include_historical)
        return {**result, "mode": "write", "diff": _diff(plan, include_historical, result, position_changes(before, after)), "closing": closing}
    finally:
        conn.close()
