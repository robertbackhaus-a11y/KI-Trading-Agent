"""Read-only, ECB-convention FX resolution for valuation.

ECB rates are stored only as ``1 EUR = N quote currency``.  Phase 3A.5
supports direct EUR pairs; it deliberately does not triangulate currencies.
Provider pence quotes (``GBp``) are first normalized to GBP (x0.01).
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
    # Share of one ISO unit that the provider quote unit represents (0.01 for
    # pence).  Already included in ``conversion_factor``; kept for provenance.
    price_factor: float = 1.0

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


# Provider quote units that are a fixed fraction of an ISO currency.  Yahoo
# quotes London listings in pence ("GBp"); the data contains no other such
# unit, so none is listed.  The lookup is exact and case-sensitive: "GBP" is
# pounds and must never be scaled.
MINOR_UNIT_QUOTES: dict[str, tuple[str, float]] = {"GBp": ("GBP", 0.01)}


def normalize_quote_unit(value: Optional[str]) -> tuple[Optional[str], float]:
    """Return ``(ISO code, price factor)`` for a provider quote unit.

    ``GBp`` -> ``("GBP", 0.01)``; a valid ISO code -> ``(code, 1.0)``;
    anything else -> ``(None, 1.0)``.
    """

    if value is not None and value.strip() in MINOR_UNIT_QUOTES:
        return MINOR_UNIT_QUOTES[value.strip()]
    return _currency(value), 1.0


def _table_exists(conn: sqlite3.Connection, *, cache: Optional[dict[str, bool]] = None) -> bool:
    """fx_rates' existence never changes within one connection's lifetime (a
    run). ``cache`` lets a caller that evaluates many securities in the same
    run (see ``table_exists_cache`` on ``resolve_fx_rate``) resolve this once
    and reuse it instead of re-querying ``sqlite_master`` per security. A
    caller that passes nothing (the default) gets exactly the previous,
    always-query behavior."""
    if cache is not None and "fx_rates" in cache:
        return cache["fx_rates"]
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fx_rates'"
    ).fetchone() is not None
    if cache is not None:
        cache["fx_rates"] = exists
    return exists


def resolve_fx_rate(
    conn: sqlite3.Connection,
    from_currency: Optional[str],
    to_currency: Optional[str],
    as_of: str | date | datetime,
    config: Optional[FXConfig] = None,
    *,
    table_exists_cache: Optional[dict[str, bool]] = None,
) -> FXRateResolution:
    """Resolve a direct ECB rate at or before ``as_of`` without look-ahead.

    ``table_exists_cache``, when provided by a caller resolving many
    securities within one run, is shared with :func:`_table_exists` so the
    ``fx_rates`` schema-existence check happens once per run instead of once
    per security. ``None`` (the default) is fully backward compatible."""

    fx_config = config or FXConfig()
    as_of_date = _as_date(as_of)
    source = fx_config.preferred_fx_source.upper()
    from_code, price_factor = normalize_quote_unit(from_currency)
    to_code = _currency(to_currency)
    from_label = from_currency.strip() if from_code is not None else (from_currency or "")
    if from_code is None or to_code is None:
        return FXRateResolution(
            from_currency=from_currency or "",
            to_currency=to_currency or "",
            quality=DataQuality(AvailabilityStatus.UNAVAILABLE, ("invalid or missing currency",), as_of_date),
        )
    if from_code == to_code:
        quality = (
            DataQuality(AvailabilityStatus.NOT_APPLICABLE, ("same currency",), as_of_date)
            if price_factor == 1.0
            else DataQuality(AvailabilityStatus.AVAILABLE, (f"quote unit {from_label} is {price_factor:g} {from_code}",), as_of_date)
        )
        return FXRateResolution(
            from_currency=from_label,
            to_currency=to_code,
            rate=1.0,
            conversion_factor=price_factor,
            rate_date=as_of_date,
            quality=quality,
            price_factor=price_factor,
        )
    if not _table_exists(conn, cache=table_exists_cache):
        return FXRateResolution(
            from_currency=from_label,
            to_currency=to_code,
            quality=DataQuality(AvailabilityStatus.UNAVAILABLE, ("fx_rates migration has not been applied",), as_of_date),
        )
    if from_code == "EUR":
        base_currency, quote_currency, factor_kind = "EUR", to_code, "multiply"
    elif to_code == "EUR":
        base_currency, quote_currency, factor_kind = "EUR", from_code, "divide"
    else:
        return FXRateResolution(
            from_currency=from_label,
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
            from_currency=from_label,
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
        factor = (rate if factor_kind == "multiply" else 1.0 / rate) * price_factor
    return FXRateResolution(
        from_currency=from_label,
        to_currency=to_code,
        rate=rate,
        conversion_factor=factor,
        rate_date=row["rate_date"],
        source=row["source"],
        quality=quality,
        price_factor=price_factor,
    )
