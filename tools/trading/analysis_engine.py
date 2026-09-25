"""Read-only assembly of one deterministic trading analysis snapshot.

The engine owns data loading and composition.  Individual calculations remain
in ``trading_analytics`` and this module never writes to SQLite.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from analysis_contracts import (
    AnalysisSnapshot,
    AvailabilityStatus,
    DataQuality,
    EventRiskAnalysis,
    FundamentalAnalysis,
    PositionContext,
    PortfolioContext,
    PortfolioSecurityExposure,
    RiskAnalysis,
    SnapshotDataQuality,
    StrategyType,
    TechnicalAnalysis,
    ValuationAnalysis,
    to_primitive,
)
from strategy_config import CapitalStateConfig
from strategy_config import DataQualityConfig
from strategy_config import FXConfig
from strategy_config import PortfolioTargetConfig
from capital_state import resolve_capital_state
from fx_resolver import resolve_fx_rate
from portfolio_context import derive_allocation_guardrails
from swing_lifecycle import derive_open_lifecycle
import trading_analytics


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")


def _normalise_as_of(as_of: Optional[str | date | datetime]) -> Optional[str]:
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return as_of.date().isoformat()
    if isinstance(as_of, date):
        return as_of.isoformat()
    try:
        return date.fromisoformat(as_of[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("as_of must start with an ISO date (YYYY-MM-DD)") from exc


def _open_read_only_connection(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {db_path}")
    uri = f"file:///{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _quality_from_technical(
    result: dict,
    reference_as_of: str,
    data_quality_config: DataQualityConfig,
) -> DataQuality:
    points = result["data_points"]
    if points == 0:
        return DataQuality(
            AvailabilityStatus.UNAVAILABLE,
            ("no usable market_data rows",),
            result["as_of"],
        )
    if result["quality"] == "INSUFFICIENT":
        return DataQuality(
            AvailabilityStatus.INSUFFICIENT,
            (f"only {points} usable price points; at least 30 are required",),
            result["as_of"],
        )
    latest_trade_date = result["as_of"]
    if latest_trade_date is not None:
        age_days = (
            date.fromisoformat(reference_as_of)
            - date.fromisoformat(latest_trade_date)
        ).days
        if age_days > data_quality_config.market_data_max_age_days:
            return DataQuality(
                AvailabilityStatus.STALE,
                (
                    f"latest market data is {age_days} days old; "
                    f"maximum is {data_quality_config.market_data_max_age_days}",
                ),
                latest_trade_date,
            )
    if result["quality"] == "LIMITED":
        return DataQuality(
            AvailabilityStatus.PARTIAL,
            (f"only {points} usable price points",),
            result["as_of"],
        )
    return DataQuality(AvailabilityStatus.AVAILABLE, (), result["as_of"])


def _build_technical(
    conn: sqlite3.Connection,
    security_id: int,
    evaluation_as_of: str,
    data_quality_config: DataQualityConfig,
) -> TechnicalAnalysis:
    # Even a live snapshot is bounded by its evaluation date so preloaded
    # future market rows cannot create look-ahead bias.
    result = trading_analytics.analyze_security_as_of(
        conn, security_id, evaluation_as_of
    )
    return TechnicalAnalysis(
        quality=_quality_from_technical(
            result, evaluation_as_of, data_quality_config
        ),
        current_price=result["current_price"],
        data_points=result["data_points"],
        perf_1m_pct=result["perf_1m_pct"],
        perf_3m_pct=result["perf_3m_pct"],
        perf_6m_pct=result["perf_6m_pct"],
        sma50=result["sma50"],
        sma200=result["sma200"],
        dist_sma50_pct=result["dist_sma50_pct"],
        dist_sma200_pct=result["dist_sma200_pct"],
        rsi14=result["rsi14"],
        drawdown_52w_pct=result["drawdown_52w_pct"],
        volatility_60d_annualized_pct=result["volatility_60d_annualized_pct"],
        momentum_score=result["score"],
        score_components=result["score_components"],
    )


def _latest_fundamental(
    conn: sqlite3.Connection,
    security_id: int,
    as_of: Optional[str],
) -> Optional[sqlite3.Row]:
    query = """
        SELECT *
        FROM fundamentals
        WHERE security_id = ?
    """
    params: list[object] = [security_id]
    if as_of is not None:
        # Historical snapshots may use only information that was published by
        # the requested date. ``fetched_at`` is not a publication timestamp.
        query += " AND period_end <= ? AND filing_date IS NOT NULL AND filing_date <= ?"
        params.extend((as_of, as_of))
    query += " ORDER BY period_end DESC, filing_date DESC, id DESC LIMIT 1"
    return conn.execute(query, params).fetchone()


def _previous_comparable_fundamental(
    conn: sqlite3.Connection,
    current: sqlite3.Row,
    as_of: Optional[str],
) -> Optional[sqlite3.Row]:
    fiscal_year = current["fiscal_year"]
    if fiscal_year is None:
        return None
    query = """
        SELECT *
        FROM fundamentals
        WHERE security_id = ?
          AND source_id IS ?
          AND period_type = ?
          AND fiscal_year = ?
          AND fiscal_quarter IS ?
          AND currency IS ?
    """
    params: list[object] = [
        current["security_id"],
        current["source_id"],
        current["period_type"],
        fiscal_year - 1,
        current["fiscal_quarter"],
        current["currency"],
    ]
    if as_of is not None:
        query += " AND period_end <= ? AND filing_date IS NOT NULL AND filing_date <= ?"
        params.extend((as_of, as_of))
    query += " ORDER BY period_end DESC, id DESC LIMIT 1"
    return conn.execute(query, params).fetchone()


def _growth_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return (float(current) - float(previous)) / abs(float(previous)) * 100.0


def _build_fundamental(
    conn: sqlite3.Connection,
    security_id: int,
    as_of: Optional[str],
    asset_type: str,
) -> FundamentalAnalysis:
    if asset_type.lower() in {"etf", "fund"}:
        return FundamentalAnalysis(
            quality=DataQuality(
                AvailabilityStatus.NOT_APPLICABLE,
                (f"fundamentals are not applicable to asset_type={asset_type}",),
                as_of,
            )
        )
    row = _latest_fundamental(conn, security_id, as_of)
    if row is None:
        return FundamentalAnalysis(
            quality=DataQuality(AvailabilityStatus.UNAVAILABLE, ("no fundamentals rows",))
        )

    comparable = _previous_comparable_fundamental(conn, row, as_of)
    revenue = row["revenue"]
    net_income = row["net_income"]
    operating_income = row["operating_income"]
    free_cash_flow = row["free_cash_flow"]
    missing_core = [
        name
        for name, value in {
            "revenue": revenue,
            "net_income": net_income,
            "operating_cash_flow": row["operating_cash_flow"],
            "free_cash_flow": free_cash_flow,
            "cash": row["cash"],
        }.items()
        if value is None
    ]
    details = tuple(f"latest period missing {name}" for name in missing_core)
    quality = DataQuality(
        AvailabilityStatus.PARTIAL if missing_core else AvailabilityStatus.AVAILABLE,
        details,
        row["period_end"],
    )
    return FundamentalAnalysis(
        quality=quality,
        period_end=row["period_end"],
        period_type=row["period_type"],
        currency=row["currency"],
        revenue=revenue,
        net_income=net_income,
        operating_cash_flow=row["operating_cash_flow"],
        free_cash_flow=free_cash_flow,
        cash=row["cash"],
        total_debt=row["total_debt"],
        revenue_yoy_growth_pct=_growth_pct(
            revenue, comparable["revenue"] if comparable is not None else None
        ),
        net_income_yoy_growth_pct=_growth_pct(
            net_income, comparable["net_income"] if comparable is not None else None
        ),
        operating_margin_pct=(
            float(operating_income) / float(revenue) * 100.0
            if operating_income is not None and revenue not in (None, 0)
            else None
        ),
        fcf_margin_pct=(
            float(free_cash_flow) / float(revenue) * 100.0
            if free_cash_flow is not None and revenue not in (None, 0)
            else None
        ),
    )


def _module_quality(counts: dict[str, int], as_of: Optional[str]) -> DataQuality:
    present = [name for name, count in counts.items() if count > 0]
    if not present:
        return DataQuality(AvailabilityStatus.UNAVAILABLE, ("no source rows",), as_of)
    if len(present) != len(counts):
        missing = tuple(f"no {name} rows" for name, count in counts.items() if count == 0)
        return DataQuality(AvailabilityStatus.PARTIAL, missing, as_of)
    return DataQuality(AvailabilityStatus.AVAILABLE, (), as_of)


def _count_for_security(
    conn: sqlite3.Connection,
    table: str,
    security_id: int,
    date_column: str,
    as_of: Optional[str],
) -> int:
    query = f"SELECT COUNT(*) FROM {table} WHERE security_id = ?"
    params: list[object] = [security_id]
    if as_of is not None:
        query += f" AND substr({date_column}, 1, 10) <= ?"
        params.append(as_of)
    return int(conn.execute(query, params).fetchone()[0])


def _strategy_assignment_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'strategy_assignment'
        """
    ).fetchone() is not None


def _resolve_strategy_assignment(
    conn: sqlite3.Connection,
    security_id: int,
    effective_date: str,
) -> tuple[StrategyType, DataQuality]:
    """Resolve exactly one assignment active on ``effective_date``.

    The resolver never derives a strategy from security attributes or market
    data. A missing table is distinct from a migrated table with no assignment.
    """

    if not _strategy_assignment_table_exists(conn):
        return (
            StrategyType.UNKNOWN,
            DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                ("strategy_assignment migration has not been applied",),
                effective_date,
            ),
        )
    rows = conn.execute(
        """
        SELECT id, strategy_type, effective_from, effective_to
        FROM strategy_assignment
        WHERE security_id = ?
          AND effective_from <= ?
          AND (effective_to IS NULL OR effective_to >= ?)
        ORDER BY effective_from DESC, id DESC
        """,
        (security_id, effective_date, effective_date),
    ).fetchall()
    if not rows:
        return (
            StrategyType.UNKNOWN,
            DataQuality(
                AvailabilityStatus.AVAILABLE,
                ("no active strategy assignment; strategy is unknown",),
                effective_date,
            ),
        )
    if len(rows) != 1:
        return (
            StrategyType.UNKNOWN,
            DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                ("multiple overlapping active strategy assignments",),
                effective_date,
            ),
        )
    row = rows[0]
    try:
        strategy = StrategyType(row["strategy_type"])
    except ValueError:
        return (
            StrategyType.UNKNOWN,
            DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                (f"invalid strategy_type: {row['strategy_type']}",),
                effective_date,
            ),
        )
    return strategy, DataQuality(AvailabilityStatus.AVAILABLE, (), effective_date)


def _build_position(
    conn: sqlite3.Connection,
    security_id: int,
    in_watchlist: Optional[bool],
    evaluation_as_of: str,
    historical_state_unsupported: bool,
    strategy: StrategyType,
    strategy_quality: DataQuality,
    fx_config: FXConfig,
) -> PositionContext:
    if historical_state_unsupported:
        return PositionContext(
            quality=DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                ("historical position and watchlist state are unsupported",),
                evaluation_as_of,
            ),
            has_position=None,
            in_watchlist=None,
            state_supported=False,
            strategy=strategy,
            strategy_quality=strategy_quality,
            lifecycle_quality=DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                ("historical campaign state is unsupported",),
                evaluation_as_of,
            ),
            campaign_reconciliation_quality=DataQuality(
                AvailabilityStatus.NOT_APPLICABLE,
                ("campaign reconciliation is unavailable for historical position state",),
                evaluation_as_of,
            ),
            fx_quality=DataQuality(
                AvailabilityStatus.NOT_APPLICABLE,
                ("FX is not applicable to unsupported historical position state",),
                evaluation_as_of,
            ),
        )
    row = conn.execute("SELECT * FROM positions WHERE security_id = ?", (security_id,)).fetchone()
    if row is None or float(row["shares"] or 0) == 0:
        # A zero-position candidate still needs an FX-safe EUR quote for
        # initial-entry sizing.  This does not create a position valuation or
        # infer a cost basis; it merely preserves quote provenance.
        query = "SELECT price, currency, as_of_at FROM market_snapshot WHERE security_id = ?"
        params: list[object] = [security_id]
        query += " AND substr(as_of_at, 1, 10) <= ? ORDER BY as_of_at DESC LIMIT 1"
        params.append(evaluation_as_of)
        market = conn.execute(query, params).fetchone()
        price = market["price"] if market is not None else None
        quote_currency = market["currency"] if market is not None else None
        if market is not None and price is not None:
            fx_resolution = resolve_fx_rate(
                conn,
                quote_currency,
                fx_config.portfolio_base_currency,
                market["as_of_at"],
                fx_config,
            )
            valuation_price = fx_resolution.convert(float(price))
            fx_quality = fx_resolution.quality
        else:
            valuation_price = None
            fx_quality = DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                ("FX cannot be resolved without a market_snapshot price",),
                evaluation_as_of,
            )
        lifecycle = (
            derive_open_lifecycle(
                conn,
                security_id,
                current_quantity=0.0,
                evaluation_as_of=evaluation_as_of,
            )
            if strategy is StrategyType.SWING
            else None
        )
        return PositionContext(
            quality=DataQuality(
                AvailabilityStatus.NOT_APPLICABLE,
                ("no open position",),
                evaluation_as_of,
            ),
            in_watchlist=in_watchlist,
            strategy=strategy,
            strategy_quality=strategy_quality,
            swing_campaign_id=lifecycle.campaign_id if lifecycle else None,
            swing_campaign_status=lifecycle.status if lifecycle else None,
            swing_campaign_original_quantity=(lifecycle.original_quantity if lifecycle else None),
            swing_campaign_reference_avg_cost=(lifecycle.reference_avg_cost if lifecycle else None),
            swing_campaign_reference_currency=(lifecycle.reference_currency if lifecycle else None),
            swing_campaign_derived_quantity=(lifecycle.event_derived_quantity if lifecycle else None),
            tp1_lifecycle_status=lifecycle.tp1_status if lifecycle else None,
            tp2_lifecycle_status=lifecycle.tp2_status if lifecycle else None,
            lifecycle_quality=(lifecycle.quality if lifecycle else DataQuality(AvailabilityStatus.NOT_APPLICABLE, ("Swing campaign lifecycle is not applicable to this strategy",))),
            campaign_reconciliation_quality=(lifecycle.reconciliation_quality if lifecycle else DataQuality(AvailabilityStatus.NOT_APPLICABLE, ("campaign reconciliation is not applicable to this strategy",))),
            campaign_reconciliation_delta=(lifecycle.reconciliation_delta if lifecycle else None),
            current_price=price,
            current_price_native=price,
            current_price_currency=quote_currency,
            current_price_cost_currency=valuation_price,
            valuation_currency=(fx_config.portfolio_base_currency.upper() if valuation_price is not None else None),
            fx_rate=(fx_resolution.rate if market is not None and price is not None else None),
            fx_rate_date=(fx_resolution.rate_date if market is not None and price is not None else None),
            fx_source=(fx_resolution.source if market is not None and price is not None else None),
            fx_quality=fx_quality,
        )

    query = "SELECT price, currency, as_of_at FROM market_snapshot WHERE security_id = ?"
    params: list[object] = [security_id]
    if evaluation_as_of is not None:
        query += " AND substr(as_of_at, 1, 10) <= ?"
        params.append(evaluation_as_of)
    query += " ORDER BY as_of_at DESC LIMIT 1"
    snapshot = conn.execute(query, params).fetchone()
    price = snapshot["price"] if snapshot is not None else None
    price_currency = snapshot["currency"] if snapshot is not None else None
    position_currency = row["currency"]
    avg_cost = row["avg_cost"]
    currency_matches = (
        position_currency is not None
        and price_currency is not None
        and position_currency.upper() == price_currency.upper()
    )
    if snapshot is None or price is None:
        fx_resolution = None
        valuation_price = None
        fx_quality = DataQuality(
            AvailabilityStatus.UNAVAILABLE,
            ("FX cannot be resolved without a market_snapshot price",),
            evaluation_as_of,
        )
    else:
        fx_resolution = resolve_fx_rate(
            conn,
            price_currency,
            position_currency,
            snapshot["as_of_at"],
            fx_config,
        )
        valuation_price = fx_resolution.convert(float(price))
        fx_quality = fx_resolution.quality
    can_calculate_gain = valuation_price is not None and avg_cost not in (None, 0)
    lifecycle = None
    if strategy is StrategyType.SWING:
        lifecycle = derive_open_lifecycle(
            conn,
            security_id,
            current_quantity=float(row["shares"]),
            evaluation_as_of=evaluation_as_of,
        )
    details: tuple[str, ...] = ()
    status = AvailabilityStatus.AVAILABLE
    if snapshot is None or price is None:
        status = AvailabilityStatus.PARTIAL
        details = ("no market_snapshot price",)
    elif not currency_matches:
        status = AvailabilityStatus.PARTIAL
        details = tuple(fx_quality.details) or ("position and market price currencies differ",)
    return PositionContext(
        quality=DataQuality(status, details, snapshot["as_of_at"] if snapshot else evaluation_as_of),
        has_position=True,
        in_watchlist=in_watchlist,
        strategy=strategy,
        strategy_quality=strategy_quality,
        swing_campaign_id=lifecycle.campaign_id if lifecycle else None,
        swing_campaign_status=lifecycle.status if lifecycle else None,
        swing_campaign_original_quantity=(
            lifecycle.original_quantity if lifecycle else None
        ),
        swing_campaign_reference_avg_cost=(
            lifecycle.reference_avg_cost if lifecycle else None
        ),
        swing_campaign_reference_currency=(
            lifecycle.reference_currency if lifecycle else None
        ),
        swing_campaign_derived_quantity=(
            lifecycle.event_derived_quantity if lifecycle else None
        ),
        tp1_lifecycle_status=lifecycle.tp1_status if lifecycle else None,
        tp2_lifecycle_status=lifecycle.tp2_status if lifecycle else None,
        tp1_executed_quantity=lifecycle.tp1_executed_quantity if lifecycle else 0.0,
        tp2_executed_quantity=lifecycle.tp2_executed_quantity if lifecycle else 0.0,
        total_add_quantity=lifecycle.total_add_quantity if lifecycle else 0.0,
        add_event_count=lifecycle.add_event_count if lifecycle else 0,
        manual_reduction_quantity=(
            lifecycle.manual_reduction_quantity if lifecycle else 0.0
        ),
        post_tp2_add_detected=(
            lifecycle.post_tp2_add_detected if lifecycle else False
        ),
        stop_execution_quantity=(
            lifecycle.stop_execution_quantity if lifecycle else 0.0
        ),
        lifecycle_quality=(
            lifecycle.quality
            if lifecycle
            else DataQuality(
                AvailabilityStatus.NOT_APPLICABLE,
                ("Swing campaign lifecycle is not applicable to this strategy",),
            )
        ),
        campaign_reconciliation_quality=(
            lifecycle.reconciliation_quality
            if lifecycle
            else DataQuality(
                AvailabilityStatus.NOT_APPLICABLE,
                ("campaign reconciliation is not applicable to this strategy",),
            )
        ),
        campaign_reconciliation_delta=(
            lifecycle.reconciliation_delta if lifecycle else None
        ),
        shares=row["shares"],
        avg_cost=avg_cost,
        remaining_cost_basis=row["remaining_cost_basis"],
        invested_amount=row["invested_amount"],
        realized_gain=row["realized_gain"],
        transaction_count=row["transaction_count"],
        currency=position_currency,
        cost_basis_currency=position_currency,
        current_price=price,
        current_price_native=price,
        current_price_currency=price_currency,
        current_price_cost_currency=valuation_price,
        valuation_currency=position_currency if valuation_price is not None else None,
        fx_rate=fx_resolution.rate if fx_resolution is not None else None,
        fx_rate_date=fx_resolution.rate_date if fx_resolution is not None else None,
        fx_source=fx_resolution.source if fx_resolution is not None else None,
        fx_quality=fx_quality,
        unrealized_gain_pct=(
            (float(valuation_price) - float(avg_cost)) / float(avg_cost) * 100.0
            if can_calculate_gain
            else None
        ),
        unrealized_gain_amount=(
            float(row["shares"]) * (float(valuation_price) - float(avg_cost))
            if can_calculate_gain
            else None
        ),
    )


def build_analysis_snapshot(
    security_id: int,
    as_of: Optional[str | date | datetime] = None,
    *,
    connection: Optional[sqlite3.Connection] = None,
    data_quality_config: Optional[DataQualityConfig] = None,
    fx_config: Optional[FXConfig] = None,
) -> AnalysisSnapshot:
    """Build one read-only analysis snapshot.

    ``connection`` is optional dependency injection for tests.  When omitted,
    the production database is opened in SQLite read-only mode.
    """

    requested_as_of = _normalise_as_of(as_of)
    evaluation_as_of = requested_as_of or date.today().isoformat()
    quality_config = data_quality_config or DataQualityConfig()
    resolved_fx_config = fx_config or FXConfig()
    owns_connection = connection is None
    conn = connection or _open_read_only_connection(DB_PATH)
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        security = conn.execute(
            "SELECT id, symbol, name, asset_type FROM security WHERE id = ?", (security_id,)
        ).fetchone()
        if security is None:
            raise ValueError(f"security_id {security_id} not found")
        historical_state_unsupported = (
            requested_as_of is not None and requested_as_of < date.today().isoformat()
        )
        in_watchlist = None
        if not historical_state_unsupported:
            in_watchlist = conn.execute(
                "SELECT 1 FROM watchlist WHERE security_id = ?", (security_id,)
            ).fetchone() is not None

        strategy, strategy_quality = _resolve_strategy_assignment(
            conn, security_id, evaluation_as_of
        )

        technical = _build_technical(
            conn, security_id, evaluation_as_of, quality_config
        )
        fundamental = _build_fundamental(
            conn, security_id, requested_as_of, security["asset_type"]
        )
        valuation_counts = {
            "estimates": _count_for_security(conn, "estimates", security_id, "as_of_date", requested_as_of),
            "ratings": _count_for_security(conn, "ratings", security_id, "as_of_date", requested_as_of),
            "price_targets": _count_for_security(conn, "price_targets", security_id, "as_of_date", requested_as_of),
        }
        valuation = ValuationAnalysis(
            quality=_module_quality(valuation_counts, requested_as_of),
            estimates_count=valuation_counts["estimates"],
            ratings_count=valuation_counts["ratings"],
            price_targets_count=valuation_counts["price_targets"],
        )
        event_counts = {
            "events": _count_for_security(conn, "events", security_id, "event_date", requested_as_of),
            "news": _count_for_security(conn, "news", security_id, "published_at", requested_as_of),
        }
        event_risk = EventRiskAnalysis(
            quality=_module_quality(event_counts, requested_as_of),
            events_count=event_counts["events"],
            news_count=event_counts["news"],
        )
        position = _build_position(
            conn,
            security_id,
            in_watchlist,
            evaluation_as_of,
            historical_state_unsupported,
            strategy,
            strategy_quality,
            resolved_fx_config,
        )
        risk = RiskAnalysis(
            quality=technical.quality,
            annualized_volatility_pct=technical.volatility_60d_annualized_pct,
            drawdown_52w_pct=technical.drawdown_52w_pct,
        )
        signals = []
        if position.has_position:
            signals.append("HAS_POSITION")
        if in_watchlist:
            signals.append("WATCHLIST")
        return AnalysisSnapshot(
            security_id=security["id"],
            symbol=security["symbol"],
            name=security["name"],
            as_of=evaluation_as_of,
            asset_type=security["asset_type"],
            technical=technical,
            fundamental=fundamental,
            valuation=valuation,
            event_risk=event_risk,
            risk=risk,
            position=position,
            data_quality=SnapshotDataQuality(
                technical=technical.quality,
                fundamental=fundamental.quality,
                valuation=valuation.quality,
                event_risk=event_risk.quality,
                risk=risk.quality,
                position=position.quality,
                strategy_assignment=strategy_quality,
            ),
            signals=tuple(signals),
            evaluation_as_of=evaluation_as_of,
            market_data_as_of=technical.quality.as_of,
        )
    finally:
        if owns_connection:
            conn.close()
        else:
            conn.row_factory = previous_row_factory


def _portfolio_exposure(
    snapshot: AnalysisSnapshot,
    base_currency: str,
) -> PortfolioSecurityExposure:
    """Return one open-position exposure without falling back to native price."""

    position = snapshot.position
    valuation_details: list[str] = []
    valuation_available = (
        position.has_position is True
        and position.shares is not None
        and float(position.shares) > 0
        and position.current_price_cost_currency is not None
        and position.valuation_currency is not None
        and position.valuation_currency.upper() == base_currency
        and position.fx_quality.status
        in {AvailabilityStatus.AVAILABLE, AvailabilityStatus.NOT_APPLICABLE}
    )
    if not valuation_available:
        if position.current_price_cost_currency is None:
            valuation_details.append("EUR-normalized market price is unavailable")
        if position.valuation_currency is None:
            valuation_details.append("valuation currency is unavailable")
        elif position.valuation_currency.upper() != base_currency:
            valuation_details.append(
                f"valuation currency {position.valuation_currency} is not {base_currency}"
            )
        if position.fx_quality.status not in {
            AvailabilityStatus.AVAILABLE,
            AvailabilityStatus.NOT_APPLICABLE,
        }:
            valuation_details.extend(position.fx_quality.details)
        if not valuation_details:
            valuation_details.append("open position valuation is unavailable")

    market_value = (
        float(position.shares) * float(position.current_price_cost_currency)
        if valuation_available
        else None
    )
    return PortfolioSecurityExposure(
        security_id=snapshot.security_id,
        symbol=snapshot.symbol,
        strategy=position.strategy,
        quantity=position.shares,
        market_value_eur=market_value,
        market_price_native=position.current_price_native,
        market_price_currency=position.current_price_currency,
        market_price_eur=(
            float(position.current_price_cost_currency)
            if valuation_available
            else None
        ),
        valuation_quality=DataQuality(
            AvailabilityStatus.AVAILABLE
            if valuation_available
            else AvailabilityStatus.UNAVAILABLE,
            () if valuation_available else tuple(valuation_details),
            position.fx_quality.as_of or snapshot.evaluation_as_of,
        ),
        strategy_quality=position.strategy_quality,
    )


def build_portfolio_context(
    current_security_id: Optional[int] = None,
    as_of: Optional[str | date | datetime] = None,
    *,
    connection: Optional[sqlite3.Connection] = None,
    data_quality_config: Optional[DataQualityConfig] = None,
    fx_config: Optional[FXConfig] = None,
    portfolio_target_config: Optional[PortfolioTargetConfig] = None,
    capital_state_config: Optional[CapitalStateConfig] = None,
) -> PortfolioContext:
    """Assemble a read-only EUR portfolio context for future sizing rules.

    No SQL aggregate is used for valuation: every holding is composed through
    :func:`build_analysis_snapshot` and its existing FX-safe valuation.  If a
    single open holding cannot be valued in EUR, totals and allocation weights
    are withheld rather than calculated from a partial denominator.
    """

    requested_as_of = _normalise_as_of(as_of)
    evaluation_as_of = requested_as_of or date.today().isoformat()
    resolved_fx_config = fx_config or FXConfig()
    base_currency = resolved_fx_config.portfolio_base_currency.upper()
    if base_currency != "EUR":
        raise ValueError("PortfolioContext currently supports EUR base currency only")
    target_config = portfolio_target_config or PortfolioTargetConfig()
    owns_connection = connection is None
    conn = connection or _open_read_only_connection(DB_PATH)
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        if current_security_id is not None:
            exists = conn.execute(
                "SELECT 1 FROM security WHERE id = ?", (current_security_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"security_id {current_security_id} not found")

        position_rows = conn.execute(
            "SELECT security_id FROM positions WHERE shares > 0 ORDER BY security_id"
        ).fetchall()
        snapshots = tuple(
            build_analysis_snapshot(
                int(row["security_id"]),
                as_of=requested_as_of,
                connection=conn,
                data_quality_config=data_quality_config,
                fx_config=resolved_fx_config,
            )
            for row in position_rows
        )
        exposures = tuple(_portfolio_exposure(snapshot, base_currency) for snapshot in snapshots)

        valuation_complete = all(
            exposure.valuation_quality.status is AvailabilityStatus.AVAILABLE
            for exposure in exposures
        )
        if valuation_complete:
            total_market_value: Optional[float] = sum(
                float(exposure.market_value_eur or 0.0) for exposure in exposures
            )
            valuation_quality = DataQuality(
                AvailabilityStatus.AVAILABLE, (), evaluation_as_of
            )
        else:
            valuation_details = tuple(
                f"{exposure.symbol or exposure.security_id}: {detail}"
                for exposure in exposures
                if exposure.valuation_quality.status is not AvailabilityStatus.AVAILABLE
                for detail in exposure.valuation_quality.details
            )
            total_market_value = None
            valuation_quality = DataQuality(
                AvailabilityStatus.PARTIAL,
                valuation_details or ("one or more open positions lack an EUR valuation",),
                evaluation_as_of,
            )

        strategy_valid = all(
            exposure.strategy_quality.status is AvailabilityStatus.AVAILABLE
            and exposure.strategy is not StrategyType.UNKNOWN
            for exposure in exposures
        )
        target_scope_complete = all(
            exposure.strategy in {StrategyType.SWING, StrategyType.LONG_TERM}
            for exposure in exposures
        )
        allocation_details: list[str] = []
        if not valuation_complete:
            allocation_details.append("all open positions require successful EUR valuation")
        if not strategy_valid:
            allocation_details.append("all open positions require valid strategy assignments")
        if not target_scope_complete:
            allocation_details.append("allocation targets are undefined for one or more strategies")
        if total_market_value is not None and total_market_value <= 0:
            allocation_details.append("portfolio market value must be positive")

        allocation_available = not allocation_details
        if allocation_available:
            swing_market_value: Optional[float] = sum(
                float(exposure.market_value_eur or 0.0)
                for exposure in exposures
                if exposure.strategy is StrategyType.SWING
            )
            long_term_market_value: Optional[float] = sum(
                float(exposure.market_value_eur or 0.0)
                for exposure in exposures
                if exposure.strategy is StrategyType.LONG_TERM
            )
            swing_weight_pct: Optional[float] = (
                swing_market_value / float(total_market_value) * 100.0
            )
            long_term_weight_pct: Optional[float] = (
                long_term_market_value / float(total_market_value) * 100.0
            )
            allocation_quality = DataQuality(
                AvailabilityStatus.AVAILABLE, (), evaluation_as_of
            )
        else:
            swing_market_value = None
            swing_weight_pct = None
            long_term_market_value = None
            long_term_weight_pct = None
            allocation_quality = DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                tuple(allocation_details),
                evaluation_as_of,
            )

        current_exposure = next(
            (
                exposure
                for exposure in exposures
                if exposure.security_id == current_security_id
            ),
            None,
        )
        if current_security_id is None:
            current_security_value = None
            current_security_weight = None
        elif current_exposure is None:
            # A known security without an open position has a genuine zero
            # exposure; this is distinct from an unavailable holding value.
            current_security_value = 0.0
            current_security_weight = 0.0 if allocation_available else None
        else:
            current_security_value = current_exposure.market_value_eur
            current_security_weight = (
                current_security_value / float(total_market_value) * 100.0
                if allocation_available
                and current_security_value is not None
                and total_market_value is not None
                else None
            )

        guardrails = derive_allocation_guardrails(
            swing_weight_pct=swing_weight_pct,
            long_term_weight_pct=long_term_weight_pct,
            allocation_quality=allocation_quality,
            config=target_config,
        )
        capital_state = resolve_capital_state(
            conn,
            evaluation_as_of,
            capital_state_config,
            base_currency=base_currency,
        )
        return PortfolioContext(
            evaluation_as_of=evaluation_as_of,
            base_currency=base_currency,
            total_market_value=total_market_value,
            valuation_quality=valuation_quality,
            swing_market_value=swing_market_value,
            swing_weight_pct=swing_weight_pct,
            long_term_market_value=long_term_market_value,
            long_term_weight_pct=long_term_weight_pct,
            cash_available=capital_state.cash_available,
            cash_quality=capital_state.cash_quality,
            buying_power=capital_state.buying_power,
            buying_power_quality=capital_state.buying_power_quality,
            capital_state_as_of=capital_state.as_of,
            capital_state_source=capital_state.source,
            current_security_id=current_security_id,
            current_security_market_value=current_security_value,
            current_security_market_value_eur=current_security_value,
            current_security_weight_pct=current_security_weight,
            allocation_quality=allocation_quality,
            allocation_guardrails=guardrails,
            exposures=exposures,
        )
    finally:
        if owns_connection:
            conn.close()
        else:
            conn.row_factory = previous_row_factory


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only trading analysis snapshot")
    parser.add_argument("--security-id", required=True, type=int)
    parser.add_argument("--as-of", default=None, help="optional ISO date, YYYY-MM-DD")
    args = parser.parse_args()
    print(json.dumps(to_primitive(build_analysis_snapshot(args.security_id, args.as_of)), indent=2))


if __name__ == "__main__":
    main()
