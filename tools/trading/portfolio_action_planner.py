"""Deterministic Portfolio Action Planner (pure, read-only, no orders).

Flow::

    existing SELL/TRIM (+ ADD/BUY)  ->  POST-ACTION CAPITAL
    PROMOTE  ->  ENTRY-EVALUATION  ->  RANKING  ->  POSITION SIZING  ->  PLANNED ENTRY

``PROMOTE`` is **not** a BUY. It is only the input of the entry evaluation; the
plan is a simulation and never creates a strategy assignment, a campaign, a
transaction or an order.

Everything here is a pure function of its inputs. Nothing in this module reads
a database, calls an external source, or involves an LLM. Capital rules are not
re-implemented: eligibility and sizing reuse
:func:`entry_sizing.evaluate_entry_recommendation` (trend filter, Swing maximum,
cash reserve, per-security weight limits, whole-share floor) against the
simulated post-action capital state; the allocation guardrails reuse
:func:`portfolio_context.derive_allocation_guardrails`.

Taxes: SELL/TRIM proceeds enter the capital state **net** of an estimated tax
(``strategy_config.TaxConfig``, 26.375 % of the realized gain, never of the gross
proceeds). The estimate uses ``positions.avg_cost`` -- an average-cost basis, **not**
FIFO lots -- and is marked ``estimated_from_average_cost``. Planning uses the
conservative view (no loss offset); a second, indicative view offsets losses of the
planned actions against their gains and is only shown. Loss pot, tax-free allowance
and this year's realized results are unknown and never assumed. A loss never creates
a negative tax or extra cash.

Ranking (``ENTRY_READY`` only)::

    entry_score = momentum_score * confidence

``momentum_score`` is the existing analytics score (also used by
``rank_watchlist``); ``confidence`` is the existing ``decision_engine._confidence``
data-quality value (technical/fundamental/valuation/event-risk quality).
Ties break on a higher watchlist priority, then the lower ``security_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Sequence

from analysis_contracts import (
    AnalysisSnapshot,
    AvailabilityStatus,
    DataQuality,
    PortfolioContext,
    StrategyType,
)
from candidate_decision import BUY_SCORE_THRESHOLD
from decision_engine import _confidence
from entry_sizing import _weight_cap_value, evaluate_entry_recommendation
from orchestrator_presentation import _fmt_eur, _fmt_pct, _fmt_quantity
from portfolio_context import derive_allocation_guardrails
from strategy_config import StrategyConfig, TaxConfig


ENTRY_READY = "ENTRY_READY"
WAIT_FOR_TRIGGER = "WAIT_FOR_TRIGGER"
BLOCKED_BY_ALLOCATION = "BLOCKED_BY_ALLOCATION"
BLOCKED_BY_DATA = "BLOCKED_BY_DATA"
BLOCKED_BY_CONCENTRATION = "BLOCKED_BY_CONCENTRATION"
BLOCKED_EXISTING_POSITION = "BLOCKED_EXISTING_POSITION"
BLOCKED_EXISTING_CAMPAIGN = "BLOCKED_EXISTING_CAMPAIGN"

ENTRY_STATUSES = (
    ENTRY_READY,
    WAIT_FOR_TRIGGER,
    BLOCKED_BY_ALLOCATION,
    BLOCKED_BY_DATA,
    BLOCKED_BY_CONCENTRATION,
    BLOCKED_EXISTING_POSITION,
    BLOCKED_EXISTING_CAMPAIGN,
)

# entry_sizing block reason -> planner status
_REASON_STATUS = {
    "ENTRY_EXISTING_POSITION": BLOCKED_EXISTING_POSITION,
    "ENTRY_OPEN_CAMPAIGN_EXISTS": BLOCKED_EXISTING_CAMPAIGN,
    "ENTRY_SECURITY_WEIGHT_LIMIT": BLOCKED_BY_CONCENTRATION,
    "ENTRY_INITIAL_WEIGHT_LIMIT": BLOCKED_BY_CONCENTRATION,
    "ENTRY_SWING_ALLOCATION_LIMIT": BLOCKED_BY_ALLOCATION,
    "ENTRY_CASH_RESERVE_LIMIT": BLOCKED_BY_ALLOCATION,
    "ENTRY_QUANTITY_ZERO": BLOCKED_BY_ALLOCATION,
    "ENTRY_PRICE_BELOW_OR_EQUAL_SMA50": WAIT_FOR_TRIGGER,
    "ENTRY_PRICE_BELOW_OR_EQUAL_SMA200": WAIT_FOR_TRIGGER,
    "ENTRY_SMA50_BELOW_OR_EQUAL_SMA200": WAIT_FOR_TRIGGER,
}
# entry_sizing block reason -> reason of an ENTRY_READY candidate that cannot be funded
_DEFERRAL_REASON = {
    "ENTRY_SWING_ALLOCATION_LIMIT": "SWING_MAX_REACHED",
    "ENTRY_CASH_RESERVE_LIMIT": "CASH_RESERVE_REACHED",
    "ENTRY_QUANTITY_ZERO": "ALLOWED_CAPITAL_BELOW_ONE_SHARE_PRICE",
    "ENTRY_SECURITY_WEIGHT_LIMIT": "CONCENTRATION_LIMIT",
    "ENTRY_INITIAL_WEIGHT_LIMIT": "CONCENTRATION_LIMIT",
}
_EPS = 1e-9


@dataclass(frozen=True)
class _State:
    """Simulated capital state; weights are percent of the invested market value."""

    total: float
    swing: float
    long_term: float
    cash: Optional[float]

    @property
    def swing_pct(self) -> float:
        return self.swing / self.total * 100.0 if self.total > 0 else 0.0

    @property
    def long_term_pct(self) -> float:
        return self.long_term / self.total * 100.0 if self.total > 0 else 0.0

    def after_sale(self, strategy: Optional[str], proceeds: float, cash_in: Optional[float] = None) -> "_State":
        """The position value falls by the gross ``proceeds``; cash rises by ``cash_in`` (net of tax)."""
        received = proceeds if cash_in is None else cash_in
        return _State(
            total=self.total - proceeds,
            swing=self.swing - proceeds if strategy == "swing" else self.swing,
            long_term=self.long_term - proceeds if strategy == "long_term" else self.long_term,
            cash=None if self.cash is None else self.cash + received,
        )

    def after_purchase(self, value: float) -> "_State":
        return _State(
            total=self.total + value,
            swing=self.swing + value,
            long_term=self.long_term,
            cash=None if self.cash is None else self.cash - value,
        )

    def as_portfolio(self, base: PortfolioContext, security_id: int) -> PortfolioContext:
        return replace(
            base,
            total_market_value=self.total,
            swing_market_value=self.swing,
            swing_weight_pct=self.swing_pct,
            long_term_market_value=self.long_term,
            long_term_weight_pct=self.long_term_pct,
            cash_available=self.cash,
            current_security_id=security_id,
            current_security_market_value=0.0,
            current_security_market_value_eur=0.0,
            current_security_weight_pct=0.0,
        )


def _initial_state(portfolio: PortfolioContext) -> Optional[_State]:
    if (
        portfolio.valuation_quality.status is not AvailabilityStatus.AVAILABLE
        or portfolio.allocation_quality.status is not AvailabilityStatus.AVAILABLE
        or portfolio.total_market_value is None
        or portfolio.swing_market_value is None
        or portfolio.long_term_market_value is None
    ):
        return None
    cash = (
        float(portfolio.cash_available)
        if portfolio.cash_available is not None and portfolio.cash_quality.status is AvailabilityStatus.AVAILABLE
        else None
    )
    return _State(float(portfolio.total_market_value), float(portfolio.swing_market_value), float(portfolio.long_term_market_value), cash)


def _state_view(state: _State, config: StrategyConfig) -> dict[str, Any]:
    swing_max = config.portfolio.swing_max_pct
    reserve = config.sizing.minimum_cash_reserve
    guardrails = derive_allocation_guardrails(
        swing_weight_pct=state.swing_pct,
        long_term_weight_pct=state.long_term_pct,
        allocation_quality=DataQuality(AvailabilityStatus.AVAILABLE),
        config=config.portfolio,
    )
    return {
        "total_market_value_eur": state.total,
        "swing_value_eur": state.swing,
        "swing_pct": state.swing_pct,
        "long_term_value_eur": state.long_term,
        "long_term_pct": state.long_term_pct,
        "cash_eur": state.cash,
        "deployable_cash_eur": None if state.cash is None or reserve is None else max(0.0, state.cash - reserve),
        "remaining_swing_capacity_eur": _weight_cap_value(total=state.total, current=state.swing, maximum=swing_max),
        "remaining_swing_capacity_pct": max(0.0, swing_max * 100.0 - state.swing_pct),
        "allocation_guardrails": list(guardrails),
    }


def _float(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


TAX_QUALITY_AVERAGE_COST = "estimated_from_average_cost"
TAX_QUALITY_UPPER_BOUND = "cost_basis_unavailable_upper_bound"
_TAX_UNKNOWN_INPUTS = ["loss_pot", "tax_free_allowance", "realized_gains_and_losses_current_year"]


def _estimate_taxes(
    sales: list[dict[str, Any]],
    cost_basis: Optional[Mapping[int, Mapping[str, Any]]],
    tax: TaxConfig,
) -> dict[str, Any]:
    """Estimate the tax of the planned SELL/TRIM actions (mutates only the planner's own item dicts).

    Conservative view (used for planning): ``max(gain, 0) * rate`` per action, no
    offset. Indicative view: losses of the planned actions offset the gains of the
    same plan; the resulting tax is spread over the gaining actions in proportion
    to their gain. A loss never yields a negative tax. If the cost basis of an
    action is unavailable (or not in EUR) its whole proceeds are taxed as an upper
    bound -- never an assumed advantage. ``cost_basis=None`` means no estimate was
    requested: proceeds stay gross.
    """
    rate = tax.combined_rate
    gross_total = sum(sale["gross"] for sale in sales)
    parameters = {
        "capital_gains_tax_rate": tax.capital_gains_tax_rate,
        "solidarity_surcharge_rate": tax.solidarity_surcharge_rate,
        "church_tax_rate": tax.church_tax_rate,
        "combined_rate_on_realized_gain": rate,
    }
    common = {
        "parameters": parameters,
        "cost_basis_method": "average_cost",
        "fifo_implemented": False,
        "unknown_inputs": list(_TAX_UNKNOWN_INPUTS),
        "planning_basis": "net_proceeds_total_conservative",
    }
    if cost_basis is None:
        for sale in sales:
            sale["net"] = sale["gross"]
        return {**common, "tax_estimate_quality": "not_estimated", "planning_basis": "gross", "gross_proceeds_total_eur": gross_total,
                "estimated_tax_total_conservative": None, "estimated_tax_total_with_plan_offset": None,
                "net_proceeds_total_conservative": None, "net_proceeds_total_with_plan_offset": None}
    for sale in sales:
        basis = cost_basis.get(int(sale["security_id"])) if sale["security_id"] is not None else None
        average = _float((basis or {}).get("avg_cost"))
        known = average is not None and average >= 0.0 and str((basis or {}).get("currency") or "").upper() == "EUR"
        if known:
            sale["cost"] = sale["quantity"] * average
            sale["gain"] = sale["gross"] - sale["cost"]
            sale["quality"] = TAX_QUALITY_AVERAGE_COST
        else:
            sale["cost"] = None
            sale["gain"] = sale["gross"]  # upper bound: all proceeds taxable
            sale["quality"] = TAX_QUALITY_UPPER_BOUND
    gains = sum(max(sale["gain"], 0.0) for sale in sales)
    losses = sum(min(sale["gain"], 0.0) for sale in sales)
    offset_total_tax = max(gains + losses, 0.0) * rate
    for sale in sales:
        gain = sale["gain"]
        sale["tax_conservative"] = max(gain, 0.0) * rate
        sale["tax_offset"] = offset_total_tax * gain / gains if gain > 0.0 and gains > 0.0 else 0.0
        sale["net"] = sale["gross"] - sale["tax_conservative"]
        sale["net_offset"] = sale["gross"] - sale["tax_offset"]
    qualities = {sale["quality"] for sale in sales}
    quality = "not_applicable" if not sales else (TAX_QUALITY_AVERAGE_COST if qualities == {TAX_QUALITY_AVERAGE_COST} else (TAX_QUALITY_UPPER_BOUND if qualities == {TAX_QUALITY_UPPER_BOUND} else "partial_cost_basis_unavailable"))
    tax_conservative_total = sum(sale["tax_conservative"] for sale in sales)
    tax_offset_total = sum(sale["tax_offset"] for sale in sales)
    return {
        **common,
        "tax_estimate_quality": quality,
        "gross_proceeds_total_eur": gross_total,
        "estimated_tax_total_conservative": tax_conservative_total,
        "estimated_tax_total_with_plan_offset": tax_offset_total,
        "net_proceeds_total_conservative": gross_total - tax_conservative_total,
        "net_proceeds_total_with_plan_offset": gross_total - tax_offset_total,
    }


def _existing_actions(
    records: Sequence[Mapping[str, Any]],
    state: Optional[_State],
    warnings: list[str],
    *,
    cost_basis: Optional[Mapping[int, Mapping[str, Any]]] = None,
    tax: Optional[TaxConfig] = None,
) -> tuple[list[dict[str, Any]], Optional[_State], dict[str, Any], float]:
    """Summarise actionable positions and fold them into the capital state.

    SELL/TRIM add proceeds (qty x current EUR price, never more than the held
    quantity) -- net of the conservative tax estimate when cost bases were
    supplied, gross otherwise. ADD and initial-entry BUY recommendations are
    existing purchases: they consume cash and Swing capacity before any new entry.
    """
    items: list[dict[str, Any]] = []
    sales: list[dict[str, Any]] = []
    purchases: list[float] = []
    for record in records:
        decision = record.get("decision") or {}
        action = decision.get("action")
        symbol = record.get("symbol")
        base = {
            "security_id": record.get("security_id"),
            "symbol": symbol,
            "action": action,
            "strategy": record.get("strategy"),
            "priority": record.get("priority"),
            "reasons": list(decision.get("reasons") or ()),
        }
        if action in ("SELL", "TRIM"):
            quantity, price, held = _float(decision.get("action_quantity")), _float(record.get("valuation_price_eur")), _float(record.get("position_quantity"))
            if quantity is None or price is None or quantity <= 0:
                warnings.append(f"PROCEEDS_UNAVAILABLE:{symbol}")
                items.append({**base, "quantity": quantity, "price_eur": price, "estimated_proceeds_eur": None})
                continue
            quantity = min(quantity, held) if held is not None else quantity
            proceeds = quantity * price
            item = {**base, "quantity": quantity, "price_eur": price, "estimated_proceeds_eur": proceeds}
            sales.append({"item": item, "security_id": record.get("security_id"), "strategy": record.get("strategy"), "quantity": quantity, "gross": proceeds})
            items.append(item)
        elif action in ("ADD", "BUY"):
            value = _float(decision.get("add_purchase_value_eur" if action == "ADD" else "entry_purchase_value_eur"))
            quantity = _float(decision.get("add_recommended_quantity" if action == "ADD" else "entry_recommended_quantity"))
            price = _float(decision.get("add_current_price_eur" if action == "ADD" else "entry_current_price_eur"))
            if value is None or value <= 0:
                warnings.append(f"PURCHASE_VALUE_UNAVAILABLE:{symbol}")
                items.append({**base, "quantity": quantity, "price_eur": price, "estimated_purchase_eur": None})
                continue
            purchases.append(value)
            items.append({**base, "quantity": quantity, "price_eur": price, "estimated_purchase_eur": value})
    tax_summary = _estimate_taxes(sales, cost_basis, tax or TaxConfig())
    for sale in sales:
        item = sale["item"]
        item["gross_proceeds_eur"] = sale["gross"]
        if cost_basis is not None:
            item.update({
                "estimated_cost_basis_eur": sale["cost"],
                "estimated_realized_gain_eur": sale["gain"] if sale["cost"] is not None else None,
                "estimated_tax_eur_conservative": sale["tax_conservative"],
                "estimated_tax_eur_with_plan_offset": sale["tax_offset"],
                "estimated_net_proceeds_eur_conservative": sale["net"],
                "estimated_net_proceeds_eur_with_plan_offset": sale["net_offset"],
                "tax_estimate_quality": sale["quality"],
            })
    items.sort(key=lambda item: (str(item.get("priority") or "P9"), int(item.get("security_id") or 0)))
    purchases_total = sum(purchases)
    if state is not None:
        for sale in sales:
            state = state.after_sale(sale["strategy"], sale["gross"], sale["net"])
        for value in purchases:
            state = state.after_purchase(value)
    return items, state, tax_summary, purchases_total


def _as_swing_candidate(snapshot: AnalysisSnapshot) -> AnalysisSnapshot:
    """Hypothetical 'if promoted' view; in-memory only, nothing is persisted."""
    quality = DataQuality(AvailabilityStatus.AVAILABLE, (), snapshot.position.strategy_quality.as_of)
    return replace(snapshot, position=replace(snapshot.position, strategy=StrategyType.SWING, strategy_quality=quality))


def _quality_view(snapshot: Optional[AnalysisSnapshot]) -> Optional[dict[str, str]]:
    if snapshot is None:
        return None
    return {
        "technical": snapshot.technical.quality.status.value,
        "fundamental": snapshot.fundamental.quality.status.value,
        "valuation": snapshot.valuation.quality.status.value,
        "event_risk": snapshot.event_risk.quality.status.value,
    }


def _candidate_base(item: Mapping[str, Any], snapshot: Optional[AnalysisSnapshot]) -> dict[str, Any]:
    momentum = _float(snapshot.technical.momentum_score) if snapshot is not None else None
    confidence = _confidence(snapshot) if snapshot is not None else None
    price = _float(snapshot.position.current_price_cost_currency) if snapshot is not None else _float(item.get("current_price_eur"))
    return {
        "security_id": item.get("security_id"),
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "entry_status": None,
        "reason_codes": [],
        "rank": None,
        "entry_score": round(momentum * confidence, 4) if momentum is not None and confidence is not None else None,
        "momentum_score": momentum,
        "confidence": confidence,
        "watchlist_priority": item.get("watchlist_priority"),
        "price_eur": price,
        "quality": _quality_view(snapshot),
        "sizing": None,
    }


def _sizing_view(rec) -> dict[str, Any]:
    purchase = float(rec.purchase_value_eur)
    return {
        "proposed_capital_eur": purchase,
        "proposed_quantity": float(rec.recommended_quantity),
        "price_eur": float(rec.current_price_eur),
        "resulting_position_value_eur": purchase,
        "resulting_portfolio_weight_pct": float(rec.projected_security_weight) * 100.0,
        "resulting_swing_allocation_pct": float(rec.projected_swing_allocation) * 100.0,
        "cash_after_eur": _float(rec.cash_after),
    }


def _evaluate_candidate(
    item: Mapping[str, Any],
    snapshot: Optional[AnalysisSnapshot],
    state: Optional[_State],
    portfolio: PortfolioContext,
    config: StrategyConfig,
    held_ids: set[int],
):
    """Return ``(candidate_dict, EntryRecommendation | None)`` for one PROMOTE item."""
    candidate = _candidate_base(item, snapshot)
    security_id = int(item["security_id"])

    def done(status: str, *reasons: str, rec=None):
        candidate["entry_status"] = status
        candidate["reason_codes"] = list(reasons)
        return candidate, rec

    if security_id in held_ids or (snapshot is not None and (snapshot.position.has_position is True or (snapshot.position.shares is not None and float(snapshot.position.shares) > 0))):
        return done(BLOCKED_EXISTING_POSITION, "ENTRY_EXISTING_POSITION")
    if snapshot is not None and snapshot.position.swing_campaign_id is not None and snapshot.position.swing_campaign_status == "open":
        return done(BLOCKED_EXISTING_CAMPAIGN, "ENTRY_OPEN_CAMPAIGN_EXISTS")
    if snapshot is None:
        return done(BLOCKED_BY_DATA, "CANDIDATE_SNAPSHOT_UNAVAILABLE")
    if state is None:
        return done(BLOCKED_BY_DATA, "PORTFOLIO_STATE_UNAVAILABLE")

    technical, position = snapshot.technical, snapshot.position
    data_reasons: list[str] = []
    if technical.quality.status is not AvailabilityStatus.AVAILABLE:
        data_reasons.append(f"TECHNICAL_DATA_{technical.quality.status.name}")
    if technical.current_price is None:
        data_reasons.append("PRICE_UNAVAILABLE")
    if technical.sma50 is None or technical.sma200 is None:
        data_reasons.append("SMA_UNAVAILABLE")
    if position.current_price_cost_currency is None or (position.valuation_currency or "").upper() != "EUR":
        data_reasons.append("PRICE_EUR_UNAVAILABLE")
    if position.fx_quality.status in {AvailabilityStatus.STALE, AvailabilityStatus.UNAVAILABLE}:
        data_reasons.append("FX_UNAVAILABLE")
    if data_reasons:
        return done(BLOCKED_BY_DATA, *data_reasons)

    score = candidate["momentum_score"]
    if score is None:
        return done(WAIT_FOR_TRIGGER, "SCORE_UNAVAILABLE")
    if score < BUY_SCORE_THRESHOLD:
        return done(WAIT_FOR_TRIGGER, "SCORE_BELOW_THRESHOLD")

    rec = evaluate_entry_recommendation(_as_swing_candidate(snapshot), state.as_portfolio(portfolio, security_id), config)
    if rec.eligible:
        candidate["sizing"] = _sizing_view(rec)
        return done(ENTRY_READY, "ENTRY_READY", rec=rec)
    reason = rec.block_reasons[0] if rec.block_reasons else "ENTRY_BLOCKED"
    return done(_REASON_STATUS.get(reason, BLOCKED_BY_DATA), reason, rec=rec)


def _rank_key(candidate: Mapping[str, Any]):
    priority = candidate.get("watchlist_priority")
    return (-float(candidate["entry_score"]), -(float(priority) if priority is not None else -1e18), int(candidate["security_id"]))


def unavailable_plan(reason: str, *, evaluation_as_of: Optional[str] = None) -> dict[str, Any]:
    """Well-typed empty plan for a run in which the capital state cannot be established."""
    return {
        "status": "UNAVAILABLE",
        "simulation_only": True,
        "orders_created": False,
        "evaluation_as_of": evaluation_as_of,
        "current_state": {},
        "existing_position_actions": [],
        "post_action_state": {},
        "entry_candidates": [],
        "planned_entries": [],
        "deferred_entries": [],
        "final_simulated_state": {},
        "tax_estimate": {},
        "entry_summary": {status: 0 for status in ENTRY_STATUSES},
        "methodology": _methodology(),
        "warnings": [reason],
        "rendered_de": f"Portfolio-Aktionsplan nicht verfügbar ({reason}). Keine Orders.",
    }


def _methodology(tax: Optional[TaxConfig] = None, *, net_basis: bool = False) -> dict[str, Any]:
    return {
        "promote_is_buy": False,
        "entry_score": "momentum_score * confidence (existing analytics score x decision_engine data-quality confidence)",
        "rank_tie_breaks": ["watchlist_priority (higher first)", "security_id (lower first)"],
        "wait_for_trigger_score_threshold": BUY_SCORE_THRESHOLD,
        "sizing": "entry_sizing.evaluate_entry_recommendation against the simulated capital state (swing maximum, cash reserve, per-security weight limits, whole-share floor)",
        "proceeds_basis": "net_conservative" if net_basis else "gross",
        "taxes_modelled": net_basis,
        "fees_modelled": False,
        "tax": {
            "combined_rate_on_realized_gain": (tax or TaxConfig()).combined_rate,
            "cost_basis": "positions.avg_cost (average cost, NOT FIFO lots)",
            "fifo_implemented": False,
            "planning_view": "conservative: no loss offset",
            "indicative_view": "loss offset within the planned SELL/TRIM actions (display only, never used for planning)",
            "unknown_inputs": list(_TAX_UNKNOWN_INPUTS),
        },
    }


def build_portfolio_action_plan(
    *,
    portfolio: PortfolioContext,
    existing_position_results: Sequence[Mapping[str, Any]],
    entry_candidate_results: Sequence[Mapping[str, Any]] = (),
    promotion_results: Sequence[Mapping[str, Any]] = (),
    candidate_snapshots: Optional[Mapping[int, AnalysisSnapshot]] = None,
    config: Optional[StrategyConfig] = None,
    position_cost_basis: Optional[Mapping[int, Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Build the deterministic ``portfolio_action_plan``. Inputs are never mutated.

    ``position_cost_basis`` maps ``security_id -> {"avg_cost", "currency"}``. When
    given, SELL/TRIM proceeds are planned net of the conservative tax estimate;
    when ``None`` no tax estimate is requested and proceeds stay gross.
    """
    cfg = config or StrategyConfig()
    candidate_snapshots = candidate_snapshots or {}
    warnings: list[str] = ["CONCENTRATION_SINGLE_SECURITY_LIMITS_ONLY"]
    initial = _initial_state(portfolio)
    if initial is None:
        warnings.append("PORTFOLIO_STATE_UNAVAILABLE")
    if initial is not None and initial.cash is None:
        warnings.append("CASH_STATE_UNAVAILABLE")
    if portfolio.buying_power_quality.status is not AvailabilityStatus.AVAILABLE:
        warnings.append("BUYING_POWER_UNAVAILABLE_CASH_USED_AS_CAPITAL")

    # A. existing actions   B. post-action capital
    actions, post_state, tax_estimate, purchases_total = _existing_actions(
        [*existing_position_results, *entry_candidate_results], initial, warnings, cost_basis=position_cost_basis, tax=cfg.tax
    )
    proceeds_total = tax_estimate["gross_proceeds_total_eur"]
    net_basis = tax_estimate["tax_estimate_quality"] != "not_estimated"
    if proceeds_total > 0:
        if net_basis:
            warnings.extend(["TAX_ESTIMATED_FROM_AVERAGE_COST_NOT_FIFO", "TAX_LOSS_OFFSET_DATA_UNKNOWN", "FEES_NOT_MODELLED"])
            if tax_estimate["tax_estimate_quality"] != TAX_QUALITY_AVERAGE_COST:
                warnings.append("TAX_COST_BASIS_UNAVAILABLE_UPPER_BOUND_USED")
        else:
            warnings.append("PROCEEDS_GROSS_TAXES_FEES_NOT_MODELLED")
    post_action = None
    if post_state is not None:
        net_cons = tax_estimate["net_proceeds_total_conservative"] if net_basis else proceeds_total
        post_action = {
            "expected_proceeds_gross_eur": proceeds_total,
            "expected_proceeds_net_conservative_eur": net_cons,
            "proceeds_basis": "net_conservative" if net_basis else "gross",
            "taxes_modelled": net_basis,
            "fees_modelled": False,
            "planned_purchases_from_actions_eur": purchases_total,
            "cash_after_actions_eur": post_state.cash,
            **_state_view(post_state, cfg),
        }
        if net_basis and post_state.cash is not None:
            tax_estimate["indicative_cash_after_actions_with_plan_offset_eur"] = post_state.cash + (tax_estimate["net_proceeds_total_with_plan_offset"] - tax_estimate["net_proceeds_total_conservative"])

    # C. entry evaluation of PROMOTE candidates
    held_ids = {int(r["security_id"]) for r in existing_position_results if (r.get("position_quantity") or 0) > 0}
    held_ids |= {int(e.security_id) for e in portfolio.exposures if e.quantity is not None and float(e.quantity) > 0}
    evaluated: list[tuple[dict[str, Any], Any, Optional[AnalysisSnapshot]]] = []
    for item in promotion_results:
        if item.get("recommendation") != "PROMOTE":
            continue
        snapshot = candidate_snapshots.get(int(item["security_id"]))
        candidate, rec = _evaluate_candidate(item, snapshot, post_state, portfolio, cfg, held_ids)
        evaluated.append((candidate, rec, snapshot))

    # D. ranking (ENTRY_READY only)
    ready = sorted((c for c, _, _ in evaluated if c["entry_status"] == ENTRY_READY), key=_rank_key)
    for rank, candidate in enumerate(ready, start=1):
        candidate["rank"] = rank
    snapshots_by_id = {int(c["security_id"]): s for c, _, s in evaluated}

    # E./F. sequential capital allocation in rank order
    planned: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    running = post_state
    stop_reason: Optional[str] = None
    reserve = cfg.sizing.minimum_cash_reserve
    def defer(candidate: Mapping[str, Any], reason: str) -> None:
        deferred.append({"security_id": int(candidate["security_id"]), "symbol": candidate["symbol"], "rank": candidate["rank"], "entry_score": candidate["entry_score"], "reason": reason})

    # Strict rank order: once capital cannot fund the next-ranked candidate (or the
    # Swing maximum / cash reserve is reached) allocation stops; lower ranks never
    # leapfrog into leftover crumbs. Size-cap or concentration failures only defer
    # that one candidate.
    for candidate in ready:
        sid = int(candidate["security_id"])
        if running is None:
            defer(candidate, "PORTFOLIO_STATE_UNAVAILABLE")
            continue
        if stop_reason is None:
            if running.swing_pct >= cfg.portfolio.swing_max_pct * 100.0 - _EPS:
                stop_reason = "STOPPED_SWING_MAX_REACHED"
            elif running.cash is not None and reserve is not None and running.cash <= reserve + _EPS:
                stop_reason = "STOPPED_CASH_RESERVE_REACHED"
        if stop_reason is not None:
            defer(candidate, stop_reason)
            continue
        rec = evaluate_entry_recommendation(_as_swing_candidate(snapshots_by_id[sid]), running.as_portfolio(portfolio, sid), cfg)
        if not rec.eligible:
            raw = rec.block_reasons[0] if rec.block_reasons else "ENTRY_BLOCKED"
            deployable = None if running.cash is None or reserve is None else max(0.0, running.cash - reserve)
            price = candidate["price_eur"]
            if raw == "ENTRY_SWING_ALLOCATION_LIMIT":
                stop_reason = "STOPPED_SWING_MAX_REACHED"
                defer(candidate, stop_reason)
            elif raw in ("ENTRY_QUANTITY_ZERO", "ENTRY_CASH_RESERVE_LIMIT") and (deployable is None or price is None or deployable < price):
                stop_reason = "STOPPED_CAPITAL_EXHAUSTED"
                defer(candidate, stop_reason)
            else:
                defer(candidate, _DEFERRAL_REASON.get(raw, raw))
            continue
        sizing = _sizing_view(rec)
        planned.append({
            "rank": candidate["rank"],
            "security_id": sid,
            "symbol": candidate["symbol"],
            "quantity": sizing["proposed_quantity"],
            "price_eur": sizing["price_eur"],
            "capital_eur": sizing["proposed_capital_eur"],
            "expected_weight_pct": sizing["resulting_portfolio_weight_pct"],
            "resulting_position_value_eur": sizing["resulting_position_value_eur"],
            "resulting_swing_allocation_pct": sizing["resulting_swing_allocation_pct"],
            "cash_after_eur": sizing["cash_after_eur"],
            "entry_score": candidate["entry_score"],
            "reason": "ENTRY_READY",
        })
        running = running.after_purchase(sizing["proposed_capital_eur"])

    # G. final simulated state
    final_state = None
    if running is not None:
        entries_total = sum(entry["capital_eur"] for entry in planned)
        final_state = {"planned_entries_total_eur": entries_total, **_state_view(running, cfg)}
        final_state["swing_within_max"] = running.swing_pct <= cfg.portfolio.swing_max_pct * 100.0 + 1e-6
        final_state["cash_above_reserve"] = running.cash is None or reserve is None or running.cash >= reserve - 1e-6

    order = {status: index for index, status in enumerate(ENTRY_STATUSES)}
    candidates = sorted((c for c, _, _ in evaluated), key=lambda c: (order[c["entry_status"]], c["rank"] if c["rank"] is not None else 10**9, -(c["entry_score"] if c["entry_score"] is not None else -1e18), int(c["security_id"])))
    summary = {status: sum(1 for c in candidates if c["entry_status"] == status) for status in ENTRY_STATUSES}

    plan = {
        "status": "AVAILABLE" if initial is not None else "UNAVAILABLE",
        "simulation_only": True,
        "orders_created": False,
        "evaluation_as_of": portfolio.evaluation_as_of,
        "current_state": {**_state_view(initial, cfg), "allocation_guardrails_current": list(portfolio.allocation_guardrails), "buying_power_eur": portfolio.buying_power, "minimum_cash_reserve_eur": reserve, "swing_target_pct": [cfg.portfolio.swing_min_pct * 100.0, cfg.portfolio.swing_max_pct * 100.0], "long_term_target_pct": [cfg.portfolio.long_term_min_pct * 100.0, cfg.portfolio.long_term_max_pct * 100.0]} if initial is not None else {},
        "existing_position_actions": actions,
        "post_action_state": post_action or {},
        "entry_candidates": candidates,
        "planned_entries": planned,
        "deferred_entries": deferred,
        "final_simulated_state": final_state or {},
        "tax_estimate": tax_estimate,
        "entry_summary": summary,
        "methodology": _methodology(cfg.tax, net_basis=net_basis),
        "warnings": warnings,
    }
    plan["rendered_de"] = render_plan_de(plan)
    return plan


def render_plan_de(plan: Mapping[str, Any]) -> str:
    """Deterministic German rendering of an already computed plan (formatting only)."""
    if plan.get("status") != "AVAILABLE":
        return f"Portfolio-Aktionsplan nicht verfügbar ({', '.join(plan.get('warnings') or [])}). Keine Orders."
    cur, post, fin = plan["current_state"], plan["post_action_state"], plan["final_simulated_state"]
    lines = ["Portfolio-Aktionsplan (Simulation, keine Orders; PROMOTE ist kein BUY)"]
    lines.append(f"1. Ausgangslage: Swing {_fmt_pct(cur['swing_pct'])}, Long-Term {_fmt_pct(cur['long_term_pct'])}, Cash {_fmt_eur(cur['cash_eur'])}")
    lines.append("2. Bestehende Aktionen:")
    actions = plan["existing_position_actions"]
    sales = [item for item in actions if item["action"] in ("SELL", "TRIM") and item.get("estimated_net_proceeds_eur_conservative") is not None]
    if sales:
        tax = plan["tax_estimate"]
        lines.append("### Verkäufe / Reduktionen")
        lines.append("| Position | Aktion | Stück | Brutto | Steuer geschätzt | Netto |")
        lines.append("|---|---|---|---|---|---|")
        for item in sales:
            lines.append(
                f"| {item['symbol']} | {item['action']} | {_fmt_quantity(item.get('quantity')).strip()} | {_fmt_eur(item['gross_proceeds_eur'])} "
                f"| {_fmt_eur(item['estimated_tax_eur_conservative'])} | {_fmt_eur(item['estimated_net_proceeds_eur_conservative'])} |"
            )
        lines.append(f"- Bruttoerlös gesamt: {_fmt_eur(tax['gross_proceeds_total_eur'])}")
        lines.append(f"- Geschätzte Steuer (konservativ, ohne Verlustverrechnung): {_fmt_eur(tax['estimated_tax_total_conservative'])}")
        lines.append(f"- Nettoerlös (konservativ, Planungsbasis): {_fmt_eur(tax['net_proceeds_total_conservative'])}")
        lines.append(f"- Zusätzlich, nur Anzeige: Netto mit planinterner Verlustverrechnung {_fmt_eur(tax['net_proceeds_total_with_plan_offset'])} (geschätzte Steuer {_fmt_eur(tax['estimated_tax_total_with_plan_offset'])})")
        lines.append("Steuerschätzung auf Basis des Durchschnitts-Einstands, kein steuerliches FIFO.")
        lines.append("Verlusttopf, Freistellungsauftrag und bereits realisierte Jahresergebnisse sind nicht bekannt.")
        if tax["tax_estimate_quality"] != TAX_QUALITY_AVERAGE_COST:
            lines.append(f"Hinweis: Einstand teilweise unbekannt, dort wurde der gesamte Erlös als Obergrenze besteuert ({tax['tax_estimate_quality']}).")
    shown = bool(sales)
    for item in actions:
        if item in sales:
            continue
        if item["action"] in ("SELL", "TRIM"):
            detail = f"ca. {_fmt_eur(item.get('estimated_proceeds_eur'))} brutto"
        else:
            detail = f"ca. {_fmt_eur(item.get('estimated_purchase_eur'))}"
        lines.append(f"   {item['symbol']}: {item['action']}{_fmt_quantity(item.get('quantity'))} ({detail})")
        shown = True
    if not shown:
        lines.append("   keine SELL/TRIM/ADD/BUY-Aktionen")
    if post.get("taxes_modelled"):
        lines.append(f"3. Nach Aktionen (Nettoerlös konservativ {_fmt_eur(post['expected_proceeds_net_conservative_eur'])}; Gebühren nicht berücksichtigt): Cash {_fmt_eur(post['cash_after_actions_eur'])}, Swing {_fmt_pct(post['swing_pct'])}, Long-Term {_fmt_pct(post['long_term_pct'])}, freie Swing-Kapazität {_fmt_eur(post['remaining_swing_capacity_eur'])}")
    else:
        lines.append(f"3. Nach Aktionen (Erlös brutto, ohne Steuern/Gebühren: {_fmt_eur(post['expected_proceeds_gross_eur'])}): Cash {_fmt_eur(post['cash_after_actions_eur'])}, Swing {_fmt_pct(post['swing_pct'])}, Long-Term {_fmt_pct(post['long_term_pct'])}, freie Swing-Kapazität {_fmt_eur(post['remaining_swing_capacity_eur'])}")
    summary = plan["entry_summary"]
    lines.append("4. Entry-Bewertung: " + ", ".join(f"{status} {count}" for status, count in summary.items() if count) if any(summary.values()) else "4. Entry-Bewertung: keine PROMOTE-Kandidaten")
    lines.append("5. Geplante Entries:")
    for entry in plan["planned_entries"]:
        lines.append(f"   {entry['rank']}. {entry['symbol']}:{_fmt_quantity(entry['quantity'])} Stk. (ca. {_fmt_eur(entry['capital_eur'])}, Gewicht {_fmt_pct(entry['expected_weight_pct'])}, Swing danach {_fmt_pct(entry['resulting_swing_allocation_pct'])})")
    if not plan["planned_entries"]:
        lines.append("   keine")
    if plan["deferred_entries"]:
        lines.append("   Zurückgestellt: " + ", ".join(f"{d['symbol']} ({d['reason']})" for d in plan["deferred_entries"]))
    lines.append(f"6. Final (simuliert): Swing {_fmt_pct(fin['swing_pct'])}, Long-Term {_fmt_pct(fin['long_term_pct'])}, Cash {_fmt_eur(fin['cash_eur'])}")
    lines.append("Die tatsächliche Orderausführung erfolgt außerhalb des Trading Agents.")
    return "\n".join(lines)
