"""Controlled management CLI for explicit Swing campaign lifecycle records.

All mutations are dry-runs unless ``--write`` is passed.  This CLI records
manual lifecycle facts; it does not place orders, infer historic campaigns, or
turn analytical signals into executions.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from swing_lifecycle import (
    CAMPAIGN_STATUSES,
    EVENT_TYPES,
    EXECUTION_EVENT_TYPES,
    FEATURE_VERSION,
    FEATURE_VERSION_KEY,
    derive_open_lifecycle,
    lifecycle_schema_available,
    lifecycle_to_primitive,
)


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
SIGNAL_EVENT_TYPES = {"tp1_signal", "tp2_signal"}
REDUCTION_EVENT_TYPES = {
    "tp1_execution",
    "tp2_execution",
    "manual_reduction",
    "stop_execution",
}


class CampaignValidationError(ValueError):
    """Raised when a manual lifecycle record is invalid or unsafe."""


class CampaignSchemaError(RuntimeError):
    """Raised when the explicit lifecycle migration is unavailable."""


def _normalise_timestamp(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignValidationError(f"{field_name} is required")
    raw = value.strip()
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw).isoformat()
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError as exc:
        raise CampaignValidationError(
            f"{field_name} must be an ISO date or datetime"
        ) from exc


def _timestamp_value(value: str, field_name: str) -> datetime:
    normalised = _normalise_timestamp(value, field_name)
    if len(normalised) == 10:
        return datetime.combine(date.fromisoformat(normalised), datetime.min.time())
    return datetime.fromisoformat(normalised)


def _normalise_currency(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    if len(value) != 3 or value != value.upper() or not value.isalpha():
        raise CampaignValidationError(f"{field_name} must be a three-letter uppercase currency")
    return value


def _positive(value: Optional[float], field_name: str, *, required: bool) -> Optional[float]:
    if value is None:
        if required:
            raise CampaignValidationError(f"{field_name} is required")
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CampaignValidationError(f"{field_name} must be numeric") from exc
    if result <= 0:
        raise CampaignValidationError(f"{field_name} must be greater than zero")
    return result


def _nonempty(value: Optional[str], field_name: str, *, required: bool = True) -> Optional[str]:
    if value is None:
        if required:
            raise CampaignValidationError(f"{field_name} is required")
        return None
    cleaned = value.strip()
    if not cleaned:
        raise CampaignValidationError(f"{field_name} must not be empty")
    return cleaned


def _connect(db_path: Path, *, write: bool) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {db_path}")
    if write:
        conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
    else:
        conn = sqlite3.connect(f"file:///{db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _require_feature(conn: sqlite3.Connection) -> None:
    if not lifecycle_schema_available(conn):
        raise CampaignSchemaError(
            "Swing campaign lifecycle schema is missing; apply "
            "Migrate-TradingSwingLifecycle.py --write first"
        )


def _security_exists(conn: sqlite3.Connection, security_id: int) -> bool:
    return conn.execute("SELECT 1 FROM security WHERE id = ?", (security_id,)).fetchone() is not None


def _active_swing_assignment(
    conn: sqlite3.Connection, security_id: int, opened_at: str
) -> sqlite3.Row:
    effective_date = opened_at[:10]
    rows = conn.execute(
        """
        SELECT id, security_id, strategy_type, effective_from, effective_to
        FROM strategy_assignment
        WHERE security_id = ?
          AND strategy_type = 'swing'
          AND effective_from <= ?
          AND (effective_to IS NULL OR effective_to >= ?)
        ORDER BY effective_from DESC, id DESC
        """,
        (security_id, effective_date, effective_date),
    ).fetchall()
    if len(rows) != 1:
        raise CampaignValidationError(
            f"security_id {security_id} requires exactly one active Swing strategy assignment "
            f"at {effective_date}; found {len(rows)}"
        )
    return rows[0]


def _campaign_payload(row: sqlite3.Row | dict) -> dict:
    keys = row.keys() if hasattr(row, "keys") else row.keys()
    return {
        "id": row["id"],
        "security_id": row["security_id"],
        "symbol": row["symbol"] if "symbol" in keys else None,
        "name": row["name"] if "name" in keys else None,
        "strategy_assignment_id": row["strategy_assignment_id"],
        "opened_at": row["opened_at"],
        "original_quantity": row["original_quantity"],
        "reference_avg_cost": row["reference_avg_cost"],
        "reference_currency": row["reference_currency"],
        "status": row["status"],
        "closed_at": row["closed_at"],
        "source": row["source"],
        "rationale": row["rationale"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _event_payload(row: sqlite3.Row | dict) -> dict:
    return {key: row[key] for key in row.keys()}


def _open_campaign_row(conn: sqlite3.Connection, campaign_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM swing_campaign WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        raise CampaignValidationError(f"campaign_id {campaign_id} does not exist")
    return row


def _validate_reference(
    reference_avg_cost: Optional[float], reference_currency: Optional[str]
) -> tuple[Optional[float], Optional[str]]:
    cost = _positive(reference_avg_cost, "reference_avg_cost", required=False)
    currency = _normalise_currency(reference_currency, "reference_currency")
    if (cost is None) != (currency is None):
        raise CampaignValidationError(
            "reference_avg_cost and reference_currency must be supplied together"
        )
    return cost, currency


def open_campaign(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    opened_at: str,
    original_quantity: float,
    reference_avg_cost: Optional[float],
    reference_currency: Optional[str],
    source: str,
    rationale: Optional[str] = None,
    write: bool = False,
) -> dict:
    """Create one explicit campaign and its baseline event atomically."""

    _require_feature(conn)
    if not _security_exists(conn, security_id):
        raise CampaignValidationError(f"security_id {security_id} does not exist")
    opened = _normalise_timestamp(opened_at, "opened_at")
    quantity = _positive(original_quantity, "original_quantity", required=True)
    cost, currency = _validate_reference(reference_avg_cost, reference_currency)
    event_source = _nonempty(source, "source")
    assignment = _active_swing_assignment(conn, security_id, opened)
    duplicate = conn.execute(
        "SELECT id FROM swing_campaign WHERE security_id = ? AND status = 'open'",
        (security_id,),
    ).fetchone()
    if duplicate is not None:
        raise CampaignValidationError(
            f"security_id {security_id} already has open campaign id={duplicate['id']}"
        )
    payload = {
        "security_id": security_id,
        "strategy_assignment_id": assignment["id"],
        "opened_at": opened,
        "original_quantity": quantity,
        "reference_avg_cost": cost,
        "reference_currency": currency,
        "status": "open",
        "source": event_source,
        "rationale": rationale,
        "baseline_event": {
            "event_type": "baseline",
            "event_at": opened,
            "quantity": quantity,
            "price": cost,
            "currency": currency,
            "source": event_source,
            "notes": rationale,
        },
    }
    if not write:
        return {"dry_run": True, "would_insert": payload}
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """
            INSERT INTO swing_campaign(
                security_id, strategy_assignment_id, opened_at, original_quantity,
                reference_avg_cost, reference_currency, status, source, rationale
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                security_id,
                assignment["id"],
                opened,
                quantity,
                cost,
                currency,
                event_source,
                rationale,
            ),
        )
        campaign_id = int(cursor.lastrowid)
        baseline_cursor = conn.execute(
            """
            INSERT INTO swing_campaign_event(
                campaign_id, event_type, event_at, quantity, price, currency, source, notes
            ) VALUES (?, 'baseline', ?, ?, ?, ?, ?, ?)
            """,
            (campaign_id, opened, quantity, cost, currency, event_source, rationale),
        )
        campaign = conn.execute(
            """SELECT campaign.*, security.symbol, security.name
               FROM swing_campaign campaign JOIN security ON security.id = campaign.security_id
               WHERE campaign.id = ?""",
            (campaign_id,),
        ).fetchone()
        baseline = conn.execute(
            "SELECT * FROM swing_campaign_event WHERE id = ?", (baseline_cursor.lastrowid,)
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "dry_run": False,
        "inserted_campaign": _campaign_payload(campaign),
        "inserted_baseline_event": _event_payload(baseline),
    }


def _validate_event(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    event_type: str,
    event_at: str,
    quantity: Optional[float],
    price: Optional[float],
    currency: Optional[str],
    transaction_id: Optional[int],
    source: str,
    external_event_id: Optional[str],
    allow_closed_campaign: bool = False,
    allow_close_event: bool = False,
) -> tuple[sqlite3.Row, dict]:
    _require_feature(conn)
    if event_type not in EVENT_TYPES:
        raise CampaignValidationError(f"invalid event type: {event_type}")
    if event_type == "baseline":
        raise CampaignValidationError("baseline events are created only by open")
    if event_type == "close" and not allow_close_event:
        raise CampaignValidationError("close events are created only by close")
    campaign = _open_campaign_row(conn, campaign_id)
    if campaign["status"] != "open" and not allow_closed_campaign:
        raise CampaignValidationError(f"campaign_id {campaign_id} is not open")
    event_time = _normalise_timestamp(event_at, "event_at")
    if _timestamp_value(event_time, "event_at") < _timestamp_value(campaign["opened_at"], "opened_at"):
        raise CampaignValidationError("event_at must not be before campaign opened_at")
    event_quantity = _positive(
        quantity,
        "quantity",
        required=event_type in EXECUTION_EVENT_TYPES or event_type == "add",
    )
    event_price = _positive(price, "price", required=False)
    event_currency = _normalise_currency(currency, "currency")
    if event_price is not None and event_currency is None:
        raise CampaignValidationError("currency is required when price is supplied")
    if event_type in SIGNAL_EVENT_TYPES:
        if transaction_id is not None:
            raise CampaignValidationError("signal events cannot link a transaction")
        if event_quantity is not None:
            raise CampaignValidationError("signal events must not contain a quantity")
    if event_type == "baseline":
        raise CampaignValidationError("baseline events are created only by open")
    event_source = _nonempty(source, "source")
    event_external_id = _nonempty(external_event_id, "external_event_id", required=False)
    if event_external_id is not None:
        existing = conn.execute(
            "SELECT id FROM swing_campaign_event WHERE external_event_id = ?",
            (event_external_id,),
        ).fetchone()
        if existing is not None:
            raise CampaignValidationError(
                f"external_event_id already exists on event id={existing['id']}"
            )
    transaction = None
    if transaction_id is not None:
        transaction = conn.execute(
            "SELECT id, security_id, transaction_type, shares FROM transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()
        if transaction is None:
            raise CampaignValidationError(f"transaction_id {transaction_id} does not exist")
        if transaction["security_id"] != campaign["security_id"]:
            raise CampaignValidationError(
                "linked transaction belongs to a different security than the campaign"
            )
        expected_type = "BUY" if event_type == "add" else "SELL" if event_type in REDUCTION_EVENT_TYPES | {"close"} else None
        if expected_type is not None and transaction["transaction_type"] != expected_type:
            raise CampaignValidationError(
                f"{event_type} requires a {expected_type} transaction when transaction_id is supplied"
            )
        if event_quantity is not None and transaction["shares"] is not None and event_quantity > float(transaction["shares"]):
            raise CampaignValidationError("event quantity cannot exceed linked transaction shares")
        duplicate_link = conn.execute(
            "SELECT id FROM swing_campaign_event WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if duplicate_link is not None:
            raise CampaignValidationError(
                f"transaction_id {transaction_id} is already linked to event id={duplicate_link['id']}"
            )
    return campaign, {
        "campaign_id": campaign_id,
        "event_type": event_type,
        "event_at": event_time,
        "quantity": event_quantity,
        "price": event_price,
        "currency": event_currency,
        "transaction_id": transaction_id,
        "source": event_source,
        "external_event_id": event_external_id,
    }


def add_event(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    event_type: str,
    event_at: str,
    quantity: Optional[float] = None,
    price: Optional[float] = None,
    currency: Optional[str] = None,
    transaction_id: Optional[int] = None,
    source: str,
    external_event_id: Optional[str] = None,
    notes: Optional[str] = None,
    write: bool = False,
) -> dict:
    """Record an explicit manual lifecycle fact; never execute a trade."""

    _, payload = _validate_event(
        conn,
        campaign_id=campaign_id,
        event_type=event_type,
        event_at=event_at,
        quantity=quantity,
        price=price,
        currency=currency,
        transaction_id=transaction_id,
        source=source,
        external_event_id=external_event_id,
    )
    payload["notes"] = notes
    if not write:
        return {"dry_run": True, "would_insert": payload}
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """
            INSERT INTO swing_campaign_event(
                campaign_id, event_type, event_at, quantity, price, currency,
                transaction_id, source, external_event_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(payload[key] for key in (
                "campaign_id", "event_type", "event_at", "quantity", "price", "currency",
                "transaction_id", "source", "external_event_id", "notes",
            )),
        )
        event = conn.execute(
            "SELECT * FROM swing_campaign_event WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"dry_run": False, "inserted_event": _event_payload(event)}


def close_campaign(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    closed_at: str,
    write: bool = False,
    record_close_event: bool = False,
    quantity: Optional[float] = None,
    price: Optional[float] = None,
    currency: Optional[str] = None,
    transaction_id: Optional[int] = None,
    source: Optional[str] = None,
    external_event_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    _require_feature(conn)
    campaign = _open_campaign_row(conn, campaign_id)
    if campaign["status"] != "open":
        raise CampaignValidationError(f"campaign_id {campaign_id} is already closed")
    closed = _normalise_timestamp(closed_at, "closed_at")
    if _timestamp_value(closed, "closed_at") < _timestamp_value(campaign["opened_at"], "opened_at"):
        raise CampaignValidationError("closed_at must not be before opened_at")
    close_event = None
    if record_close_event:
        _, close_event = _validate_event(
            conn,
            campaign_id=campaign_id,
            event_type="close",
            event_at=closed,
            quantity=quantity,
            price=price,
            currency=currency,
            transaction_id=transaction_id,
            source=source or "",
            external_event_id=external_event_id,
            allow_close_event=True,
        )
        close_event["notes"] = notes
    payload = {"campaign_id": campaign_id, "status": "closed", "closed_at": closed}
    if close_event is not None:
        payload["close_event"] = close_event
    if not write:
        return {"dry_run": True, "would_close": payload}
    conn.execute("BEGIN IMMEDIATE")
    try:
        inserted_event = None
        if close_event is not None:
            cursor = conn.execute(
                """
                INSERT INTO swing_campaign_event(
                    campaign_id, event_type, event_at, quantity, price, currency,
                    transaction_id, source, external_event_id, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(close_event[key] for key in (
                    "campaign_id", "event_type", "event_at", "quantity", "price", "currency",
                    "transaction_id", "source", "external_event_id", "notes",
                )),
            )
            inserted_event = conn.execute(
                "SELECT * FROM swing_campaign_event WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        conn.execute(
            """UPDATE swing_campaign
               SET status = 'closed', closed_at = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (closed, campaign_id),
        )
        updated = conn.execute(
            "SELECT * FROM swing_campaign WHERE id = ?", (campaign_id,)
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result = {"dry_run": False, "closed_campaign": _campaign_payload(updated)}
    if inserted_event is not None:
        result["inserted_close_event"] = _event_payload(inserted_event)
    return result


def list_campaigns(
    conn: sqlite3.Connection,
    *,
    security_id: Optional[int] = None,
    status: Optional[str] = None,
) -> list[dict]:
    _require_feature(conn)
    if status is not None and status not in CAMPAIGN_STATUSES:
        raise CampaignValidationError(f"invalid campaign status: {status}")
    query = """
        SELECT campaign.*, security.symbol, security.name
        FROM swing_campaign campaign
        JOIN security ON security.id = campaign.security_id
        WHERE 1 = 1
    """
    params: list[object] = []
    if security_id is not None:
        query += " AND campaign.security_id = ?"
        params.append(security_id)
    if status is not None:
        query += " AND campaign.status = ?"
        params.append(status)
    query += " ORDER BY campaign.security_id, campaign.opened_at, campaign.id"
    result = []
    for row in conn.execute(query, params).fetchall():
        payload = _campaign_payload(row)
        if row["status"] == "open":
            position = conn.execute(
                "SELECT shares FROM positions WHERE security_id = ?", (row["security_id"],)
            ).fetchone()
            quantity = float(position["shares"]) if position is not None else None
            payload["lifecycle"] = lifecycle_to_primitive(
                derive_open_lifecycle(conn, row["security_id"], current_quantity=quantity)
            )
        result.append(payload)
    return result


def validate_campaigns(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    try:
        _require_feature(conn)
    except CampaignSchemaError as exc:
        return [str(exc)]
    campaigns = conn.execute(
        """
        SELECT campaign.*, security.id AS matching_security_id,
               assignment.security_id AS assignment_security_id,
               assignment.strategy_type AS assignment_strategy_type
        FROM swing_campaign campaign
        LEFT JOIN security ON security.id = campaign.security_id
        LEFT JOIN strategy_assignment assignment ON assignment.id = campaign.strategy_assignment_id
        ORDER BY campaign.id
        """
    ).fetchall()
    campaign_rows = {row["id"]: row for row in campaigns}
    for row in campaigns:
        prefix = f"campaign id={row['id']}"
        if row["matching_security_id"] is None:
            errors.append(f"{prefix} references missing security_id={row['security_id']}")
        if row["assignment_security_id"] is None:
            errors.append(f"{prefix} references missing strategy_assignment_id={row['strategy_assignment_id']}")
        elif row["assignment_security_id"] != row["security_id"]:
            errors.append(f"{prefix} strategy assignment belongs to another security")
        elif row["assignment_strategy_type"] != "swing":
            errors.append(f"{prefix} strategy assignment is not swing")
        if row["status"] not in CAMPAIGN_STATUSES:
            errors.append(f"{prefix} has invalid status={row['status']}")
        if row["original_quantity"] is None or float(row["original_quantity"]) <= 0:
            errors.append(f"{prefix} has invalid original_quantity")
        try:
            opened = _timestamp_value(row["opened_at"], "opened_at")
            closed = _timestamp_value(row["closed_at"], "closed_at") if row["closed_at"] else None
            if closed is not None and closed < opened:
                errors.append(f"{prefix} closes before it opens")
        except CampaignValidationError as exc:
            errors.append(f"{prefix}: {exc}")
        if row["status"] == "open" and row["closed_at"] is not None:
            errors.append(f"{prefix} is open with closed_at")
        if row["status"] == "closed" and row["closed_at"] is None:
            errors.append(f"{prefix} is closed without closed_at")
        if (row["reference_avg_cost"] is None) != (row["reference_currency"] is None):
            errors.append(f"{prefix} reference cost and currency must be paired")
        if row["reference_avg_cost"] is not None and float(row["reference_avg_cost"]) <= 0:
            errors.append(f"{prefix} has invalid reference_avg_cost")
        try:
            _normalise_currency(row["reference_currency"], "reference_currency")
        except CampaignValidationError as exc:
            errors.append(f"{prefix}: {exc}")
    duplicate_open = conn.execute(
        """SELECT security_id, COUNT(*) count FROM swing_campaign WHERE status = 'open'
           GROUP BY security_id HAVING COUNT(*) > 1"""
    ).fetchall()
    errors.extend(
        f"security_id={row['security_id']} has {row['count']} open campaigns"
        for row in duplicate_open
    )

    events = conn.execute(
        """
        SELECT event.*, campaign.security_id, campaign.opened_at, campaign.closed_at,
               transaction_row.security_id AS transaction_security_id,
               transaction_row.transaction_type AS transaction_type,
               transaction_row.shares AS transaction_shares
        FROM swing_campaign_event event
        LEFT JOIN swing_campaign campaign ON campaign.id = event.campaign_id
        LEFT JOIN transactions transaction_row ON transaction_row.id = event.transaction_id
        ORDER BY event.id
        """
    ).fetchall()
    baseline_counts: dict[int, int] = {}
    baseline_quantities: dict[int, float] = {}
    for row in events:
        prefix = f"event id={row['id']}"
        campaign = campaign_rows.get(row["campaign_id"])
        if campaign is None:
            errors.append(f"{prefix} references missing campaign_id={row['campaign_id']}")
            continue
        if row["event_type"] not in EVENT_TYPES:
            errors.append(f"{prefix} has invalid event_type={row['event_type']}")
        try:
            event_time = _timestamp_value(row["event_at"], "event_at")
            if event_time < _timestamp_value(campaign["opened_at"], "opened_at"):
                errors.append(f"{prefix} occurs before campaign opens")
            if campaign["closed_at"] and event_time > _timestamp_value(campaign["closed_at"], "closed_at"):
                errors.append(f"{prefix} occurs after campaign closes")
        except CampaignValidationError as exc:
            errors.append(f"{prefix}: {exc}")
        if row["quantity"] is not None and float(row["quantity"]) <= 0:
            errors.append(f"{prefix} has invalid quantity")
        if row["event_type"] in EXECUTION_EVENT_TYPES | {"add"} and row["quantity"] is None:
            errors.append(f"{prefix} requires a quantity")
        if row["event_type"] in SIGNAL_EVENT_TYPES and row["quantity"] is not None:
            errors.append(f"{prefix} signal events must not contain a quantity")
        if row["price"] is not None and float(row["price"]) <= 0:
            errors.append(f"{prefix} has invalid price")
        try:
            _normalise_currency(row["currency"], "currency")
        except CampaignValidationError as exc:
            errors.append(f"{prefix}: {exc}")
        if row["price"] is not None and row["currency"] is None:
            errors.append(f"{prefix} has price without currency")
        if row["transaction_id"] is not None and row["transaction_security_id"] is None:
            errors.append(f"{prefix} references missing transaction_id={row['transaction_id']}")
        elif row["transaction_id"] is not None and row["transaction_security_id"] != row["security_id"]:
            errors.append(f"{prefix} linked transaction belongs to another security")
        elif row["transaction_id"] is not None:
            expected_type = (
                "BUY" if row["event_type"] == "add"
                else "SELL" if row["event_type"] in REDUCTION_EVENT_TYPES | {"close"}
                else None
            )
            if expected_type is not None and row["transaction_type"] != expected_type:
                errors.append(f"{prefix} requires a {expected_type} transaction")
            if row["quantity"] is not None and row["transaction_shares"] is not None and float(row["quantity"]) > float(row["transaction_shares"]):
                errors.append(f"{prefix} quantity exceeds linked transaction shares")
        if row["event_type"] in SIGNAL_EVENT_TYPES and row["transaction_id"] is not None:
            errors.append(f"{prefix} signal events cannot link a transaction")
        if row["event_type"] == "baseline":
            baseline_counts[row["campaign_id"]] = baseline_counts.get(row["campaign_id"], 0) + 1
            baseline_quantities[row["campaign_id"]] = float(row["quantity"] or 0)
    for campaign_id, campaign in campaign_rows.items():
        count = baseline_counts.get(campaign_id, 0)
        if count != 1:
            errors.append(f"campaign id={campaign_id} has {count} baseline events")
        elif abs(baseline_quantities[campaign_id] - float(campaign["original_quantity"])) >= 1e-9:
            errors.append(f"campaign id={campaign_id} baseline quantity differs from original_quantity")
    duplicate_external = conn.execute(
        """SELECT external_event_id, COUNT(*) count FROM swing_campaign_event
           WHERE external_event_id IS NOT NULL GROUP BY external_event_id HAVING COUNT(*) > 1"""
    ).fetchall()
    errors.extend(
        f"external_event_id={row['external_event_id']} is duplicated {row['count']} times"
        for row in duplicate_external
    )
    return errors


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage explicit Swing campaign lifecycle records")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="override trading DB path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list campaign records")
    list_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    list_parser.add_argument("--security-id", type=int)
    list_parser.add_argument("--status", choices=sorted(CAMPAIGN_STATUSES))

    open_parser = subparsers.add_parser("open", help="open a campaign and baseline (dry-run by default)")
    open_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    open_parser.add_argument("--security-id", required=True, type=int)
    open_parser.add_argument("--opened-at", required=True)
    open_parser.add_argument("--original-quantity", required=True, type=float)
    open_parser.add_argument("--reference-avg-cost", type=float)
    open_parser.add_argument("--reference-currency")
    open_parser.add_argument("--source", required=True)
    open_parser.add_argument("--rationale")
    open_parser.add_argument("--write", action="store_true")

    event_parser = subparsers.add_parser("event", help="record one explicit lifecycle event (dry-run by default)")
    event_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    event_parser.add_argument("--campaign-id", required=True, type=int)
    event_parser.add_argument("--type", dest="event_type", required=True, choices=sorted(EVENT_TYPES - {"baseline"}))
    event_parser.add_argument("--event-at", required=True)
    event_parser.add_argument("--quantity", type=float)
    event_parser.add_argument("--price", type=float)
    event_parser.add_argument("--currency")
    event_parser.add_argument("--transaction-id", type=int)
    event_parser.add_argument("--source", required=True)
    event_parser.add_argument("--external-event-id")
    event_parser.add_argument("--notes")
    event_parser.add_argument("--write", action="store_true")

    close_parser = subparsers.add_parser("close", help="close a campaign explicitly (dry-run by default)")
    close_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    close_parser.add_argument("--campaign-id", required=True, type=int)
    close_parser.add_argument("--closed-at", required=True)
    close_parser.add_argument("--record-close-event", action="store_true")
    close_parser.add_argument("--quantity", type=float)
    close_parser.add_argument("--price", type=float)
    close_parser.add_argument("--currency")
    close_parser.add_argument("--transaction-id", type=int)
    close_parser.add_argument("--source")
    close_parser.add_argument("--external-event-id")
    close_parser.add_argument("--notes")
    close_parser.add_argument("--write", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="validate campaign lifecycle integrity")
    validate_parser.add_argument("--db", dest="command_db", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    write = bool(getattr(args, "write", False))
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(getattr(args, "command_db", None) or args.db, write=write)
        if args.command == "list":
            _print(list_campaigns(conn, security_id=args.security_id, status=args.status))
            return 0
        if args.command == "open":
            _print(open_campaign(conn, security_id=args.security_id, opened_at=args.opened_at, original_quantity=args.original_quantity, reference_avg_cost=args.reference_avg_cost, reference_currency=args.reference_currency, source=args.source, rationale=args.rationale, write=write))
            return 0
        if args.command == "event":
            _print(add_event(conn, campaign_id=args.campaign_id, event_type=args.event_type, event_at=args.event_at, quantity=args.quantity, price=args.price, currency=args.currency, transaction_id=args.transaction_id, source=args.source, external_event_id=args.external_event_id, notes=args.notes, write=write))
            return 0
        if args.command == "close":
            _print(close_campaign(conn, campaign_id=args.campaign_id, closed_at=args.closed_at, write=write, record_close_event=args.record_close_event, quantity=args.quantity, price=args.price, currency=args.currency, transaction_id=args.transaction_id, source=args.source, external_event_id=args.external_event_id, notes=args.notes))
            return 0
        errors = validate_campaigns(conn)
        _print({"ok": not errors, "errors": errors})
        return 0 if not errors else 1
    except (CampaignSchemaError, CampaignValidationError, FileNotFoundError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
