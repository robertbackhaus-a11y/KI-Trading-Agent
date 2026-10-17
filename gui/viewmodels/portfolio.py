# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""View models for Dashboard and Planner: orchestrator JSON -> display strings.

Pure Python (no Qt, no database).  Nothing is computed here beyond formatting and counting list entries; every figure comes from
the ``run_trading_orchestrator`` result (``detail=true``) and ``database_status``.  A missing or malformed value shows as "—".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .reason_codes import Presented, format_reason_code, format_reason_codes, format_status_and_reasons

DASH = "—"
ACTION_ORDER = ("SELL", "TRIM", "ADD", "BUY", "HOLD")


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)  # optional per-row level: "", "warning", "blocker"


@dataclass
class DashboardVM:
    available: bool = False
    error: str = ""
    refreshed: str = DASH
    portfolio: list[tuple[str, str]] = field(default_factory=list)
    planner: list[tuple[str, str]] = field(default_factory=list)
    planned: Table = field(default_factory=lambda: Table([]))
    deferred: Table = field(default_factory=lambda: Table([]))
    issues: list[tuple[str, str]] = field(default_factory=list)  # (level, text)
    simulation_only: Optional[bool] = None
    orders_created: Optional[bool] = None


@dataclass
class PlannerVM:
    available: bool = False
    error: str = ""
    header: list[tuple[str, str]] = field(default_factory=list)
    counts: str = DASH
    positions: Table = field(default_factory=lambda: Table([]))
    planned: Table = field(default_factory=lambda: Table([]))
    deferred: Table = field(default_factory=lambda: Table([]))
    candidates: Table = field(default_factory=lambda: Table([]))
    notes: list[str] = field(default_factory=list)


# -- formatting -----------------------------------------------------------------------------------------
def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _de(value: float, digits: int) -> str:
    return f"{value:,.{digits}f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


def fmt_eur(value: Any, digits: int = 0) -> str:
    n = _num(value)
    return DASH if n is None else f"{_de(n, digits)} €"


def fmt_pct(value: Any, digits: int = 1) -> str:
    n = _num(value)
    return DASH if n is None else f"{_de(n, digits)} %"


def fmt_qty(value: Any) -> str:
    n = _num(value)
    return DASH if n is None else _de(n, 0 if n == int(n) else 2)


def fmt_score(value: Any) -> str:
    n = _num(value)
    return DASH if n is None else _de(n, 1)


def fmt_bool(value: Any, yes: str = "Ja", no: str = "Nein") -> str:
    return DASH if not isinstance(value, bool) else (yes if value else no)


def fmt_time(value: Optional[datetime]) -> str:
    return DASH if value is None else value.strftime("%d.%m.%Y %H:%M:%S")


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return DASH if value in (None, "") else str(value)


def _decorated(shown: Presented, prefix: str = "", suffix: str = "") -> Presented:
    """``shown`` with text around it; the tooltip of the code stays."""
    return Presented(f"{prefix}{shown}{suffix}", shown.tip)


def unwrap(envelope: Any) -> tuple[Optional[dict], str]:
    """``(result, error)`` of a ``run_trading_orchestrator`` response envelope."""
    env = _d(envelope)
    if not env:
        return None, "Keine Daten"
    if env.get("ok") is False:
        return None, str(env.get("message") or (format_reason_code(env["error"]) if env.get("error") else "Orchestrator meldet einen Fehler"))
    result = env.get("result")
    if not isinstance(result, dict):
        return None, "Orchestrator-Antwort ohne Ergebnis"
    if "portfolio_action_plan" not in result:
        return None, "Orchestrator-Antwort ohne Planner (nicht im Detailmodus?)"
    return result, ""


# -- planned / deferred entries ------------------------------------------------------------------------------
PURCHASE_ACTIONS = ("ADD", "BUY")


def entries_of(plan: dict) -> tuple[list[dict], list[dict]]:
    """``(planned, deferred)`` entries of the plan as the pages list them: the ranked entries of the entry evaluation (``planned_entries`` / ``deferred_entries``) followed by the BUY / ADD
    purchases of the plan's own ``entries_overview`` (the planner decides which purchase is planned and which is deferred; nothing is decided here), shown as ``SYMBOL (BUY)``."""
    planned = [e for e in _l(plan.get("planned_entries")) if isinstance(e, dict)]
    deferred = [e for e in _l(plan.get("deferred_entries")) if isinstance(e, dict)]
    overview = _d(plan.get("entries_overview"))
    for target, key in ((planned, "planned"), (deferred, "deferred")):
        for item in (x for x in _l(overview.get(key)) if isinstance(x, dict) and x.get("kind") in PURCHASE_ACTIONS):
            target.append({**item, "symbol": f"{_text(item.get('symbol'))} ({item['kind']})"})
    return planned, deferred


# -- dashboard ---------------------------------------------------------------------------------------------
def _issue_level(severity: Any) -> str:
    return "blocker" if "BLOCKING" in str(severity) and not str(severity).startswith("NON_") else "warning"


def build_dashboard(envelope: Any, db_status: Any = None, refreshed: Optional[datetime] = None) -> DashboardVM:
    vm = DashboardVM(refreshed=fmt_time(refreshed))
    result, error = unwrap(envelope)
    if result is None:
        vm.error = error
        return vm
    vm.available = True
    ctx, plan = _d(result.get("portfolio_context")), _d(result.get("portfolio_action_plan"))
    capital, current = _d(plan.get("capital")), _d(plan.get("current_state"))
    swing_t, lt_t = _l(current.get("swing_target_pct")), _l(current.get("long_term_target_pct"))

    def target(values: list) -> str:
        return f" (Ziel {fmt_pct(values[0], 0)} – {fmt_pct(values[1], 0)})" if len(values) == 2 else ""

    external = capital.get("external_funding_available", ctx.get("external_funding_available"))
    db = _d(db_status)
    vm.portfolio = [
        ("Bewertungsdatum", _text(result.get("evaluation_as_of"))),
        ("Marktdaten bis", _text(result.get("market_data_as_of"))),
        ("Investierter Wert", fmt_eur(ctx.get("total_market_value"))),
        ("Swing", fmt_pct(ctx.get("swing_weight_pct")) + target(swing_t)),
        ("Long-Term", fmt_pct(ctx.get("long_term_weight_pct")) + target(lt_t)),
        ("Cash", fmt_eur(ctx.get("cash_available")) + (f" (Stand {ctx['capital_state_as_of']})" if ctx.get("capital_state_as_of") else "")),
        ("External Funding verfügbar", fmt_bool(external)),
        ("Allokations-Hinweise", format_reason_codes(_l(ctx.get("allocation_guardrails")))),
        ("Datenbank", DASH if not db else ("OK" if db.get("ok") else "Fehler")),
    ]
    planned, deferred = entries_of(plan)
    summary = _d(result.get("action_summary"))
    vm.simulation_only, vm.orders_created = plan.get("simulation_only"), plan.get("orders_created")
    vm.planner = [
        ("Planner-Status", format_reason_code(plan.get("status"))),
        ("Simulation (simulation_only)", fmt_bool(vm.simulation_only)),
        ("Orders erzeugt (orders_created)", fmt_bool(vm.orders_created)),
        ("Globaler Status", format_reason_code(result.get("global_status"))),
        ("Aktionen der Engine", " · ".join(f"{k} {summary[k]}" for k in ACTION_ORDER + ("PROMOTE",) if isinstance(summary.get(k), int)) or DASH),
        ("Geplante / zurückgestellte Entries", f"{len(planned)} / {len(deferred)}"),
        ("Begrenzung", format_reason_code(capital.get("limiting_guard"))),
        ("Externes Kapital für den Plan", fmt_eur(capital.get("external_funding_required_eur")) if external else "nicht nötig (kein External Funding)"),
    ]
    vm.planned = Table(["Rang", "Symbol", "Stück", "Preis", "Kapital", "Gewicht", "Swing danach", "Extern"], [
        [_text(e.get("rank")), _text(e.get("symbol")), fmt_qty(e.get("quantity")), fmt_eur(e.get("price_eur"), 2), fmt_eur(e.get("capital_eur")),
         fmt_pct(e.get("expected_weight_pct")), fmt_pct(e.get("resulting_swing_allocation_pct")), fmt_eur(e.get("external_funding_eur"))]
        for e in planned if isinstance(e, dict)
    ])
    vm.deferred = Table(["Rang", "Symbol", "Grund"], [[_text(e.get("rank")), _text(e.get("symbol")), format_reason_code(e.get("reason"))] for e in deferred if isinstance(e, dict)],
                        ["blocker" if str(e.get("reason", "")).startswith("BLOCKED_BY_") else "" for e in deferred if isinstance(e, dict)])
    for issue in _l(result.get("global_issues")):
        if isinstance(issue, dict):
            who = f" ({issue['symbol']})" if issue.get("symbol") else ""
            vm.issues.append((_issue_level(issue.get("severity")), _decorated(format_reason_code(issue.get("code")), suffix=who)))
    for warning in _l(plan.get("warnings")):
        vm.issues.append(("info", _decorated(format_reason_code(warning), prefix="Planner: ")))
    if not db_status and db_status is not None:
        vm.issues.append(("warning", "database_status ohne Antwort"))
    return vm


# -- planner -------------------------------------------------------------------------------------------------
def _risk_cells(risk: Any) -> list[str]:
    """Sector / correlation diagnostics of the planner (only present for evaluated candidates and ADD/BUY actions)."""
    r = _d(risk)
    if not r:
        return [DASH] * 5
    s, c = _d(r.get("sector")), _d(r.get("correlation"))
    sector = f"{_text(s.get('sector'))}: {fmt_pct(s.get('current_weight_pct'))} → {fmt_pct(s.get('projected_weight_pct'))} (Limit {fmt_pct(s.get('limit_pct'), 0)})" if s.get("sector") else format_reason_code(s.get("status"))
    top = _d(c.get("strongest"))
    corr = f"{_text(top.get('symbol'))} ρ={_de(top['correlation'], 2)} ({top.get('common_days')} Tage)" if _num(top.get("correlation")) is not None else format_reason_code(c.get("status"))
    cluster = fmt_pct(c.get("cluster_projected_weight_pct")) if c.get("cluster_projected_weight_pct") is not None else DASH
    members = ", ".join(map(str, _l(c.get("cluster_members")))) or DASH
    return [format_reason_code(s.get("status")), sector, corr, cluster, members]


def build_planner(envelope: Any, refreshed: Optional[datetime] = None) -> PlannerVM:
    vm = PlannerVM()
    result, error = unwrap(envelope)
    if result is None:
        vm.error = error
        return vm
    vm.available = True
    plan = _d(result.get("portfolio_action_plan"))
    vm.header = [
        ("Planner-Status", format_reason_code(plan.get("status"))),
        ("Simulation (simulation_only)", fmt_bool(plan.get("simulation_only"))),
        ("Orders erzeugt (orders_created)", fmt_bool(plan.get("orders_created"))),
        ("Bewertungsdatum", _text(result.get("evaluation_as_of"))),
        ("Aktualisiert", fmt_time(refreshed)),
    ]
    summary = _d(result.get("action_summary"))
    vm.counts = " · ".join(f"{k} {summary[k]}" for k in ACTION_ORDER if isinstance(summary.get(k), int)) or DASH
    plan_actions = {(a.get("security_id"), a.get("action")): a for a in _l(plan.get("existing_position_actions")) if isinstance(a, dict)}
    records = [r for r in _l(result.get("existing_position_results")) + _l(result.get("entry_candidate_results")) if isinstance(r, dict)]
    records.sort(key=lambda r: (str(r.get("priority") or "P9"), str(r.get("symbol") or "")))
    rows, levels = [], []
    for record in records:
        decision = _d(record.get("decision"))
        action = str(decision.get("action") or "")
        pa = _d(plan_actions.get((record.get("security_id"), action)))
        block = pa.get("plan_block_reason")
        rows.append([
            _text(record.get("symbol")), _text(record.get("strategy")), _text(action), fmt_qty(record.get("position_quantity")), fmt_qty(decision.get("action_quantity")),
            _text(record.get("priority")), format_reason_codes(_l(decision.get("reasons")), action=action), format_reason_code(block) if block else "",
            *(_risk_cells(pa.get("risk"))[1:3] if pa.get("risk") else [DASH, DASH]),
        ])
        levels.append("blocker" if block else ("warning" if action in ("SELL", "TRIM") else ""))
    vm.positions = Table(["Symbol", "Strategie", "Aktion", "Bestand", "Aktionsmenge", "Prio", "Gründe", "Plan-Block", "Sektor", "Korrelation"], rows, levels)
    planned, deferred = entries_of(plan)
    vm.planned = Table(["Rang", "Symbol", "Stück", "Preis", "Kapital", "Gewicht", "Swing danach", "Intern", "Extern", "Score"], [
        [_text(e.get("rank")), _text(e.get("symbol")), fmt_qty(e.get("quantity")), fmt_eur(e.get("price_eur"), 2), fmt_eur(e.get("capital_eur")), fmt_pct(e.get("expected_weight_pct")),
         fmt_pct(e.get("resulting_swing_allocation_pct")), fmt_eur(e.get("internal_capital_eur")), fmt_eur(e.get("external_funding_eur")), fmt_score(e.get("entry_score"))]
        for e in planned
    ])
    vm.deferred = Table(["Rang", "Symbol", "Score", "Grund"], [[_text(e.get("rank")), _text(e.get("symbol")), fmt_score(e.get("entry_score")), format_reason_code(e.get("reason"))] for e in deferred],
                        ["blocker" if str(e.get("reason", "")).startswith("BLOCKED_BY_") else "" for e in deferred])
    cand_rows, cand_levels = [], []
    for c in (x for x in _l(plan.get("entry_candidates")) if isinstance(x, dict)):
        risk = _risk_cells(c.get("risk"))
        status, reasons = format_status_and_reasons(c.get("entry_status"), _l(c.get("reason_codes")))
        cand_rows.append([_text(c.get("rank")), _text(c.get("symbol")), status, fmt_score(c.get("entry_score")), reasons, *risk])
        blocked = _d(_d(c.get("risk")).get("sector")).get("status") == "BLOCKED" or _d(_d(c.get("risk")).get("correlation")).get("status") == "BLOCKED"
        cand_levels.append("blocker" if blocked else "")
    vm.candidates = Table(["Rang", "Symbol", "Status", "Score", "Codes", "Sektor-Status", "Sektor (aktuell → nach Kauf)", "Stärkste Korrelation", "Cluster nach Kauf", "Cluster"], cand_rows, cand_levels)
    vm.notes = [format_reason_code(w) for w in _l(plan.get("warnings"))]
    return vm
