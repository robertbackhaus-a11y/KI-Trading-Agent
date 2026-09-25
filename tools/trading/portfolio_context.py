"""Pure portfolio allocation calculations with no persistence dependency."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from analysis_contracts import (
    AvailabilityStatus,
    DataQuality,
    PortfolioContext,
    StrategyType,
)
from strategy_config import PortfolioTargetConfig, SizingPolicyConfig


@dataclass(frozen=True)
class CapitalUseProjection:
    """Informational result for a proposed EUR capital addition.

    ``allocation_permitted`` is intentionally optional: ``None`` means the
    proposal or a complete allocation denominator is unavailable.  It never
    means a proposed amount of zero.
    """

    strategy: StrategyType
    proposed_amount_eur: Optional[float]
    projected_total_market_value: Optional[float]
    projected_strategy_market_value: Optional[float]
    projected_strategy_weight_pct: Optional[float]
    allocation_permitted: Optional[bool]
    quality: DataQuality


@dataclass(frozen=True)
class SizingReadiness:
    """Pure statement of whether future ADD sizing inputs are complete."""

    can_compute_add_sizing: bool
    missing_requirements: tuple[str, ...]


def evaluate_sizing_readiness(
    context: PortfolioContext,
    policy: SizingPolicyConfig,
) -> SizingReadiness:
    """Return explicit missing inputs without calculating an ADD quantity."""

    missing: list[str] = []
    if context.valuation_quality.status is not AvailabilityStatus.AVAILABLE:
        missing.append("portfolio valuation is unavailable")
    if context.allocation_quality.status is not AvailabilityStatus.AVAILABLE:
        missing.append("portfolio allocation is unavailable")
    if (
        context.cash_available is None
        or context.cash_quality.status is not AvailabilityStatus.AVAILABLE
    ):
        missing.append("available cash is unavailable")
    if policy.max_security_weight is None:
        missing.append("max security weight is undefined")
    if policy.max_add_pct_of_original is None:
        missing.append("max ADD policy is undefined")
    if policy.minimum_cash_reserve is None:
        missing.append("minimum cash reserve is undefined")
    if policy.whole_share_policy is None:
        missing.append("rounding policy is undefined")
    return SizingReadiness(not missing, tuple(missing))


def derive_allocation_guardrails(
    *,
    swing_weight_pct: Optional[float],
    long_term_weight_pct: Optional[float],
    allocation_quality: DataQuality,
    config: PortfolioTargetConfig,
) -> tuple[str, ...]:
    """Return informational allocation states, never a trading action."""

    if allocation_quality.status is not AvailabilityStatus.AVAILABLE:
        return ()
    if swing_weight_pct is None or long_term_weight_pct is None:
        return ()

    states: list[str] = []
    if swing_weight_pct > config.swing_max_pct * 100.0:
        states.append("SWING_ALLOCATION_ABOVE_MAX")
    elif swing_weight_pct < config.swing_min_pct * 100.0:
        states.append("SWING_ALLOCATION_BELOW_MIN")

    if long_term_weight_pct > config.long_term_max_pct * 100.0:
        states.append("LONG_TERM_ALLOCATION_ABOVE_MAX")
    elif long_term_weight_pct < config.long_term_min_pct * 100.0:
        states.append("LONG_TERM_ALLOCATION_BELOW_MIN")

    return tuple(states) if states else ("WITHIN_TARGET_RANGE",)


def project_strategy_capital_use(
    context: PortfolioContext,
    strategy: StrategyType,
    proposed_amount_eur: Optional[float],
    config: PortfolioTargetConfig,
) -> CapitalUseProjection:
    """Project a non-negative EUR addition against a strategy ceiling.

    This is a pure informational guardrail.  It neither checks cash nor
    creates a recommendation, and it refuses to calculate from an incomplete
    portfolio valuation.
    """

    if proposed_amount_eur is None:
        return CapitalUseProjection(
            strategy=strategy,
            proposed_amount_eur=None,
            projected_total_market_value=None,
            projected_strategy_market_value=None,
            projected_strategy_weight_pct=None,
            allocation_permitted=None,
            quality=DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                ("proposed additional EUR amount is unknown",),
                context.evaluation_as_of,
            ),
        )
    amount = float(proposed_amount_eur)
    if amount < 0:
        raise ValueError("proposed additional EUR amount must be non-negative")
    if strategy not in {StrategyType.SWING, StrategyType.LONG_TERM}:
        raise ValueError("capital-use projection supports only swing or long_term")
    if (
        context.allocation_quality.status is not AvailabilityStatus.AVAILABLE
        or context.total_market_value is None
        or context.swing_market_value is None
        or context.long_term_market_value is None
    ):
        return CapitalUseProjection(
            strategy=strategy,
            proposed_amount_eur=amount,
            projected_total_market_value=None,
            projected_strategy_market_value=None,
            projected_strategy_weight_pct=None,
            allocation_permitted=None,
            quality=DataQuality(
                AvailabilityStatus.UNAVAILABLE,
                ("complete portfolio allocation is unavailable",),
                context.evaluation_as_of,
            ),
        )

    current_strategy_value = (
        context.swing_market_value
        if strategy is StrategyType.SWING
        else context.long_term_market_value
    )
    projected_total = float(context.total_market_value) + amount
    projected_strategy = float(current_strategy_value) + amount
    projected_weight = projected_strategy / projected_total * 100.0
    ceiling = (
        config.swing_max_pct
        if strategy is StrategyType.SWING
        else config.long_term_max_pct
    ) * 100.0
    return CapitalUseProjection(
        strategy=strategy,
        proposed_amount_eur=amount,
        projected_total_market_value=projected_total,
        projected_strategy_market_value=projected_strategy,
        projected_strategy_weight_pct=projected_weight,
        allocation_permitted=projected_weight <= ceiling,
        quality=DataQuality(AvailabilityStatus.AVAILABLE, (), context.evaluation_as_of),
    )


def is_strategy_addition_permitted(
    context: PortfolioContext,
    strategy: StrategyType,
    proposed_amount_eur: Optional[float],
    config: PortfolioTargetConfig,
) -> Optional[bool]:
    """Convenience form of :func:`project_strategy_capital_use`."""

    return project_strategy_capital_use(
        context, strategy, proposed_amount_eur, config
    ).allocation_permitted
