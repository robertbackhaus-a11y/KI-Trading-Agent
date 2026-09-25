"""Controlled management CLI for auditable portfolio capital-state records."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from capital_state import (
    FEATURE_VERSION,
    FEATURE_VERSION_KEY,
    TABLE_NAME,
    resolve_capital_state,
)
from strategy_config import CapitalStateConfig


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
ALLOWED_QUALITIES = {"available", "partial", "unavailable", "stale"}


class CapitalStateValidationError(ValueError):
    """Raised for an invalid or unsafe capital-state request."""


class CapitalStateSchemaError(RuntimeError):
    """Raised until the explicit capital-state migration is applied."""


def _iso_date(value: str, field_name: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise CapitalStateValidationError(
            f"{field_name} must be an ISO date (YYYY-MM-DD)"
        ) from exc


def _connect(db_path: Path, *, write: bool) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {db_path}")
    if write:
        conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
    else:
        conn = sqlite3.connect(
            f"file:///{db_path.as_posix()}?mode=ro", uri=True, timeout=10.0
        )
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _require_feature(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE_NAME,)
    ).fetchone()
    if table is None:
        raise CapitalStateSchemaError(
            "portfolio_capital_state table is missing; apply "
            "Migrate-TradingCapitalState.py --write first"
        )
    marker = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (FEATURE_VERSION_KEY,)
    ).fetchone()
    if marker is None or marker["value"] != FEATURE_VERSION:
        raise CapitalStateSchemaError(
            f"metadata.{FEATURE_VERSION_KEY} must equal {FEATURE_VERSION}"
        )


def _validate_new_state(
    *,
    as_of: str,
    currency: str,
    cash_available: Optional[float],
    buying_power: Optional[float],
    source: str,
    quality: str,
) -> dict:
    normalized_currency = str(currency or "").strip().upper()
    if normalized_currency != "EUR":
        raise CapitalStateValidationError("currency must be EUR for the current portfolio")
    normalized_source = str(source or "").strip()
    if not normalized_source:
        raise CapitalStateValidationError("source must not be empty")
    if quality not in ALLOWED_QUALITIES:
        raise CapitalStateValidationError(f"invalid quality: {quality}")
    return {
        "as_of": _iso_date(as_of, "as_of"),
        "base_currency": normalized_currency,
        "cash_available": float(cash_available) if cash_available is not None else None,
        "buying_power": float(buying_power) if buying_power is not None else None,
        "source": normalized_source,
        "quality": quality,
    }


def _payload(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "as_of": row["as_of"],
        "base_currency": row["base_currency"],
        "cash_available": row["cash_available"],
        "buying_power": row["buying_power"],
        "source": row["source"],
        "quality": row["quality"],
        "notes": row["notes"],
        "created_at": row["created_at"],
    }


def set_capital_state(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    currency: str,
    cash_available: Optional[float],
    buying_power: Optional[float],
    source: str,
    quality: str = "available",
    notes: Optional[str] = None,
    write: bool = False,
) -> dict:
    _require_feature(conn)
    payload = _validate_new_state(
        as_of=as_of,
        currency=currency,
        cash_available=cash_available,
        buying_power=buying_power,
        source=source,
        quality=quality,
    )
    payload["notes"] = notes
    if not write:
        return {"dry_run": True, "would_insert": payload}

    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """
            INSERT INTO portfolio_capital_state(
                as_of, base_currency, cash_available, buying_power, source, quality, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["as_of"], payload["base_currency"], payload["cash_available"],
                payload["buying_power"], payload["source"], payload["quality"], notes,
            ),
        )
        row = conn.execute(
            "SELECT * FROM portfolio_capital_state WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"dry_run": False, "inserted": _payload(row)}


def show_capital_state(
    conn: sqlite3.Connection,
    *,
    evaluation_as_of: str,
    freshness_max_age_days: Optional[int] = None,
) -> dict:
    _require_feature(conn)
    evaluation = _iso_date(evaluation_as_of, "as_of")
    if freshness_max_age_days is not None and freshness_max_age_days < 0:
        raise CapitalStateValidationError("freshness_max_age_days must be non-negative")
    resolved = resolve_capital_state(
        conn,
        evaluation,
        CapitalStateConfig(freshness_max_age_days=freshness_max_age_days),
    )
    return {
        "evaluation_as_of": evaluation,
        "cash_available": resolved.cash_available,
        "cash_quality": resolved.cash_quality.status.value,
        "cash_quality_details": resolved.cash_quality.details,
        "buying_power": resolved.buying_power,
        "buying_power_quality": resolved.buying_power_quality.status.value,
        "buying_power_quality_details": resolved.buying_power_quality.details,
        "capital_state_as_of": resolved.as_of,
        "capital_state_source": resolved.source,
    }


def validate_capital_states(conn: sqlite3.Connection) -> list[str]:
    try:
        _require_feature(conn)
    except CapitalStateSchemaError as exc:
        return [str(exc)]
    errors: list[str] = []
    rows = conn.execute("SELECT * FROM portfolio_capital_state ORDER BY id").fetchall()
    for row in rows:
        try:
            _iso_date(row["as_of"], "as_of")
        except CapitalStateValidationError as exc:
            errors.append(f"capital state id={row['id']}: {exc}")
        if row["base_currency"] != "EUR":
            errors.append(f"capital state id={row['id']} has non-EUR base_currency")
        if not str(row["source"] or "").strip():
            errors.append(f"capital state id={row['id']} has an empty source")
        if row["quality"] not in ALLOWED_QUALITIES:
            errors.append(f"capital state id={row['id']} has invalid quality={row['quality']}")
    return errors


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage auditable portfolio capital state")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="override trading DB path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    show_parser = subparsers.add_parser("show", help="resolve capital state at an evaluation date")
    show_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    show_parser.add_argument("--as-of", default=date.today().isoformat())
    show_parser.add_argument("--freshness-max-age-days", type=int)

    set_parser = subparsers.add_parser("set", help="record one capital state (dry-run by default)")
    set_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    set_parser.add_argument("--as-of", required=True)
    set_parser.add_argument("--currency", required=True)
    set_parser.add_argument("--cash-available", type=float)
    set_parser.add_argument("--buying-power", type=float)
    set_parser.add_argument("--source", required=True)
    set_parser.add_argument("--quality", choices=sorted(ALLOWED_QUALITIES), default="available")
    set_parser.add_argument("--notes")
    set_parser.add_argument("--write", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="validate capital-state records")
    validate_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    write = bool(getattr(args, "write", False))
    db_path = getattr(args, "command_db", None) or args.db
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(db_path, write=write)
        if args.command == "show":
            _print(show_capital_state(
                conn,
                evaluation_as_of=args.as_of,
                freshness_max_age_days=args.freshness_max_age_days,
            ))
            return 0
        if args.command == "set":
            _print(set_capital_state(
                conn,
                as_of=args.as_of,
                currency=args.currency,
                cash_available=args.cash_available,
                buying_power=args.buying_power,
                source=args.source,
                quality=args.quality,
                notes=args.notes,
                write=write,
            ))
            return 0
        errors = validate_capital_states(conn)
        _print({"ok": not errors, "errors": errors})
        return 0 if not errors else 1
    except (CapitalStateSchemaError, CapitalStateValidationError, FileNotFoundError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
