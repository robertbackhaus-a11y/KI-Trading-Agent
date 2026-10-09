"""Pure, deterministic strength-only Swing ADD eligibility and sizing."""

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
class AddRecommendation:
    """Read-only ADD recommendation data; it never represents execution."""

    eligible: bool
    base_quantity: Optional[float]
    recommended_quantity: Optional[float]
    purchase_value_eur: Optional[float]
    reference_price_eur: Optional[float]
    current_price_eur: Optional[float]
    current_security_weight: Optional[float]
    projected_security_weight: Optional[float]
    current_swing_allocation: Optional[float]
    projected_swing_allocation: Optional[float]
    cash_before: Optional[float]
    cash_after: Optional[float]
    block_reasons: tuple[str, ...] = ()


def _result(
    reasons: list[str],
    *,
    base: Optional[float] = None,
    reference: Optional[float] = None,
    current: Optional[float] = None,
    security_weight: Optional[float] = None,
    swing_allocation: Optional[float] = None,
    cash: Optional[float] = None,
) -> AddRecommendation:
    return AddRecommendation(
        eligible=False,
        base_quantity=base,
        recommended_quantity=0.0 if base is not None else None,
        purchase_value_eur=0.0 if base is not None else None,
        reference_price_eur=reference,
        current_price_eur=current,
        current_security_weight=security_weight,
        projected_security_weight=None,
        current_swing_allocation=swing_allocation,
        projected_swing_allocation=None,
        cash_before=cash,
        cash_after=cash,
        block_reasons=tuple(reasons),
    )


def _cap_quantity(
    *,
    current_value: float,
    total_value: float,
    price: float,
    maximum_weight: float,
) -> int:
    """Largest whole quantity whose post-purchase weight remains at the cap."""
    if price <= 0 or total_value <= 0 or maximum_weight <= 0 or maximum_weight >= 1:
        return 0
    allowed = (maximum_weight * total_value - current_value) / (
        price * (1.0 - maximum_weight)
    )
    return max(0, floor(allowed + 1e-12))


def evaluate_add_recommendation(
    snapshot: AnalysisSnapshot,
    portfolio: Optional[PortfolioContext],
    config: StrategyConfig,
) -> AddRecommendation:
    """Evaluate a single open Swing campaign without I/O or mutation.

    EUR portfolio exposures form the allocation denominator.  Technical trend
    comparisons stay in the market's native currency, while campaign-profit
    and cash maths use the normalized EUR price.
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
    exposure = next(
        (
            item for item in (portfolio.exposures if portfolio is not None else ())
            if item.security_id == snapshot.security_id
        ),
        None,
    )
    current_eur = (
        float(exposure.market_price_eur)
        if exposure is not None
        and exposure.valuation_quality.status is AvailabilityStatus.AVAILABLE
        and exposure.market_price_eur is not None
        else None
    )
    reference_eur = (
        float(position.swing_campaign_reference_avg_cost)
        if position.swing_campaign_reference_avg_cost is not None
        and position.swing_campaign_reference_currency is not None
        and position.swing_campaign_reference_currency.upper() == "EUR"
        else None
    )

    if position.strategy is not StrategyType.SWING:
        return _result(["ADD_NOT_SWING"], reference=reference_eur, current=current_eur, cash=cash)
    if position.swing_campaign_id is None or position.swing_campaign_status != "open":
        return _result(["ADD_NO_OPEN_CAMPAIGN"], reference=reference_eur, current=current_eur, cash=cash)
    original = position.swing_campaign_original_quantity
    base = (
        float(floor(float(original) * policy.max_add_pct_of_original))
        if original is not None
        and float(original) > 0
        and policy.max_add_pct_of_original is not None
        else 0.0
    )

    def blocked(reason: str) -> AddRecommendation:
        return _result(
            [reason],
            base=base,
            reference=reference_eur,
            current=current_eur,
            security_weight=security_weight,
            swing_allocation=swing_allocation,
            cash=cash,
        )

    if position.tp1_lifecycle_status == "executed":
        return blocked("ADD_TP1_ALREADY_EXECUTED")
    if position.tp2_lifecycle_status == "executed":
        return blocked("ADD_TP2_ALREADY_EXECUTED")
    if policy.max_add_count is None or policy.max_add_count < 1:
        return blocked("ADD_ALREADY_USED")
    if position.add_event_count >= policy.max_add_count:
        return blocked("ADD_ALREADY_USED")
    if position.shares is None or float(position.shares) <= 0:
        return blocked("ADD_POSITION_UNAVAILABLE")
    if (
        position.lifecycle_quality.status is not AvailabilityStatus.AVAILABLE
        or
        position.campaign_reconciliation_quality.status is not AvailabilityStatus.AVAILABLE
        or position.campaign_reconciliation_delta is None
        or abs(float(position.campaign_reconciliation_delta)) > 1e-9
    ):
        return blocked("ADD_LIFECYCLE_UNRECONCILED")
    if (
        portfolio is None
        or portfolio.valuation_quality.status is not AvailabilityStatus.AVAILABLE
        or portfolio.total_market_value is None
        or float(portfolio.total_market_value) <= 0
    ):
        return blocked("ADD_PORTFOLIO_VALUATION_UNAVAILABLE")
    if (
        portfolio.current_security_id != snapshot.security_id
        or portfolio.current_security_market_value_eur is None
        or security_weight is None
    ):
        return blocked("ADD_SECURITY_EXPOSURE_UNAVAILABLE")
    if (
        portfolio.allocation_quality.status is not AvailabilityStatus.AVAILABLE
        or portfolio.swing_market_value is None
        or swing_allocation is None
    ):
        return blocked("ADD_SWING_ALLOCATION_UNAVAILABLE")
    # An existing breach is an unconditional block; adding capital may not
    # exploit denominator effects to appear compliant after the purchase.
    if swing_allocation >= config.portfolio.swing_max_pct:
        return blocked("ADD_SWING_ALLOCATION_LIMIT")
    if snapshot.technical.quality.status is not AvailabilityStatus.AVAILABLE:
        return blocked("ADD_TECHNICAL_DATA_UNAVAILABLE")
    technical = snapshot.technical
    if technical.current_price is None or technical.sma50 is None or technical.sma200 is None:
        return blocked("ADD_TECHNICAL_DATA_UNAVAILABLE")
    if float(technical.current_price) <= float(technical.sma50):
        return blocked("ADD_BELOW_OR_EQUAL_SMA50")
    if float(technical.current_price) <= float(technical.sma200):
        return blocked("ADD_BELOW_OR_EQUAL_SMA200")
    if current_eur is None or reference_eur is None or current_eur <= 0:
        return blocked("ADD_PORTFOLIO_VALUATION_UNAVAILABLE")
    if current_eur < reference_eur:
        return blocked("ADD_CAMPAIGN_NOT_PROFITABLE")
    if current_eur == reference_eur:
        return blocked("ADD_BELOW_REFERENCE_COST")
    if portfolio.cash_quality.status is not AvailabilityStatus.AVAILABLE or cash is None:
        return blocked("ADD_CASH_UNAVAILABLE")
    if base <= 0:
        return blocked("ADD_QUANTITY_ZERO")
    if (
        policy.max_security_weight is None
        or policy.minimum_cash_reserve is None
        or policy.whole_share_policy != "floor"
    ):
        return blocked("ADD_QUANTITY_ZERO")

    swing_ceiling = config.portfolio.swing_max_pct
    capital = float(cash)
    if (
        portfolio.buying_power_quality.status is AvailabilityStatus.AVAILABLE
        and portfolio.buying_power is not None
    ):
        capital = min(capital, float(portfolio.buying_power))
    cash_cap = max(0, floor((capital - policy.minimum_cash_reserve) / current_eur))
    security_cap = _cap_quantity(
        current_value=float(portfolio.current_security_market_value_eur),
        total_value=float(portfolio.total_market_value),
        price=current_eur,
        maximum_weight=policy.max_security_weight,
    )
    swing_cap = _cap_quantity(
        current_value=float(portfolio.swing_market_value),
        total_value=float(portfolio.total_market_value),
        price=current_eur,
        maximum_weight=swing_ceiling,
    )
    quantity = float(max(0, min(int(base), cash_cap, security_cap, swing_cap)))
    if quantity <= 0:
        if cash_cap <= 0:
            reason = "ADD_CASH_RESERVE_LIMIT"
        elif security_cap <= 0:
            reason = "ADD_SECURITY_WEIGHT_LIMIT"
        else:
            reason = "ADD_SWING_ALLOCATION_LIMIT"
        return _result([reason], base=base, reference=reference_eur, current=current_eur, security_weight=security_weight, swing_allocation=swing_allocation, cash=cash)

    purchase = quantity * current_eur
    total = float(portfolio.total_market_value) + purchase
    projected_security = (float(portfolio.current_security_market_value_eur) + purchase) / total
    projected_swing = (float(portfolio.swing_market_value) + purchase) / total
    return AddRecommendation(
        eligible=True,
        base_quantity=base,
        recommended_quantity=quantity,
        purchase_value_eur=purchase,
        reference_price_eur=reference_eur,
        current_price_eur=current_eur,
        current_security_weight=security_weight,
        projected_security_weight=projected_security,
        current_swing_allocation=swing_allocation,
        projected_swing_allocation=projected_swing,
        cash_before=float(cash),
        cash_after=float(cash) - purchase,
        block_reasons=(),
    )
