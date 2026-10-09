"""Compact view of the ``run_trading_orchestrator`` result for the MCP adapter (default, ``detail=false``).

Presentation only.  Every value is copied unchanged from the full result; nothing is recomputed, re-ranked or re-rounded, and no
trading rule lives here.  What the compact view drops is either a duplicate of a kept value (the same list or figure appears in
several blocks of the full result), a rendered text that repeats structured fields, or per-security technical detail.  The complete
result stays available through ``detail=true``.

Two sources are kept apart on purpose (they answer different questions):

* ``position_engine``: the decision engine for EXISTING positions (SELL / TRIM / HOLD / ADD, plus BUY for zero positions);
* ``entry_plan``: the Portfolio Action Planner (simulation of NEW entries financed by the cash and the planned sales).

"No BUY/ADD from the position engine" therefore never means "no planned entries".
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

COMPACT_VERSION = 2
EXECUTION_BOUNDARY_DEFAULT = "Die tatsächliche Orderausführung erfolgt außerhalb des Trading Agents."
POSITION_ENGINE_NOTE = (
    "Position engine only (existing positions: SELL/TRIM/HOLD/ADD, BUY for zero positions). "
    "Planned NEW entries are in entry_plan; engine_buy_count/engine_add_count = 0 does not mean there are no planned entries."
)
OMITTED_FOR_CONTEXT = (
    "existing_position_results", "entry_candidate_results", "promotion_results", "presentation_summary", "rendered_summary_de",
    "highest_priority_actions", "next_review_items", "portfolio_context", "capital_state_summary", "data_quality_summary",
    "portfolio_action_plan.rendered_de", "portfolio_action_plan.methodology", "portfolio_action_plan.entry_candidates",
    "portfolio_action_plan.existing_position_actions", "promotion keep_watching_symbols",
)
ACTION_TAX_FIELDS = (
    "price_eur", "gross_proceeds_eur", "estimated_realized_gain_eur", "estimated_tax_eur_conservative",
    "estimated_net_proceeds_eur_conservative", "estimated_purchase_eur",
)
FUNDING_FIELDS = (
    "external_funding_available", "cash_available_eur", "cash_reserve_min_eur", "internal_deployable_capital_eur", "deployable_capital_eur",
    "external_funding_required_eur", "internal_capital_used_eur", "limiting_guard",
)
PLANNED_ENTRY_FIELDS = (
    "rank", "symbol", "quantity", "price_eur", "capital_eur", "expected_weight_pct", "resulting_swing_allocation_pct", "cash_after_eur", "entry_score",
)
AFTER_ACTIONS_FIELDS = (
    "planned_purchases_from_actions_eur", "cash_after_actions_eur", "deployable_cash_eur", "swing_pct", "long_term_pct",
    "remaining_swing_capacity_eur", "allocation_guardrails",
)
CURRENT_FIELDS = ("deployable_cash_eur", "remaining_swing_capacity_eur", "minimum_cash_reserve_eur", "swing_target_pct", "long_term_target_pct")


def _d(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _l(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _pick(source: Mapping[str, Any], keys, *, keep_none: bool = True) -> dict:
    return {key: source[key] for key in keys if key in source and (keep_none or source[key] is not None)}


def _portfolio(inner: Mapping[str, Any]) -> dict:
    summary, plan, capital = _d(inner.get("presentation_summary")), _d(inner.get("portfolio_action_plan")), _d(inner.get("capital_state_summary"))
    base = _d(summary.get("portfolio"))
    out = _pick(base, ("total_market_value_eur", "swing_market_value_eur", "swing_pct", "long_term_market_value_eur", "long_term_pct", "cash_available_eur", "buying_power"))
    out.update(_pick(_d(plan.get("current_state")), CURRENT_FIELDS))
    if "as_of" in capital:
        out["cash_as_of"] = capital["as_of"]
        out["cash_source"] = capital.get("source")
    out["allocation_warnings"] = _l(base.get("allocation_warnings"))
    return out


def _positions(inner: Mapping[str, Any]) -> list:
    return [_pick(item, ("symbol", "strategy", "quantity", "market_value_eur")) for item in _l(_d(inner.get("portfolio_context")).get("exposures")) if isinstance(item, Mapping)]


def _position_engine(inner: Mapping[str, Any]) -> dict:
    summary, plan = _d(inner.get("presentation_summary")), _d(inner.get("portfolio_action_plan"))
    planner_items = {(item.get("security_id"), item.get("action")): item for item in _l(plan.get("existing_position_actions")) if isinstance(item, Mapping)}
    actions = []
    for item in _l(summary.get("actions")):
        entry = _pick(item, ("symbol", "action", "action_quantity", "action_quantity_basis", "priority", "reason_codes"))
        entry.update(_pick(_d(planner_items.get((item.get("security_id"), item.get("action")))), ACTION_TAX_FIELDS))
        actions.append(entry)
    return {
        "actions": actions,
        "hold": _l(summary.get("holds")),
        "engine_buy_count": _d(summary.get("entries")).get("count"),
        "engine_add_count": _d(summary.get("adds")).get("count"),
        "note": POSITION_ENGINE_NOTE,
    }


def _promotion(inner: Mapping[str, Any]) -> dict:
    promotions = dict(_d(_d(inner.get("presentation_summary")).get("promotions")))
    promotions.pop("keep_watching_symbols", None)
    return promotions


def _entry_plan(inner: Mapping[str, Any]) -> dict:
    plan = _d(inner.get("portfolio_action_plan"))
    out = _pick(plan, ("status", "simulation_only", "orders_created"))
    if plan.get("status") != "AVAILABLE":
        out["warnings"] = _l(plan.get("warnings"))
        return out
    final = _d(plan.get("final_simulated_state"))
    out["entry_status_counts"] = dict(_d(plan.get("entry_summary")))
    funding = _d(plan.get("capital"))
    split = ("internal_capital_eur", "external_funding_eur") if funding.get("external_funding_available") else ()  # the split only matters (and only costs tokens) with external funding
    planned = [_pick(item, PLANNED_ENTRY_FIELDS + split) for item in _l(plan.get("planned_entries")) if isinstance(item, Mapping)]
    out["planned_count"] = len(planned)
    out["planned_total_eur"] = final.get("planned_entries_total_eur")
    out["planned_entries"] = planned
    groups: dict[Any, dict] = {}
    for item in _l(plan.get("deferred_entries")):
        if not isinstance(item, Mapping):
            continue
        group = groups.setdefault(item.get("reason"), {"reason": item.get("reason"), "ranks": [], "symbols": []})
        group["ranks"].append(item.get("rank"))
        group["symbols"].append(item.get("symbol"))
    out["deferred_count"] = sum(len(group["symbols"]) for group in groups.values())
    out["deferred"] = list(groups.values())
    out["stop_reason"] = next((reason for reason in groups if isinstance(reason, str) and reason.startswith("STOPPED_")), None)
    not_ready: dict[Any, list] = {}
    for item in _l(plan.get("entry_candidates")):
        if isinstance(item, Mapping) and item.get("entry_status") not in (None, "ENTRY_READY"):
            not_ready.setdefault(item["entry_status"], []).append(item.get("symbol"))
    out["not_ready_candidates"] = not_ready
    out["funding"] = _pick(funding, FUNDING_FIELDS) if funding.get("external_funding_available") else {"external_funding_available": False}
    out["remaining_buying_capacity_eur"] = funding["remaining_buying_capacity_eur"] if funding.get("external_funding_available") else final.get("deployable_cash_eur")
    return out


def _proceeds(inner: Mapping[str, Any]) -> dict:
    plan = _d(inner.get("portfolio_action_plan"))
    tax, post = _d(plan.get("tax_estimate")), _d(plan.get("post_action_state"))
    out = {"basis": post.get("proceeds_basis", tax.get("planning_basis")), "taxes_modelled": post.get("taxes_modelled"), "fees_modelled": post.get("fees_modelled")}
    out.update({
        "gross_eur": tax.get("gross_proceeds_total_eur"),
        "estimated_tax_eur": tax.get("estimated_tax_total_conservative"),
        "net_conservative_eur": tax.get("net_proceeds_total_conservative"),
        "net_with_plan_offset_eur": tax.get("net_proceeds_total_with_plan_offset"),
        "estimated_tax_with_plan_offset_eur": tax.get("estimated_tax_total_with_plan_offset"),
        "tax_estimate_quality": tax.get("tax_estimate_quality"),
        "cost_basis_method": tax.get("cost_basis_method"),
    })
    return out


def _capital(inner: Mapping[str, Any]) -> dict:
    plan = _d(inner.get("portfolio_action_plan"))
    return {"after_actions": _pick(_d(plan.get("post_action_state")), AFTER_ACTIONS_FIELDS), "final_simulated": dict(_d(plan.get("final_simulated_state")))}


def _issues(inner: Mapping[str, Any]) -> dict:
    quality = _d(_d(inner.get("presentation_summary")).get("data_quality"))
    warnings = [{key: value for key, value in item.items() if value is not None} for item in _l(quality.get("warnings")) if isinstance(item, Mapping)]
    blockers = [{key: value for key, value in item.items() if value is not None} for item in _l(quality.get("blockers")) if isinstance(item, Mapping)]
    return {"blockers": blockers, "warnings": warnings, "plan_warnings": _l(_d(inner.get("portfolio_action_plan")).get("warnings")),
            "blocking_summary": dict(_d(inner.get("blocking_summary")))}


def compact_orchestrator_result(inner: Mapping[str, Any]) -> dict:
    """Compact view of the orchestrator ``result`` dict (see module docstring)."""
    plan = _d(inner.get("portfolio_action_plan"))
    boundary = _d(_d(inner.get("presentation_summary")).get("execution_boundary"))
    return {
        "compact_version": COMPACT_VERSION,
        "evaluation_as_of": inner.get("evaluation_as_of"),
        "market_data_as_of": inner.get("market_data_as_of"),
        "global_status": inner.get("global_status"),
        "simulation_only": plan.get("simulation_only", True),
        "orders_created": plan.get("orders_created", False),
        "execution_boundary": boundary.get("message", EXECUTION_BOUNDARY_DEFAULT),
        "portfolio": _portfolio(inner),
        "positions": _positions(inner),
        "position_engine": _position_engine(inner),
        "promotion": _promotion(inner),
        "entry_plan": _entry_plan(inner),
        "proceeds": _proceeds(inner),
        "capital": _capital(inner),
        "action_summary": dict(_d(inner.get("action_summary"))),
        "issues": _issues(inner),
        "omitted_for_context": list(OMITTED_FOR_CONTEXT),
        "hint": "run_trading_orchestrator with detail=true returns the complete result",
    }


def compact_orchestrator_response(response: Mapping[str, Any]) -> Optional[dict]:
    """Envelope helper: ``{ok, operation, result}`` -> same envelope with the compact result and ``detail: false``; ``None`` if not applicable."""
    if not isinstance(response, Mapping) or not isinstance(response.get("result"), Mapping):
        return None
    return {**response, "result": compact_orchestrator_result(response["result"]), "detail": False}
