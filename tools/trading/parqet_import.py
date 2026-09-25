"""Safe, incremental Parqet CSV import planning and execution.

This module deliberately separates parsing/classification from writes.  A
caller first builds an :class:`ImportPlan`, presents its JSON-friendly form to
the user, and then applies that exact plan token.  Generic portfolio trades
are never interpreted as Swing lifecycle events.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

from corporate_actions import cumulative_split_factor, load_corporate_actions


DEFAULT_DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
IMPORT_SOURCE = "Parqet"
NUMERIC_SCALE = Decimal("0.00000001")

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
_TYPE_MAP = {
    "buy": "BUY", "kauf": "BUY", "purchase": "BUY",
    "sell": "SELL", "verkauf": "SELL", "sale": "SELL",
    "dividend": "DIVIDEND", "dividende": "DIVIDEND",
    "cost": "COST", "kosten": "COST", "fee": "COST", "gebuhr": "COST",
    "transferin": "TRANSFERIN", "transfer in": "TRANSFERIN",
    "einlieferung": "TRANSFERIN", "einbuchung": "TRANSFERIN",
    "transferout": "TRANSFEROUT", "transfer out": "TRANSFEROUT",
    "auslieferung": "TRANSFEROUT", "ausbuchung": "TRANSFEROUT",
}
_HEADER_ALIASES = {
    "datetime": ("datetime", "date_time", "zeitpunkt"),
    "date": ("date", "datum"),
    "time": ("time", "uhrzeit"),
    "type": ("type", "typ", "transactiontype"),
    # Current exports carry both `holding` (often blank) and `holdingname`.
    # Prefer the actual display name deterministically.
    "holding": ("holdingname", "holding", "wertpapier", "security"),
    "identifier": ("identifier", "isin", "kennung"),
    "wkn": ("wkn",),
    "shares": ("shares", "anteile", "stuck", "quantity"),
    "price": ("price", "preis"),
    "amount": ("amount", "betrag"),
    "fee": ("fee", "fees", "gebuhr", "gebuehr"),
    "tax": ("tax", "taxes", "steuer", "steuern"),
    "realizedgains": ("realizedgains", "realizedgain", "realisiertergewinn"),
    "currency": ("currency", "wahrung", "waehrung"),
    "broker": ("broker",),
    "assettype": ("assettype", "asset_type", "anlageklasse"),
    "notes": ("notes", "notizen", "note"),
}


class ParqetImportError(ValueError):
    """Raised when a requested import cannot safely proceed."""


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


def _header_key(value: str) -> str:
    return "".join(char for char in (value or "").casefold() if char.isalnum())


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _decimal(value: Any, field_name: str, *, required: bool = True) -> Optional[Decimal]:
    text = _text(value)
    if text is None:
        if required:
            raise ParqetImportError(f"missing {field_name}")
        return Decimal("0")
    normalized = text.replace(" ", "").replace("\u00a0", "")
    if "," in normalized and "." in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ParqetImportError(f"invalid {field_name}: {text!r}") from exc


def _number(value: Any, field_name: str, *, required: bool = True) -> Optional[float]:
    decimal = _decimal(value, field_name, required=required)
    return None if decimal is None else float(decimal)


def _canonical_number(value: Optional[float]) -> str:
    if value is None:
        return ""
    return format(Decimal(str(value)).quantize(NUMERIC_SCALE), "f")


def _normalize_datetime(value: str) -> str:
    text = value.strip()
    if not text:
        raise ParqetImportError("missing datetime")
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            raise ParqetImportError(f"invalid datetime: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def _csv_headers(fieldnames: Iterable[str] | None) -> dict[str, str]:
    if not fieldnames:
        raise ParqetImportError("CSV has no header row")
    indexed = {_header_key(name): name for name in fieldnames if name}
    result: dict[str, str] = {}
    for canonical, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if _header_key(alias) in indexed:
                result[canonical] = indexed[_header_key(alias)]
                break
    missing = [field for field in ("type", "identifier", "shares", "price", "amount", "currency", "broker") if field not in result]
    if "datetime" not in result and "date" not in result:
        missing.append("datetime/date")
    if missing:
        raise ParqetImportError("CSV missing required columns: " + ", ".join(missing))
    return result


def _value(row: dict[str, str], headers: dict[str, str], name: str) -> Optional[str]:
    header = headers.get(name)
    return _text(row.get(header)) if header else None


def _normalized_type(value: Optional[str]) -> str:
    key = " ".join((value or "").casefold().replace("_", " ").split())
    if key not in _TYPE_MAP:
        raise ParqetImportError(f"unsupported transaction type: {value!r}")
    return _TYPE_MAP[key]


def economic_fingerprint(
    *, security_id: int, transaction_type: str, transaction_date: str,
    shares: float, price: float, amount: float, fees: float, taxes: float,
    currency: str, broker: str,
) -> str:
    """Return the deterministic economic identity used for idempotency."""
    payload = {
        "security_id": int(security_id), "transaction_type": transaction_type.upper(),
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


def _resolve_security(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    wkn: Optional[str],
    holding: Optional[str],
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
    return None, None, "UNKNOWN_SECURITY: no exact persisted ISIN, source symbol, WKN, or normalized holding-name match"


def _existing_for_security(conn: sqlite3.Connection, security_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, security_id, transaction_type, transaction_date, shares, price,
                  amount, fees, taxes, currency, broker, external_id
             FROM transactions WHERE security_id = ?""", (security_id,)
    ).fetchall()


def _record_from_row(
    row_number: int, row: dict[str, str], headers: dict[str, str], conn: sqlite3.Connection,
    latest_date: Optional[str], cache: dict[int, list[sqlite3.Row]],
) -> NormalizedImportRecord:
    source_values = {key: value for key, value in row.items() if value not in (None, "")}
    try:
        datetime_text = _value(row, headers, "datetime")
        if datetime_text is None:
            date_part = _value(row, headers, "date")
            time_part = _value(row, headers, "time") or "00:00:00"
            datetime_text = f"{date_part} {time_part}" if date_part else ""
        transaction_date = _normalize_datetime(datetime_text)
        transaction_type = _normalized_type(_value(row, headers, "type"))
        identifier = (_value(row, headers, "identifier") or "").upper()
        if not identifier:
            raise ParqetImportError("missing identifier")
        shares = _number(_value(row, headers, "shares"), "shares")
        price = _number(_value(row, headers, "price"), "price")
        amount = _number(_value(row, headers, "amount"), "amount")
        fees = _number(_value(row, headers, "fee"), "fee", required=False) or 0.0
        taxes = _number(_value(row, headers, "tax"), "tax", required=False) or 0.0
        realized_gain = _number(_value(row, headers, "realizedgains"), "realizedgains", required=False)
        currency = (_value(row, headers, "currency") or "").upper()
        broker = _value(row, headers, "broker")
        if not currency:
            raise ParqetImportError("missing currency")
        if transaction_type not in {"TRANSFERIN", "TRANSFEROUT"} and not broker:
            raise ParqetImportError("missing broker")
        if shares < 0 or price < 0 or amount < 0 or fees < 0 or taxes < 0:
            raise ParqetImportError("negative monetary or share values are not supported")
    except ParqetImportError as exc:
        return NormalizedImportRecord(
            source_row_number=row_number,
            transaction_date=None,
            transaction_type=None,
            identifier=None,
            security_id=None,
            shares=None,
            price=None,
            amount=None,
            fees=None,
            taxes=None,
            currency=None,
            broker=None,
            realized_gain=None,
            source_notes=None,
            external_id=None,
            classification=CLASS_INVALID,
            classification_reason=str(exc),
            holding=_value(row, headers, "holding"),
            asset_type=_value(row, headers, "assettype"),
            source_values=source_values,
        )

    holding = _value(row, headers, "holding")
    wkn = _value(row, headers, "wkn")
    security_id, resolution, resolution_error = _resolve_security(conn, identifier, wkn=wkn, holding=holding)
    if security_id is None:
        classification = CLASS_UNKNOWN_SECURITY
        return NormalizedImportRecord(row_number, transaction_date, transaction_type, identifier, None, shares, price, amount, fees, taxes, currency, broker, realized_gain, _value(row, headers, "notes"), None, classification, resolution_error or "unknown security", holding, _value(row, headers, "assettype"), source_values, resolution)

    fingerprint = economic_fingerprint(security_id=security_id, transaction_type=transaction_type, transaction_date=transaction_date, shares=shares, price=price, amount=amount, fees=fees, taxes=taxes, currency=currency, broker=broker)
    external_id = f"parqet:{fingerprint}"
    existing = cache.setdefault(security_id, _existing_for_security(conn, security_id))
    exact = [item for item in existing if economic_fingerprint(
        security_id=int(item["security_id"]), transaction_type=item["transaction_type"], transaction_date=item["transaction_date"],
        shares=float(item["shares"] or 0), price=float(item["price"] or 0), amount=float(item["amount"] or 0),
        fees=float(item["fees"] or 0), taxes=float(item["taxes"] or 0), currency=item["currency"] or "", broker=item["broker"] or "",
    ) == fingerprint]
    external_matches = [item for item in existing if item["external_id"] == external_id]
    same_event = [item for item in existing if item["transaction_type"] == transaction_type and item["transaction_date"] == transaction_date]
    if exact:
        classification, reason = CLASS_DUPLICATE, f"economic match with transaction_id {exact[0]['id']}"
    elif external_matches:
        classification, reason = CLASS_CONFLICT, f"external_id matches transaction_id {external_matches[0]['id']} but economic values differ"
    elif same_event:
        classification, reason = CLASS_CONFLICT, f"same security/type/datetime as transaction_id {same_event[0]['id']} but economic values differ"
    elif latest_date is not None and transaction_date < latest_date:
        classification, reason = CLASS_NEW_HISTORICAL, "transaction predates latest persisted transaction"
    else:
        classification, reason = CLASS_NEW, "not present and not historical"
    return NormalizedImportRecord(row_number, transaction_date, transaction_type, identifier, security_id, shares, price, amount, fees, taxes, currency, broker, realized_gain, _value(row, headers, "notes"), external_id, classification, reason, holding, _value(row, headers, "assettype"), source_values, resolution)


def build_import_plan(conn: sqlite3.Connection, csv_path: str | Path) -> ImportPlan:
    """Parse and classify a CSV without performing writes."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"Parqet CSV not found: {path}")
    latest_date, db_token = _db_state_token(conn)
    records: list[NormalizedImportRecord] = []
    cache: dict[int, list[sqlite3.Row]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        headers = _csv_headers(reader.fieldnames)
        for row_number, row in enumerate(reader, start=2):
            records.append(_record_from_row(row_number, row, headers, conn, latest_date, cache))
    source_hash = _file_hash(path)
    token_payload = {"file_sha256": source_hash, "db_state": db_token, "rows": [record.primitive() for record in records]}
    plan_token = hashlib.sha256(json.dumps(token_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return ImportPlan(str(path), source_hash, len(records), latest_date, db_token, plan_token, tuple(records))


def _notes_for(record: NormalizedImportRecord) -> str:
    notes: dict[str, Any] = {"import_source": "parqet_incremental", "isin": record.identifier}
    if record.realized_gain is not None:
        notes["parqet_realizedgains"] = record.realized_gain
    if record.source_notes is not None:
        notes["parqet_source_notes"] = record.source_notes
    return json.dumps(notes, sort_keys=True, separators=(",", ":"))


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


def _lifecycle_report(conn: sqlite3.Connection, security_ids: Iterable[int], records: Iterable[NormalizedImportRecord]) -> list[dict[str, Any]]:
    try:
        from swing_lifecycle import derive_open_lifecycle, lifecycle_schema_available, lifecycle_to_primitive
    except ImportError:  # module can also be imported as tools.trading.parqet_import
        from tools.trading.swing_lifecycle import derive_open_lifecycle, lifecycle_schema_available, lifecycle_to_primitive
    if not lifecycle_schema_available(conn):
        return []
    by_security: dict[int, list[NormalizedImportRecord]] = {}
    for record in records:
        if record.security_id is not None:
            by_security.setdefault(record.security_id, []).append(record)
    report: list[dict[str, Any]] = []
    for security_id in sorted(set(security_ids)):
        position = conn.execute("SELECT shares FROM positions WHERE security_id = ?", (security_id,)).fetchone()
        current_quantity = float(position[0]) if position else None
        lifecycle = derive_open_lifecycle(conn, security_id, current_quantity=current_quantity)
        if lifecycle.campaign_id is None:
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
        item["flags"] = flags
        item["lifecycle_reconciliation_required"] = lifecycle.reconciliation_delta not in (None, 0.0)
        report.append(item)
    return report


def _duplicate_audit(conn: sqlite3.Connection) -> dict[str, list[Any]]:
    economic: dict[str, list[int]] = {}
    for row in conn.execute("SELECT id, security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker FROM transactions"):
        key = economic_fingerprint(security_id=int(row["security_id"]), transaction_type=row["transaction_type"], transaction_date=row["transaction_date"], shares=float(row["shares"] or 0), price=float(row["price"] or 0), amount=float(row["amount"] or 0), fees=float(row["fees"] or 0), taxes=float(row["taxes"] or 0), currency=row["currency"] or "", broker=row["broker"] or "")
        economic.setdefault(key, []).append(int(row["id"]))
    external = conn.execute("SELECT external_id, GROUP_CONCAT(id) FROM transactions WHERE external_id IS NOT NULL GROUP BY external_id HAVING COUNT(*) > 1").fetchall()
    return {"exact_economic_duplicates": [ids for ids in economic.values() if len(ids) > 1], "duplicate_external_ids": [tuple(row) for row in external]}


def apply_import_plan(conn: sqlite3.Connection, plan: ImportPlan, *, expected_plan_token: str, include_historical: bool = False, create_backup: bool = False, backup_path: str | Path | None = None) -> dict[str, Any]:
    """Apply only approved NEW rows (and opted-in NEW_HISTORICAL rows).

    The caller must rebuild a fresh plan immediately before this call.  The
    token guards both source-file and relevant transaction-state drift.
    """
    fresh = build_import_plan(conn, plan.source_file)
    if expected_plan_token != plan.plan_token or fresh.plan_token != expected_plan_token:
        raise ParqetImportError("import plan is stale; preview again before writing")
    approved = [r for r in fresh.records if r.classification == CLASS_NEW or (include_historical and r.classification == CLASS_NEW_HISTORICAL)]
    if any(r.classification in {CLASS_CONFLICT, CLASS_UNKNOWN_SECURITY, CLASS_INVALID} for r in fresh.records):
        # They are not importable, but do not turn valid unrelated rows into
        # silent writes.  The result explicitly records that they were skipped.
        blocked_rows = [r.source_row_number for r in fresh.records if r.classification in {CLASS_CONFLICT, CLASS_UNKNOWN_SECURITY, CLASS_INVALID}]
    else:
        blocked_rows = []
    if not approved:
        return {"written": False, "inserted_transaction_ids": [], "transaction_count_before": None, "transaction_count_after": None, "backup_path": None, "blocked_row_numbers": blocked_rows, "lifecycle": [], "validation": None, "plan_token": fresh.plan_token}

    backup: Optional[Path] = None
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        # Rebuild while holding the write lock. This closes the gap between a
        # preview and its apply transaction: neither a changed CSV nor changed
        # relevant transaction state can be imported under an old plan token.
        locked = build_import_plan(conn, plan.source_file)
        if locked.plan_token != expected_plan_token:
            raise ParqetImportError("import plan is stale; preview again before writing")
        fresh = locked
        approved = [r for r in fresh.records if r.classification == CLASS_NEW or (include_historical and r.classification == CLASS_NEW_HISTORICAL)]
        if create_backup:
            db_row = conn.execute("PRAGMA database_list").fetchone()
            db_path = Path(db_row[2])
            backup = Path(backup_path) if backup_path is not None else db_path.with_name(f"{db_path.name}.parqet-import-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak")
            if backup.exists():
                raise ParqetImportError(f"refusing to overwrite backup: {backup}")
            # Use a separate read-only connection so the backup contains the
            # fully locked, still pre-write state without the pending import.
            source = sqlite3.connect(f"file:///{db_path.as_posix()}?mode=ro", uri=True)
            destination = sqlite3.connect(str(backup))
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        before = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        inserted: list[int] = []
        for record in approved:
            cursor = conn.execute("""INSERT INTO transactions(security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker, external_id, notes)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (record.security_id, record.transaction_type, record.transaction_date, record.shares, record.price, record.amount, record.fees, record.taxes, record.currency, record.broker, record.external_id, _notes_for(record)))
            inserted.append(int(cursor.lastrowid))
        affected = sorted({int(r.security_id) for r in approved if r.security_id is not None})
        for security_id in affected:
            _rebuild_position(conn, security_id, approved)
        audit = _duplicate_audit(conn)
        if audit["exact_economic_duplicates"] or audit["duplicate_external_ids"]:
            raise ParqetImportError("global duplicate audit failed after insert")
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        if integrity != ["ok"] or foreign_keys:
            raise ParqetImportError("integrity or foreign-key validation failed")
        lifecycle = _lifecycle_report(conn, affected, approved)
        after = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        conn.execute("INSERT INTO imports(import_type, source, file_name, started_at, completed_at, records_total, records_imported, records_failed, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("PARQET_CSV_INCREMENTAL", IMPORT_SOURCE, Path(fresh.source_file).name, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), fresh.source_row_count, len(inserted), fresh.source_row_count - len(inserted), "COMPLETED", json.dumps({"plan_token": fresh.plan_token, "blocked_rows": blocked_rows}, sort_keys=True)))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return {"written": True, "inserted_transaction_ids": inserted, "transaction_count_before": before, "transaction_count_after": after, "backup_path": str(backup) if backup else None, "blocked_row_numbers": blocked_rows, "lifecycle": lifecycle, "validation": {"integrity_check": integrity, "foreign_key_check": foreign_keys, **audit}, "plan_token": fresh.plan_token}
