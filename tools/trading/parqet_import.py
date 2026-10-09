"""Parqet source adapter for the provider-neutral transaction import.

Only the Parqet specifics live here: the semicolon CSV with its German/English column aliases, decimal comma, the type vocabulary and
the Parqet notes keys.  A Parqet row is mapped onto the canonical model (:class:`transaction_import.CanonicalRow`); planning, security
resolution, duplicate/historical handling, positions, strategy, campaigns and reconciliation are the generic code of
:mod:`transaction_import`.

The names below that are re-exported from :mod:`transaction_import` keep existing callers (and the incremental Parqet workflow with
cumulative exports) working unchanged.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

from transaction_import import (  # noqa: F401  (re-exports for backward compatibility)
    CLASS_CONFLICT,
    CLASS_DUPLICATE,
    CLASS_INVALID,
    CLASS_NEW,
    CLASS_NEW_HISTORICAL,
    CLASS_UNKNOWN_SECURITY,
    DEFAULT_DB_PATH,
    LEGACY_RECONCILIATION_SOURCE,
    CanonicalRow,
    ImportAdapter,
    ImportOptions,
    ImportPlan,
    NormalizedImportRecord,
    TransactionImportError,
    _normalize_datetime,
    _rebuild_position,
    _text,
    apply_campaign_reconciliation,
    apply_import_plan,
    economic_fingerprint,
    rebuild_position,
    reconcile_campaign_transactions,
)
from transaction_import import build_import_plan as _build_import_plan


IMPORT_SOURCE = "Parqet"
RECONCILIATION_SOURCE = LEGACY_RECONCILIATION_SOURCE
ParqetImportError = TransactionImportError

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
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def _header_key(value: str) -> str:
    return "".join(char for char in (value or "").casefold() if char.isalnum())


def _decimal(value: Any, field_name: str, *, required: bool = True) -> Optional[Decimal]:
    text = _text(value)
    if text is None:
        if required:
            raise TransactionImportError(f"missing {field_name}")
        return Decimal("0")
    normalized = text.replace(" ", "").replace(" ", "")
    if "," in normalized and "." in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise TransactionImportError(f"invalid {field_name}: {text!r}") from exc


def _number(value: Any, field_name: str, *, required: bool = True) -> Optional[float]:
    decimal = _decimal(value, field_name, required=required)
    return None if decimal is None else float(decimal)


def _csv_headers(fieldnames: Iterable[str] | None) -> dict[str, str]:
    if not fieldnames:
        raise TransactionImportError("CSV has no header row")
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
        raise TransactionImportError("CSV missing required columns: " + ", ".join(missing))
    return result


def _value(row: dict[str, str], headers: dict[str, str], name: str) -> Optional[str]:
    header = headers.get(name)
    return _text(row.get(header)) if header else None


def _normalized_type(value: Optional[str]) -> str:
    key = " ".join((value or "").casefold().replace("_", " ").split())
    if key not in _TYPE_MAP:
        raise TransactionImportError(f"unsupported transaction type: {value!r}")
    return _TYPE_MAP[key]


def _canonical_row(row_number: int, row: dict[str, str], headers: dict[str, str]) -> CanonicalRow:
    """Map one Parqet row onto the canonical model (same field rules the Parqet importer always had)."""
    source_values = {key: value for key, value in row.items() if value not in (None, "")}
    holding = _value(row, headers, "holding")
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
            raise TransactionImportError("missing identifier")
        shares = _number(_value(row, headers, "shares"), "shares")
        price = _number(_value(row, headers, "price"), "price")
        amount = _number(_value(row, headers, "amount"), "amount")
        fees = _number(_value(row, headers, "fee"), "fee", required=False) or 0.0
        taxes = _number(_value(row, headers, "tax"), "tax", required=False) or 0.0
        realized_gain = _number(_value(row, headers, "realizedgains"), "realizedgains", required=False)
        currency = (_value(row, headers, "currency") or "").upper()
        broker = _value(row, headers, "broker")
        if not currency:
            raise TransactionImportError("missing currency")
        if transaction_type not in {"TRANSFERIN", "TRANSFEROUT"} and not broker:
            raise TransactionImportError("missing broker")
        if shares < 0 or price < 0 or amount < 0 or fees < 0 or taxes < 0:
            raise TransactionImportError("negative monetary or share values are not supported")
    except TransactionImportError as exc:
        return CanonicalRow(row_number=row_number, name=holding, asset_type=_value(row, headers, "assettype"), source_values=source_values, error=str(exc))
    return CanonicalRow(
        row_number=row_number, transaction_date=transaction_date, transaction_type=transaction_type, identifier=identifier,
        isin=identifier if _ISIN.match(identifier) else None, symbol=None, wkn=_value(row, headers, "wkn"), name=holding,
        shares=shares, price=price, amount=amount, fees=fees, taxes=taxes, currency=currency, broker=broker, external_id=None,
        realized_gain=realized_gain, asset_type=_value(row, headers, "assettype"), notes=_value(row, headers, "notes"),
        source_values=source_values,
    )


def parse_parqet_csv(path: Path) -> list[CanonicalRow]:
    """Read a Parqet export (semicolon CSV, decimal comma) as canonical rows."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        headers = _csv_headers(reader.fieldnames)
        return [_canonical_row(row_number, row, headers) for row_number, row in enumerate(reader, start=2)]


def _notes_for(record: NormalizedImportRecord) -> str:
    notes: dict[str, Any] = {"import_source": "parqet_incremental", "isin": record.identifier}
    if record.realized_gain is not None:
        notes["parqet_realizedgains"] = record.realized_gain
    if record.source_notes is not None:
        notes["parqet_source_notes"] = record.source_notes
    return json.dumps(notes, sort_keys=True, separators=(",", ":"))


PARQET_ADAPTER = ImportAdapter(
    name="parqet", parse=parse_parqet_csv, import_type="PARQET_CSV_INCREMENTAL", source_label=IMPORT_SOURCE,
    external_prefix="parqet:", reconciliation_source=LEGACY_RECONCILIATION_SOURCE, notes=_notes_for,
)


def build_import_plan(conn: sqlite3.Connection, csv_path: str | Path, *, options: Optional[ImportOptions] = None) -> ImportPlan:
    """Parse and classify a Parqet CSV without performing writes (Parqet default of :func:`transaction_import.build_import_plan`)."""
    return _build_import_plan(conn, csv_path, adapter=PARQET_ADAPTER, options=options)
