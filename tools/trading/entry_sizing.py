"""Pure, deterministic initial Swing-entry eligibility and sizing."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Optional

from analysis_contracts import (
    AnalysisSnapshot,
    AvailabilityStatus,
    PortfolioContext,
    StrategyType,
)
from strategy_config import StrategyConfig


@dataclass(frozen=True)
class EntryRecommendation:
    """A recommendation-only EUR initial-entry calculation."""

    eligible: bool
    recommended_quantity: Optional[float]
    purchase_value_eur: Optional[float]
    current_price_eur: Optional[float]
    current_security_weight: Optional[float]
    projected_security_weight: Optional[float]
    current_swing_allocation: Optional[float]
    projected_swing_allocation: Optional[float]
    initial_position_weight: Optional[float]
    cash_before: Optional[float]
    cash_after: Optional[float]
    block_reasons: tuple[str, ...] = ()


def _blocked(
    reason: str,
    *,
    current: Optional[float] = None,
    security_weight: Optional[float] = None,
    swing_allocation: Optional[float] = None,
    cash: Optional[float] = None,
) -> EntryRecommendation:
    return EntryRecommendation(
        eligible=False,
        recommended_quantity=0.0,
        purchase_value_eur=0.0,
        current_price_eur=current,
        current_security_weight=security_weight,
        projected_security_weight=None,
        current_swing_allocation=swing_allocation,
        projected_swing_allocation=None,
        initial_position_weight=None,
        cash_before=cash,
        cash_after=cash,
        block_reasons=(reason,),
    )


def _weight_cap_value(*, total: float, current: float, maximum: float) -> float:
    """Maximum EUR purchase that retains a post-trade weight at ``maximum``."""
    if total <= 0 or maximum <= 0 or maximum >= 1:
        return 0.0
    return max(0.0, (maximum * total - current) / (1.0 - maximum))


def evaluate_entry_recommendation(
    snapshot: AnalysisSnapshot,
    portfolio: Optional[PortfolioContext],
    config: StrategyConfig,
) -> EntryRecommendation:
    """Evaluate a zero-position Swing candidate with no I/O or mutation.

    An explicit current Swing assignment is the candidate authority.  The
    generic watchlist has no strategy semantics and is deliberately not a
    fallback candidate source.  Technical tests use native quote units; all
    capital and allocation mathematics uses the EUR-normalized market price.
    """
    position = snapshot.position
    policy = config.sizing
    cash = portfolio.cash_available if portfolio is not None else None
    security_weight = (
        portfolio.current_security_weight_pct / 100.0
        if portfolio is not None and portfolio.current_security_weight_pct is not None
        else None
    )
    swing_allocation = (
        portfolio.swing_weight_pct / 100.0
        if portfolio is not None and portfolio.swing_weight_pct is not None
        else None
    )

    if position.strategy is not StrategyType.SWING:
        reason = (
            "ENTRY_STRATEGY_CONFLICT"
            if position.strategy in {StrategyType.LONG_TERM, StrategyType.TACTICAL}
            else "ENTRY_NOT_SWING_CANDIDATE"
        )
        return _blocked(reason, security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if position.strategy_quality.status is not AvailabilityStatus.AVAILABLE:
        return _blocked("ENTRY_NOT_SWING_CANDIDATE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if position.has_position is True or (position.shares is not None and float(position.shares) > 0):
        return _blocked("ENTRY_EXISTING_POSITION", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if position.swing_campaign_id is not None and position.swing_campaign_status == "open":
        return _blocked("ENTRY_OPEN_CAMPAIGN_EXISTS", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)

    # Capital blockers are authoritative once candidate identity is known.
    # In particular, an already-overweight Swing portfolio cannot turn a
    # technically strong candidate into a BUY through denominator effects.
    if (
        portfolio is None
        or portfolio.valuation_quality.status is not AvailabilityStatus.AVAILABLE
        or portfolio.total_market_value is None
        or float(portfolio.total_market_value) <= 0
    ):
        return _blocked("ENTRY_PORTFOLIO_VALUATION_UNAVAILABLE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if (
        portfolio.allocation_quality.status is not AvailabilityStatus.AVAILABLE
        or portfolio.swing_market_value is None
        or swing_allocation is None
    ):
        return _blocked("ENTRY_SWING_ALLOCATION_UNAVAILABLE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if portfolio.current_security_id != snapshot.security_id or security_weight is None:
        return _blocked("ENTRY_PORTFOLIO_VALUATION_UNAVAILABLE", swing_allocation=swing_allocation, cash=cash)
    if swing_allocation >= config.portfolio.swing_max_pct:
        return _blocked("ENTRY_SWING_ALLOCATION_LIMIT", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if portfolio.cash_quality.status is not AvailabilityStatus.AVAILABLE or cash is None:
        return _blocked("ENTRY_CASH_UNAVAILABLE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if policy.minimum_cash_reserve is None or float(cash) <= policy.minimum_cash_reserve:
        return _blocked("ENTRY_CASH_RESERVE_LIMIT", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)

    technical = snapshot.technical
    if technical.quality.status is not AvailabilityStatus.AVAILABLE:
        return _blocked("ENTRY_TECHNICAL_DATA_UNAVAILABLE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if technical.current_price is None:
        return _blocked("ENTRY_PRICE_UNAVAILABLE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if technical.sma50 is None:
        return _blocked("ENTRY_SMA50_UNAVAILABLE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if technical.sma200 is None:
        return _blocked("ENTRY_SMA200_UNAVAILABLE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if float(technical.current_price) <= float(technical.sma50):
        return _blocked("ENTRY_PRICE_BELOW_OR_EQUAL_SMA50", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if float(technical.current_price) <= float(technical.sma200):
        return _blocked("ENTRY_PRICE_BELOW_OR_EQUAL_SMA200", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if float(technical.sma50) <= float(technical.sma200):
        return _blocked("ENTRY_SMA50_BELOW_OR_EQUAL_SMA200", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)

    if policy.max_initial_swing_weight is None or policy.max_security_weight is None or policy.whole_share_policy != "floor":
        return _blocked("ENTRY_QUANTITY_ZERO", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)

    # A zero-position candidate has no exposure row, so analysis_engine
    # supplies the independently FX-normalized candidate quote in the
    # PositionContext.  No native quote is ever silently treated as EUR.
    exposure = next((item for item in portfolio.exposures if item.security_id == snapshot.security_id), None)
    current_eur = (
        float(exposure.market_price_eur)
        if exposure is not None and exposure.market_price_eur is not None
        and exposure.valuation_quality.status is AvailabilityStatus.AVAILABLE
        else (
            float(position.current_price_cost_currency)
            if position.current_price_cost_currency is not None
            and (position.valuation_currency or "").upper() == "EUR"
            and position.fx_quality.status in {AvailabilityStatus.AVAILABLE, AvailabilityStatus.NOT_APPLICABLE}
            else None
        )
    )
    if current_eur is None or current_eur <= 0:
        return _blocked("ENTRY_PORTFOLIO_VALUATION_UNAVAILABLE", security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)

    total = float(portfolio.total_market_value)
    swing_value = float(portfolio.swing_market_value)
    security_value = float(portfolio.current_security_market_value_eur or 0.0)
    available_capital = float(cash)
    if portfolio.buying_power_quality.status is AvailabilityStatus.AVAILABLE and portfolio.buying_power is not None:
        available_capital = min(available_capital, float(portfolio.buying_power))
    initial_cap = _weight_cap_value(total=total, current=security_value, maximum=policy.max_initial_swing_weight)
    security_cap = _weight_cap_value(total=total, current=security_value, maximum=policy.max_security_weight)
    swing_cap = _weight_cap_value(total=total, current=swing_value, maximum=config.portfolio.swing_max_pct)
    cash_cap = max(0.0, available_capital - float(policy.minimum_cash_reserve))
    allowed = min(initial_cap, security_cap, swing_cap, cash_cap)
    quantity = float(max(0, floor(allowed / current_eur + 1e-12)))
    if quantity <= 0:
        reason = (
            "ENTRY_CASH_RESERVE_LIMIT" if cash_cap <= 0
            else "ENTRY_INITIAL_WEIGHT_LIMIT" if initial_cap <= 0
            else "ENTRY_SECURITY_WEIGHT_LIMIT" if security_cap <= 0
            else "ENTRY_SWING_ALLOCATION_LIMIT" if swing_cap <= 0
            else "ENTRY_QUANTITY_ZERO"
        )
        return _blocked(reason, current=current_eur, security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    purchase = quantity * current_eur
    projected_total = total + purchase
    projected_security = (security_value + purchase) / projected_total
    projected_swing = (swing_value + purchase) / projected_total
    initial_weight = purchase / projected_total
    # Final post-rounding verification preserves every declared ceiling.
    if projected_security > policy.max_security_weight + 1e-12:
        return _blocked("ENTRY_SECURITY_WEIGHT_LIMIT", current=current_eur, security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if initial_weight > policy.max_initial_swing_weight + 1e-12:
        return _blocked("ENTRY_INITIAL_WEIGHT_LIMIT", current=current_eur, security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    if projected_swing > config.portfolio.swing_max_pct + 1e-12:
        return _blocked("ENTRY_SWING_ALLOCATION_LIMIT", current=current_eur, security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)
    return EntryRecommendation(
        eligible=True,
        recommended_quantity=quantity,
        purchase_value_eur=purchase,
        current_price_eur=current_eur,
        current_security_weight=security_weight,
        projected_security_weight=projected_security,
        current_swing_allocation=swing_allocation,
        projected_swing_allocation=projected_swing,
        initial_position_weight=initial_weight,
        cash_before=float(cash),
        cash_after=float(cash) - purchase,
    )
