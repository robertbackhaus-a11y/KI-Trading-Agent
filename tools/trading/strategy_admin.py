# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Narrow strategy administration: set or remove the strategy of ONE security that is not held (the ``set_security_strategy`` MCP action).

The strategy of a security is its active row in ``strategy_assignment`` (dated intervals per security, never overlapping).  This module changes
nothing else: no watchlist row, position, campaign, promotion record or transaction is written, and no campaign is opened or closed.  Every
statement is parameterized and the change runs in one atomic transaction (``BEGIN IMMEDIATE`` .. ``COMMIT``; any error rolls back).

Allowed values are ``swing``, ``long_term`` and ``None`` (= remove the active assignment).  ``tactical`` and ``unknown`` exist in the schema but
are not used and are not offered.  The checks run in this order and a block writes nothing::

    1. the security exists   2. the strategy is valid   3. the security is NOT held (shares > 0)   4. there is NO open swing campaign
    5. the active strategy already equals the wanted one -> UNCHANGED   6. write

Why held securities and open campaigns are blocked: the strategy of a held security is coupled to the campaign lifecycle (a held ``swing`` position
without an open campaign makes the orchestrator report a blocking ``SWING_CAMPAIGN_UNAVAILABLE`` issue).  A change there belongs to the import /
campaign workflow, not to an isolated edit.

Writing: an open-ended assignment that started before today is closed on the day before today and the new one starts today (the history is kept).
An assignment that started today and is not referenced by a campaign is corrected in place (or deleted when the strategy is removed), because the
interval model cannot express "closed before it started".  Anything else that is not a plain history (several active rows, a future-dated or
finite row, a referenced row that would have to change in place) is reported as ``ASSIGNMENT_HISTORY_UNCLEAR`` and nothing is written.

Side effect worth knowing (documented, not hidden): a not held security with ``swing`` becomes an entry candidate of the planner (still simulation
only, no order, no campaign).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Iterator, Optional

OPERATION = "set_security_strategy"
STRATEGIES = ("swing", "long_term")  # the values that exist and may be set; None removes the assignment
SOURCE = "manual"  # the value of the existing manual assignments
RATIONALE = "set in the desktop GUI (Watchlist)"

# results
STRATEGY_SET = "STRATEGY_SET"
STRATEGY_REMOVED = "STRATEGY_REMOVED"
UNCHANGED = "UNCHANGED"
NOT_FOUND = "NOT_FOUND"
INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
INVALID_STRATEGY = "INVALID_STRATEGY"
BLOCKED_HELD_POSITION = "BLOCKED_HELD_POSITION"
BLOCKED_ACTIVE_CAMPAIGN = "BLOCKED_ACTIVE_CAMPAIGN"
ASSIGNMENT_HISTORY_UNCLEAR = "ASSIGNMENT_HISTORY_UNCLEAR"
ERROR = "ERROR"

MESSAGES = {
    STRATEGY_SET: "Strategie gespeichert.",
    STRATEGY_REMOVED: "Strategie entfernt.",
    UNCHANGED: "Die Strategie war bereits so gesetzt.",
    NOT_FOUND: "Wertpapier nicht gefunden.",
    INVALID_IDENTIFIER: "Ungültige security_id.",
    INVALID_STRATEGY: "Ungültige Strategie (erlaubt: swing, long_term oder keine).",
    BLOCKED_HELD_POSITION: "Strategie kann bei einer gehaltenen Position nicht geändert werden.",
    BLOCKED_ACTIVE_CAMPAIGN: "Strategie kann bei einer aktiven Campaign nicht geändert werden.",
    ASSIGNMENT_HISTORY_UNCLEAR: "Die Strategie-Historie dieses Wertpapiers ist nicht eindeutig; es wurde nichts geändert.",
}


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """One atomic write: ``BEGIN IMMEDIATE`` takes the write lock first, any exception rolls back."""
    if conn.in_transaction:
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _result(result: str, *, changed: bool = False, ok: bool = True, **fields: Any) -> dict[str, Any]:
    out = {"ok": ok, "operation": OPERATION, "result": result, "changed": changed, **fields}
    out.setdefault("message", MESSAGES.get(result, ""))
    return out


def _error(exc: BaseException) -> dict[str, Any]:
    return _result(ERROR, ok=False, error=type(exc).__name__, message=str(exc)[:200])


def check_strategy(strategy: Any) -> Optional[str]:
    """The wanted strategy: ``'swing'``, ``'long_term'`` or ``None`` (remove); raises ``ValueError`` for anything else."""
    if strategy is None:
        return None
    if isinstance(strategy, str):
        text = strategy.strip().lower()
        if text in STRATEGIES:
            return text
        if text in ("", "none", "null"):
            return None
    raise ValueError("strategy must be swing, long_term or null")


def _security(conn: sqlite3.Connection, security_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT id, symbol, name FROM security WHERE id = ?", (security_id,)).fetchone()
    return None if row is None else {"security_id": int(row[0]), "symbol": row[1], "name": row[2]}


def _held(conn: sqlite3.Connection, security_id: int) -> bool:
    return conn.execute("SELECT 1 FROM positions WHERE security_id = ? AND shares > 0 LIMIT 1", (security_id,)).fetchone() is not None


def _open_campaign(conn: sqlite3.Connection, security_id: int) -> bool:
    return conn.execute("SELECT 1 FROM swing_campaign WHERE security_id = ? AND status = 'open' LIMIT 1", (security_id,)).fetchone() is not None


def _assignments(conn: sqlite3.Connection, security_id: int) -> list[sqlite3.Row | tuple]:
    return conn.execute("SELECT id, strategy_type, effective_from, effective_to FROM strategy_assignment WHERE security_id = ? ORDER BY effective_from, id", (security_id,)).fetchall()


def _decide(conn: sqlite3.Connection, security_id: int, wanted: Optional[str], today: str) -> tuple[Optional[dict[str, Any]], Optional[tuple], Optional[str]]:
    """``(blocking result or None, active assignment row or None, current strategy)`` -- the checks 3 to 5, read-only."""
    info = _security(conn, security_id)
    if _held(conn, security_id):
        return _result(BLOCKED_HELD_POSITION, **info), None, None
    if _open_campaign(conn, security_id):
        return _result(BLOCKED_ACTIVE_CAMPAIGN, **info), None, None
    rows = _assignments(conn, security_id)
    active = [r for r in rows if r[2] <= today and (r[3] is None or r[3] >= today)]
    if len(active) > 1 or any(r[2] > today for r in rows):
        return _result(ASSIGNMENT_HISTORY_UNCLEAR, **info), None, None
    current = active[0] if active else None
    current_strategy = current[1] if current else None
    if current_strategy == wanted:
        return _result(UNCHANGED, strategy=wanted, previous_strategy=current_strategy, **info), None, None
    if current is not None and current[3] is not None:
        return _result(ASSIGNMENT_HISTORY_UNCLEAR, **info), None, None  # an assignment that ends in the future
    return None, current, current_strategy


def set_security_strategy(conn: sqlite3.Connection, security_id: Any, strategy: Any, *, today: Optional[str] = None) -> dict[str, Any]:
    """Set (``swing`` / ``long_term``) or remove (``None``) the strategy of one security that is not held.  Never raises for bad input."""
    try:
        if isinstance(security_id, bool) or not isinstance(security_id, (int, str)) or not str(security_id).strip().lstrip("-").isdigit():
            return _result(INVALID_IDENTIFIER)
        sid = int(str(security_id).strip())
        if conn.in_transaction:
            conn.rollback()
        if _security(conn, sid) is None:
            return _result(NOT_FOUND, security_id=sid)
        try:
            wanted = check_strategy(strategy)
        except ValueError:
            return _result(INVALID_STRATEGY, security_id=sid)
        day = today or date.today().isoformat()
        blocked, _active, _previous = _decide(conn, sid, wanted, day)  # a cheap check without the write lock first
        if blocked is not None:
            return blocked
        try:
            with _transaction(conn):
                blocked, active, previous = _decide(conn, sid, wanted, day)  # the same checks again under the write lock
                if blocked is not None:
                    return blocked
                info = _security(conn, sid)
                _write(conn, sid, active, wanted, day)
        except HistoryUnclear:
            return _result(ASSIGNMENT_HISTORY_UNCLEAR, security_id=sid)  # the transaction was rolled back
        return _result(STRATEGY_SET if wanted else STRATEGY_REMOVED, changed=True, strategy=wanted, previous_strategy=previous, effective_from=day if wanted else None, **info)
    except Exception as exc:  # noqa: BLE001 - every failure is reported, the transaction rolled back
        return _error(exc)


class HistoryUnclear(Exception):
    """The assignment cannot be changed without rewriting history."""


def _write(conn: sqlite3.Connection, security_id: int, active: Optional[tuple], wanted: Optional[str], today: str) -> None:
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    if active is not None and active[2] == today:  # started today: correct in place (or delete), unless a campaign refers to it
        if conn.execute("SELECT 1 FROM swing_campaign WHERE strategy_assignment_id = ? LIMIT 1", (active[0],)).fetchone() is not None:
            raise HistoryUnclear("the assignment of today is referenced by a campaign")
        if wanted is None:
            conn.execute("DELETE FROM strategy_assignment WHERE id = ?", (active[0],))
        else:
            conn.execute("UPDATE strategy_assignment SET strategy_type = ?, source = ?, rationale = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (wanted, SOURCE, RATIONALE, active[0]))
        return
    if active is not None:  # keep the history: close it yesterday, the new one starts today
        conn.execute("UPDATE strategy_assignment SET effective_to = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (yesterday, active[0]))
    if wanted is not None:
        conn.execute("INSERT INTO strategy_assignment (security_id, strategy_type, effective_from, effective_to, source, rationale) VALUES (?, ?, ?, NULL, ?, ?)",
                     (security_id, wanted, today, SOURCE, RATIONALE))
