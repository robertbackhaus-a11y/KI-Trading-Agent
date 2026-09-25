"""Deterministic presentation contract for ``run_trading_orchestrator``.

This module never computes a trading decision, a promotion rule, or an
allocation recommendation. It only projects, filters, counts, and formats
values the orchestrator (``trading_orchestrator.py``) has already computed.

Every count here is ``len()`` of the paired exact list -- never a separate
number that could drift from the list it describes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from analysis_contracts import to_primitive


RESPONSE_CONTRACT_VERSION = "1"
PRESENTATION_MODE_AUTHORITATIVE = "authoritative"

# Fixed, non-negotiable boundary statement. Never generated, never varied.
EXECUTION_BOUNDARY_MESSAGE = (
    "Die tatsächliche Orderausführung erfolgt außerhalb des Trading Agents."
)

ACTIONABLE_DECISIONS = ("SELL", "TRIM", "ADD", "BUY")


@dataclass(frozen=True)
class PresentationPortfolio:
    total_market_value_eur: Optional[float]
    swing_market_value_eur: Optional[float]
    swing_pct: Optional[float]
    long_term_market_value_eur: Optional[float]
    long_term_pct: Optional[float]
    cash_available_eur: Optional[float]
    buying_power: Optional[float]
    # Factual engine codes only (e.g. SWING_ALLOCATION_ABOVE_MAX). Never a
    # prescriptive recommendation derived from them.
    allocation_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PresentationAction:
    security_id: int
    symbol: Optional[str]
    action: str
    action_quantity: Optional[float]
    action_quantity_basis: Optional[str]
    priority: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PresentationEntries:
    count: int
    results: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PresentationAdds:
    count: int
    results: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PresentationPromotions:
    promote_count: int
    promote_symbols: tuple[str, ...]
    keep_watching_count: int
    keep_watching_symbols: tuple[str, ...]
    reject_count: int
    data_insufficient_count: int
    data_insufficient_symbols: tuple[str, ...]


@dataclass(frozen=True)
class PresentationDataQuality:
    blockers: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PresentationExecutionBoundary:
    broker_execution_available: bool
    message: str


@dataclass(frozen=True)
class PresentationSummary:
    response_contract_version: str
    presentation_mode: str
    portfolio: PresentationPortfolio
    actions: tuple[PresentationAction, ...]
    holds: tuple[str, ...]
    entries: PresentationEntries
    adds: PresentationAdds
    promotions: PresentationPromotions
    data_quality: PresentationDataQuality
    execution_boundary: PresentationExecutionBoundary

    def primitive(self) -> dict[str, Any]:
        return to_primitive(self)


def _presentation_portfolio(capital_state_summary: dict[str, Any]) -> PresentationPortfolio:
    return PresentationPortfolio(
        total_market_value_eur=capital_state_summary.get("total_portfolio_market_value_eur"),
        swing_market_value_eur=capital_state_summary.get("swing_market_value_eur"),
        swing_pct=capital_state_summary.get("swing_weight_pct"),
        long_term_market_value_eur=capital_state_summary.get("long_term_market_value_eur"),
        long_term_pct=capital_state_summary.get("long_term_weight_pct"),
        cash_available_eur=capital_state_summary.get("cash_available"),
        buying_power=capital_state_summary.get("buying_power"),
        allocation_warnings=tuple(capital_state_summary.get("allocation_guardrails") or ()),
    )


def _presentation_actions(
    existing_position_results: Sequence[dict[str, Any]],
    entry_candidate_results: Sequence[dict[str, Any]],
) -> tuple[PresentationAction, ...]:
    items: list[PresentationAction] = []
    for record in (*existing_position_results, *entry_candidate_results):
        decision = record["decision"]
        if decision["action"] not in ACTIONABLE_DECISIONS:
            continue
        items.append(
            PresentationAction(
                security_id=record["security_id"],
                symbol=record.get("symbol"),
                action=decision["action"],
                action_quantity=decision.get("action_quantity"),
                action_quantity_basis=decision.get("action_quantity_basis"),
                priority=record["priority"],
                reason_codes=tuple(decision.get("reasons") or ()),
            )
        )
    # Priority strings are always "P" + one digit; deterministic, stable sort.
    items.sort(key=lambda item: (int(item.priority[1:]), item.security_id))
    return tuple(items)


def _presentation_holds(existing_position_results: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        record["symbol"]
        for record in existing_position_results
        if record["decision"]["action"] == "HOLD" and record.get("symbol")
    )


def _presentation_entries(entry_candidate_results: Sequence[dict[str, Any]]) -> PresentationEntries:
    results = tuple(entry_candidate_results)
    return PresentationEntries(count=len(results), results=results)


def _presentation_adds(existing_position_results: Sequence[dict[str, Any]]) -> PresentationAdds:
    results = tuple(record for record in existing_position_results if record["decision"]["action"] == "ADD")
    return PresentationAdds(count=len(results), results=results)


def _presentation_promotions(promotion_results: Sequence[dict[str, Any]]) -> PresentationPromotions:
    promote = tuple(item["symbol"] for item in promotion_results if item.get("recommendation") == "PROMOTE" and item.get("symbol"))
    keep_watching = tuple(item["symbol"] for item in promotion_results if item.get("recommendation") == "KEEP_WATCHING" and item.get("symbol"))
    reject_count = sum(1 for item in promotion_results if item.get("recommendation") == "REJECT")
    data_insufficient = tuple(item["symbol"] for item in promotion_results if item.get("recommendation") == "DATA_INSUFFICIENT" and item.get("symbol"))
    return PresentationPromotions(
        promote_count=len(promote),
        promote_symbols=promote,
        keep_watching_count=len(keep_watching),
        keep_watching_symbols=keep_watching,
        reject_count=reject_count,
        data_insufficient_count=len(data_insufficient),
        data_insufficient_symbols=data_insufficient,
    )


def _presentation_data_quality(
    global_issues: Sequence[dict[str, Any]],
    security_issues: Sequence[dict[str, Any]],
) -> PresentationDataQuality:
    # Exact match only: "NON_BLOCKING_WARNING" contains the substring
    # "BLOCKING" too, so a substring check would wrongly double-count every
    # warning as a blocker as well.
    blocking_severities = {"GLOBAL_BLOCKING", "SECURITY_BLOCKING"}
    all_issues = (*global_issues, *security_issues)
    blockers = tuple(issue for issue in all_issues if issue.get("severity") in blocking_severities)
    warnings = tuple(issue for issue in all_issues if issue.get("severity") == "NON_BLOCKING_WARNING")
    return PresentationDataQuality(blockers=blockers, warnings=warnings)


def _presentation_execution_boundary() -> PresentationExecutionBoundary:
    return PresentationExecutionBoundary(broker_execution_available=False, message=EXECUTION_BOUNDARY_MESSAGE)


def build_presentation_summary(
    *,
    capital_state_summary: dict[str, Any],
    existing_position_results: Sequence[dict[str, Any]],
    entry_candidate_results: Sequence[dict[str, Any]],
    promotion_results: Sequence[dict[str, Any]],
    global_issues: Sequence[dict[str, Any]],
    security_issues: Sequence[dict[str, Any]],
) -> PresentationSummary:
    """Pure projection over already-computed orchestrator results.

    No decision, promotion rule, or allocation recommendation is computed
    here. Every list is an exact filter of an existing engine result, and
    every count is ``len()`` of its paired list.
    """
    return PresentationSummary(
        response_contract_version=RESPONSE_CONTRACT_VERSION,
        presentation_mode=PRESENTATION_MODE_AUTHORITATIVE,
        portfolio=_presentation_portfolio(capital_state_summary),
        actions=_presentation_actions(existing_position_results, entry_candidate_results),
        holds=_presentation_holds(existing_position_results),
        entries=_presentation_entries(entry_candidate_results),
        adds=_presentation_adds(existing_position_results),
        promotions=_presentation_promotions(promotion_results),
        data_quality=_presentation_data_quality(global_issues, security_issues),
        execution_boundary=_presentation_execution_boundary(),
    )


def _fmt_eur(value: Optional[float]) -> str:
    if value is None:
        return "unbekannt"
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".") + " EUR"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "unbekannt"
    return f"{value:.2f}".replace(".", ",") + " %"


def _fmt_quantity(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f" {value:g}"


def format_orchestrator_summary(
    summary: PresentationSummary,
    *,
    evaluation_as_of: str,
    market_data_as_of: Optional[str] = None,
) -> str:
    """Deterministic German portfolio-status renderer. Never calls an LLM.

    Pure string formatting over an already-computed :class:`PresentationSummary`.
    Adds no recommendation, no "Next Steps", no independent security ranking,
    and no allocation advice beyond the factual warning codes.
    """
    p = summary.portfolio
    lines: list[str] = []

    lines.append(f"1. Portfolio-Status (Stand: {evaluation_as_of})")
    if market_data_as_of:
        lines.append(f"   Marktdaten-Stand: {market_data_as_of}")
    lines.append(f"   Gesamtwert: {_fmt_eur(p.total_market_value_eur)}")
    lines.append(f"   Swing: {_fmt_eur(p.swing_market_value_eur)} ({_fmt_pct(p.swing_pct)})")
    lines.append(f"   Long-Term: {_fmt_eur(p.long_term_market_value_eur)} ({_fmt_pct(p.long_term_pct)})")
    lines.append(f"   Cash: {_fmt_eur(p.cash_available_eur)}")
    lines.append(f"   Buying Power: {_fmt_eur(p.buying_power)}")
    if p.allocation_warnings:
        lines.append(f"   Allocation-Warnungen: {', '.join(p.allocation_warnings)}")
    lines.append("")

    lines.append("2. Actionable Positions")
    if summary.actions:
        for action in summary.actions:
            reasons = ", ".join(action.reason_codes)
            lines.append(
                f"   {action.symbol}: {action.action}{_fmt_quantity(action.action_quantity)}"
                f" [{action.priority}] ({reasons})" if reasons else
                f"   {action.symbol}: {action.action}{_fmt_quantity(action.action_quantity)} [{action.priority}]"
            )
    else:
        lines.append("   Keine.")
    lines.append("")

    lines.append("3. HOLD Positions")
    lines.append(f"   {', '.join(summary.holds) if summary.holds else 'Keine.'}")
    lines.append("")

    lines.append("4. Entry / ADD Status")
    lines.append(f"   Entry-Kandidaten: {summary.entries.count}")
    lines.append(f"   ADD-Empfehlungen: {summary.adds.count}")
    lines.append("")

    lines.append("5. Promotion Status")
    lines.append(f"   PROMOTE: {summary.promotions.promote_count}")
    if summary.promotions.promote_symbols:
        lines.append(f"   {', '.join(summary.promotions.promote_symbols)}")
    lines.append(f"   KEEP_WATCHING: {summary.promotions.keep_watching_count}")
    lines.append(f"   DATA_INSUFFICIENT: {summary.promotions.data_insufficient_count}")
    if summary.promotions.data_insufficient_symbols:
        lines.append(f"   {', '.join(summary.promotions.data_insufficient_symbols)}")
    lines.append("")

    lines.append("6. Data Quality")
    has_data_quality_items = bool(summary.data_quality.blockers or summary.data_quality.warnings)
    for issue in summary.data_quality.blockers:
        lines.append(f"   BLOCKER: {issue.get('code')} ({issue.get('symbol') or 'global'})")
    for issue in summary.data_quality.warnings:
        lines.append(f"   WARNUNG: {issue.get('code')} ({issue.get('symbol') or 'global'})")
    if not has_data_quality_items:
        lines.append("   Keine.")
    lines.append("")

    lines.append("7. Allocation / Capital Warnings")
    lines.append(f"   {', '.join(p.allocation_warnings) if p.allocation_warnings else 'Keine.'}")
    lines.append("")

    lines.append(summary.execution_boundary.message)

    return "\n".join(lines)
