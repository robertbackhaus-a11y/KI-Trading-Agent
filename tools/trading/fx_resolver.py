"""Read-only, ECB-convention FX resolution for valuation.

ECB rates are stored only as ``1 EUR = N quote currency``.  Phase 3A.5
supports direct EUR pairs; it deliberately does not triangulate currencies.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from analysis_contracts import AvailabilityStatus, DataQuality
from strategy_config import FXConfig


@dataclass(frozen=True)
class FXRateResolution:
    """A resolved stored ECB rate and its safe conversion factor."""

    from_currency: str
    to_currency: str
    rate: Optional[float] = None
    conversion_factor: Optional[float] = None
    rate_date: Optional[str] = None
    source: Optional[str] = None
    quality: DataQuality = DataQuality(AvailabilityStatus.UNAVAILABLE)

    def convert(self, value: float) -> Optional[float]:
        if self.conversion_factor is None:
            return None
        return float(value) * self.conversion_factor


def _as_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("as_of must start with an ISO date (YYYY-MM-DD)") from exc


def _currency(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    # Do not case-fold provider quote units.  Yahoo's ``GBp`` means pence,
    # not the ISO ``GBP`` currency, and converting it as pounds would produce
    # a 100x valuation error.  Only an explicit uppercase ISO-style code is
    # safely compatible with the ECB EUR-pair model.
    normalized = value.strip()
    return normalized if len(normalized) == 3 and normalized.isalpha() and normalized == normalized.upper() else None


def _table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fx_rates'"
    ).fetchone() is not None


def resolve_fx_rate(
    conn: sqlite3.Connection,
    from_currency: Optional[str],
    to_currency: Optional[str],
    as_of: str | date | datetime,
    config: Optional[FXConfig] = None,
) -> FXRateResolution:
    """Resolve a direct ECB rate at or before ``as_of`` without look-ahead."""

    fx_config = config or FXConfig()
    as_of_date = _as_date(as_of)
    source = fx_config.preferred_fx_source.upper()
    from_code = _currency(from_currency)
    to_code = _currency(to_currency)
    if from_code is None or to_code is None:
        return FXRateResolution(
            from_currency=from_currency or "",
            to_currency=to_currency or "",
            quality=DataQuality(AvailabilityStatus.UNAVAILABLE, ("invalid or missing currency",), as_of_date),
        )
    if from_code == to_code:
        return FXRateResolution(
            from_currency=from_code,
            to_currency=to_code,
            rate=1.0,
            conversion_factor=1.0,
            rate_date=as_of_date,
            quality=DataQuality(AvailabilityStatus.NOT_APPLICABLE, ("same currency",), as_of_date),
        )
    if not _table_exists(conn):
        return FXRateResolution(
            from_currency=from_code,
            to_currency=to_code,
            quality=DataQuality(AvailabilityStatus.UNAVAILABLE, ("fx_rates migration has not been applied",), as_of_date),
        )
    if from_code == "EUR":
        base_currency, quote_currency, factor_kind = "EUR", to_code, "multiply"
    elif to_code == "EUR":
        base_currency, quote_currency, factor_kind = "EUR", from_code, "divide"
    else:
        return FXRateResolution(
            from_currency=from_code,
            to_currency=to_code,
            quality=DataQuality(AvailabilityStatus.UNAVAILABLE, ("direct EUR-based FX pair is unavailable",), as_of_date),
        )
    row = conn.execute(
        """
        SELECT rate_date, rate, source
        FROM fx_rates
        WHERE base_currency = ? AND quote_currency = ? AND source = ? AND rate_date <= ?
        ORDER BY rate_date DESC, id DESC
        LIMIT 1
        """,
        (base_currency, quote_currency, source, as_of_date),
    ).fetchone()
    if row is None:
        return FXRateResolution(
            from_currency=from_code,
            to_currency=to_code,
            quality=DataQuality(AvailabilityStatus.UNAVAILABLE, ("no FX rate at or before as_of",), as_of_date),
        )
    rate = float(row["rate"])
    age_days = (date.fromisoformat(as_of_date) - date.fromisoformat(row["rate_date"])).days
    if age_days > fx_config.fx_max_age_days:
        quality = DataQuality(
            AvailabilityStatus.STALE,
            (f"FX rate is {age_days} days old; maximum is {fx_config.fx_max_age_days}",),
            row["rate_date"],
        )
        factor = None
    else:
        quality = DataQuality(AvailabilityStatus.AVAILABLE, (), row["rate_date"])
        factor = rate if factor_kind == "multiply" else 1.0 / rate
    return FXRateResolution(
        from_currency=from_code,
        to_currency=to_code,
        rate=rate,
        conversion_factor=factor,
        rate_date=row["rate_date"],
        source=row["source"],
        quality=quality,
    )
