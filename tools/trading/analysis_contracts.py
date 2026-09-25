"""Typed, persistence-independent contracts for analysis and decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class AvailabilityStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    NOT_APPLICABLE = "not_applicable"


class Action(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD = "HOLD"
    TRIM = "TRIM"
    SELL = "SELL"
    WATCH = "WATCH"


class ActionQuantityBasis(str, Enum):
    """Explicit quantity semantics for a recommendation, never an execution."""

    TP1_25_PERCENT_ORIGINAL = "TP1_25_PERCENT_ORIGINAL"
    TP2_75_PERCENT_CUMULATIVE_ORIGINAL = "TP2_75_PERCENT_CUMULATIVE_ORIGINAL"
    RUNNER_FULL_REMAINDER = "RUNNER_FULL_REMAINDER"
    STOP_FULL_EXIT = "STOP_FULL_EXIT"
    ADD_25_PERCENT_ORIGINAL = "ADD_25_PERCENT_ORIGINAL"
    INITIAL_ENTRY_SIZING = "INITIAL_ENTRY_SIZING"


class StrategyType(str, Enum):
    LONG_TERM = "long_term"
    SWING = "swing"
    TACTICAL = "tactical"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DataQuality:
    status: AvailabilityStatus
    details: tuple[str, ...] = ()
    as_of: Optional[str] = None


@dataclass(frozen=True)
class TechnicalAnalysis:
    quality: DataQuality
    current_price: Optional[float] = None
    data_points: int = 0
    perf_1m_pct: Optional[float] = None
    perf_3m_pct: Optional[float] = None
    perf_6m_pct: Optional[float] = None
    sma50: Optional[float] = None
    sma200: Optional[float] = None
    dist_sma50_pct: Optional[float] = None
    dist_sma200_pct: Optional[float] = None
    rsi14: Optional[float] = None
    drawdown_52w_pct: Optional[float] = None
    volatility_60d_annualized_pct: Optional[float] = None
    momentum_score: Optional[float] = None
    score_components: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class FundamentalAnalysis:
    quality: DataQuality
    period_end: Optional[str] = None
    period_type: Optional[str] = None
    currency: Optional[str] = None
    revenue: Optional[float] = None
    net_income: Optional[float] = None
    operating_cash_flow: Optional[float] = None
    free_cash_flow: Optional[float] = None
    cash: Optional[float] = None
    total_debt: Optional[float] = None
    revenue_yoy_growth_pct: Optional[float] = None
    net_income_yoy_growth_pct: Optional[float] = None
    operating_margin_pct: Optional[float] = None
    fcf_margin_pct: Optional[float] = None


@dataclass(frozen=True)
class ValuationAnalysis:
    quality: DataQuality
    estimates_count: int = 0
    ratings_count: int = 0
    price_targets_count: int = 0


@dataclass(frozen=True)
class EventRiskAnalysis:
    quality: DataQuality
    events_count: int = 0
    news_count: int = 0


@dataclass(frozen=True)
class RiskAnalysis:
    quality: DataQuality
    annualized_volatility_pct: Optional[float] = None
    drawdown_52w_pct: Optional[float] = None


@dataclass(frozen=True)
class PositionContext:
    quality: DataQuality
    has_position: Optional[bool] = False
    in_watchlist: Optional[bool] = False
    # False means a requested historical snapshot has no reconstructed
    # position/watchlist state and must not be treated as historical truth.
    state_supported: bool = True
    # Phase 3A assigns strategy at security level. Future models may attach it
    # to individual lots, allowing simultaneous strategies for one security.
    strategy: StrategyType = StrategyType.UNKNOWN
    strategy_quality: DataQuality = field(
        default_factory=lambda: DataQuality(
            AvailabilityStatus.UNAVAILABLE,
            ("strategy assignment was not resolved",),
        )
    )
    # Phase 3B.1 keeps campaign identity separate from strategy assignment.
    # These fields are read-only lifecycle context, never trading instructions.
    swing_campaign_id: Optional[int] = None
    swing_campaign_status: Optional[str] = None
    swing_campaign_original_quantity: Optional[float] = None
    swing_campaign_reference_avg_cost: Optional[float] = None
    swing_campaign_reference_currency: Optional[str] = None
    swing_campaign_derived_quantity: Optional[float] = None
    tp1_lifecycle_status: Optional[str] = None
    tp2_lifecycle_status: Optional[str] = None
    tp1_executed_quantity: float = 0.0
    tp2_executed_quantity: float = 0.0
    total_add_quantity: float = 0.0
    add_event_count: int = 0
    manual_reduction_quantity: float = 0.0
    post_tp2_add_detected: bool = False
    stop_execution_quantity: float = 0.0
    lifecycle_quality: DataQuality = field(
        default_factory=lambda: DataQuality(
            AvailabilityStatus.NOT_APPLICABLE,
            ("Swing campaign lifecycle is not applicable",),
        )
    )
    campaign_reconciliation_quality: DataQuality = field(
        default_factory=lambda: DataQuality(
            AvailabilityStatus.NOT_APPLICABLE,
            ("campaign reconciliation is not applicable",),
        )
    )
    campaign_reconciliation_delta: Optional[float] = None
    shares: Optional[float] = None
    avg_cost: Optional[float] = None
    remaining_cost_basis: Optional[float] = None
    invested_amount: Optional[float] = None
    realized_gain: Optional[float] = None
    transaction_count: Optional[int] = None
    # ``currency`` remains for Phase-2 compatibility; it is the same value as
    # ``cost_basis_currency`` and must never be interpreted as quote currency.
    currency: Optional[str] = None
    cost_basis_currency: Optional[str] = None
    # ``current_price`` remains the native quote for compatibility. New
    # consumers should use the explicit native/cost-currency fields below.
    current_price: Optional[float] = None
    current_price_native: Optional[float] = None
    current_price_currency: Optional[str] = None
    current_price_cost_currency: Optional[float] = None
    valuation_currency: Optional[str] = None
    fx_rate: Optional[float] = None
    fx_rate_date: Optional[str] = None
    fx_source: Optional[str] = None
    fx_quality: DataQuality = field(
        default_factory=lambda: DataQuality(
            AvailabilityStatus.NOT_APPLICABLE,
            ("FX is not applicable without a position valuation",),
        )
    )
    unrealized_gain_pct: Optional[float] = None
    unrealized_gain_amount: Optional[float] = None


@dataclass(frozen=True)
class SnapshotDataQuality:
    """Required quality sections for every assembled analysis snapshot."""

    technical: DataQuality
    fundamental: DataQuality
    valuation: DataQuality
    event_risk: DataQuality
    risk: DataQuality
    position: DataQuality
    strategy_assignment: DataQuality


@dataclass(frozen=True)
class AnalysisSnapshot:
    security_id: int
    symbol: Optional[str]
    name: str
    # Backward-compatible alias for ``evaluation_as_of``. It is never the
    # market-data date; consumers needing market provenance use
    # ``market_data_as_of`` instead.
    as_of: Optional[str]
    asset_type: str
    technical: TechnicalAnalysis
    fundamental: FundamentalAnalysis
    valuation: ValuationAnalysis
    event_risk: EventRiskAnalysis
    risk: RiskAnalysis
    position: PositionContext
    data_quality: SnapshotDataQuality
    signals: tuple[str, ...] = ()
    # Evaluation time is the temporal authority for strategy and lifecycle
    # resolution. Market and FX provenance remain distinct module fields.
    evaluation_as_of: Optional[str] = None
    market_data_as_of: Optional[str] = None


@dataclass(frozen=True)
class PortfolioSecurityExposure:
    """One open holding expressed in the portfolio base currency.

    ``market_value_eur`` is populated only when the position's existing
    FX-normalized valuation is safely available in EUR.  It deliberately is
    not a fallback to a native market-data value.
    """

    security_id: int
    symbol: Optional[str]
    strategy: StrategyType
    quantity: Optional[float]
    market_value_eur: Optional[float]
    market_price_native: Optional[float]
    market_price_currency: Optional[str]
    market_price_eur: Optional[float]
    valuation_quality: DataQuality
    strategy_quality: DataQuality


@dataclass(frozen=True)
class CapitalStateContext:
    """Resolved broker/manual capital state, separate from position value."""

    cash_available: Optional[float]
    cash_quality: DataQuality
    buying_power: Optional[float]
    buying_power_quality: DataQuality
    as_of: Optional[str]
    source: Optional[str]


@dataclass(frozen=True)
class PortfolioContext:
    """Read-only EUR portfolio state supplied to future sizing logic.

    The context intentionally carries no inferred cash balance and no trading
    instruction.  An unavailable cash value and a real zero balance are
    represented distinctly by ``cash_quality`` and ``cash_available``.
    """

    evaluation_as_of: str
    base_currency: str
    total_market_value: Optional[float]
    valuation_quality: DataQuality
    swing_market_value: Optional[float]
    swing_weight_pct: Optional[float]
    long_term_market_value: Optional[float]
    long_term_weight_pct: Optional[float]
    cash_available: Optional[float]
    cash_quality: DataQuality
    buying_power: Optional[float]
    buying_power_quality: DataQuality
    capital_state_as_of: Optional[str]
    capital_state_source: Optional[str]
    current_security_id: Optional[int]
    current_security_market_value: Optional[float]
    current_security_market_value_eur: Optional[float]
    current_security_weight_pct: Optional[float]
    allocation_quality: DataQuality
    allocation_guardrails: tuple[str, ...] = ()
    exposures: tuple[PortfolioSecurityExposure, ...] = ()


@dataclass(frozen=True)
class DecisionResult:
    action: Action
    confidence: float
    target_position: Optional[float] = None
    entry_zone: Optional[tuple[float, float]] = None
    stop: Optional[float] = None
    stop_price_currency: Optional[str] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    target_price_currency: Optional[str] = None
    action_quantity: Optional[float] = None
    action_quantity_basis: Optional[ActionQuantityBasis] = None
    target_remaining_quantity: Optional[float] = None
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    blocking_data_gaps: tuple[str, ...] = ()
    # Phase 3B.5e ADD recommendation context. Weights are decimal fractions
    # (0.20 == 20%), while values and prices are EUR. ``None`` means that the
    # input could not be established safely, never zero by assumption.
    add_eligible: bool = False
    add_base_quantity: Optional[float] = None
    add_recommended_quantity: Optional[float] = None
    add_purchase_value_eur: Optional[float] = None
    add_reference_price_eur: Optional[float] = None
    add_current_price_eur: Optional[float] = None
    add_current_security_weight: Optional[float] = None
    add_projected_security_weight: Optional[float] = None
    add_current_swing_allocation: Optional[float] = None
    add_projected_swing_allocation: Optional[float] = None
    add_cash_before: Optional[float] = None
    add_cash_after: Optional[float] = None
    add_block_reasons: tuple[str, ...] = ()
    # Phase 3B.7 initial-entry recommendation context.  Weights are decimal
    # fractions, values/prices are EUR, and these fields describe a proposal
    # only: no campaign, transaction, or capital-state mutation is implied.
    entry_eligible: bool = False
    entry_recommended_quantity: Optional[float] = None
    entry_purchase_value_eur: Optional[float] = None
    entry_current_price_eur: Optional[float] = None
    entry_sma50: Optional[float] = None
    entry_sma200: Optional[float] = None
    entry_rsi14: Optional[float] = None
    entry_momentum_score: Optional[float] = None
    entry_current_security_weight: Optional[float] = None
    entry_projected_security_weight: Optional[float] = None
    entry_current_swing_allocation: Optional[float] = None
    entry_projected_swing_allocation: Optional[float] = None
    entry_initial_position_weight: Optional[float] = None
    entry_cash_before: Optional[float] = None
    entry_cash_after: Optional[float] = None
    entry_block_reasons: tuple[str, ...] = ()


def to_primitive(value: Any) -> Any:
    """Convert a contract into standard JSON-serializable Python values."""

    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return to_primitive(asdict(value))
    if isinstance(value, dict):
        return {key: to_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_primitive(item) for item in value]
    return value
