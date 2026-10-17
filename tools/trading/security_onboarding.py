# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Controlled onboarding of ONE new security into the Security Master (the ``onboard_security`` MCP action).

A security is created only from values the caller supplies explicitly: nothing is looked up on the web, nothing is guessed (no ISIN, WKN,
exchange suffix, Yahoo symbol, sector or CIK), and nothing is defaulted that would invent a business fact.  The workflow has two phases:

* ``dry_run=True`` normalizes and validates the input, checks it against the Security Master and returns ``READY`` or the conflicts.  It writes nothing.
* ``dry_run=False`` repeats the same checks inside the write lock and then, in ONE transaction (``BEGIN IMMEDIATE`` .. ``COMMIT``), creates the
  ``security`` row, the optional provider symbols (``source_symbols``) and the optional ``watchlist`` row.  Any error rolls everything back.

Scope: CREATE only.  No position, strategy assignment, campaign, transaction, market-data, fundamentals or news row is written, and no sector is
assigned (the sector stays unassigned; the risk guards then treat it as "not assessable").  The ``security_id`` is the ``AUTOINCREMENT`` id of the
table.  The model has no alias table: the aliases of a security are its public symbol, its ISIN/WKN and its ``source_symbols`` rows, so provider
symbols are stored there (Yahoo Finance and FMP only; SEC EDGAR stores CIKs, which are never guessed).

Duplicates are found with the existing deterministic alias index (``security_aliases``): the ISIN (unique in the database), the public symbol
(exact alias or controlled symbol stem), the WKN and the provider symbols.  All conflicts are reported, not just the first one.  An exact name is
only a hint (``NAME_MATCH``).  An existing security is never changed (editing is a separate workflow).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional

import security_aliases as sa
import watchlist_admin as wa

OPERATION = "onboard_security"

# results
READY = "READY"  # dry run: nothing in the way, nothing written
CREATED = "CREATED"
ALREADY_EXISTS = "ALREADY_EXISTS"
ISIN_CONFLICT = "ISIN_CONFLICT"
SYMBOL_CONFLICT = "SYMBOL_CONFLICT"
WKN_CONFLICT = "WKN_CONFLICT"
ALIAS_CONFLICT = "ALIAS_CONFLICT"
INVALID_INPUT = "INVALID_INPUT"
ERROR = "ERROR"
# the most specific result wins when several conflicts exist (all of them are listed in ``conflicts``)
RESULT_ORDER = (INVALID_INPUT, ALREADY_EXISTS, ISIN_CONFLICT, SYMBOL_CONFLICT, WKN_CONFLICT, ALIAS_CONFLICT)

ASSET_TYPES = ("stock", "etf")  # the values present in the Security Master; the schema default is never applied silently
# provider key (any case) -> data_sources.name.  SEC EDGAR holds CIKs (never guessed) and is not offered.
PROVIDERS = {"yahoo": "Yahoo Finance", "yahoo finance": "Yahoo Finance", "fmp": "FMP"}
DEFAULT_WATCH_STATUS = wa.DEFAULT_STATUS

MAX_NAME = 200
MAX_EXCHANGE = 40

_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
_PROVIDER_SYMBOL = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,23}$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_WKN = re.compile(r"^[A-Z0-9]{6}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Rollback(Exception):
    """Raised inside the write transaction to undo everything and report ``ERROR``."""


# --------------------------------------------------------------------------- input
def isin_checksum_ok(isin: str) -> bool:
    """ISO 6166 check digit (Luhn over the digits obtained by expanding letters to 10..35)."""
    digits = "".join(str(int(ch, 36)) for ch in isin)
    total = 0
    for position, ch in enumerate(reversed(digits)):
        n = int(ch)
        if position % 2 == 1:
            n *= 2
            n = n - 9 if n > 9 else n
        total += n
    return total % 10 == 0


def _text(value: Any) -> Optional[str]:
    """A trimmed string, ``None`` for an empty / missing value; raises ``TypeError`` for a non-string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected text")
    text = value.strip()
    return text or None


def normalize(
    symbol: Any, name: Any, asset_type: Any, isin: Any = None, wkn: Any = None, exchange: Any = None, currency: Any = None, country: Any = None,
    provider_symbols: Any = None, add_to_watchlist: Any = False, watchlist_priority: Any = None, symbol_required: bool = True,
    isin_checksum: bool = True,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """``(clean values, errors)``.  Errors are ``{"field", "reason"}``; every field is checked, not just the first failing one.

    ``isin_checksum=False`` keeps the format check only (the ISINs of a broker export are not retyped by hand and synthetic test files exist).
    ``symbol_required=False`` is for the transaction import: a source such as Parqet identifies a holding by ISIN, WKN and name and has no ticker, and a ticker is never guessed."""
    errors: list[dict[str, str]] = []

    def fail(field: str, reason: str) -> None:
        errors.append({"field": field, "reason": reason})

    def text(field: str, value: Any, *, required: bool = False, upper: bool = False, pattern: Optional[re.Pattern] = None, limit: Optional[int] = None, hint: str = "") -> Optional[str]:
        try:
            out = _text(value)
        except TypeError:
            fail(field, "TEXT_EXPECTED")
            return None
        if out is None:
            if required:
                fail(field, "REQUIRED")
            return None
        if _CONTROL.search(out):
            fail(field, "CONTROL_CHARACTERS")
            return None
        if upper:
            out = out.upper()
        if limit is not None and len(out) > limit:
            fail(field, "TOO_LONG")
            return None
        if pattern is not None and not pattern.match(out):
            fail(field, hint or "BAD_FORMAT")
            return None
        return out

    clean: dict[str, Any] = {
        "symbol": text("symbol", symbol, required=symbol_required, upper=True, pattern=_SYMBOL, hint="BAD_SYMBOL"),
        "name": text("name", name, required=True, limit=MAX_NAME),
        "isin": text("isin", isin, upper=True, pattern=_ISIN, hint="BAD_ISIN"),
        "wkn": text("wkn", wkn, upper=True, pattern=_WKN, hint="BAD_WKN"),
        "exchange": text("exchange", exchange, limit=MAX_EXCHANGE),
        "currency": text("currency", currency, upper=True, pattern=_CURRENCY, hint="BAD_CURRENCY"),
        "country": text("country", country, upper=True, pattern=_COUNTRY, hint="BAD_COUNTRY"),
    }
    if isin_checksum and clean["isin"] and not isin_checksum_ok(clean["isin"]):
        fail("isin", "BAD_ISIN_CHECKSUM")
        clean["isin"] = None
    kind = text("asset_type", asset_type, required=True)
    if kind is not None:
        kind = kind.lower()
        if kind not in ASSET_TYPES:
            fail("asset_type", "UNKNOWN_ASSET_TYPE")
            kind = None
    clean["asset_type"] = kind

    providers: dict[str, str] = {}
    if provider_symbols not in (None, {}):
        if not isinstance(provider_symbols, Mapping):
            fail("provider_symbols", "MAPPING_EXPECTED")
        else:
            for key, value in provider_symbols.items():
                source = PROVIDERS.get(str(key).strip().lower())
                if source is None:
                    fail("provider_symbols", "UNKNOWN_PROVIDER")
                    continue
                if source in providers:
                    fail("provider_symbols", "DUPLICATE_PROVIDER")
                    continue
                symbol_text = text(f"provider_symbols.{source}", value, upper=True, pattern=_PROVIDER_SYMBOL, hint="BAD_PROVIDER_SYMBOL")
                if symbol_text is not None:
                    providers[source] = symbol_text
    clean["provider_symbols"] = providers

    if not isinstance(add_to_watchlist, bool):
        fail("add_to_watchlist", "BOOLEAN_EXPECTED")
        add_to_watchlist = False
    clean["add_to_watchlist"] = add_to_watchlist
    priority = None
    try:
        priority = wa.check_priority(watchlist_priority)
    except ValueError:
        fail("watchlist_priority", "BAD_PRIORITY")
    if priority is not None and not add_to_watchlist:
        fail("watchlist_priority", "PRIORITY_WITHOUT_WATCHLIST")
    clean["watchlist_priority"] = priority
    return clean, errors


# --------------------------------------------------------------------------- checks
def _plain(text: Any) -> str:
    return re.sub(r"[^\w]+", " ", str(text or "").casefold()).strip()


def _security(conn: sqlite3.Connection, security_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT id, symbol, name, isin, wkn, active FROM security WHERE id = ?", (int(security_id),)).fetchone()
    if row is None:
        return {"security_id": int(security_id)}
    return {"security_id": int(row[0]), "symbol": row[1], "name": row[2], "isin": row[3], "wkn": row[4], "active": bool(row[5])}


def _conflict(code: str, conn: sqlite3.Connection, security_id: int, field: str, value: str, match: str = "EXACT") -> dict[str, Any]:
    return {"code": code, "field": field, "value": value, "match": match, **_security(conn, security_id)}


def find_conflicts(conn: sqlite3.Connection, clean: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(conflicts, warnings)`` of validated values against the Security Master.  Read-only; every conflict is returned."""
    index = sa.AliasIndex.from_connection(conn)
    conflicts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    same: set[int] = set()  # securities that ARE this security (ISIN equal, or symbol and full name equal)

    if clean.get("isin"):
        for (sid,) in conn.execute("SELECT id FROM security WHERE UPPER(isin) = ?", (clean["isin"],)).fetchall():
            other = _security(conn, sid)
            if sa.symbol_key(other.get("symbol")) == sa.symbol_key(clean["symbol"]) or _plain(other.get("name")) == _plain(clean["name"]):
                same.add(int(sid))  # the ISIN identifies this very security
                conflicts.append(_conflict(ALREADY_EXISTS, conn, sid, "isin", clean["isin"]))
            else:
                conflicts.append(_conflict(ISIN_CONFLICT, conn, sid, "isin", clean["isin"]))  # the ISIN belongs to a different security: a typo or a different instrument
    symbol = clean["symbol"]
    exact = index.candidates(sa.ALIAS, symbol)
    stem = index.candidates(sa.SYMBOL_STEM, symbol) - exact
    for sid in sorted(exact):
        if sid in same or any(c["security_id"] == sid and c["code"] == ISIN_CONFLICT for c in conflicts):
            continue
        if _plain(_security(conn, sid).get("name")) == _plain(clean["name"]):
            same.add(sid)
            conflicts.append(_conflict(ALREADY_EXISTS, conn, sid, "symbol", symbol))
        else:
            conflicts.append(_conflict(SYMBOL_CONFLICT, conn, sid, "symbol", symbol))
    for sid in sorted(stem - same):
        conflicts.append(_conflict(SYMBOL_CONFLICT, conn, sid, "symbol", symbol, match="STEM"))  # "SAP" next to "SAP.DE": the same stem is not a new listing
    if clean.get("wkn"):
        for sid in sorted(index.candidates(sa.WKN, clean["wkn"]) - same):
            conflicts.append(_conflict(WKN_CONFLICT, conn, sid, "wkn", clean["wkn"]))
    for source, provider_symbol in sorted(clean.get("provider_symbols", {}).items()):
        for sid in sorted(index.candidates(sa.ALIAS, provider_symbol) - same):
            conflicts.append(_conflict(ALIAS_CONFLICT, conn, sid, f"provider_symbols.{source}", provider_symbol))
    for sid in sorted(index.candidates(sa.COMPANY_NAME, clean["name"]) - same):
        if _plain(_security(conn, sid).get("name")) == _plain(clean["name"]):
            warnings.append({"code": "NAME_MATCH", "field": "name", "value": clean["name"], **_security(conn, sid)})  # a hint only: identity is technical
    for conflict in conflicts:
        if conflict["code"] == ALREADY_EXISTS and conflict.get("active") is False:
            warnings.append({"code": "EXISTING_SECURITY_INACTIVE", **{k: conflict[k] for k in ("security_id", "symbol", "name") if k in conflict}})
    return conflicts, warnings


def _verdict(conflicts: list[dict[str, Any]]) -> str:
    codes = {c["code"] for c in conflicts}
    return next((code for code in RESULT_ORDER if code in codes), READY)


def _plan(clean: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "security": {k: clean.get(k) for k in ("symbol", "name", "asset_type", "isin", "wkn", "exchange", "currency", "country")} | {"active": True},
        "provider_symbols": [{"source": source, "symbol": symbol} for source, symbol in sorted(clean.get("provider_symbols", {}).items())],
        "watchlist": {"status": DEFAULT_WATCH_STATUS, "priority": clean.get("watchlist_priority")} if clean.get("add_to_watchlist") else None,
        "sector": None,
    }


def _result(result: str, *, changed: bool = False, ok: bool = True, **fields: Any) -> dict[str, Any]:
    return {"ok": ok, "operation": OPERATION, "result": result, "changed": changed, **fields}


MESSAGES = {
    READY: "Keine Konflikte gefunden.",
    CREATED: "Wertpapier wurde im Security Master angelegt.",
    ALREADY_EXISTS: "Wertpapier ist bereits im Security Master.",
    ISIN_CONFLICT: "Die ISIN gehört zu einem anderen Wertpapier.",
    SYMBOL_CONFLICT: "Das Symbol gehört bereits zu einem anderen Wertpapier.",
    WKN_CONFLICT: "Die WKN gehört zu einem anderen Wertpapier.",
    ALIAS_CONFLICT: "Ein Provider-Symbol gehört bereits zu einem anderen Wertpapier.",
    INVALID_INPUT: "Die Eingaben sind unvollständig oder ungültig.",
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


def _evaluate(conn: sqlite3.Connection, clean: Mapping[str, Any]) -> dict[str, Any]:
    conflicts, warnings = find_conflicts(conn, clean)
    verdict = _verdict(conflicts)
    out = _result(verdict, message=MESSAGES[verdict], conflicts=conflicts, warnings=warnings, planned=_plan(clean))
    existing = next((c for c in conflicts if c["code"] == ALREADY_EXISTS), None)
    if existing is not None:
        out["existing"] = {k: existing[k] for k in ("security_id", "symbol", "name", "isin", "active") if k in existing}
        out["on_watchlist"] = wa._watchlist_row(conn, existing["security_id"]) is not None
    return out


# --------------------------------------------------------------------------- operation
def onboard_security(
    conn: sqlite3.Connection, symbol: Any, name: Any, asset_type: Any, isin: Any = None, wkn: Any = None, exchange: Any = None, currency: Any = None,
    country: Any = None, provider_symbols: Any = None, add_to_watchlist: Any = False, watchlist_priority: Any = None, dry_run: Any = True,
) -> dict[str, Any]:
    """Create one new security (``dry_run=False``) or only check it (``dry_run=True``, the default).  Never raises for bad input."""
    try:
        if not isinstance(dry_run, bool):
            return _result(INVALID_INPUT, message=MESSAGES[INVALID_INPUT], errors=[{"field": "dry_run", "reason": "BOOLEAN_EXPECTED"}])
        clean, errors = normalize(symbol, name, asset_type, isin, wkn, exchange, currency, country, provider_symbols, add_to_watchlist, watchlist_priority)
        if errors:
            return _result(INVALID_INPUT, message=MESSAGES[INVALID_INPUT], errors=errors)
        if dry_run:
            if conn.in_transaction:
                conn.rollback()
            return _evaluate(conn, clean)
        with _transaction(conn):
            verdict = _evaluate(conn, clean)  # the same checks again under the write lock: nothing changed since the dry run
            if verdict["result"] != READY:
                return verdict
            security_id = _create(conn, clean)
            return _result(CREATED, changed=True, message=MESSAGES[CREATED], security_id=security_id, symbol=clean["symbol"], name=clean["name"],
                           provider_symbols=_plan(clean)["provider_symbols"], watchlist=_plan(clean)["watchlist"], warnings=verdict["warnings"])
    except Rollback as exc:
        return _result(ERROR, ok=False, message=str(exc)[:200], error="Rollback")
    except Exception as exc:  # noqa: BLE001 - every failure is reported, nothing half-written remains (the transaction rolled back)
        return _result(ERROR, ok=False, error=type(exc).__name__, message=str(exc)[:200])


def create_security(conn: sqlite3.Connection, *, symbol: Any, name: Any, asset_type: Any, isin: Any = None, wkn: Any = None, exchange: Any = None, currency: Any = None,
                    country: Any = None) -> int:
    """The one place that inserts a security for a caller that runs its own transaction (the transaction import).  Same validation as ``onboard_security``; the symbol may be
    missing when the ISIN is given.  Raises ``ValueError`` for invalid values; the caller's transaction then rolls back."""
    clean, errors = normalize(symbol, name, asset_type, isin, wkn, exchange, currency, country, symbol_required=False, isin_checksum=False)
    if not errors and not clean["symbol"] and not clean["isin"]:
        errors.append({"field": "symbol", "reason": "SYMBOL_OR_ISIN_REQUIRED"})
    if errors:
        raise ValueError("invalid security: " + ", ".join(f"{e['field']}:{e['reason']}" for e in errors))
    return _create(conn, clean)


def _create(conn: sqlite3.Connection, clean: Mapping[str, Any]) -> int:
    """The three inserts, inside the caller's transaction.  Raises on anything unexpected so the transaction rolls back."""
    cursor = conn.execute(
        "INSERT INTO security (symbol, isin, wkn, name, exchange, currency, country, asset_type, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (clean["symbol"], clean["isin"], clean["wkn"], clean["name"], clean["exchange"], clean["currency"], clean["country"], clean["asset_type"]),
    )
    security_id = int(cursor.lastrowid)
    for source, provider_symbol in sorted(clean.get("provider_symbols", {}).items()):
        row = conn.execute("SELECT id FROM data_sources WHERE name = ? AND active = 1", (source,)).fetchone()
        if row is None:
            raise Rollback(f"data source {source} is not available")
        conn.execute("INSERT INTO source_symbols (security_id, source_id, symbol) VALUES (?, ?, ?)", (security_id, int(row[0]), provider_symbol))
    if clean.get("add_to_watchlist"):
        conn.execute("INSERT INTO watchlist (security_id, status, priority) VALUES (?, ?, ?)", (security_id, DEFAULT_WATCH_STATUS, clean.get("watchlist_priority")))
    found = sa.AliasIndex.from_connection(conn).resolve(security_id=security_id, symbol=clean["symbol"])
    if found is None or found.security_id != security_id:
        raise Rollback("the new security is not resolvable")
    return security_id
