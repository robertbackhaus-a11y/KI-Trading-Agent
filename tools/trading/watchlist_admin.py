# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Narrow watchlist administration: add, remove and re-prioritize one watchlist entry (the only writes of the desktop GUI to the database).

Each operation touches the ``watchlist`` table and nothing else: no position, strategy assignment, campaign, transaction or market-data
row is read for writing, and **no security is ever created** -- an identifier that is not in the Security Master is reported as
``SECURITY_NOT_FOUND`` (onboarding a new security is a separate workflow).  Every statement is parameterized and runs in one atomic
transaction (``BEGIN IMMEDIATE`` .. ``COMMIT``; any error rolls back).  No SQL text is ever accepted from a caller.

Identifier resolution (``resolve_identifier``) uses the existing alias index and is deterministic; the first step that finds anything decides,
several hits in that step are ``AMBIGUOUS_IDENTIFIER`` and nothing is guessed::

    1. security_id (digits)   2. ISIN   3. WKN   4. exact known alias / public symbol   5. controlled symbol stem ("SAP.DE" -> "SAP")
    6. exact full company name (case, punctuation and spacing aside; "Alpha" does not find "Alpha Corp")

A digit string that is both an existing ``security_id`` and a WKN of another security is ambiguous as well.  There is no fuzzy search.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import security_aliases as sa

# results of add_watchlist_candidate
ADDED = "ADDED"
WOULD_ADD = "WOULD_ADD"  # dry run only
ALREADY_EXISTS = "ALREADY_EXISTS"
SECURITY_NOT_FOUND = "SECURITY_NOT_FOUND"
AMBIGUOUS_IDENTIFIER = "AMBIGUOUS_IDENTIFIER"
INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
INVALID_PRIORITY = "INVALID_PRIORITY"
# results of remove_watchlist_candidate / set_watchlist_priority
REMOVED = "REMOVED"
NOT_FOUND = "NOT_FOUND"
PRIORITY_SET = "PRIORITY_SET"
ERROR = "ERROR"

DEFAULT_STATUS = "WATCH"  # the status every existing watchlist row has; the column default of the table
# ``watchlist.priority`` is a nullable INTEGER and the schema defines no range; a higher value ranks first (planner tie-break).  No business range
# is invented here: any whole number is accepted, or ``None`` to clear it.  The bounds below are only the storage limits of an SQLite INTEGER
# (a larger number could not be bound at all).
PRIORITY_MIN, PRIORITY_MAX = -(2**63), 2**63 - 1
MAX_IDENTIFIER_LENGTH = 80
MAX_CANDIDATES = 5

_ISIN = re.compile(r"^[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]$")
_WKN = re.compile(r"^[A-Za-z0-9]{6}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NOT_IN_MASTER = "Wertpapier ist noch nicht im Security Master."
_SEPARATE_WORKFLOW = "Die Aufnahme eines vollständig neuen Wertpapiers ist ein separater Workflow."


@dataclass(frozen=True)
class Resolution:
    outcome: str  # FOUND | NOT_FOUND | AMBIGUOUS | INVALID
    security_id: Optional[int] = None
    matched_by: Optional[str] = None
    candidates: tuple[int, ...] = ()


def _identifier_text(identifier: Any) -> Optional[str]:
    """The cleaned identifier, or ``None`` if it cannot be one (empty, too long, control characters, not text / a whole number)."""
    if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
        return None
    text = str(identifier).strip()
    if not text or len(text) > MAX_IDENTIFIER_LENGTH or _CONTROL.search(text):
        return None
    return text


def resolve_identifier(conn: sqlite3.Connection, identifier: Any) -> Resolution:
    """Resolve one identifier against the Security Master (read-only SELECTs)."""
    text = _identifier_text(identifier)
    if text is None:
        return Resolution("INVALID")
    index = sa.AliasIndex.from_connection(conn)
    steps: list[tuple[str, frozenset[int]]] = []
    if text.isdigit():
        steps.append((sa.SECURITY_ID, index.candidates(sa.SECURITY_ID, text)))
    if _ISIN.match(text):
        steps.append((sa.ISIN, index.candidates(sa.ISIN, text)))
    if _WKN.match(text):
        steps.append((sa.WKN, index.candidates(sa.WKN, text)))
    if text.isdigit() and len(steps) == 2:  # a security_id that is also a WKN: both readings count (never silently the first)
        merged = steps[0][1] | steps[1][1]
        steps = [(steps[0][0], merged)] if merged else []
    steps += [(sa.ALIAS, index.candidates(sa.ALIAS, text)), (sa.SYMBOL_STEM, index.candidates(sa.SYMBOL_STEM, text)), (sa.COMPANY_NAME, _exact_names(conn, index, text))]
    for kind, ids in steps:
        if len(ids) == 1:
            return Resolution("FOUND", next(iter(ids)), kind)
        if len(ids) > 1:
            return Resolution("AMBIGUOUS", None, kind, tuple(sorted(ids)))
    return Resolution("NOT_FOUND")


def _plain(text: Any) -> str:
    return re.sub(r"[^\w]+", " ", str(text or "").casefold()).strip()


def _exact_names(conn: sqlite3.Connection, index: sa.AliasIndex, text: str) -> frozenset[int]:
    """The company-name step: the alias index ignores legal-form words ("Alpha" would find "Alpha Corp"); a write needs the *full* name,
    equal apart from case, punctuation and spacing."""
    wanted = _plain(text)
    keep = set()
    for sid in index.candidates(sa.COMPANY_NAME, text):
        row = conn.execute("SELECT name FROM security WHERE id = ?", (sid,)).fetchone()
        if row is not None and _plain(row[0]) == wanted:
            keep.add(sid)
    return frozenset(keep)


def _security(conn: sqlite3.Connection, security_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT id, symbol, name, isin, active FROM security WHERE id = ?", (int(security_id),)).fetchone()
    return None if row is None else {"security_id": int(row[0]), "symbol": row[1], "name": row[2], "isin": row[3], "active": bool(row[4])}


def _watchlist_row(conn: sqlite3.Connection, security_id: int) -> Optional[sqlite3.Row | tuple]:
    return conn.execute("SELECT status, priority FROM watchlist WHERE security_id = ?", (int(security_id),)).fetchone()


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


def _result(operation: str, result: str, *, changed: bool = False, ok: bool = True, **fields: Any) -> dict[str, Any]:
    """``ok`` is the technical outcome (false only for ``ERROR``); ``result`` is the deterministic business outcome; ``changed`` says whether the database changed."""
    return {"ok": ok, "operation": operation, "result": result, "changed": changed, **fields}


def _error(operation: str, exc: BaseException) -> dict[str, Any]:
    return _result(operation, ERROR, ok=False, error=type(exc).__name__, message=str(exc)[:200])


def check_priority(priority: Any) -> Optional[int]:
    """The priority as an int, or ``None`` for 'no priority'; raises ``ValueError`` for anything else (bool, a fraction, text, NaN, beyond the SQLite INTEGER range)."""
    if priority is None:
        return None
    message = "priority must be a whole number (or null to clear it)"
    if isinstance(priority, bool) or not isinstance(priority, (int, float, str)):
        raise ValueError(message)
    if isinstance(priority, float):
        if not priority.is_integer():  # NaN and infinity are not integers either
            raise ValueError(message)
        number = int(priority)
    else:
        try:
            number = int(priority)
        except ValueError:
            raise ValueError(message) from None
    if not PRIORITY_MIN <= number <= PRIORITY_MAX:
        raise ValueError("priority is outside the range of the database column")
    return number


def add_watchlist_candidate(conn: sqlite3.Connection, identifier: Any, priority: Any = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Add one existing security to the watchlist (status ``WATCH``).  ``dry_run`` resolves only (``WOULD_ADD``) and writes nothing."""
    op = "add_watchlist_candidate"
    try:
        try:
            wanted = check_priority(priority)
        except ValueError as exc:
            return _result(op, INVALID_PRIORITY, message=str(exc))
        resolution = resolve_identifier(conn, identifier)
        if resolution.outcome == "INVALID":
            return _result(op, INVALID_IDENTIFIER, message="Der Identifier ist leer, zu lang oder enthält Steuerzeichen.")
        if resolution.outcome == "NOT_FOUND":
            return _result(op, SECURITY_NOT_FOUND, message=_NOT_IN_MASTER, hint=_SEPARATE_WORKFLOW)
        if resolution.outcome == "AMBIGUOUS":
            found = [s for s in (_security(conn, i) for i in resolution.candidates[:MAX_CANDIDATES]) if s]
            return _result(op, AMBIGUOUS_IDENTIFIER, matched_by=resolution.matched_by, candidates=found, message="Der Identifier passt auf mehrere Wertpapiere; bitte die security_id oder die ISIN verwenden.")
        security = _security(conn, resolution.security_id)
        if security is None or not security["active"]:  # an inactive security is not offered for the watchlist
            return _result(op, SECURITY_NOT_FOUND, message=_NOT_IN_MASTER if security is None else "Das Wertpapier ist im Security Master deaktiviert.", hint=_SEPARATE_WORKFLOW,
                           reason=None if security is None else "SECURITY_INACTIVE")
        info = {**security, "matched_by": resolution.matched_by}
        existing = _watchlist_row(conn, security["security_id"])
        if existing is not None:
            return _result(op, ALREADY_EXISTS, **info, status=existing[0], priority=existing[1], message="Das Wertpapier steht bereits auf der Watchlist; nichts wurde geändert.")
        if dry_run:
            return _result(op, WOULD_ADD, **info, priority=wanted, message="Das Wertpapier ist im Security Master und kann hinzugefügt werden.")
        with _transaction(conn):
            if _watchlist_row(conn, security["security_id"]) is not None:  # added by someone else since the check above
                return _result(op, ALREADY_EXISTS, **info, message="Das Wertpapier steht bereits auf der Watchlist; nichts wurde geändert.")
            conn.execute("INSERT INTO watchlist (security_id, status, priority) VALUES (?, ?, ?)", (security["security_id"], DEFAULT_STATUS, wanted))
        return _result(op, ADDED, changed=True, **info, status=DEFAULT_STATUS, priority=wanted, message="Zur Watchlist hinzugefügt.")
    except (sqlite3.Error, ValueError) as exc:
        return _error(op, exc)


def remove_watchlist_candidate(conn: sqlite3.Connection, security_id: Any) -> dict[str, Any]:
    """Remove the watchlist row of one security.  Nothing else changes: a held position, its strategy and its campaigns stay exactly as they are.

    The row's own free-text fields (entry reason, thesis, notes, target) go with it; ``had_notes`` tells whether there were any."""
    op = "remove_watchlist_candidate"
    try:
        sid = _whole_id(security_id)
        if sid is None:
            return _result(op, INVALID_IDENTIFIER, message="security_id muss eine positive ganze Zahl sein.")
        security = _security(conn, sid)
        if _watchlist_row(conn, sid) is None:
            return _result(op, NOT_FOUND, security_id=sid, message="Das Wertpapier steht nicht auf der Watchlist.")
        held = conn.execute("SELECT 1 FROM positions WHERE security_id = ? AND shares > 0", (sid,)).fetchone() is not None
        had_notes = conn.execute("SELECT 1 FROM watchlist WHERE security_id = ? AND (COALESCE(entry_reason, '') <> '' OR COALESCE(thesis, '') <> '' OR COALESCE(notes, '') <> '' OR target_entry IS NOT NULL)",
                                 (sid,)).fetchone() is not None
        with _transaction(conn):
            removed = conn.execute("DELETE FROM watchlist WHERE security_id = ?", (sid,)).rowcount
        if removed != 1:
            return _result(op, NOT_FOUND, security_id=sid, message="Das Wertpapier steht nicht auf der Watchlist.")
        return _result(op, REMOVED, changed=True, security_id=sid, symbol=(security or {}).get("symbol"), name=(security or {}).get("name"), still_held=held, had_notes=had_notes,
                       message="Von der Watchlist entfernt." + (" Die Position bleibt unverändert." if held else "") + (" Einstiegsgrund, These oder Notizen des Eintrags wurden mit entfernt." if had_notes else ""))
    except (sqlite3.Error, ValueError) as exc:
        return _error(op, exc)


def set_watchlist_priority(conn: sqlite3.Connection, security_id: Any, priority: Any) -> dict[str, Any]:
    """Set (or, with ``None``, clear) the priority of one watchlist entry.  Only ``priority`` and ``updated_at`` of that row change."""
    op = "set_watchlist_priority"
    try:
        sid = _whole_id(security_id)
        if sid is None:
            return _result(op, INVALID_IDENTIFIER, message="security_id muss eine positive ganze Zahl sein.")
        try:
            wanted = check_priority(priority)
        except ValueError as exc:
            return _result(op, INVALID_PRIORITY, security_id=sid, message=str(exc))
        row = _watchlist_row(conn, sid)
        if row is None:
            return _result(op, NOT_FOUND, security_id=sid, message="Das Wertpapier steht nicht auf der Watchlist.")
        if row[1] == wanted:
            return _result(op, PRIORITY_SET, security_id=sid, priority=wanted, previous_priority=row[1], message="Die Priorität war bereits so gesetzt; nichts wurde geändert.")
        with _transaction(conn):
            changed = conn.execute("UPDATE watchlist SET priority = ?, updated_at = CURRENT_TIMESTAMP WHERE security_id = ?", (wanted, sid)).rowcount
        if changed != 1:
            return _result(op, NOT_FOUND, security_id=sid, message="Das Wertpapier steht nicht auf der Watchlist.")
        return _result(op, PRIORITY_SET, changed=True, security_id=sid, priority=wanted, previous_priority=row[1], message="Priorität gesetzt.")
    except (sqlite3.Error, ValueError) as exc:
        return _error(op, exc)


def _whole_id(value: Any) -> Optional[int]:
    """A positive whole security_id from an int or a digit string; ``None`` for everything else (bool, float, text, zero, negative)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
        return int(value.strip())
    return None
