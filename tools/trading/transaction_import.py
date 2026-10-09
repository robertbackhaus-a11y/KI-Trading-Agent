"""Provider-neutral transaction import: canonical model, planning, strategy/campaign initialization, execution.

Pipeline::

    source file -> source adapter -> CanonicalRow -> validation -> security resolution
                -> transactions -> positions -> strategy assignment -> Swing campaign
                -> campaign reconciliation -> validation -> orchestrator-ready

Everything after the adapter is provider-neutral.  A caller first builds an :class:`ImportPlan` (no writes), presents it, and then
applies that exact plan token.  A preview (:func:`preview_import`) runs the complete execution path on an in-memory copy of the
database, so the previewed end state is exactly what a write produces while the real database is never touched.  A write is one
SQLite transaction: either everything below is committed, or nothing is.

Trades are never interpreted as strategic Swing events (TP1/TP2/stop); only unambiguous post-campaign BUY/SELL trades are reconciled
as neutral, transaction-linked quantity events (see :func:`reconcile_campaign_transactions`).
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from corporate_actions import cumulative_split_factor, load_corporate_actions


DEFAULT_DB_PATH = Path(r"C:\tools\trading\data\trading.db")
# Value of campaign events written by the Parqet adapter before the importer became provider neutral; kept for continuity.
LEGACY_RECONCILIATION_SOURCE = "parqet_reconciliation"
NUMERIC_SCALE = Decimal("0.00000001")
_RECONCILIATION_EPS = 1e-9
_EPS = 1e-9

CLASS_DUPLICATE = "DUPLICATE"
CLASS_NEW = "NEW"
CLASS_NEW_HISTORICAL = "NEW_HISTORICAL"
CLASS_CONFLICT = "CONFLICT"
CLASS_UNKNOWN_SECURITY = "UNKNOWN_SECURITY"
CLASS_INVALID = "INVALID"

_CLASSIFICATIONS = {
    CLASS_DUPLICATE, CLASS_NEW, CLASS_NEW_HISTORICAL, CLASS_CONFLICT,
    CLASS_UNKNOWN_SECURITY, CLASS_INVALID,
}
_BLOCKING_CLASSES = {CLASS_CONFLICT, CLASS_UNKNOWN_SECURITY, CLASS_INVALID}

# The transaction types the position model actually knows (see _rebuild_position).
SUPPORTED_TYPES = ("BUY", "SELL", "TRANSFERIN", "TRANSFEROUT", "DIVIDEND", "COST")
IMPORT_STRATEGIES = ("swing", "long_term")

# open_items codes
STRATEGY_ASSIGNMENT_REQUIRED = "STRATEGY_ASSIGNMENT_REQUIRED"
STRATEGY_CONFLICT = "STRATEGY_CONFLICT"
STRATEGY_EFFECTIVE_FROM_UNCLEAR = "STRATEGY_EFFECTIVE_FROM_UNCLEAR"
CAMPAIGN_INITIALIZATION_REQUIRED = "CAMPAIGN_INITIALIZATION_REQUIRED"
CAMPAIGN_RECONCILIATION_REQUIRED = "CAMPAIGN_RECONCILIATION_REQUIRED"
MARKET_DATA_MISSING = "MARKET_DATA_MISSING"
ROWS_BLOCKED = "ROWS_BLOCKED"
HISTORICAL_ROWS_NOT_IMPORTED = "HISTORICAL_ROWS_NOT_IMPORTED"
CAMPAIGN_START_IGNORED = "CAMPAIGN_START_IGNORED"


class TransactionImportError(ValueError):
    """Raised when a requested import cannot safely proceed."""


# ---------------------------------------------------------------------------------------------------------------------------------
# Canonical model and adapters
# ---------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CanonicalRow:
    """One source row after the adapter: provider-neutral, validated field values (or an ``error``)."""

    row_number: int
    transaction_date: Optional[str] = None
    transaction_type: Optional[str] = None
    identifier: Optional[str] = None  # ISIN (or another persisted identity of the security), upper case
    isin: Optional[str] = None
    symbol: Optional[str] = None
    wkn: Optional[str] = None
    name: Optional[str] = None
    shares: Optional[float] = None
    price: Optional[float] = None
    amount: Optional[float] = None
    fees: float = 0.0
    taxes: float = 0.0
    currency: Optional[str] = None
    broker: Optional[str] = None
    external_id: Optional[str] = None
    realized_gain: Optional[float] = None
    asset_type: Optional[str] = None
    notes: Optional[str] = None
    source_values: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass(frozen=True)
class ImportAdapter:
    name: str  # value of --format
    parse: Callable[[Path], list[CanonicalRow]]
    import_type: str  # imports.import_type
    source_label: str  # imports.source
    external_prefix: str  # transactions.external_id prefix
    reconciliation_source: str  # swing_campaign_event.source of reconciled trades
    notes: Callable[["NormalizedImportRecord"], str]  # transactions.notes (JSON)


def get_adapter(name: str) -> ImportAdapter:
    if name == "canonical":
        return CANONICAL_ADAPTER
    if name == "parqet":
        from parqet_import import PARQET_ADAPTER  # lazy: the Parqet adapter imports this module

        return PARQET_ADAPTER
    raise TransactionImportError(f"unknown import format {name!r} (supported: canonical, parqet)")


@dataclass(frozen=True)
class ImportOptions:
    """Choices made by the caller for one import (all optional)."""

    create_securities: bool = False
    strategies: tuple[tuple[str, str], ...] = ()  # (security key, swing|long_term)
    campaign_opened_at: tuple[tuple[str, str], ...] = ()  # (security key, YYYY-MM-DD)
    as_of_date: Optional[str] = None  # "today" for assignment lookups (tests); default: current UTC date

    @property
    def has_phase_work(self) -> bool:
        return bool(self.strategies or self.campaign_opened_at)


def parse_key_value_options(items: Iterable[str], *, kind: str) -> tuple[tuple[str, str], ...]:
    """Parse repeated ``KEY=VALUE`` CLI items (``kind`` is ``strategy`` or ``date``)."""
    result: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        key, value = key.strip().upper(), value.strip()
        if not separator or not key or not value:
            raise TransactionImportError(f"expected SYMBOL=VALUE, got {item!r}")
        if kind == "strategy":
            value = value.lower().replace("-", "_")
            if value not in IMPORT_STRATEGIES:
                raise TransactionImportError(f"strategy for {key} must be one of {', '.join(IMPORT_STRATEGIES)}, got {value!r}")
        else:
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError as exc:
                raise TransactionImportError(f"date for {key} must be YYYY-MM-DD, got {value!r}") from exc
        if key in result and result[key] != value:
            raise TransactionImportError(f"{key} is given twice with different values")
        result[key] = value
    return tuple(sorted(result.items()))


@dataclass(frozen=True)
class NormalizedImportRecord:
    source_row_number: int
    transaction_date: Optional[str]
    transaction_type: Optional[str]
    identifier: Optional[str]
    security_id: Optional[int]
    shares: Optional[float]
    price: Optional[float]
    amount: Optional[float]
    fees: Optional[float]
    taxes: Optional[float]
    currency: Optional[str]
    broker: Optional[str]
    realized_gain: Optional[float]
    source_notes: Optional[str]
    external_id: Optional[str]
    classification: str
    classification_reason: str
    holding: Optional[str] = None
    asset_type: Optional[str] = None
    source_values: dict[str, str] = field(default_factory=dict)
    security_resolution: Optional[str] = None
    symbol: Optional[str] = None
    new_security: Optional[dict[str, Any]] = None  # master data of a security that would be created (--create-securities)
    source_external_id: Optional[str] = None  # external id given by the source file
    matched_transaction_id: Optional[int] = None  # existing transaction a DUPLICATE row matches / transaction an inserted row became

    def primitive(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportPlan:
    source_file: str
    source_file_sha256: str
    source_row_count: int
    latest_db_transaction_date: Optional[str]
    db_state_token: str
    plan_token: str
    records: tuple[NormalizedImportRecord, ...]
    adapter_name: str = "parqet"
    options: ImportOptions = ImportOptions()

    @property
    def counts(self) -> dict[str, int]:
        return {
            name.lower(): sum(record.classification == name for record in self.records)
            for name in sorted(_CLASSIFICATIONS)
        }

    @property
    def affected_security_ids(self) -> list[int]:
        return sorted({r.security_id for r in self.records if r.security_id is not None})

    @property
    def safe_to_import_new_rows(self) -> bool:
        counts = self.counts
        return counts[CLASS_NEW.lower()] > 0 and not any(
            counts[name.lower()] for name in (CLASS_CONFLICT, CLASS_UNKNOWN_SECURITY, CLASS_INVALID)
        )

    def primitive(self) -> dict[str, Any]:
        counts = self.counts
        return {
            "format": self.adapter_name,
            "source_file": self.source_file,
            "source_file_sha256": self.source_file_sha256,
            "source_row_count": self.source_row_count,
            "latest_db_transaction_date": self.latest_db_transaction_date,
            "db_state_token": self.db_state_token,
            "plan_token": self.plan_token,
            "counts": counts,
            "duplicate": counts[CLASS_DUPLICATE.lower()],
            "new": counts[CLASS_NEW.lower()],
            "new_historical": counts[CLASS_NEW_HISTORICAL.lower()],
            "conflict": counts[CLASS_CONFLICT.lower()],
            "unknown_security": counts[CLASS_UNKNOWN_SECURITY.lower()],
            "invalid": counts[CLASS_INVALID.lower()],
            "affected_security_ids": self.affected_security_ids,
            "safe_to_import_new_rows": self.safe_to_import_new_rows,
            "historical_requires_approval": counts[CLASS_NEW_HISTORICAL.lower()] > 0,
            "conflict_count": counts[CLASS_CONFLICT.lower()],
            "rows": [record.primitive() for record in self.records],
        }


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _canonical_number(value: Optional[float]) -> str:
    if value is None:
        return ""
    return format(Decimal(str(value)).quantize(NUMERIC_SCALE), "f")


def _normalize_datetime(value: str, *, iso_only: bool = False) -> str:
    text = value.strip()
    if not text:
        raise TransactionImportError("missing datetime")
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        if iso_only:
            raise TransactionImportError(f"invalid transaction_date: {value!r} (ISO 8601, e.g. 2026-03-02 or 2026-03-02T10:15:00Z)")
        for pattern in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            raise TransactionImportError(f"invalid datetime: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def economic_fingerprint(
    *, security_id: int | str, transaction_type: str, transaction_date: str,
    shares: float, price: float, amount: float, fees: float, taxes: float,
    currency: str, broker: str,
) -> str:
    """Return the deterministic economic identity used for idempotency."""
    payload = {
        "security_id": security_id if isinstance(security_id, str) else int(security_id), "transaction_type": transaction_type.upper(),
        "transaction_date": transaction_date, "shares": _canonical_number(shares),
        "price": _canonical_number(price), "amount": _canonical_number(amount),
        "fees": _canonical_number(fees), "taxes": _canonical_number(taxes),
        "currency": currency.upper(), "broker": (broker or "").casefold(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _db_state_token(conn: sqlite3.Connection) -> tuple[Optional[str], str]:
    row = conn.execute("SELECT COUNT(*) AS count, MAX(id) AS max_id, MAX(transaction_date) AS latest FROM transactions").fetchone()
    payload = {"count": int(row[0]), "max_id": row[1], "latest": row[2]}
    return row[2], hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _normalized_holding_name(value: str) -> str:
    """Normalize only case and punctuation; this is not fuzzy matching."""
    return "".join(character for character in value.casefold() if character.isalnum())


def _unique_security(
    rows: list[sqlite3.Row],
    *,
    resolution: str,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    if len(rows) == 1:
        return int(rows[0][0]), resolution, None
    if len(rows) > 1:
        return None, None, f"AMBIGUOUS_SECURITY: {resolution} maps to multiple securities"
    return None, None, None


def _existing_for_security(conn: sqlite3.Connection, security_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, security_id, transaction_type, transaction_date, shares, price,
                  amount, fees, taxes, currency, broker, external_id
             FROM transactions WHERE security_id = ?""", (security_id,)
    ).fetchall()


def _rebuild_position(
    conn: sqlite3.Connection,
    security_id: int,
    imported_records: Iterable[NormalizedImportRecord] = (),
    *,
    as_of: Optional[str] = None,
) -> None:
    """Recompute one position using the DB's average-cost transaction model.

    BUY and TRANSFERIN increase cost basis by amount + fees + taxes.  SELL and
    TRANSFEROUT reduce it at the current average cost. ``invested_amount`` is
    the lifetime *purchase* total, not the remaining cost basis; security
    transfers are deliberately excluded from that cash-investment field.
    DIVIDEND and COST are cash events and do not alter share/cost basis.

    The schema does not retain a reconstructable realized-gain field for old
    rows: Parqet's value lives in JSON notes and legacy position rows already
    contain an accumulated value with mixed historical provenance.  Retain the
    persisted value and add only explicit realized gains from newly inserted
    rows, so an incremental import never erases existing financial history.
    This deliberately does not assign transaction semantics to Swing events.

    Phase 4B.3: each transaction's raw share quantity is converted to its
    ``as_of``-equivalent quantity via any persisted ``corporate_action``
    (stock split) rows for this security (see ``corporate_actions.py``).
    Source transactions are never rewritten. Cash figures (amount, fees,
    taxes, and therefore cost basis and realized gain) are never adjusted by
    a split -- a split is non-cash by definition. ``as_of`` defaults to
    today, matching this function's existing "rebuild the current position"
    behavior for every pre-existing caller.
    """
    rows = conn.execute("""SELECT transaction_type, transaction_date, shares, price, amount, fees, taxes, currency
                           FROM transactions WHERE security_id = ? ORDER BY transaction_date, id""", (security_id,)).fetchall()
    actions = load_corporate_actions(conn, security_id)
    shares = 0.0
    remaining = 0.0
    invested = 0.0
    currency: Optional[str] = None
    final_transaction_type: Optional[str] = None
    for row in rows:
        kind = row["transaction_type"]
        final_transaction_type = kind
        split_factor = cumulative_split_factor(actions, row["transaction_date"], as_of)
        quantity = float(row["shares"] or 0.0) * split_factor
        amount = float(row["amount"] or 0.0)
        fees = float(row["fees"] or 0.0)
        taxes = float(row["taxes"] or 0.0)
        if row["currency"]:
            currency = row["currency"]
        if kind in {"BUY", "TRANSFERIN"}:
            shares += quantity
            remaining += amount + fees + taxes
            if kind == "BUY":
                invested += amount + fees + taxes
        elif kind in {"SELL", "TRANSFEROUT"}:
            reduction = min(quantity, shares) * (remaining / shares) if shares > 0 else 0.0
            shares = max(0.0, shares - quantity)
            remaining -= reduction
    if abs(shares) < 1e-9:
        shares = 0.0
        remaining = 0.0
        # A full security transfer leaves the broker-account purchase value
        # empty in the established position representation.
        if final_transaction_type == "TRANSFEROUT":
            invested = 0.0
    average = remaining / shares if shares > 0 else None
    first = rows[0]["transaction_date"] if rows else None
    last = rows[-1]["transaction_date"] if rows else None
    prior = conn.execute("SELECT realized_gain FROM positions WHERE security_id = ?", (security_id,)).fetchone()
    prior_realized = float(prior[0] or 0.0) if prior is not None else 0.0
    imported_realized = sum(
        float(record.realized_gain or 0.0)
        for record in imported_records
        if record.security_id == security_id
    )
    realized = prior_realized + imported_realized
    conn.execute(
        """INSERT INTO positions(security_id, shares, avg_cost, remaining_cost_basis, currency, invested_amount,
                                  realized_gain, first_transaction_at, last_transaction_at, transaction_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(security_id) DO UPDATE SET
              shares=excluded.shares, avg_cost=excluded.avg_cost, remaining_cost_basis=excluded.remaining_cost_basis,
              currency=excluded.currency, invested_amount=excluded.invested_amount, realized_gain=excluded.realized_gain,
              first_transaction_at=excluded.first_transaction_at, last_transaction_at=excluded.last_transaction_at,
              transaction_count=excluded.transaction_count, updated_at=CURRENT_TIMESTAMP""",
        (security_id, shares, average, remaining, currency, invested, realized, first, last, len(rows)),
    )


def rebuild_position(conn: sqlite3.Connection, security_id: int, *, as_of: Optional[str] = None) -> None:
    """Public entry point to the canonical position-rebuild path.

    Used by ``apply_import_plan`` (implicitly, via the private function) and
    by ``Manage-TradingCorporateActions.py`` after a corporate action is
    added, so a split-affected position is rebuilt through the exact same
    split-aware logic rather than a duplicated one-off calculation.
    """
    _rebuild_position(conn, security_id, (), as_of=as_of)


def _lifecycle_api():
    try:
        from swing_lifecycle import derive_open_lifecycle, lifecycle_schema_available, lifecycle_to_primitive
    except ImportError:  # module can also be imported as tools.trading.parqet_import
        from tools.trading.swing_lifecycle import derive_open_lifecycle, lifecycle_schema_available, lifecycle_to_primitive
    return derive_open_lifecycle, lifecycle_schema_available, lifecycle_to_primitive


def _reconciliation_event_values(transaction_type: str, price: Any, currency: Any) -> tuple[str, Optional[float], Optional[str]]:
    # SELL -> neutral manual_reduction (never tp1/tp2/stop: the broker trade
    # carries no strategic reason). BUY -> add (already supported by the lifecycle).
    event_type = "manual_reduction" if transaction_type == "SELL" else "add"
    event_price = float(price) if price is not None and float(price) > 0 else None
    event_currency = currency if isinstance(currency, str) and len(currency) == 3 and currency.isalpha() and currency.isupper() else None
    if event_currency is None:
        event_price = None
    return event_type, event_price, event_currency


def _reconcile_one_campaign(conn: sqlite3.Connection, campaign_id: int, security_id: int, opened_at: str, *, write: bool, source: str = LEGACY_RECONCILIATION_SOURCE) -> dict[str, Any]:
    derive_open_lifecycle, _, _ = _lifecycle_api()
    opened_day = str(opened_at)[:10]
    position = conn.execute("SELECT shares FROM positions WHERE security_id = ?", (security_id,)).fetchone()
    position_quantity = float(position[0]) if position is not None and position[0] is not None else None
    lifecycle = derive_open_lifecycle(conn, security_id, current_quantity=position_quantity)
    base_quantity = lifecycle.event_derived_quantity if lifecycle.campaign_id == campaign_id else None
    report: dict[str, Any] = {
        "campaign_id": campaign_id, "security_id": security_id, "opened_at": opened_at,
        "status": "NOTHING_TO_RECONCILE", "applied": False,
        "reconciled_transactions": [], "skipped_pre_campaign": 0, "skipped_already_processed": 0,
        "ambiguous": [], "manual_review_required": [], "flags": [],
        "position_quantity": position_quantity, "campaign_expected_quantity": base_quantity,
        "delta_after_reconciliation": None,
    }
    review: list[dict[str, Any]] = report["manual_review_required"]
    if base_quantity is None:
        review.append({"transaction_id": None, "reason": "LIFECYCLE_UNAVAILABLE"})
    if position_quantity is None:
        review.append({"transaction_id": None, "reason": "POSITION_UNAVAILABLE"})
    if review:
        report["status"] = "MANUAL_REVIEW_REQUIRED"
        return report

    actions = load_corporate_actions(conn, security_id)
    rows = conn.execute(
        """SELECT t.id, t.transaction_type, t.transaction_date, t.shares, t.price, t.currency,
                  EXISTS(SELECT 1 FROM swing_campaign_event e WHERE e.transaction_id = t.id)
             FROM transactions t
            WHERE t.security_id = ? AND t.transaction_type IN ('BUY', 'SELL', 'TRANSFERIN', 'TRANSFEROUT')
            ORDER BY t.transaction_date, t.id""",
        (security_id,),
    ).fetchall()
    planned: list[dict[str, Any]] = []
    same_day_ids: list[int] = []
    running = float(base_quantity)
    for transaction_id, kind, transaction_date, shares, price, currency, linked in rows:
        if str(transaction_date)[:10] < opened_day:
            report["skipped_pre_campaign"] += 1
            continue
        if linked:
            report["skipped_already_processed"] += 1
            continue
        if str(transaction_date)[:10] == opened_day:
            # opened_at has date resolution only: whether a same-day trade lies
            # before or after the baseline is decided by the quantity proof below.
            same_day_ids.append(int(transaction_id))
        quantity = float(shares) if shares is not None else 0.0
        reason: Optional[str] = None
        if kind in {"TRANSFERIN", "TRANSFEROUT"}:
            reason = "TRANSFER_REQUIRES_MANUAL_REVIEW"
        elif quantity <= 0:
            reason = "INVALID_QUANTITY"
        elif cumulative_split_factor(actions, str(transaction_date)) != 1.0:
            reason = "CORPORATE_ACTION_AFTER_TRANSACTION"
        elif kind == "SELL" and quantity > running + _RECONCILIATION_EPS:
            reason = "REDUCTION_EXCEEDS_CAMPAIGN_QUANTITY"
        if reason is not None:
            review.append({"transaction_id": int(transaction_id), "reason": reason})
            continue
        event_type, event_price, event_currency = _reconciliation_event_values(kind, price, currency)
        running += quantity if kind == "BUY" else -quantity
        planned.append({
            "transaction_id": int(transaction_id), "event_type": event_type, "event_at": str(transaction_date)[:10],
            "quantity": quantity, "price": event_price, "currency": event_currency,
            "transaction_date": str(transaction_date), "transaction_type": kind,
        })

    # Same-day rule: a trade on the opened_at calendar day is reconciled only if
    # it is the single unlinked same-day trade AND the quantity history proves it
    # lies after the baseline (baseline +/- that trade == position, checked below
    # via delta 0). With several same-day trades, order relative to the baseline
    # cannot be proven, so nothing is written.
    if len(same_day_ids) > 1:
        review.extend({"transaction_id": item, "reason": "SAME_DAY_TRADES_AMBIGUOUS"} for item in same_day_ids)
    delta_after = position_quantity - running
    if review:
        status = "MANUAL_REVIEW_REQUIRED"
    elif planned and abs(delta_after) > _RECONCILIATION_EPS:
        # All-or-nothing: an event set that does not close the gap (e.g. an
        # unlinked manual event already covering the same trade) is never written.
        if same_day_ids:
            review.append({"transaction_id": same_day_ids[0], "reason": "SAME_DAY_TRADE_NOT_PROVEN"})
        review.append({"transaction_id": None, "reason": "PROJECTED_DELTA_NONZERO", "delta": delta_after})
        status = "MANUAL_REVIEW_REQUIRED"
    elif planned:
        status = "RECONCILED"
    elif abs(delta_after) > _RECONCILIATION_EPS:
        review.append({"transaction_id": None, "reason": "DELTA_WITHOUT_UNPROCESSED_TRANSACTIONS", "delta": delta_after})
        status = "MANUAL_REVIEW_REQUIRED"
    else:
        status = "NOTHING_TO_RECONCILE"
    report["status"] = status
    if status != "RECONCILED":
        report["delta_after_reconciliation"] = position_quantity - float(base_quantity)
        return report

    report["reconciled_transactions"] = [{key: item[key] for key in ("transaction_id", "event_type", "quantity", "price", "currency", "transaction_date")} for item in planned]
    report["campaign_expected_quantity"] = running
    report["delta_after_reconciliation"] = delta_after
    if abs(position_quantity) <= _RECONCILIATION_EPS:
        report["flags"].append("FULL_EXIT_CAMPAIGN_STILL_OPEN")
    if not write:
        return report

    conn.execute("SAVEPOINT campaign_reconciliation")
    try:
        for item, listed in zip(planned, report["reconciled_transactions"]):
            cursor = conn.execute(
                """INSERT INTO swing_campaign_event(campaign_id, event_type, event_at, quantity, price, currency, transaction_id, source, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (campaign_id, item["event_type"], item["event_at"], item["quantity"], item["price"], item["currency"], item["transaction_id"], source,
                 json.dumps({"reconciled_from_transaction_id": item["transaction_id"], "transaction_date": item["transaction_date"], "transaction_type": item["transaction_type"]}, sort_keys=True, separators=(",", ":"))),
            )
            listed["event_id"] = int(cursor.lastrowid)
        verify = derive_open_lifecycle(conn, security_id, current_quantity=position_quantity)
        if verify.reconciliation_delta is None or abs(verify.reconciliation_delta) > _RECONCILIATION_EPS:
            raise TransactionImportError("campaign quantity does not reconcile after writing events")
        conn.execute("RELEASE SAVEPOINT campaign_reconciliation")
    except (sqlite3.Error, TransactionImportError) as exc:
        conn.execute("ROLLBACK TO SAVEPOINT campaign_reconciliation")
        conn.execute("RELEASE SAVEPOINT campaign_reconciliation")
        report.update(status="MANUAL_REVIEW_REQUIRED", reconciled_transactions=[], campaign_expected_quantity=base_quantity,
                      delta_after_reconciliation=position_quantity - float(base_quantity), flags=[])
        review.append({"transaction_id": None, "reason": f"EVENT_WRITE_FAILED: {exc}"})
        return report
    report["applied"] = True
    return report


def reconcile_campaign_transactions(conn: sqlite3.Connection, security_ids: Optional[Iterable[int]] = None, *, write: bool = False, source: str = LEGACY_RECONCILIATION_SOURCE) -> list[dict[str, Any]]:
    """Reconcile unprocessed post-campaign broker trades with open Swing campaigns.

    Per open campaign, every BUY/SELL on or after ``opened_at`` that is not yet
    linked to a ``swing_campaign_event.transaction_id`` becomes a transaction-
    linked ``add`` (BUY) or ``manual_reduction`` (SELL) event; earlier trades are
    baseline. Strategic reasons (tp1/tp2/stop) are never derived here. Events are
    written only when the campaign then reconciles exactly (delta 0) and nothing
    needs manual review. Idempotent via the unique ``transaction_id`` link.
    Dry-run (``write=False``) performs no writes.
    """
    _, lifecycle_schema_available, _ = _lifecycle_api()
    if not lifecycle_schema_available(conn):
        return []
    query = "SELECT id, security_id, opened_at FROM swing_campaign WHERE status = 'open'"
    params: list[Any] = []
    if security_ids is not None:
        ids = sorted({int(item) for item in security_ids})
        if not ids:
            return []
        query += f" AND security_id IN ({','.join('?' * len(ids))})"
        params.extend(ids)
    by_security: dict[int, list[tuple[int, str]]] = {}
    for campaign_id, security_id, opened_at in conn.execute(query + " ORDER BY security_id, id", params).fetchall():
        by_security.setdefault(int(security_id), []).append((int(campaign_id), opened_at))
    reports: list[dict[str, Any]] = []
    for security_id, campaigns in by_security.items():
        if len(campaigns) > 1:
            reports.append({
                "campaign_id": None, "campaign_ids": [item[0] for item in campaigns], "security_id": security_id, "opened_at": None,
                "status": "AMBIGUOUS", "applied": False, "reconciled_transactions": [], "skipped_pre_campaign": 0,
                "skipped_already_processed": 0, "ambiguous": ["MULTIPLE_OPEN_CAMPAIGNS"], "manual_review_required": [], "flags": [],
                "position_quantity": None, "campaign_expected_quantity": None, "delta_after_reconciliation": None,
            })
            continue
        campaign_id, opened_at = campaigns[0]
        reports.append(_reconcile_one_campaign(conn, campaign_id, security_id, opened_at, write=write, source=source))
    return reports


def apply_campaign_reconciliation(conn: sqlite3.Connection, security_ids: Optional[Iterable[int]] = None, *, create_backup: bool = False, backup_path: str | Path | None = None, source: str = LEGACY_RECONCILIATION_SOURCE) -> dict[str, Any]:
    """Apply :func:`reconcile_campaign_transactions` for already-imported trades.

    Needed because a trade imported earlier is DUPLICATE on re-import and would
    otherwise never reach the reconciliation. No-op (no backup, no write) when
    nothing is reconcilable.
    """
    ids = None if security_ids is None else sorted({int(item) for item in security_ids})
    preview = reconcile_campaign_transactions(conn, ids, write=False, source=source)
    if not any(item["status"] == "RECONCILED" for item in preview):
        return {"written": False, "backup_path": None, "campaigns": preview, "validation": None}
    backup: Optional[Path] = None
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        if create_backup:
            backup = _write_backup(conn, "parqet-reconcile", backup_path)
        reports = reconcile_campaign_transactions(conn, ids, write=True, source=source)
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        if integrity != ["ok"] or foreign_keys:
            raise TransactionImportError("integrity or foreign-key validation failed")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return {"written": any(item["applied"] for item in reports), "backup_path": str(backup) if backup else None, "campaigns": reports,
            "validation": {"integrity_check": integrity, "foreign_key_check": foreign_keys}}


def _lifecycle_report(conn: sqlite3.Connection, security_ids: Iterable[int], records: Iterable[NormalizedImportRecord], reconciliation: Optional[Iterable[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    derive_open_lifecycle, lifecycle_schema_available, lifecycle_to_primitive = _lifecycle_api()
    if not lifecycle_schema_available(conn):
        return []
    by_security: dict[int, list[NormalizedImportRecord]] = {}
    for record in records:
        if record.security_id is not None:
            by_security.setdefault(record.security_id, []).append(record)
    reconciled_by_security = {int(item["security_id"]): item for item in (reconciliation or ())}
    report: list[dict[str, Any]] = []
    for security_id in sorted(set(security_ids)):
        position = conn.execute("SELECT shares FROM positions WHERE security_id = ?", (security_id,)).fetchone()
        current_quantity = float(position[0]) if position else None
        lifecycle = derive_open_lifecycle(conn, security_id, current_quantity=current_quantity)
        detail = reconciled_by_security.get(security_id)
        if lifecycle.campaign_id is None:
            if detail is not None and detail["status"] == "AMBIGUOUS":
                report.append({**detail, "flags": ["AMBIGUOUS_OPEN_CAMPAIGNS"], "lifecycle_reconciliation_required": True})
            continue
        item = lifecycle_to_primitive(lifecycle)
        imported = by_security.get(security_id, [])
        pre_campaign = any((r.transaction_date or "")[:10] < lifecycle.opened_at[:10] for r in imported)
        post_campaign = any((r.transaction_date or "")[:10] >= lifecycle.opened_at[:10] for r in imported)
        flags: list[str] = []
        if pre_campaign:
            flags.append("PRE_CAMPAIGN_BASELINE_IMPACT")
        if post_campaign and lifecycle.reconciliation_delta not in (None, 0.0):
            flags.append("POST_CAMPAIGN_LIFECYCLE_UNCLASSIFIED")
        if detail is not None:
            item.update({key: detail[key] for key in (
                "status", "reconciled_transactions", "skipped_pre_campaign", "skipped_already_processed", "ambiguous",
                "manual_review_required", "position_quantity", "campaign_expected_quantity", "delta_after_reconciliation")})
            flags.extend(flag for flag in detail["flags"] if flag not in flags)
        item["flags"] = flags
        item["lifecycle_reconciliation_required"] = lifecycle.reconciliation_delta not in (None, 0.0)
        report.append(item)
    return report


def _write_backup(conn: sqlite3.Connection, tag: str, backup_path: str | Path | None) -> Path:
    db_row = conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(db_row[2])
    backup = Path(backup_path) if backup_path is not None else db_path.with_name(f"{db_path.name}.{tag}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak")
    if backup.exists():
        raise TransactionImportError(f"refusing to overwrite backup: {backup}")
    # Use a separate read-only connection so the backup contains the
    # fully locked, still pre-write state without the pending import.
    source = sqlite3.connect(f"file:///{db_path.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(str(backup))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup


def _duplicate_audit(conn: sqlite3.Connection) -> dict[str, list[Any]]:
    economic: dict[str, list[int]] = {}
    for row in conn.execute("SELECT id, security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker FROM transactions"):
        key = economic_fingerprint(security_id=int(row["security_id"]), transaction_type=row["transaction_type"], transaction_date=row["transaction_date"], shares=float(row["shares"] or 0), price=float(row["price"] or 0), amount=float(row["amount"] or 0), fees=float(row["fees"] or 0), taxes=float(row["taxes"] or 0), currency=row["currency"] or "", broker=row["broker"] or "")
        economic.setdefault(key, []).append(int(row["id"]))
    external = conn.execute("SELECT external_id, GROUP_CONCAT(id) FROM transactions WHERE external_id IS NOT NULL GROUP BY external_id HAVING COUNT(*) > 1").fetchall()
    return {"exact_economic_duplicates": [ids for ids in economic.values() if len(ids) > 1], "duplicate_external_ids": [tuple(row) for row in external]}

# ---------------------------------------------------------------------------------------------------------------------------------
# Canonical CSV adapter
# ---------------------------------------------------------------------------------------------------------------------------------
CANONICAL_COLUMNS = (
    "transaction_date", "transaction_type", "isin", "symbol", "wkn", "name", "shares", "price", "amount", "fees", "taxes",
    "currency", "broker", "external_id", "realized_gain", "asset_type", "notes",
)
_STRICT_NUMBER = re.compile(r"^\d+(\.\d+)?$")
_STRICT_SIGNED_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_REQUIRES_SHARES = {"BUY", "SELL", "TRANSFERIN", "TRANSFEROUT"}


def _strict_number(value: Optional[str], name: str, *, signed: bool = False) -> Optional[float]:
    text = _text(value)
    if text is None:
        return None
    if not (_STRICT_SIGNED_NUMBER if signed else _STRICT_NUMBER).match(text):
        raise TransactionImportError(f"invalid {name}: {text!r} (decimal point, no thousands separator, {'optional minus' if signed else 'not negative'})")
    return float(Decimal(text))


def _canonical_row(row_number: int, raw: dict[str, str]) -> CanonicalRow:
    values = {key: _text(raw.get(key)) for key in CANONICAL_COLUMNS}
    source_values = {key: value for key, value in raw.items() if value not in (None, "")}
    try:
        transaction_date = _normalize_datetime(values["transaction_date"] or "", iso_only=True)
        kind = (values["transaction_type"] or "").upper().replace(" ", "").replace("_", "")
        if kind == "FEE":
            raise TransactionImportError("unsupported transaction type 'FEE' (use COST for a fee booked on its own)")
        if kind not in SUPPORTED_TYPES:
            raise TransactionImportError(f"unsupported transaction type {values['transaction_type']!r} (supported: {', '.join(SUPPORTED_TYPES)})")
        isin = (values["isin"] or "").upper() or None
        symbol = (values["symbol"] or "").upper() or None
        if isin is None and symbol is None:
            raise TransactionImportError("missing isin and symbol (at least one identifies the security)")
        if isin is not None and not _ISIN.match(isin):
            raise TransactionImportError(f"invalid isin: {isin!r}")
        currency = (values["currency"] or "").upper()
        if not _CURRENCY.match(currency):
            raise TransactionImportError(f"missing or invalid currency: {values['currency']!r} (three letters, e.g. EUR)")
        shares = _strict_number(values["shares"], "shares")
        price = _strict_number(values["price"], "price")
        amount = _strict_number(values["amount"], "amount")
        fees = _strict_number(values["fees"], "fees") or 0.0
        taxes = _strict_number(values["taxes"], "taxes") or 0.0
        realized_gain = _strict_number(values["realized_gain"], "realized_gain", signed=True)
        if kind in _REQUIRES_SHARES and not shares:
            raise TransactionImportError(f"{kind} requires shares greater than zero")
        if kind in {"BUY", "SELL"}:
            if price is None:
                raise TransactionImportError(f"{kind} requires price")
            if amount is None:
                amount = float(Decimal(str(shares)) * Decimal(str(price)))
        elif kind == "TRANSFERIN":
            if amount is None:
                raise TransactionImportError("TRANSFERIN requires amount (the cost basis that is carried in)")
            if price is None:
                price = amount / shares
        elif kind == "TRANSFEROUT":
            amount = amount if amount is not None else 0.0
            price = price if price is not None else 0.0
        else:  # DIVIDEND, COST
            if not amount:
                raise TransactionImportError(f"{kind} requires amount greater than zero")
            shares = shares or 0.0
            price = price if price is not None else 0.0
        return CanonicalRow(
            row_number=row_number, transaction_date=transaction_date, transaction_type=kind, identifier=isin or symbol, isin=isin,
            symbol=symbol, wkn=(values["wkn"] or "").upper() or None, name=values["name"], shares=shares, price=price, amount=amount,
            fees=fees, taxes=taxes, currency=currency, broker=values["broker"], external_id=values["external_id"],
            realized_gain=realized_gain, asset_type=values["asset_type"], notes=values["notes"], source_values=source_values,
        )
    except TransactionImportError as exc:
        return CanonicalRow(row_number=row_number, name=values.get("name"), asset_type=values.get("asset_type"), source_values=source_values, error=str(exc))


def parse_canonical_csv(path: Path) -> list[CanonicalRow]:
    """Read the canonical Trading CSV (UTF-8, comma or semicolon separated, decimal point, ISO 8601 dates)."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        first = handle.readline()
        if not first.strip():
            raise TransactionImportError("CSV has no header row")
        delimiter = ";" if first.count(";") > first.count(",") else ","
        handle.seek(0)
        reader = csv.DictReader(handle, delimiter=delimiter)
        names = [(name or "").strip().lower() for name in (reader.fieldnames or [])]
        unknown = [name for name in names if name not in CANONICAL_COLUMNS]
        if unknown:
            raise TransactionImportError("CSV has unknown columns: " + ", ".join(unknown) + " (allowed: " + ", ".join(CANONICAL_COLUMNS) + ")")
        missing = [name for name in ("transaction_date", "transaction_type", "currency") if name not in names]
        if "isin" not in names and "symbol" not in names:
            missing.append("isin/symbol")
        if missing:
            raise TransactionImportError("CSV missing required columns: " + ", ".join(missing))
        reader.fieldnames = names
        return [_canonical_row(number, {key: value for key, value in row.items() if key is not None}) for number, row in enumerate(reader, start=2)]


def _canonical_notes(record: NormalizedImportRecord) -> str:
    notes: dict[str, Any] = {"import_source": "canonical_csv", "isin": record.identifier}
    if record.realized_gain is not None:
        notes["realized_gain"] = record.realized_gain
    if record.source_notes is not None:
        notes["source_notes"] = record.source_notes
    return json.dumps(notes, sort_keys=True, separators=(",", ":"))


CANONICAL_ADAPTER = ImportAdapter(
    name="canonical", parse=parse_canonical_csv, import_type="CANONICAL_CSV_INCREMENTAL", source_label="Canonical CSV",
    external_prefix="canonical:", reconciliation_source="canonical_reconciliation", notes=_canonical_notes,
)


# ---------------------------------------------------------------------------------------------------------------------------------
# Security resolution, classification, planning
# ---------------------------------------------------------------------------------------------------------------------------------
def _resolve_security(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    wkn: Optional[str],
    holding: Optional[str],
    symbol: Optional[str] = None,
    isin: Optional[str] = None,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Resolve only deterministic, persisted security identities in precedence order."""
    rows = conn.execute("SELECT id FROM security WHERE upper(isin) = upper(?) ORDER BY id", (identifier,)).fetchall()
    security_id, resolution, error = _unique_security(rows, resolution="EXACT_ISIN")
    if security_id is not None or error is not None:
        return security_id, resolution, error

    # source_symbols is the repository's explicit persisted source mapping.
    source_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_symbols'").fetchone()
    if source_table is not None:
        rows = conn.execute("SELECT DISTINCT security_id FROM source_symbols WHERE upper(symbol) = upper(?) ORDER BY security_id", (identifier,)).fetchall()
        security_id, resolution, error = _unique_security(rows, resolution="EXACT_SOURCE_SYMBOL")
        if security_id is not None or error is not None:
            return security_id, resolution, error

    if symbol:
        rows = conn.execute("SELECT id, isin FROM security WHERE upper(symbol) = upper(?) ORDER BY id", (symbol,)).fetchall()
        if len(rows) == 1 and isin and rows[0]["isin"] and str(rows[0]["isin"]).upper() != isin.upper():
            return None, None, "AMBIGUOUS_SECURITY: symbol matches a security with a different ISIN"
        security_id, resolution, error = _unique_security(rows, resolution="EXACT_SYMBOL")
        if security_id is not None or error is not None:
            return security_id, resolution, error

    if wkn:
        rows = conn.execute("SELECT id FROM security WHERE upper(wkn) = upper(?) ORDER BY id", (wkn,)).fetchall()
        security_id, resolution, error = _unique_security(rows, resolution="EXACT_WKN")
        if security_id is not None or error is not None:
            return security_id, resolution, error

    if holding:
        normalized = _normalized_holding_name(holding)
        if normalized:
            candidates = conn.execute("SELECT id, name FROM security ORDER BY id").fetchall()
            rows = [candidate for candidate in candidates if _normalized_holding_name(candidate["name"]) == normalized]
            security_id, resolution, error = _unique_security(rows, resolution="EXACT_NORMALIZED_HOLDING_NAME")
            if security_id is not None or error is not None:
                return security_id, resolution, error
    return None, None, "UNKNOWN_SECURITY: no exact persisted ISIN, source symbol, symbol, WKN, or normalized holding-name match"


def _invalid_record(row: CanonicalRow) -> NormalizedImportRecord:
    return NormalizedImportRecord(
        source_row_number=row.row_number, transaction_date=None, transaction_type=None, identifier=None, security_id=None, shares=None,
        price=None, amount=None, fees=None, taxes=None, currency=None, broker=None, realized_gain=None, source_notes=None, external_id=None,
        classification=CLASS_INVALID, classification_reason=row.error or "invalid row", holding=row.name, asset_type=row.asset_type,
        source_values=row.source_values,
    )


def _security_key(row: CanonicalRow, pending: dict[str, dict[str, Any]]) -> str:
    """Stable key of a security that does not exist yet; one key per logical security across the rows of a file."""
    if row.isin:
        return row.isin
    if row.symbol and row.symbol in {spec["symbol"] for spec in pending.values() if spec["symbol"]}:
        return next(key for key, spec in pending.items() if spec["symbol"] == row.symbol)
    return row.symbol or row.identifier or ""


def _record_from_row(
    row: CanonicalRow,
    adapter: ImportAdapter,
    conn: sqlite3.Connection,
    latest_date: Optional[str],
    cache: dict[int, list[sqlite3.Row]],
    seen: set[str],
    pending: dict[str, dict[str, Any]],
    options: ImportOptions,
) -> NormalizedImportRecord:
    if row.error is not None:
        return _invalid_record(row)
    assert row.transaction_date and row.transaction_type and row.identifier and row.currency is not None
    shares, price, amount = float(row.shares or 0.0), float(row.price or 0.0), float(row.amount or 0.0)
    fees, taxes = float(row.fees or 0.0), float(row.taxes or 0.0)
    security_id, resolution, resolution_error = _resolve_security(
        conn, row.identifier, wkn=row.wkn, holding=row.name, symbol=row.symbol, isin=row.isin,
    )

    def build(classification: str, reason: str, *, sid: Optional[int], external_id: Optional[str], new_security: Optional[dict] = None, matched: Optional[int] = None) -> NormalizedImportRecord:
        return NormalizedImportRecord(
            source_row_number=row.row_number, transaction_date=row.transaction_date, transaction_type=row.transaction_type,
            identifier=row.identifier, security_id=sid, shares=shares, price=price, amount=amount, fees=fees, taxes=taxes,
            currency=row.currency, broker=row.broker, realized_gain=row.realized_gain, source_notes=row.notes, external_id=external_id,
            classification=classification, classification_reason=reason, holding=row.name, asset_type=row.asset_type,
            source_values=row.source_values, security_resolution=resolution, symbol=row.symbol, new_security=new_security,
            source_external_id=row.external_id, matched_transaction_id=matched,
        )

    def fingerprint(key: Any) -> str:
        return economic_fingerprint(
            security_id=key, transaction_type=row.transaction_type, transaction_date=row.transaction_date, shares=shares, price=price,
            amount=amount, fees=fees, taxes=taxes, currency=row.currency, broker=row.broker or "",
        )

    if security_id is None:
        creatable = (
            options.create_securities and resolution_error is not None and resolution_error.startswith("UNKNOWN_SECURITY")
            and bool(row.name) and bool(row.isin or row.symbol)
        )
        if not creatable:
            reason = resolution_error or "unknown security"
            if options.create_securities and reason.startswith("UNKNOWN_SECURITY") and not row.name:
                reason += "; a name is required to create the security"
            return build(CLASS_UNKNOWN_SECURITY, reason, sid=None, external_id=None)
        key = _security_key(row, pending)
        spec = pending.setdefault(key, {
            "key": key, "symbol": row.symbol, "isin": row.isin, "wkn": row.wkn, "name": row.name, "currency": row.currency,
            "asset_type": row.asset_type or "stock",
        })
        digest = fingerprint(f"new:{key}")
        if digest in seen:
            return build(CLASS_CONFLICT, "identical row occurs more than once in this file", sid=None, external_id=None, new_security=spec)
        if row.external_id and "ext:" + adapter.external_prefix + row.external_id in seen:
            return build(CLASS_CONFLICT, "external_id occurs more than once in this file", sid=None, external_id=None, new_security=spec)
        seen.add(digest)
        if row.external_id:
            seen.add("ext:" + adapter.external_prefix + row.external_id)
        historical = latest_date is not None and row.transaction_date < latest_date
        return build(CLASS_NEW_HISTORICAL if historical else CLASS_NEW, "security will be created" + ("; predates latest persisted transaction" if historical else ""),
                     sid=None, external_id=None, new_security=spec)

    digest = fingerprint(security_id)
    external_id = adapter.external_prefix + (row.external_id or digest)
    existing = cache.setdefault(security_id, _existing_for_security(conn, security_id))
    exact = [item for item in existing if economic_fingerprint(
        security_id=int(item["security_id"]), transaction_type=item["transaction_type"], transaction_date=item["transaction_date"],
        shares=float(item["shares"] or 0), price=float(item["price"] or 0), amount=float(item["amount"] or 0),
        fees=float(item["fees"] or 0), taxes=float(item["taxes"] or 0), currency=item["currency"] or "", broker=item["broker"] or "",
    ) == digest]
    external_matches = conn.execute("SELECT id FROM transactions WHERE external_id = ?", (external_id,)).fetchall()  # the unique index is global
    same_event = [item for item in existing if item["transaction_type"] == row.transaction_type and item["transaction_date"] == row.transaction_date]
    if exact:
        classification, reason = CLASS_DUPLICATE, f"economic match with transaction_id {exact[0]['id']}"
    elif external_matches:
        classification, reason = CLASS_CONFLICT, f"external_id matches transaction_id {external_matches[0]['id']} but economic values differ"
    elif row.external_id and "ext:" + external_id in seen:
        classification, reason = CLASS_CONFLICT, "external_id occurs more than once in this file"
    elif same_event:
        classification, reason = CLASS_CONFLICT, f"same security/type/datetime as transaction_id {same_event[0]['id']} but economic values differ"
    elif digest in seen:
        classification, reason = CLASS_CONFLICT, "identical row occurs more than once in this file"
    elif latest_date is not None and row.transaction_date < latest_date:
        classification, reason = CLASS_NEW_HISTORICAL, "transaction predates latest persisted transaction"
    else:
        classification, reason = CLASS_NEW, "not present and not historical"
    seen.add(digest)
    if row.external_id:
        seen.add("ext:" + external_id)
    return build(classification, reason, sid=security_id, external_id=external_id, matched=int(exact[0]["id"]) if exact else None)


def build_import_plan(
    conn: sqlite3.Connection,
    csv_path: str | Path,
    *,
    adapter: ImportAdapter | str = "parqet",
    options: Optional[ImportOptions] = None,
) -> ImportPlan:
    """Parse and classify a source file without performing writes."""
    adapter = get_adapter(adapter) if isinstance(adapter, str) else adapter
    options = options or ImportOptions()
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"{adapter.source_label} CSV not found: {path}")
    latest_date, db_token = _db_state_token(conn)
    cache: dict[int, list[sqlite3.Row]] = {}
    seen: set[str] = set()
    pending: dict[str, dict[str, Any]] = {}
    records = [_record_from_row(row, adapter, conn, latest_date, cache, seen, pending, options) for row in adapter.parse(path)]
    source_hash = _file_hash(path)
    token_payload = {
        "file_sha256": source_hash, "db_state": db_token, "adapter": adapter.name, "rows": [record.primitive() for record in records],
        "options": {"create_securities": options.create_securities, "strategies": options.strategies, "campaign_opened_at": options.campaign_opened_at},
    }
    plan_token = hashlib.sha256(json.dumps(token_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return ImportPlan(str(path), source_hash, len(records), latest_date, db_token, plan_token, tuple(records), adapter.name, options)


# ---------------------------------------------------------------------------------------------------------------------------------
# Strategy assignment and Swing campaign initialization
# ---------------------------------------------------------------------------------------------------------------------------------
def _strategy_schema_ok(conn: sqlite3.Connection) -> bool:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_assignment)")}
    return {"id", "security_id", "strategy_type", "effective_from", "effective_to"} <= columns


def _lifecycle_available(conn: sqlite3.Connection) -> bool:
    return _lifecycle_api()[1](conn)


def _replay_position(conn: sqlite3.Connection, security_id: int) -> list[dict[str, Any]]:
    """Replay all transactions of a security with the position model of :func:`_rebuild_position` (split aware, average cost)."""
    rows = conn.execute(
        """SELECT id, transaction_type, transaction_date, shares, amount, fees, taxes, currency
           FROM transactions WHERE security_id = ? ORDER BY transaction_date, id""", (security_id,),
    ).fetchall()
    actions = load_corporate_actions(conn, security_id)
    shares = remaining = 0.0
    steps: list[dict[str, Any]] = []
    for row in rows:
        kind = row["transaction_type"]
        quantity = float(row["shares"] or 0.0) * cumulative_split_factor(actions, row["transaction_date"], None)
        cost = float(row["amount"] or 0.0) + float(row["fees"] or 0.0) + float(row["taxes"] or 0.0)
        increases, decreases = kind in {"BUY", "TRANSFERIN"}, kind in {"SELL", "TRANSFEROUT"}
        if increases:
            shares += quantity
            remaining += cost
        elif decreases:
            reduction = min(quantity, shares) * (remaining / shares) if shares > 0 else 0.0
            shares = max(0.0, shares - quantity)
            remaining -= reduction
        if abs(shares) < _EPS:
            shares = remaining = 0.0
        steps.append({
            "id": int(row["id"]), "type": kind, "day": str(row["transaction_date"])[:10], "quantity": quantity, "cost": cost,
            "shares_after": shares, "remaining_after": remaining, "currency": row["currency"], "increases": increases, "decreases": decreases,
        })
    return steps


def _current_run_start(steps: list[dict[str, Any]]) -> Optional[int]:
    """Index of the transaction that opened the currently held position (first increase after the last time it was flat)."""
    start: Optional[int] = None
    previous = 0.0
    for index, step in enumerate(steps):
        if step["shares_after"] <= _EPS:
            start = None
        elif start is None and step["increases"] and previous <= _EPS:
            start = index
        previous = step["shares_after"]
    return start if steps and steps[-1]["shares_after"] > _EPS else None


def _reference(currency: Optional[str], cost_per_share: float) -> tuple[Optional[float], Optional[str]]:
    if cost_per_share > 0 and isinstance(currency, str) and _CURRENCY.match(currency):
        return cost_per_share, currency
    return None, None


def _plan_campaign(
    steps: list[dict[str, Any]], *, explicit_date: Optional[str], opener_in_file: bool, today: str,
) -> dict[str, Any]:
    """Decide whether and how a Swing campaign can be initialized without guessing.

    * Explicit start ``E``: baseline is the quantity held before day ``E`` (all trades on/after ``E`` are reconciled as events); if the
      position is opened exactly on ``E`` by one single trade, that trade is the baseline and is linked to it.
    * No explicit start: only if the transaction that opened the held position is documented by the imported file itself and is one
      single BUY on its day.  A TRANSFERIN never defines a campaign start by itself.
    """
    start = _current_run_start(steps)
    if start is None:
        return {"status": "NOT_NEEDED", "reason": "no open position"}
    opener, run_day = steps[start], steps[start]["day"]
    day_steps = [s for s in steps if s["day"] == run_day]

    def opening(reason_note: str) -> dict[str, Any]:
        cost, currency = _reference(opener["currency"], opener["cost"] / opener["quantity"] if opener["quantity"] > 0 else 0.0)
        return {"status": "CREATE", "opened_at": run_day, "original_quantity": opener["quantity"], "reference_avg_cost": cost,
                "reference_currency": currency, "baseline_transaction_id": opener["id"], "start_basis": reason_note}

    if explicit_date is None:
        if not opener_in_file:
            return {"status": "INITIALIZATION_REQUIRED", "reason": "OPENING_TRANSACTION_NOT_IN_THIS_FILE", "run_start": run_day}
        if opener["type"] == "TRANSFERIN":
            return {"status": "INITIALIZATION_REQUIRED", "reason": "TRANSFERIN_IS_NOT_A_CAMPAIGN_START", "run_start": run_day}
        if len(day_steps) != 1:
            return {"status": "INITIALIZATION_REQUIRED", "reason": "SEVERAL_TRANSACTIONS_ON_OPENING_DAY", "run_start": run_day}
        return opening("opening BUY")
    if explicit_date > today:
        return {"status": "INITIALIZATION_REQUIRED", "reason": "CAMPAIGN_START_IN_THE_FUTURE", "run_start": run_day}
    if explicit_date < run_day:
        return {"status": "INITIALIZATION_REQUIRED", "reason": "CAMPAIGN_START_BEFORE_POSITION_START", "run_start": run_day}
    if explicit_date == run_day:
        if len(day_steps) != 1:
            return {"status": "INITIALIZATION_REQUIRED", "reason": "SEVERAL_TRANSACTIONS_ON_OPENING_DAY", "run_start": run_day}
        return opening("explicit start on the opening day")
    before = [s for s in steps if s["day"] < explicit_date][-1]
    quantity = before["shares_after"]
    if quantity <= _EPS:
        return {"status": "INITIALIZATION_REQUIRED", "reason": "NO_QUANTITY_BEFORE_CAMPAIGN_START", "run_start": run_day}
    cost, currency = _reference(before["currency"], before["remaining_after"] / quantity)
    return {"status": "CREATE", "opened_at": explicit_date, "original_quantity": quantity, "reference_avg_cost": cost,
            "reference_currency": currency, "baseline_transaction_id": None, "start_basis": "explicit start (quantity held before that day)"}


def _security_label(conn: sqlite3.Connection, security_id: int) -> str:
    row = conn.execute("SELECT symbol, isin, name FROM security WHERE id = ?", (security_id,)).fetchone()
    return (row["symbol"] or row["isin"] or row["name"]) if row is not None else str(security_id)


def _active_assignment(conn: sqlite3.Connection, security_id: int, day: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """SELECT id, strategy_type, effective_from, effective_to FROM strategy_assignment
           WHERE security_id = ? AND effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)
           ORDER BY effective_from DESC, id DESC""", (security_id, day, day),
    ).fetchone()


def _ensure_assignment(
    conn: sqlite3.Connection, security_id: int, *, requested: Optional[str], steps: list[dict[str, Any]], today: str, source: str,
) -> dict[str, Any]:
    """Make sure the position has an active strategy assignment; never guess a strategy and never overlap history."""
    active = _active_assignment(conn, security_id, today)
    if active is not None:
        if requested and requested != active["strategy_type"]:
            return {"action": "conflict", "strategy": active["strategy_type"], "requested": requested, "effective_from": active["effective_from"]}
        return {"action": "existing", "strategy": active["strategy_type"], "effective_from": active["effective_from"], "id": int(active["id"])}
    if requested is None:
        return {"action": "required"}
    start = _current_run_start(steps)
    if start is None:
        return {"action": "unclear", "requested": requested, "reason": "NO_OPEN_POSITION_START"}
    effective_from = steps[start]["day"]
    overlap = conn.execute(
        "SELECT id, effective_from, effective_to FROM strategy_assignment WHERE security_id = ? AND COALESCE(effective_to, '9999-12-31') >= ?",
        (security_id, effective_from),
    ).fetchone()
    if overlap is not None:
        return {"action": "unclear", "requested": requested, "reason": f"OVERLAPS_ASSIGNMENT_{overlap['id']}"}
    cursor = conn.execute(
        "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from, effective_to, source, rationale) VALUES (?, ?, ?, NULL, ?, ?)",
        (security_id, requested, effective_from, source, f"set at import; effective from the start of the held position ({steps[start]['type']} on {effective_from})"),
    )
    return {"action": "created", "strategy": requested, "effective_from": effective_from, "id": int(cursor.lastrowid)}


def _create_campaign(conn: sqlite3.Connection, security_id: int, plan: dict[str, Any], assignment_id: int, source: str) -> int:
    rationale = f"initialized by transaction import: {plan['start_basis']}"
    cursor = conn.execute(
        """INSERT INTO swing_campaign(security_id, strategy_assignment_id, opened_at, original_quantity, reference_avg_cost,
                                      reference_currency, status, source, rationale) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (security_id, assignment_id, plan["opened_at"], plan["original_quantity"], plan["reference_avg_cost"], plan["reference_currency"], source, rationale),
    )
    campaign_id = int(cursor.lastrowid)
    conn.execute(
        """INSERT INTO swing_campaign_event(campaign_id, event_type, event_at, quantity, price, currency, transaction_id, source, notes)
           VALUES (?, 'baseline', ?, ?, ?, ?, ?, ?, ?)""",
        (campaign_id, plan["opened_at"], plan["original_quantity"], plan["reference_avg_cost"], plan["reference_currency"], plan["baseline_transaction_id"], source, rationale),
    )
    return campaign_id


def _open_campaign_id(conn: sqlite3.Connection, security_id: int) -> Optional[int]:
    row = conn.execute("SELECT id FROM swing_campaign WHERE security_id = ? AND status = 'open'", (security_id,)).fetchone()
    return int(row[0]) if row is not None else None


def _security_index(conn: sqlite3.Connection, security_ids: Iterable[int]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for security_id in sorted(set(security_ids)):
        row = conn.execute("SELECT symbol, isin FROM security WHERE id = ?", (security_id,)).fetchone()
        for key in (row["symbol"], row["isin"]):
            if key:
                index.setdefault(str(key).upper(), []).append(security_id)
    return index


def _resolve_option_keys(index: Mapping[str, list[int]], pairs: Iterable[tuple[str, str]], what: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for key, value in pairs:
        ids = sorted(set(index.get(key, [])))
        if not ids:
            raise TransactionImportError(f"{what} {key}: no security with that symbol or ISIN in this import (are all its rows skipped as historical or not importable?)")
        if len(ids) > 1:
            raise TransactionImportError(f"{what} {key}: symbol is ambiguous, use the ISIN")
        if ids[0] in result and result[ids[0]] != value:
            raise TransactionImportError(f"{what}: two keys address the same security with different values")
        result[ids[0]] = value
    return result


def _market_data_missing(conn: sqlite3.Connection, security_id: int) -> bool:
    """No current price (market_snapshot, needed for the EUR valuation) or no daily history (market_data, needed for the technical analysis)."""
    for table in ("market_snapshot", "market_data"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            continue
        if conn.execute(f"SELECT 1 FROM {table} WHERE security_id = ? LIMIT 1", (security_id,)).fetchone() is None:
            return True
    return False


def _position_shares(conn: sqlite3.Connection, security_id: int) -> float:
    row = conn.execute("SELECT shares FROM positions WHERE security_id = ?", (security_id,)).fetchone()
    return float(row[0]) if row is not None and row[0] is not None else 0.0


def _lifecycle_clean(conn: sqlite3.Connection, security_id: int) -> tuple[bool, Optional[float]]:
    derive_open_lifecycle, _, _ = _lifecycle_api()
    lifecycle = derive_open_lifecycle(conn, security_id, current_quantity=_position_shares(conn, security_id))
    delta = lifecycle.reconciliation_delta
    return lifecycle.campaign_id is not None and delta is not None and abs(delta) <= _RECONCILIATION_EPS, delta


def _initialize_strategies_and_campaigns(
    conn: sqlite3.Connection,
    *,
    candidates: list[int],
    file_transaction_ids: set[int],
    strategies: Mapping[int, str],
    campaign_dates: Mapping[int, str],
    today: str,
    adapter: ImportAdapter,
    open_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Strategy assignment and (Swing) campaign initialization for every held security; never guesses."""
    out: dict[str, Any] = {"assignments": [], "campaigns": [], "reconciliation": [], "campaign_security_ids": []}
    source = f"{adapter.name}_import"
    strategy_ok, lifecycle_ok = _strategy_schema_ok(conn), _lifecycle_available(conn)
    if not strategy_ok:
        if strategies:
            raise TransactionImportError("--strategy needs the strategy-assignment schema (Migrate-TradingStrategyAssignments.py --write)")
        return out

    def item(code: str, security_id: int, **detail: Any) -> None:
        open_items.append({"code": code, "security_id": security_id, "symbol": _security_label(conn, security_id), **detail})

    for security_id in candidates:
        if _position_shares(conn, security_id) <= _EPS:
            continue
        steps = _replay_position(conn, security_id)
        assignment = _ensure_assignment(conn, security_id, requested=strategies.get(security_id), steps=steps, today=today, source=source)
        out["assignments"].append({"security_id": security_id, "symbol": _security_label(conn, security_id), **assignment})
        action = assignment["action"]
        if action == "required":
            item(STRATEGY_ASSIGNMENT_REQUIRED, security_id, detail="no active strategy assignment; pass --strategy SYMBOL=swing|long_term")
            continue
        if action == "conflict":
            item(STRATEGY_CONFLICT, security_id, detail=f"active assignment is {assignment['strategy']}, --strategy asked for {assignment['requested']}")
            continue
        if action == "unclear":
            item(STRATEGY_EFFECTIVE_FROM_UNCLEAR, security_id, detail=assignment["reason"])
            continue
        strategy = assignment["strategy"]
        explicit = campaign_dates.get(security_id)
        if strategy != "swing":
            if explicit:
                item(CAMPAIGN_START_IGNORED, security_id, detail=f"--campaign-opened-at ignored: the strategy is {strategy}")
            continue
        if not lifecycle_ok:
            continue
        if _open_campaign_id(conn, security_id) is not None:
            continue  # existing campaign: reconciled below, never recreated
        start_index = _current_run_start(steps)
        opener_in_file = start_index is not None and steps[start_index]["id"] in file_transaction_ids
        plan = _plan_campaign(steps, explicit_date=explicit, opener_in_file=opener_in_file, today=today)
        if plan["status"] == "INITIALIZATION_REQUIRED":
            item(CAMPAIGN_INITIALIZATION_REQUIRED, security_id, detail=plan["reason"], run_start=plan.get("run_start"),
                 hint="--campaign-opened-at SYMBOL=YYYY-MM-DD")
            continue
        if plan["status"] != "CREATE":
            continue
        if assignment["effective_from"] > plan["opened_at"]:
            item(CAMPAIGN_INITIALIZATION_REQUIRED, security_id, detail="ASSIGNMENT_STARTS_AFTER_CAMPAIGN_START")
            continue
        conn.execute("SAVEPOINT campaign_init")
        try:
            campaign_id = _create_campaign(conn, security_id, plan, int(assignment["id"]), source)
            reports = reconcile_campaign_transactions(conn, [security_id], write=True, source=adapter.reconciliation_source)
            report = reports[0] if reports else None
            clean, delta = _lifecycle_clean(conn, security_id)
            if report is None or report["manual_review_required"] or report["status"] == "AMBIGUOUS" or not clean:
                reasons = [r["reason"] for r in (report or {}).get("manual_review_required", [])] or [f"RECONCILIATION_DELTA_{delta}"]
                raise TransactionImportError("campaign does not reconcile: " + ", ".join(sorted(set(str(r) for r in reasons))))
        except (sqlite3.Error, TransactionImportError) as exc:
            conn.execute("ROLLBACK TO SAVEPOINT campaign_init")
            conn.execute("RELEASE SAVEPOINT campaign_init")
            item(CAMPAIGN_INITIALIZATION_REQUIRED, security_id, detail=str(exc), hint="--campaign-opened-at SYMBOL=YYYY-MM-DD")
            continue
        conn.execute("RELEASE SAVEPOINT campaign_init")
        out["campaigns"].append({"security_id": security_id, "symbol": _security_label(conn, security_id), "campaign_id": campaign_id,
                                 "opened_at": plan["opened_at"], "original_quantity": plan["original_quantity"],
                                 "reference_avg_cost": plan["reference_avg_cost"], "reference_currency": plan["reference_currency"],
                                 "start_basis": plan["start_basis"]})
        out["reconciliation"].append(report)
        out["campaign_security_ids"].append(security_id)
    return out



# ---------------------------------------------------------------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------------------------------------------------------------
_SECURITY_COLUMNS = ("symbol", "isin", "wkn", "name", "currency", "asset_type", "active")


def _create_security(conn: sqlite3.Connection, spec: Mapping[str, Any]) -> int:
    available = {row[1] for row in conn.execute("PRAGMA table_info(security)")}
    values = {**spec, "active": 1}
    columns = [name for name in _SECURITY_COLUMNS if name in available and values.get(name) is not None]
    cursor = conn.execute(
        f"INSERT INTO security({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", [values[name] for name in columns],
    )
    return int(cursor.lastrowid)


def _summary(
    plan: ImportPlan, approved: list[NormalizedImportRecord], inserted: list[int], include_historical: bool,
    created: list[dict[str, Any]], new_positions: int, phase: Mapping[str, Any], reconciliation: list[dict[str, Any]],
    open_items: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = plan.counts

    def n(code: str) -> int:
        return sum(item["code"] == code for item in open_items)

    return {
        "records_total": plan.source_row_count,
        "inserted": len(inserted),
        "duplicates": counts[CLASS_DUPLICATE.lower()],
        "historical_skipped": 0 if include_historical else counts[CLASS_NEW_HISTORICAL.lower()],
        "historical_inserted": sum(r.classification == CLASS_NEW_HISTORICAL for r in approved),
        "conflicts": counts[CLASS_CONFLICT.lower()],
        "failed": counts[CLASS_INVALID.lower()] + counts[CLASS_UNKNOWN_SECURITY.lower()],
        "new_securities": len(created),
        "new_positions": new_positions,
        "strategy_assignments_created": sum(a["action"] == "created" for a in phase["assignments"]),
        "strategy_required": n(STRATEGY_ASSIGNMENT_REQUIRED),
        "campaigns_created": len(phase["campaigns"]),
        "campaigns_reconciled": sum(bool(report.get("applied")) for report in reconciliation),
        "campaign_initialization_required": n(CAMPAIGN_INITIALIZATION_REQUIRED),
        "validation_status": "COMPLETE" if not open_items else "INCOMPLETE",
    }


def apply_import_plan(
    conn: sqlite3.Connection,
    plan: ImportPlan,
    *,
    expected_plan_token: str,
    include_historical: bool = False,
    create_backup: bool = False,
    backup_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply approved NEW rows (and opted-in NEW_HISTORICAL rows), then initialize strategies and campaigns - in one transaction.

    The caller must rebuild a fresh plan immediately before this call.  The token guards source-file, options and relevant
    transaction-state drift.  If nothing changes, the transaction is rolled back (no backup, no ``imports`` row).
    """
    adapter, options = get_adapter(plan.adapter_name), plan.options
    fresh = build_import_plan(conn, plan.source_file, adapter=adapter, options=options)
    if expected_plan_token != plan.plan_token or fresh.plan_token != expected_plan_token:
        raise TransactionImportError("import plan is stale; preview again before writing")

    def approved_of(p: ImportPlan) -> list[NormalizedImportRecord]:
        return [r for r in p.records if r.classification == CLASS_NEW or (include_historical and r.classification == CLASS_NEW_HISTORICAL)]

    backup: Optional[Path] = None
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        # Rebuild while holding the write lock. This closes the gap between a preview and its apply transaction: neither a changed
        # file nor changed relevant transaction state can be imported under an old plan token.
        locked = build_import_plan(conn, plan.source_file, adapter=adapter, options=options)
        if locked.plan_token != expected_plan_token:
            raise TransactionImportError("import plan is stale; preview again before writing")
        fresh = locked
        approved = approved_of(fresh)
        # Rows that cannot be imported do not turn valid unrelated rows into silent writes; the result records them.
        blocked_rows = [r.source_row_number for r in fresh.records if r.classification in _BLOCKING_CLASSES]
        today = options.as_of_date or datetime.now(timezone.utc).date().isoformat()
        before = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        record_ids = sorted({r.security_id for r in fresh.records if r.security_id is not None})
        shares_before = {security_id: _position_shares(conn, security_id) for security_id in record_ids}

        created: list[dict[str, Any]] = []
        key_to_id: dict[str, int] = {}
        for record in approved:
            spec = record.new_security
            if spec is not None and spec["key"] not in key_to_id:
                security_id = _create_security(conn, spec)
                key_to_id[spec["key"]] = security_id
                created.append({"security_id": security_id, "symbol": spec["symbol"], "isin": spec["isin"], "name": spec["name"], "currency": spec["currency"]})

        inserted: list[int] = []
        finals: list[NormalizedImportRecord] = []
        for record in approved:
            security_id = record.security_id if record.security_id is not None else key_to_id[record.new_security["key"]]
            external_id = record.external_id
            if record.security_id is None:
                digest = economic_fingerprint(
                    security_id=security_id, transaction_type=record.transaction_type, transaction_date=record.transaction_date,
                    shares=record.shares, price=record.price, amount=record.amount, fees=record.fees, taxes=record.taxes,
                    currency=record.currency, broker=record.broker or "",
                )
                external_id = adapter.external_prefix + (record.source_external_id or digest)
            final = replace(record, security_id=security_id, external_id=external_id)
            cursor = conn.execute(
                """INSERT INTO transactions(security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker, external_id, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (final.security_id, final.transaction_type, final.transaction_date, final.shares, final.price, final.amount, final.fees,
                 final.taxes, final.currency, final.broker, final.external_id, adapter.notes(final)),
            )
            inserted.append(int(cursor.lastrowid))
            finals.append(replace(final, matched_transaction_id=int(cursor.lastrowid)))
        affected = sorted({int(r.security_id) for r in finals})
        for security_id in affected:
            _rebuild_position(conn, security_id, finals)

        # Strategy assignment and campaign initialization for every held security the file documents.
        candidates = sorted(set(record_ids) | set(key_to_id.values()))
        index = _security_index(conn, candidates)
        strategies = _resolve_option_keys(index, options.strategies, "--strategy")
        campaign_dates = _resolve_option_keys(index, options.campaign_opened_at, "--campaign-opened-at")
        file_transaction_ids = {r.matched_transaction_id for r in [*fresh.records, *finals] if r.matched_transaction_id is not None}
        open_items: list[dict[str, Any]] = []
        phase = _initialize_strategies_and_campaigns(
            conn, candidates=candidates, file_transaction_ids=file_transaction_ids, strategies=strategies, campaign_dates=campaign_dates,
            today=today, adapter=adapter, open_items=open_items,
        )
        reconcile_ids = sorted(set(affected) | set(phase["campaign_security_ids"]))
        remaining_ids = [security_id for security_id in reconcile_ids if security_id not in phase["campaign_security_ids"]]
        reconciliation = [*phase["reconciliation"], *reconcile_campaign_transactions(conn, remaining_ids, write=True, source=adapter.reconciliation_source)]

        audit = _duplicate_audit(conn)
        if audit["exact_economic_duplicates"] or audit["duplicate_external_ids"]:
            raise TransactionImportError("global duplicate audit failed after insert")
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        if integrity != ["ok"] or foreign_keys:
            raise TransactionImportError("integrity or foreign-key validation failed")

        # What is still missing before the orchestrator can use the imported positions.
        flagged = {(item["code"], item["security_id"]) for item in open_items}
        for report in reconciliation:
            if report["status"] in {"MANUAL_REVIEW_REQUIRED", "AMBIGUOUS"} and (CAMPAIGN_RECONCILIATION_REQUIRED, report["security_id"]) not in flagged:
                open_items.append({"code": CAMPAIGN_RECONCILIATION_REQUIRED, "security_id": report["security_id"], "symbol": _security_label(conn, report["security_id"]),
                                   "status": report["status"], "detail": [r["reason"] for r in report["manual_review_required"]] or report["ambiguous"]})
                flagged.add((CAMPAIGN_RECONCILIATION_REQUIRED, report["security_id"]))
        if _lifecycle_available(conn):
            for security_id in candidates:
                if _position_shares(conn, security_id) > _EPS and _open_campaign_id(conn, security_id) is not None and (CAMPAIGN_RECONCILIATION_REQUIRED, security_id) not in flagged:
                    clean, delta = _lifecycle_clean(conn, security_id)
                    if not clean:
                        open_items.append({"code": CAMPAIGN_RECONCILIATION_REQUIRED, "security_id": security_id, "symbol": _security_label(conn, security_id),
                                           "detail": f"campaign quantity differs from the position by {delta}"})
        for security_id in candidates:
            if _position_shares(conn, security_id) > _EPS and _market_data_missing(conn, security_id):
                open_items.append({"code": MARKET_DATA_MISSING, "security_id": security_id, "symbol": _security_label(conn, security_id),
                                   "detail": "no market data yet; run Resolve-TradingSecurities.py and Backfill-TradingMarketData.py"})
        if blocked_rows:
            open_items.append({"code": ROWS_BLOCKED, "security_id": None, "symbol": None, "detail": f"{len(blocked_rows)} row(s) not importable (conflict, unknown security or invalid)", "rows": blocked_rows[:50]})
        skipped_historical = [r.source_row_number for r in fresh.records if r.classification == CLASS_NEW_HISTORICAL and not include_historical]
        if skipped_historical:
            open_items.append({"code": HISTORICAL_ROWS_NOT_IMPORTED, "security_id": None, "symbol": None, "detail": f"{len(skipped_historical)} historical row(s) skipped; use --include-historical", "rows": skipped_historical[:50]})

        lifecycle = _lifecycle_report(conn, reconcile_ids, finals, reconciliation)
        after = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        new_positions = sum(
            1 for security_id in candidates if shares_before.get(security_id, 0.0) <= _EPS and _position_shares(conn, security_id) > _EPS
        )
        summary = _summary(fresh, approved, inserted, include_historical, created, new_positions, phase, reconciliation, open_items)
        changed = bool(
            inserted or created or summary["strategy_assignments_created"] or phase["campaigns"]
            or any(report.get("applied") for report in reconciliation)
        )
        if changed:
            if create_backup:
                backup = _write_backup(conn, f"{adapter.name}-import", backup_path)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO imports(import_type, source, file_name, started_at, completed_at, records_total, records_imported, records_failed, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (adapter.import_type, adapter.source_label, Path(fresh.source_file).name, now, now, fresh.source_row_count, len(inserted),
                 summary["failed"], "COMPLETED", json.dumps({"plan_token": fresh.plan_token, "blocked_rows": blocked_rows, "summary": summary}, sort_keys=True)),
            )
            conn.execute("COMMIT")
        else:
            conn.execute("ROLLBACK")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return {
        "written": changed and not dry_run, "dry_run": dry_run, "would_write": changed,
        "inserted_transaction_ids": inserted,
        "transaction_count_before": before if changed else None, "transaction_count_after": after if changed else None,
        "backup_path": str(backup) if backup else None, "blocked_row_numbers": blocked_rows,
        "lifecycle": lifecycle if changed else [],
        "validation": {"integrity_check": integrity, "foreign_key_check": foreign_keys, **audit} if changed else None,
        "plan_token": fresh.plan_token,
        "summary": summary, "created_securities": created, "strategy_assignments": phase["assignments"],
        "campaigns_created": phase["campaigns"], "campaign_reconciliation": reconciliation, "open_items": open_items,
    }


def preview_import(
    conn: sqlite3.Connection,
    csv_path: str | Path,
    *,
    adapter: ImportAdapter | str = "parqet",
    options: Optional[ImportOptions] = None,
    include_historical: bool = False,
) -> dict[str, Any]:
    """Complete dry run: the full execution path on an in-memory copy; the real database is only read.

    The returned plan is that of the real database; ``planned`` is the end state (transactions, positions, strategy assignments,
    campaigns, reconciliation, remaining open items) a write with the same arguments would produce.
    """
    adapter = get_adapter(adapter) if isinstance(adapter, str) else adapter
    plan = build_import_plan(conn, csv_path, adapter=adapter, options=options)
    copy = sqlite3.connect(":memory:", isolation_level=None)
    copy.row_factory = sqlite3.Row
    try:
        conn.backup(copy)
        copy_plan = build_import_plan(copy, csv_path, adapter=adapter, options=options)
        planned = apply_import_plan(copy, copy_plan, expected_plan_token=copy_plan.plan_token, include_historical=include_historical, dry_run=True)
    finally:
        copy.close()
    return {**plan.primitive(), "dry_run": True, "planned": planned}

