"""Pure, deterministic recommendation rules.

The module accepts contracts only. It deliberately has no SQLite, file, or
network dependency and cannot create lifecycle events or execute a trade.
"""

from __future__ import annotations

from dataclasses import replace
from math import floor
from typing import Optional

from analysis_contracts import (
    Action,
    ActionQuantityBasis,
    AnalysisSnapshot,
    AvailabilityStatus,
    DecisionResult,
    StrategyType,
    PortfolioContext,
)
from add_sizing import evaluate_add_recommendation
from entry_sizing import evaluate_entry_recommendation
from strategy_config import StrategyConfig


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, value))


def _confidence(snapshot: AnalysisSnapshot) -> float:
    """Return the stable documented Phase-2 confidence value."""

    confidence = 0.50
    if snapshot.technical.quality.status is AvailabilityStatus.PARTIAL:
        confidence -= 0.10
    for quality, available_gain, partial_gain in (
        (snapshot.fundamental.quality, 0.10, 0.05),
        (snapshot.valuation.quality, 0.05, 0.02),
        (snapshot.event_risk.quality, 0.05, 0.02),
    ):
        if quality.status is AvailabilityStatus.AVAILABLE:
            confidence += available_gain
        elif quality.status is AvailabilityStatus.PARTIAL:
            confidence += partial_gain
        elif quality.status is AvailabilityStatus.UNAVAILABLE:
            confidence -= 0.05
    return round(_clamp_confidence(confidence), 4)


def _trim_safety_gaps(snapshot: AnalysisSnapshot) -> list[str]:
    """Return each missing prerequisite for a Phase-3B.2 TP trim."""

    position = snapshot.position
    gaps: list[str] = []
    if position.swing_campaign_id is None or position.swing_campaign_status != "open":
        gaps.append("open Swing campaign is unavailable")
    if position.lifecycle_quality.status is not AvailabilityStatus.AVAILABLE:
        gaps.append("Swing campaign lifecycle data is unavailable")
    if position.campaign_reconciliation_quality.status is not AvailabilityStatus.AVAILABLE:
        gaps.append("Swing campaign quantity reconciliation is unavailable")
    if snapshot.technical.quality.status is not AvailabilityStatus.AVAILABLE:
        gaps.append("technical data is not available and fresh")
    if position.shares is None or float(position.shares) <= 0:
        gaps.append("current position quantity is unavailable")
    if position.current_price_cost_currency is None or position.valuation_currency is None:
        gaps.append("currency-safe current valuation is unavailable")
    if position.fx_quality.status not in {
        AvailabilityStatus.AVAILABLE,
        AvailabilityStatus.NOT_APPLICABLE,
    }:
        gaps.append("FX rate is unavailable for current valuation")
    if (
        position.swing_campaign_reference_avg_cost is None
        or float(position.swing_campaign_reference_avg_cost) <= 0
    ):
        gaps.append("campaign reference average cost is unavailable")
    if (
        position.swing_campaign_original_quantity is None
        or float(position.swing_campaign_original_quantity) <= 0
    ):
        gaps.append("campaign original quantity is unavailable")
    reference_currency = position.swing_campaign_reference_currency
    if reference_currency is None or position.valuation_currency is None:
        gaps.append("campaign reference currency is unavailable")
    elif reference_currency.upper() != position.valuation_currency.upper():
        gaps.append("campaign reference currency does not match valuation currency")
    return gaps


def _trim_result(
    *,
    snapshot: AnalysisSnapshot,
    tp1: float,
    tp2: float,
    reasons: list[str],
    risks: list[str],
    blocking_data_gaps: tuple[str, ...],
    desired_remaining: float,
    basis: ActionQuantityBasis,
    stop: float | None = None,
    stop_price_currency: str | None = None,
) -> DecisionResult:
    """Return TRIM only when a positive reduction is outstanding."""

    position = snapshot.position
    action_quantity = max(0.0, float(position.shares) - desired_remaining)
    if action_quantity <= 0:
        return DecisionResult(
            action=Action.HOLD,
            confidence=_confidence(snapshot),
            stop=stop,
            stop_price_currency=stop_price_currency,
            tp1=tp1,
            tp2=tp2,
            target_price_currency=position.swing_campaign_reference_currency,
            reasons=tuple(reasons),
            risks=tuple(risks),
            blocking_data_gaps=blocking_data_gaps,
        )
    reason = (
        "TP1_TRIM_RECOMMENDED"
        if basis is ActionQuantityBasis.TP1_25_PERCENT_ORIGINAL
        else "TP2_CUMULATIVE_TRIM_RECOMMENDED"
    )
    return DecisionResult(
        action=Action.TRIM,
        confidence=_confidence(snapshot),
        stop=stop,
        stop_price_currency=stop_price_currency,
        tp1=tp1,
        tp2=tp2,
        target_price_currency=position.swing_campaign_reference_currency,
        action_quantity=action_quantity,
        action_quantity_basis=basis,
        target_remaining_quantity=desired_remaining,
        reasons=tuple([*reasons, reason]),
        risks=tuple(risks),
        blocking_data_gaps=blocking_data_gaps,
    )


def _hard_stop_evaluation(
    snapshot: AnalysisSnapshot,
    config: StrategyConfig,
) -> tuple[float | None, str | None, tuple[str, ...], DecisionResult | None]:
    """Return the campaign hard-stop context or a conservative terminal result.

    This rule intentionally consumes only the campaign reference and
    currency-safe position valuation. It neither uses technical indicators nor
    persists lifecycle state.
    """

    position = snapshot.position
    gaps: list[str] = []
    if position.strategy is not StrategyType.SWING:
        gaps.append("strategy is not Swing")
    if position.swing_campaign_id is None or position.swing_campaign_status != "open":
        gaps.append("open Swing campaign is unavailable")
    if position.lifecycle_quality.status is not AvailabilityStatus.AVAILABLE:
        gaps.append("Swing campaign lifecycle data is unavailable")
    if position.campaign_reconciliation_quality.status is not AvailabilityStatus.AVAILABLE:
        gaps.append("Swing campaign quantity reconciliation is unavailable")
    if position.shares is None or float(position.shares) <= 0:
        gaps.append("current position quantity is unavailable")
    reference_cost = position.swing_campaign_reference_avg_cost
    reference_currency = position.swing_campaign_reference_currency
    if reference_cost is None or float(reference_cost) <= 0:
        gaps.append("campaign reference average cost is unavailable")
    if not reference_currency:
        gaps.append("campaign reference currency is unavailable")
    if position.current_price_cost_currency is None or position.valuation_currency is None:
        gaps.append("currency-safe current valuation is unavailable")
    elif float(position.current_price_cost_currency) <= 0:
        gaps.append("currency-safe current valuation is invalid")
    if position.fx_quality.status not in {
        AvailabilityStatus.AVAILABLE,
        AvailabilityStatus.NOT_APPLICABLE,
    }:
        gaps.append("FX is unavailable or stale for hard-stop evaluation")
    if (
        reference_currency
        and position.valuation_currency
        and reference_currency.upper() != position.valuation_currency.upper()
    ):
        gaps.append("campaign reference currency does not match valuation currency")

    stop_price = (
        float(reference_cost) * (1.0 - config.swing.hard_stop_loss_pct)
        if reference_cost is not None and float(reference_cost) > 0
        else None
    )
    if gaps:
        # Preserve the existing Phase-2/3 informational output and let its
        # own safety gates suppress TP actions. The caller adds these explicit
        # hard-stop blockers to that conservative result.
        return stop_price, reference_currency, tuple(gaps), None

    assert stop_price is not None
    assert reference_currency is not None
    if float(position.current_price_cost_currency) < stop_price:
        return (
            stop_price,
            reference_currency,
            (),
            DecisionResult(
                action=Action.SELL,
                confidence=_confidence(snapshot),
                stop=stop_price,
                stop_price_currency=reference_currency,
                action_quantity=float(position.shares),
                action_quantity_basis=ActionQuantityBasis.STOP_FULL_EXIT,
                target_remaining_quantity=0.0,
                reasons=("EXISTING_POSITION_DEFAULT_HOLD", "HARD_STOP_TRIGGERED"),
            ),
        )
    return stop_price, reference_currency, (), None


def _with_hard_stop_blockers(
    result: DecisionResult, blockers: tuple[str, ...]
) -> DecisionResult:
    """Preserve existing result semantics while exposing stop-evaluation gaps."""

    if not blockers:
        return result
    return replace(
        result,
        reasons=tuple([*result.reasons, "HARD_STOP_EVALUATION_BLOCKED"]),
        risks=tuple([*result.risks, "HARD_STOP_EVALUATION_BLOCKED"]),
        blocking_data_gaps=tuple([*result.blocking_data_gaps, *blockers]),
    )


def _runner_eligibility_gaps(snapshot: AnalysisSnapshot) -> list[str]:
    """Return non-technical blockers for an explicitly executed runner."""

    position = snapshot.position
    gaps: list[str] = []
    if position.strategy is not StrategyType.SWING:
        gaps.append("Swing strategy is unavailable")
    if position.swing_campaign_id is None or position.swing_campaign_status != "open":
        gaps.append("open Swing campaign is unavailable")
    if position.lifecycle_quality.status is not AvailabilityStatus.AVAILABLE:
        gaps.append("Swing campaign lifecycle data is unavailable")
    if position.campaign_reconciliation_quality.status is not AvailabilityStatus.AVAILABLE:
        gaps.append("Swing campaign quantity reconciliation is unavailable")
    if position.shares is None or float(position.shares) <= 0:
        gaps.append("current runner quantity is unavailable")
    if (
        position.swing_campaign_original_quantity is None
        or float(position.swing_campaign_original_quantity) <= 0
    ):
        gaps.append("campaign original quantity is unavailable")
    else:
        cumulative_target = floor(float(position.swing_campaign_original_quantity) * 0.75)
        executed_quantity = (
            float(position.tp1_executed_quantity)
            + float(position.tp2_executed_quantity)
        )
        if executed_quantity < cumulative_target:
            gaps.append("TP2_EXECUTION_INCOMPLETE")
    if position.post_tp2_add_detected:
        gaps.append("POST_TP2_ADD_POLICY_UNDEFINED")
    return gaps


def _runner_hold(
    snapshot: AnalysisSnapshot,
    *,
    blocker: str,
    detail: str | None = None,
    warning: bool = False,
) -> DecisionResult:
    """Return the conservative no-execution result for runner evaluation."""

    reasons = ["EXISTING_POSITION_DEFAULT_HOLD", blocker]
    return DecisionResult(
        action=Action.HOLD,
        confidence=_confidence(snapshot),
        reasons=tuple(reasons),
        risks=(blocker,),
        blocking_data_gaps=(() if warning else (detail or blocker,)),
    )


def _runner_decision(snapshot: AnalysisSnapshot) -> DecisionResult:
    """Evaluate a fully explicit Swing runner without persistence effects."""

    position = snapshot.position
    gaps = _runner_eligibility_gaps(snapshot)
    if gaps:
        # Keep the explicit codes stable for incomplete TP2 execution and the
        # deliberately undefined post-TP2 add case.
        return _runner_hold(snapshot, blocker=gaps[0], detail="; ".join(gaps))

    technical = snapshot.technical
    if technical.quality.status is not AvailabilityStatus.AVAILABLE:
        return _runner_hold(
            snapshot,
            blocker="RUNNER_TECHNICAL_INPUT_UNAVAILABLE",
            detail=f"technical quality is {technical.quality.status.value}",
        )
    missing = [
        name
        for name, value in (
            ("technical.current_price", technical.current_price),
            ("technical.sma50", technical.sma50),
            ("technical.sma200", technical.sma200),
        )
        if value is None
    ]
    if missing:
        return _runner_hold(
            snapshot,
            blocker="RUNNER_TECHNICAL_INPUT_UNAVAILABLE",
            detail="missing " + ", ".join(missing),
        )

    current_price = float(technical.current_price)
    sma50 = float(technical.sma50)
    sma200 = float(technical.sma200)
    # SMA200 loss has precedence over the SMA50 warning path.
    if current_price < sma200:
        return DecisionResult(
            action=Action.SELL,
            confidence=_confidence(snapshot),
            action_quantity=float(position.shares),
            action_quantity_basis=ActionQuantityBasis.RUNNER_FULL_REMAINDER,
            target_remaining_quantity=0.0,
            reasons=("EXISTING_POSITION_DEFAULT_HOLD", "RUNNER_BELOW_SMA200"),
        )
    if current_price < sma50:
        return _runner_hold(
            snapshot,
            blocker="RUNNER_BELOW_SMA50",
            warning=True,
        )
    return DecisionResult(
        action=Action.HOLD,
        confidence=_confidence(snapshot),
        reasons=("EXISTING_POSITION_DEFAULT_HOLD",),
    )


def _with_add_recommendation(
    result: DecisionResult,
    snapshot: AnalysisSnapshot,
    config: StrategyConfig,
    portfolio_context: Optional[PortfolioContext],
) -> DecisionResult:
    """Attach ADD context and promote only a default HOLD to recommendation.

    This function is called exclusively after the established stop, runner,
    and TP reduction paths have declined to act.  It has no persistence effect.
    """
    if result.action is not Action.HOLD:
        return result
    add = evaluate_add_recommendation(snapshot, portfolio_context, config)
    fields = {
        "add_eligible": add.eligible,
        "add_base_quantity": add.base_quantity,
        "add_recommended_quantity": add.recommended_quantity,
        "add_purchase_value_eur": add.purchase_value_eur,
        "add_reference_price_eur": add.reference_price_eur,
        "add_current_price_eur": add.current_price_eur,
        "add_current_security_weight": add.current_security_weight,
        "add_projected_security_weight": add.projected_security_weight,
        "add_current_swing_allocation": add.current_swing_allocation,
        "add_projected_swing_allocation": add.projected_swing_allocation,
        "add_cash_before": add.cash_before,
        "add_cash_after": add.cash_after,
        "add_block_reasons": add.block_reasons,
    }
    if not add.eligible or not add.recommended_quantity:
        return replace(result, **fields)
    return replace(
        result,
        **fields,
        action=Action.ADD,
        action_quantity=add.recommended_quantity,
        action_quantity_basis=ActionQuantityBasis.ADD_25_PERCENT_ORIGINAL,
        reasons=tuple([*result.reasons, "ADD_RECOMMENDED"]),
    )


def _entry_decision(
    snapshot: AnalysisSnapshot,
    config: StrategyConfig,
    portfolio_context: Optional[PortfolioContext],
) -> DecisionResult:
    """Attach a zero-position initial-entry recommendation without mutation."""
    entry = evaluate_entry_recommendation(snapshot, portfolio_context, config)
    fields = {
        "entry_eligible": entry.eligible,
        "entry_recommended_quantity": entry.recommended_quantity,
        "entry_purchase_value_eur": entry.purchase_value_eur,
        "entry_current_price_eur": entry.current_price_eur,
        "entry_sma50": snapshot.technical.sma50,
        "entry_sma200": snapshot.technical.sma200,
        "entry_rsi14": snapshot.technical.rsi14,
        "entry_momentum_score": snapshot.technical.momentum_score,
        "entry_current_security_weight": entry.current_security_weight,
        "entry_projected_security_weight": entry.projected_security_weight,
        "entry_current_swing_allocation": entry.current_swing_allocation,
        "entry_projected_swing_allocation": entry.projected_swing_allocation,
        "entry_initial_position_weight": entry.initial_position_weight,
        "entry_cash_before": entry.cash_before,
        "entry_cash_after": entry.cash_after,
        "entry_block_reasons": entry.block_reasons,
    }
    if not entry.eligible or not entry.recommended_quantity:
        return DecisionResult(
            action=Action.WATCH,
            confidence=_confidence(snapshot),
            reasons=("NO_POSITION_DEFAULT_WATCH",),
            **fields,
        )
    return DecisionResult(
        action=Action.BUY,
        confidence=_confidence(snapshot),
        action_quantity=entry.recommended_quantity,
        action_quantity_basis=ActionQuantityBasis.INITIAL_ENTRY_SIZING,
        reasons=("NEW_ENTRY_RECOMMENDED",),
        **fields,
    )


def decide(
    snapshot: AnalysisSnapshot,
    config: StrategyConfig,
    portfolio_context: Optional[PortfolioContext] = None,
) -> DecisionResult:
    """Produce a conservative recommendation for a supplied snapshot.

    Phase 3B.2 adds TP ``TRIM`` recommendations and Phase 3B.3 adds a
    completed-runner ``SELL`` recommendation. The result has no persistence
    or execution effect.
    """

    if not snapshot.position.state_supported:
        gap = "historical position and watchlist state are unsupported"
        return DecisionResult(
            action=Action.WATCH,
            confidence=0.10,
            reasons=("POSITION_STATE_DATA_BLOCKER",),
            risks=(gap,),
            blocking_data_gaps=(gap,),
        )

    position = snapshot.position
    if not position.has_position:
        return _entry_decision(snapshot, config, portfolio_context)

    hard_stop_price = None
    hard_stop_currency = None
    hard_stop_blockers: tuple[str, ...] = ()
    # TP2 completion hands control to Phase 3B.3 runner logic. Before that,
    # Phase 3B.4 evaluates absolute loss protection before technical gates.
    if (
        position.strategy is StrategyType.SWING
        and position.tp2_lifecycle_status != "executed"
        and position.swing_campaign_id is not None
    ):
        (
            hard_stop_price,
            hard_stop_currency,
            hard_stop_blockers,
            hard_stop_result,
        ) = _hard_stop_evaluation(snapshot, config)
        if hard_stop_result is not None:
            return hard_stop_result

    # A completed TP2 runner handles bad technical inputs conservatively as a
    # HOLD, rather than turning an existing runner into a generic WATCH.
    if (
        position.strategy is StrategyType.SWING
        and position.tp2_lifecycle_status == "executed"
    ):
        return _runner_decision(snapshot)

    technical_status = snapshot.technical.quality.status
    if technical_status in {
        AvailabilityStatus.UNAVAILABLE,
        AvailabilityStatus.INSUFFICIENT,
        AvailabilityStatus.STALE,
    }:
        if technical_status is AvailabilityStatus.INSUFFICIENT:
            gap = "technical history is insufficient"
        else:
            gap = f"technical data is {technical_status.value}"
        return _with_add_recommendation(_with_hard_stop_blockers(DecisionResult(
            action=Action.WATCH,
            confidence=0.10,
            stop=hard_stop_price,
            stop_price_currency=hard_stop_currency,
            reasons=("TECHNICAL_DATA_BLOCKER",),
            risks=(gap,),
            blocking_data_gaps=(gap,),
        ), hard_stop_blockers), snapshot, config, portfolio_context)

    reasons = ["EXISTING_POSITION_DEFAULT_HOLD"]
    risks: list[str] = []
    blocking_data_gaps: tuple[str, ...] = ()
    tp1 = None
    tp2 = None
    is_swing = position.strategy is StrategyType.SWING
    has_campaign = position.swing_campaign_id is not None
    campaign_reference_available = (
        has_campaign
        and position.swing_campaign_reference_avg_cost is not None
        and float(position.swing_campaign_reference_avg_cost) > 0
    )
    # An explicit campaign fixes the TP baseline permanently.  Falling back to
    # mutable position cost data would make a partial campaign unsafe.
    if has_campaign:
        reference_cost = (
            float(position.swing_campaign_reference_avg_cost)
            if campaign_reference_available
            else None
        )
        target_currency = position.swing_campaign_reference_currency
    else:
        reference_cost = float(position.avg_cost) if position.avg_cost is not None else None
        target_currency = position.cost_basis_currency or position.currency
    if is_swing and reference_cost is not None and position.current_price_native is not None:
        if position.current_price_cost_currency is not None:
            tp1 = reference_cost * (1.0 + config.swing.tp1_gain_pct)
            tp2 = reference_cost * (1.0 + config.swing.tp2_gain_pct)
            if float(position.current_price_cost_currency) >= tp1:
                reasons.append("TP1_REACHED")
            if float(position.current_price_cost_currency) >= tp2:
                reasons.append("TP2_REACHED")
        elif position.fx_quality.status is AvailabilityStatus.STALE:
            gap = "FX rate is stale"
            risks.extend(("SWING_TP_FX_STALE", gap))
            blocking_data_gaps = (gap,)
        else:
            gap = "FX rate is unavailable"
            risks.extend(("SWING_TP_FX_UNAVAILABLE", gap))
            blocking_data_gaps = (gap,)
    elif is_swing:
        if has_campaign and reference_cost is None:
            gap = "campaign reference average cost is unavailable"
            risks.extend(("SWING_TP_CAMPAIGN_REFERENCE_UNAVAILABLE", gap))
            blocking_data_gaps = (gap,)
        else:
            risks.append("SWING_TARGETS_UNAVAILABLE_WITHOUT_COST_AND_CURRENT_PRICE")

    # Informational TP signals are retained without a campaign, but a Phase
    # 3B.2 recommendation requires a valid explicit campaign baseline.
    if not is_swing or tp1 is None or tp2 is None:
        return _with_add_recommendation(_with_hard_stop_blockers(DecisionResult(
            action=Action.HOLD,
            confidence=_confidence(snapshot),
            stop=hard_stop_price,
            stop_price_currency=hard_stop_currency,
            tp1=tp1,
            tp2=tp2,
            target_price_currency=target_currency if tp1 is not None else None,
            reasons=tuple(reasons),
            risks=tuple(risks),
            blocking_data_gaps=blocking_data_gaps,
        ), hard_stop_blockers), snapshot, config, portfolio_context)

    safety_gaps = _trim_safety_gaps(snapshot)
    if safety_gaps:
        risks.extend(f"SWING_TP_TRIM_BLOCKED: {gap}" for gap in safety_gaps)
        return _with_add_recommendation(_with_hard_stop_blockers(DecisionResult(
            action=Action.HOLD,
            confidence=_confidence(snapshot),
            stop=hard_stop_price,
            stop_price_currency=hard_stop_currency,
            tp1=tp1,
            tp2=tp2,
            target_price_currency=target_currency,
            reasons=tuple(reasons),
            risks=tuple(risks),
            blocking_data_gaps=tuple([*blocking_data_gaps, *safety_gaps]),
        ), hard_stop_blockers), snapshot, config, portfolio_context)

    original_quantity = float(position.swing_campaign_original_quantity)
    current_valuation = float(position.current_price_cost_currency)
    # TP2 takes precedence: a single cumulative catch-up recommendation.
    if current_valuation >= tp2 and position.tp2_lifecycle_status != "executed":
        desired_remaining = original_quantity - floor(original_quantity * 0.75)
        return _with_hard_stop_blockers(_trim_result(
            snapshot=snapshot,
            tp1=tp1,
            tp2=tp2,
            reasons=reasons,
            risks=risks,
            blocking_data_gaps=blocking_data_gaps,
            desired_remaining=desired_remaining,
            basis=ActionQuantityBasis.TP2_75_PERCENT_CUMULATIVE_ORIGINAL,
            stop=hard_stop_price,
            stop_price_currency=hard_stop_currency,
        ), hard_stop_blockers)
    if (
        current_valuation >= tp1
        and current_valuation < tp2
        and position.tp1_lifecycle_status != "executed"
        and position.tp2_lifecycle_status != "executed"
    ):
        desired_remaining = original_quantity - floor(original_quantity * 0.25)
        return _with_hard_stop_blockers(_trim_result(
            snapshot=snapshot,
            tp1=tp1,
            tp2=tp2,
            reasons=reasons,
            risks=risks,
            blocking_data_gaps=blocking_data_gaps,
            desired_remaining=desired_remaining,
            basis=ActionQuantityBasis.TP1_25_PERCENT_ORIGINAL,
            stop=hard_stop_price,
            stop_price_currency=hard_stop_currency,
        ), hard_stop_blockers)
    return _with_add_recommendation(_with_hard_stop_blockers(DecisionResult(
        action=Action.HOLD,
        confidence=_confidence(snapshot),
        stop=hard_stop_price,
        stop_price_currency=hard_stop_currency,
        tp1=tp1,
        tp2=tp2,
        target_price_currency=target_currency,
        reasons=tuple(reasons),
        risks=tuple(risks),
        blocking_data_gaps=blocking_data_gaps,
    ), hard_stop_blockers), snapshot, config, portfolio_context)
