"""Read-only composition layer for the existing trading workflow."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
import sqlite3
from typing import Any, Optional

from analysis_contracts import Action, AvailabilityStatus, to_primitive
from analysis_engine import build_analysis_snapshot, build_portfolio_context
from decision_engine import decide
from orchestrator_presentation import build_presentation_summary, format_orchestrator_summary
from portfolio_action_planner import build_portfolio_action_plan, unavailable_plan
from strategy_config import StrategyConfig
from swing_promotion import evaluate_swing_candidates


PRIORITY = {
    Action.SELL: "P1", Action.TRIM: "P2", Action.ADD: "P3", Action.BUY: "P4",
    Action.HOLD: "P6", Action.WATCH: "P7",
    "PROMOTE": "P5", "KEEP_WATCHING": "P6", "DATA_INSUFFICIENT": "P7", "REJECT": "P7",
}
PRIORITY_ORDER = {f"P{number}": number for number in range(8)}


@dataclass(frozen=True)
class OrchestratorResult:
    evaluation_as_of: str
    market_data_as_of: Optional[str]
    portfolio_context: dict[str, Any]
    capital_state_summary: dict[str, Any]
    global_status: str
    global_issues: tuple[dict[str, Any], ...]
    existing_position_results: tuple[dict[str, Any], ...]
    entry_candidate_results: tuple[dict[str, Any], ...]
    promotion_results: tuple[dict[str, Any], ...]
    action_summary: dict[str, int]
    blocking_summary: dict[str, int]
    data_quality_summary: dict[str, int]
    highest_priority_actions: tuple[dict[str, Any], ...]
    next_review_items: tuple[dict[str, Any], ...]
    # Phase 4B.2: deterministic presentation contract. Computed exclusively
    # from the fields above (filter/count/format only) -- never a new
    # decision, promotion rule, or allocation recommendation.
    presentation_summary: dict[str, Any] = field(default_factory=dict)
    rendered_summary_de: str = ""
    # v0.2.0: deterministic, simulation-only capital/entry plan computed
    # exclusively from the fields above (see portfolio_action_planner.py).
    portfolio_action_plan: dict[str, Any] = field(default_factory=dict)

    def primitive(self) -> dict[str, Any]:
        return to_primitive(self)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _active_swing_zero_positions(conn: sqlite3.Connection, evaluation_as_of: str) -> list[int]:
    if not _table_exists(conn, "strategy_assignment"):
        return []
    campaign_clause = ""
    if _table_exists(conn, "swing_campaign"):
        campaign_clause = " AND NOT EXISTS (SELECT 1 FROM swing_campaign c WHERE c.security_id=sa.security_id AND c.status='open')"
    rows = conn.execute(
        """SELECT sa.security_id FROM strategy_assignment sa
           LEFT JOIN positions p ON p.security_id=sa.security_id AND p.shares>0
           WHERE sa.strategy_type='swing' AND sa.effective_from<=?
             AND (sa.effective_to IS NULL OR sa.effective_to>=?)
             AND p.security_id IS NULL""" + campaign_clause + """
           ORDER BY sa.security_id""",
        (evaluation_as_of, evaluation_as_of),
    ).fetchall()
    return [int(row["security_id"]) for row in rows]


def _security_record(snapshot, decision, priority: str) -> dict[str, Any]:
    position = snapshot.position
    return {
        "security_id": snapshot.security_id,
        "symbol": snapshot.symbol,
        "name": snapshot.name,
        "strategy": position.strategy.value,
        "position_quantity": position.shares or 0.0,
        "campaign_id": position.swing_campaign_id,
        "campaign_status": position.swing_campaign_status,
        "event_derived_quantity": position.swing_campaign_derived_quantity,
        "campaign_reconciliation_delta": position.campaign_reconciliation_delta,
        "campaign_reconciliation_quality": position.campaign_reconciliation_quality.status.value,
        "native_price": position.current_price_native,
        "native_currency": position.current_price_currency,
        "valuation_price_eur": position.current_price_cost_currency,
        "valuation_currency": position.valuation_currency,
        "valuation_as_of": position.fx_quality.as_of,
        "fx_rate": position.fx_rate,
        "fx_rate_date": position.fx_rate_date,
        "fx_source": position.fx_source,
        "fx_quality": position.fx_quality.status.value,
        "technical_quality": snapshot.technical.quality.status.value,
        "fundamental_quality": snapshot.fundamental.quality.status.value,
        "strategy_quality": position.strategy_quality.status.value,
        "lifecycle_quality": position.lifecycle_quality.status.value,
        "market_data_as_of": snapshot.market_data_as_of,
        "decision": to_primitive(decision),
        "priority": priority,
    }


def _portfolio_context_for_security(portfolio, security_id: int):
    """Derive a per-security PortfolioContext from an already-computed one.

    ``build_portfolio_context(security_id, ...)`` re-values every open
    position from scratch to answer a question (current-security share of
    the portfolio) that only needs the ``exposures`` the base call already
    computed. This replicates that exact derivation (see
    ``analysis_engine.build_portfolio_context``'s ``current_exposure``
    branch) locally instead of re-querying the database once per security.
    """
    current_exposure = next(
        (exposure for exposure in portfolio.exposures if exposure.security_id == security_id),
        None,
    )
    allocation_available = portfolio.allocation_quality.status is AvailabilityStatus.AVAILABLE
    if current_exposure is None:
        current_value = 0.0
        current_weight = 0.0 if allocation_available else None
    else:
        current_value = current_exposure.market_value_eur
        current_weight = (
            current_value / float(portfolio.total_market_value) * 100.0
            if allocation_available
            and current_value is not None
            and portfolio.total_market_value is not None
            else None
        )
    return replace(
        portfolio,
        current_security_id=security_id,
        current_security_market_value=current_value,
        current_security_market_value_eur=current_value,
        current_security_weight_pct=current_weight,
    )


def _issue(scope: str, code: str, severity: str, *, security_id: Optional[int] = None, symbol: Optional[str] = None, details: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"scope": scope, "code": code, "severity": severity, "security_id": security_id, "symbol": symbol, "details": list(details)}


def _global_readiness_issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Check cheap, read-only prerequisites without changing any workflow state."""
    issues: list[dict[str, Any]] = []
    required_tables = ("security", "positions", "strategy_assignment", "watchlist")
    missing = tuple(table for table in required_tables if not _table_exists(conn, table))
    if missing:
        issues.append(_issue("GLOBAL", "REQUIRED_SCHEMA_UNAVAILABLE", "GLOBAL_BLOCKING", details=missing))
        return issues
    # PRAGMA integrity_check is deliberately not run here: it is the most
    # expensive single statement in a normal analysis run (a full page-level
    # scan) and this function only checks cheap, read-only prerequisites.
    # Integrity verification belongs to the explicit DB validation paths
    # (Initialize-TradingDatabase.py / Reset-TradingDb.py's own
    # validate_database(), and parqet_import.py's post-write check), not to
    # every read-only orchestrator run.
    if not _table_exists(conn, "candidate_promotion"):
        issues.append(_issue("GLOBAL", "PROMOTION_APPROVAL_SCHEMA_UNAVAILABLE", "NON_BLOCKING_WARNING"))
    elif _table_exists(conn, "metadata"):
        row = conn.execute("SELECT value FROM metadata WHERE key='candidate_promotion_schema_version'").fetchone()
        if row is None or str(row[0]) != "1":
            issues.append(_issue("GLOBAL", "PROMOTION_FEATURE_VERSION_UNAVAILABLE", "NON_BLOCKING_WARNING"))
    return issues


def _degenerate_presentation(evaluation_as_of: str, issues: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], str]:
    """All-empty presentation contract for a GLOBAL_BLOCKING early return.

    Kept well-typed (not an empty dict) so downstream consumers never need a
    special case: zero actions, zero holds, zero promotions -- the blocking
    issues are the only content, in ``data_quality.blockers``.
    """
    summary = build_presentation_summary(
        capital_state_summary={},
        existing_position_results=(),
        entry_candidate_results=(),
        promotion_results=(),
        global_issues=issues,
        security_issues=(),
    )
    rendered = format_orchestrator_summary(summary, evaluation_as_of=evaluation_as_of)
    return to_primitive(summary), rendered


def run_trading_orchestrator(
    conn: sqlite3.Connection,
    *,
    as_of: Optional[str] = None,
    config: Optional[StrategyConfig] = None,
) -> OrchestratorResult:
    """Compose existing read-only rules into a deterministically ordered report.

    ``as_of=None`` is the current analysis path.  An explicit date is passed
    through unchanged to the existing temporal contracts; it is never chosen
    implicitly by the orchestrator.
    """
    evaluation_as_of = as_of or date.today().isoformat()
    strategy_config = config or StrategyConfig()
    global_issues = _global_readiness_issues(conn)
    if any(issue["severity"] == "GLOBAL_BLOCKING" for issue in global_issues):
        presentation_summary, rendered_summary_de = _degenerate_presentation(evaluation_as_of, tuple(global_issues))
        return OrchestratorResult(evaluation_as_of, None, {}, {}, "GLOBAL_BLOCKING", tuple(global_issues), (), (), (), {key: 0 for key in ("SELL", "TRIM", "ADD", "BUY", "PROMOTE", "HOLD", "KEEP_WATCHING", "DATA_INSUFFICIENT")}, {"global_blocking": sum(issue["severity"] == "GLOBAL_BLOCKING" for issue in global_issues), "security_blocking": 0}, {}, (), (), presentation_summary, rendered_summary_de, portfolio_action_plan=unavailable_plan("ORCHESTRATOR_GLOBAL_BLOCKING", evaluation_as_of=evaluation_as_of))

    position_rows = conn.execute("SELECT security_id FROM positions WHERE shares>0 ORDER BY security_id").fetchall()
    # Shared for the whole run: strategy_assignment/fx_rates/swing_campaign
    # existence cannot change mid-run for one read-only connection, so this
    # lets every AnalysisSnapshot/promotion-evaluation built below resolve it
    # once instead of once per security.
    table_exists_cache: dict[str, bool] = {}
    try:
        # Built once here and handed into build_portfolio_context() below so
        # its own exposure computation reuses these instead of rebuilding an
        # identical AnalysisSnapshot per open position.
        position_snapshots = {
            int(row["security_id"]): build_analysis_snapshot(
                int(row["security_id"]), as_of=as_of, connection=conn, table_exists_cache=table_exists_cache
            )
            for row in position_rows
        }
        portfolio = build_portfolio_context(
            as_of=as_of, connection=conn, position_snapshots=position_snapshots, table_exists_cache=table_exists_cache
        )
    except Exception as exc:
        issues = (_issue("GLOBAL", "PORTFOLIO_CONTEXT_UNAVAILABLE", "GLOBAL_BLOCKING", details=(str(exc),)),)
        presentation_summary, rendered_summary_de = _degenerate_presentation(evaluation_as_of, issues)
        return OrchestratorResult(evaluation_as_of, None, {}, {}, "GLOBAL_BLOCKING", issues, (), (), (), {key: 0 for key in ("SELL", "TRIM", "ADD", "BUY", "PROMOTE", "HOLD", "KEEP_WATCHING", "DATA_INSUFFICIENT")}, {"global_blocking": 1, "security_blocking": 0}, {}, (), (), presentation_summary, rendered_summary_de, portfolio_action_plan=unavailable_plan("ORCHESTRATOR_GLOBAL_BLOCKING", evaluation_as_of=evaluation_as_of))

    if portfolio.valuation_quality.status is not AvailabilityStatus.AVAILABLE:
        global_issues.append(_issue("GLOBAL", "PORTFOLIO_VALUATION_UNAVAILABLE", "GLOBAL_BLOCKING", details=portfolio.valuation_quality.details))
    if portfolio.allocation_quality.status is not AvailabilityStatus.AVAILABLE:
        global_issues.append(_issue("GLOBAL", "PORTFOLIO_ALLOCATION_UNAVAILABLE", "GLOBAL_BLOCKING", details=portfolio.allocation_quality.details))
    if portfolio.cash_quality.status is not AvailabilityStatus.AVAILABLE:
        global_issues.append(_issue("GLOBAL", "CAPITAL_STATE_UNAVAILABLE", "NON_BLOCKING_WARNING", details=portfolio.cash_quality.details))
    if portfolio.allocation_guardrails and portfolio.allocation_guardrails != ("WITHIN_TARGET_RANGE",):
        global_issues.append(_issue("GLOBAL", "ALLOCATION_OUTSIDE_TARGET", "NON_BLOCKING_WARNING", details=portfolio.allocation_guardrails))
    if portfolio.cash_available is not None and strategy_config.sizing.minimum_cash_reserve is not None and portfolio.cash_available <= strategy_config.sizing.minimum_cash_reserve:
        global_issues.append(_issue("GLOBAL", "CASH_RESERVE_LIMIT", "NON_BLOCKING_WARNING"))

    existing: list[dict[str, Any]] = []
    security_issues: list[dict[str, Any]] = []
    market_dates: list[str] = []
    for row in position_rows:
        security_id = int(row["security_id"])
        snapshot = position_snapshots[security_id]
        per_security_portfolio = _portfolio_context_for_security(portfolio, security_id)
        decision = decide(snapshot, strategy_config, per_security_portfolio)
        priority = PRIORITY[decision.action]
        record = _security_record(snapshot, decision, priority)
        existing.append(record)
        if snapshot.market_data_as_of:
            market_dates.append(snapshot.market_data_as_of)
        if snapshot.position.campaign_reconciliation_delta not in (None, 0.0):
            security_issues.append(_issue("SECURITY", "LIFECYCLE_RECONCILIATION_DELTA", "SECURITY_BLOCKING", security_id=security_id, symbol=snapshot.symbol, details=snapshot.position.campaign_reconciliation_quality.details))
        elif snapshot.position.strategy.value == "swing" and snapshot.position.swing_campaign_id is None:
            security_issues.append(_issue("SECURITY", "SWING_CAMPAIGN_UNAVAILABLE", "SECURITY_BLOCKING", security_id=security_id, symbol=snapshot.symbol, details=("position has an active Swing strategy but no open campaign",)))
        if snapshot.technical.quality.status in {AvailabilityStatus.STALE, AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.INSUFFICIENT}:
            security_issues.append(_issue("SECURITY", "TECHNICAL_DATA_UNAVAILABLE", "SECURITY_BLOCKING", security_id=security_id, symbol=snapshot.symbol, details=snapshot.technical.quality.details))
        if snapshot.position.fx_quality.status in {AvailabilityStatus.STALE, AvailabilityStatus.UNAVAILABLE}:
            security_issues.append(_issue("SECURITY", "VALUATION_FX_UNAVAILABLE", "SECURITY_BLOCKING", security_id=security_id, symbol=snapshot.symbol, details=snapshot.position.fx_quality.details))

    entry_results: list[dict[str, Any]] = []
    entry_ids = _active_swing_zero_positions(conn, evaluation_as_of)
    for security_id in entry_ids:
        snapshot = build_analysis_snapshot(security_id, as_of=as_of, connection=conn, table_exists_cache=table_exists_cache)
        entry_portfolio = _portfolio_context_for_security(portfolio, security_id)
        decision = decide(snapshot, strategy_config, entry_portfolio)
        entry_results.append(_security_record(snapshot, decision, PRIORITY[decision.action]))

    promotion_results: list[dict[str, Any]] = []
    for promotion in evaluate_swing_candidates(conn, as_of=as_of, table_exists_cache=table_exists_cache):
        if promotion.security_id in entry_ids:
            continue
        primitive = promotion.primitive()
        primitive["priority"] = PRIORITY[promotion.recommendation]
        promotion_results.append(primitive)

    # v0.2.0: PROMOTE is not BUY. Candidate snapshots feed the planner's entry
    # evaluation; the plan is a pure simulation (no write, no order).
    # The plan must never take down the existing report: a candidate whose
    # snapshot cannot be built is marked BLOCKED_BY_DATA by the planner, and an
    # unexpected planner failure becomes a visible UNAVAILABLE plan.
    try:
        candidate_snapshots = {}
        for item in promotion_results:
            if item["recommendation"] != "PROMOTE":
                continue
            try:
                candidate_snapshots[int(item["security_id"])] = build_analysis_snapshot(
                    int(item["security_id"]), as_of=as_of, connection=conn, table_exists_cache=table_exists_cache
                )
            except Exception:
                continue
        portfolio_action_plan = build_portfolio_action_plan(
            portfolio=portfolio,
            existing_position_results=existing,
            entry_candidate_results=entry_results,
            promotion_results=promotion_results,
            candidate_snapshots=candidate_snapshots,
            config=strategy_config,
            position_cost_basis={
                sid: {"avg_cost": snap.position.avg_cost, "currency": snap.position.cost_basis_currency}
                for sid, snap in position_snapshots.items()
            },
        )
    except Exception as exc:
        portfolio_action_plan = unavailable_plan(f"PORTFOLIO_ACTION_PLAN_ERROR:{type(exc).__name__}", evaluation_as_of=evaluation_as_of)

    action_summary = {key: 0 for key in ("SELL", "TRIM", "ADD", "BUY", "PROMOTE", "HOLD", "KEEP_WATCHING", "DATA_INSUFFICIENT")}
    for item in [*existing, *entry_results]:
        action = item["decision"]["action"]
        if action in action_summary:
            action_summary[action] += 1
    for item in promotion_results:
        recommendation = item["recommendation"]
        if recommendation in action_summary:
            action_summary[recommendation] += 1

    review: list[dict[str, Any]] = []
    review.extend({"priority": "P0", **item} for item in security_issues)
    review.extend({"priority": item["priority"], "security_id": item["security_id"], "symbol": item["symbol"], "kind": item["decision"]["action"], "reasons": item["decision"]["reasons"]} for item in [*existing, *entry_results] if item["decision"]["action"] not in {"HOLD", "WATCH"})
    review.extend({"priority": item["priority"], "security_id": item["security_id"], "symbol": item["symbol"], "kind": item["recommendation"], "reasons": item["reasons"], "plan_token": item.get("plan_token")} for item in promotion_results if item["recommendation"] == "PROMOTE")
    review.sort(key=lambda item: (PRIORITY_ORDER[item["priority"]], item.get("security_id") or 0, item.get("kind") or ""))

    all_issues = [*global_issues, *security_issues]
    global_blocking = sum(item["severity"] == "GLOBAL_BLOCKING" for item in all_issues)
    capital_state_summary = {"total_portfolio_market_value_eur": portfolio.total_market_value, "swing_market_value_eur": portfolio.swing_market_value, "swing_weight_pct": portfolio.swing_weight_pct, "long_term_market_value_eur": portfolio.long_term_market_value, "long_term_weight_pct": portfolio.long_term_weight_pct, "cash_available": portfolio.cash_available, "cash_quality": portfolio.cash_quality.status.value, "buying_power": portfolio.buying_power, "buying_power_quality": portfolio.buying_power_quality.status.value, "as_of": portfolio.capital_state_as_of, "source": portfolio.capital_state_source, "allocation_guardrails": list(portfolio.allocation_guardrails), "external_funding_available": portfolio.external_funding_available}

    presentation_summary_obj = build_presentation_summary(
        capital_state_summary=capital_state_summary,
        existing_position_results=existing,
        entry_candidate_results=entry_results,
        promotion_results=promotion_results,
        global_issues=global_issues,
        security_issues=security_issues,
    )
    rendered_summary_de = format_orchestrator_summary(
        presentation_summary_obj,
        evaluation_as_of=evaluation_as_of,
        market_data_as_of=max(market_dates) if market_dates else None,
    )

    return OrchestratorResult(
        evaluation_as_of=evaluation_as_of,
        market_data_as_of=max(market_dates) if market_dates else None,
        portfolio_context=to_primitive(portfolio),
        capital_state_summary=capital_state_summary,
        global_status="GLOBAL_BLOCKING" if global_blocking else "AVAILABLE",
        global_issues=tuple(global_issues),
        existing_position_results=tuple(existing),
        entry_candidate_results=tuple(entry_results),
        promotion_results=tuple(promotion_results),
        action_summary=action_summary,
        blocking_summary={"global_blocking": global_blocking, "security_blocking": len(security_issues)},
        data_quality_summary={"existing_positions": len(existing), "entry_candidates": len(entry_results), "promotion_candidates": len(promotion_results), "global_issues": len(global_issues), "security_issues": len(security_issues)},
        highest_priority_actions=tuple(item for item in review if item["priority"] in {"P0", "P1", "P2", "P3", "P4", "P5"}),
        next_review_items=tuple(review),
        presentation_summary=to_primitive(presentation_summary_obj),
        rendered_summary_de=rendered_summary_de,
        portfolio_action_plan=portfolio_action_plan,
    )
