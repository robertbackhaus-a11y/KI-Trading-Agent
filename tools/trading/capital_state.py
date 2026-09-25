"""Read-only temporal resolution for recorded portfolio capital states."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Optional

from analysis_contracts import AvailabilityStatus, CapitalStateContext, DataQuality
from strategy_config import CapitalStateConfig


FEATURE_VERSION_KEY = "portfolio_capital_state_schema_version"
FEATURE_VERSION = "1"
TABLE_NAME = "portfolio_capital_state"
ALLOWED_QUALITIES = {
    AvailabilityStatus.AVAILABLE,
    AvailabilityStatus.PARTIAL,
    AvailabilityStatus.UNAVAILABLE,
    AvailabilityStatus.STALE,
}


def _iso_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("evaluation_as_of must start with an ISO date (YYYY-MM-DD)") from exc


def capital_state_schema_available(conn: sqlite3.Connection) -> bool:
    """Return whether the additive capital-state schema and marker exist."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (TABLE_NAME,),
    ).fetchone()
    metadata = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
    ).fetchone()
    if table is None or metadata is None:
        return False
    marker = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (FEATURE_VERSION_KEY,)
    ).fetchone()
    return marker is not None and marker[0] == FEATURE_VERSION


def _unavailable(evaluation_as_of: str, detail: str) -> CapitalStateContext:
    quality = DataQuality(AvailabilityStatus.UNAVAILABLE, (detail,), evaluation_as_of)
    return CapitalStateContext(None, quality, None, quality, None, None)


def _field_quality(
    value: Optional[float],
    field_name: str,
    source_quality: DataQuality,
    state_as_of: str,
) -> DataQuality:
    if value is None:
        return DataQuality(
            AvailabilityStatus.UNAVAILABLE,
            (f"{field_name} was not supplied by the capital state",),
            state_as_of,
        )
    return source_quality


def resolve_capital_state(
    conn: sqlite3.Connection,
    evaluation_as_of: str | date | datetime,
    config: Optional[CapitalStateConfig] = None,
    *,
    base_currency: str = "EUR",
) -> CapitalStateContext:
    """Resolve the latest eligible capital state without future look-ahead.

    A freshness threshold is deliberately optional.  Without one, a known
    state is never made stale merely because it is old.
    """

    evaluation_date = _iso_date(evaluation_as_of)
    expected_currency = base_currency.upper()
    if expected_currency != "EUR":
        raise ValueError("capital-state resolution currently supports EUR only")
    if not capital_state_schema_available(conn):
        return _unavailable(
            evaluation_date,
            "portfolio_capital_state migration feature version 1 is unavailable",
        )
    row = conn.execute(
        """
        SELECT id, as_of, base_currency, cash_available, buying_power, source, quality
        FROM portfolio_capital_state
        WHERE as_of <= ?
        ORDER BY as_of DESC, id DESC
        LIMIT 1
        """,
        (evaluation_date,),
    ).fetchone()
    if row is None:
        return _unavailable(evaluation_date, "no capital state exists at or before evaluation_as_of")
    state_as_of = _iso_date(row["as_of"])
    if str(row["base_currency"]).upper() != expected_currency:
        return _unavailable(
            evaluation_date,
            f"capital state base currency {row['base_currency']} is not {expected_currency}",
        )
    try:
        status = AvailabilityStatus(row["quality"])
    except ValueError:
        return _unavailable(evaluation_date, f"capital state has invalid quality {row['quality']}")
    if status not in ALLOWED_QUALITIES:
        return _unavailable(evaluation_date, f"capital state has unsupported quality {row['quality']}")

    source_quality = DataQuality(status, (), state_as_of)
    freshness = config or CapitalStateConfig()
    if freshness.freshness_max_age_days is not None:
        age_days = (date.fromisoformat(evaluation_date) - date.fromisoformat(state_as_of)).days
        if age_days > freshness.freshness_max_age_days:
            source_quality = DataQuality(
                AvailabilityStatus.STALE,
                (
                    f"capital state is {age_days} days old; maximum is "
                    f"{freshness.freshness_max_age_days}",
                ),
                state_as_of,
            )
    cash = row["cash_available"]
    buying_power = row["buying_power"]
    return CapitalStateContext(
        cash_available=float(cash) if cash is not None else None,
        cash_quality=_field_quality(cash, "cash_available", source_quality, state_as_of),
        buying_power=float(buying_power) if buying_power is not None else None,
        buying_power_quality=_field_quality(
            buying_power, "buying_power", source_quality, state_as_of
        ),
        as_of=state_as_of,
        source=row["source"],
    )
