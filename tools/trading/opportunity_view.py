"""Unified opportunity view: engine candidates + discovery report + market intelligence report (read-only, deterministic).

``DISCOVERY + MARKET INTELLIGENCE + PORTFOLIO/PLANNER CONTEXT -> one opportunity output``.

This module only *joins and presents* results that already exist:

* the planner / promotion results of the unchanged orchestrator (entry status, rank, entry score, sizing, plan),
* the last finished discovery report (``DISCOVERY_READY`` candidates),
* the last finished market-intelligence report (news context).

It computes no score, no rank, no sizing and no recommendation of its own.  News are attached as context and a
context status (``NEWS_*``) only; they never change a discovery score, PROMOTE, ENTRY_READY, a rank, a position size or a
SELL/TRIM/HOLD decision.  Nothing is written, nothing is fetched, no order is created, no LLM is involved.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

import discovery_report as dr
import intelligence_report as ir
from market_intelligence import MACRO_CATEGORIES, company_mentioned
from strategy_config import DataQualityConfig, MarketIntelligenceConfig

SCHEMA_VERSION = 1
SOURCES = ("WATCHLIST", "DISCOVERY", "BOTH")
GROUPS = ("ENTRY_READY", "WAIT_FOR_TRIGGER", "DISCOVERY_READY", "OTHER")
NEWS_STATUSES = ("NEWS_CLEAR", "NEWS_ATTENTION", "NEWS_HIGH_ATTENTION", "NEWS_UNAVAILABLE")
NEWS_FILTERS = (*NEWS_STATUSES, "RELEVANT", "NOT_HIGH")
STATUS_ALIASES = {"ENTRY": "ENTRY_READY", "WAIT": "WAIT_FOR_TRIGGER", "DISCOVERY": "DISCOVERY_READY", "READY": "DISCOVERY_READY"}
MACRO_ORDER = ("MONETARY_POLICY", "INFLATION", "LABOR_MARKET", "ECONOMIC_GROWTH", "GEOPOLITICS", "ENERGY", "MACRO")

DEFAULT_LIMIT = 30  # a status filter such as ENTRY_READY (12 rows today) is delivered completely, so no second call is provoked
MAX_LIMIT_COMPACT = 40
MAX_LIMIT_DETAIL = 10
RESPONSE_CHAR_BUDGET = 34_000  # the MCP server refuses results above 40,000 characters (RESULT_TOO_LARGE)
EVENTS_PER_ROW_COMPACT = 1
EVENTS_PER_ROW_DETAIL = 6
HIGH_ATTENTION_LIMIT = 10

_IMPORTANCE_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_LINK_ORDER = {"DIRECT": 0, "SECTOR": 1, "LOOSE": 2}


# ---------------------------------------------------------------- small helpers
def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _round(value: Any, digits: int = 2) -> Optional[float]:
    number = _num(value)
    return None if number is None else round(number, digits)


def _pick(source: Optional[Mapping[str, Any]], keys: Iterable[str]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys} if isinstance(source, Mapping) else {}


def load_symbol_map(conn: Any) -> dict[int, str]:
    """``security_id -> Yahoo symbol`` (read-only SELECT; the market-data source symbol of each security)."""
    rows = conn.execute(
        "SELECT ss.security_id, ss.symbol FROM source_symbols ss JOIN data_sources d ON d.id = ss.source_id WHERE d.name = 'Yahoo Finance' ORDER BY ss.security_id"
    ).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


# ---------------------------------------------------------------- news context (attached, never scored)
def index_events(mi_report: Optional[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Yahoo symbol -> events of the market-intelligence report that name it (``affected_symbols``)."""
    index: dict[str, list[Mapping[str, Any]]] = {}
    for event in (mi_report or {}).get("events", []):
        for symbol in event.get("affected_symbols", []):
            index.setdefault(symbol, []).append(event)
    return index


def event_link(event: Mapping[str, Any], yahoo_symbol: str, name: str) -> str:
    """How strongly an event is tied to *this* company.

    DIRECT: a SEC filing of the company, or a headline that names the company (legal-name phrase or a distinctive word of
    the name, or a ticker in parentheses such as "(ABCD)" / "(NASDAQ:ABCD)"; a bare ticker is not enough because tickers can be homonyms).  SECTOR: the event was mapped to the symbol
    only through its sector.  LOOSE: the provider attached the headline to the symbol without the headline naming it."""
    if event.get("source_type") == "SEC_FILING":
        return "DIRECT"
    headline = str(event.get("headline") or "")
    if company_mentioned(headline, yahoo_symbol, {"name": name}, allow_ticker=False):
        return "DIRECT"
    ticker = (yahoo_symbol or "").split(".")[0]
    if len(ticker) >= 2 and re.search(rf"\((?:[A-Z]+:)?{re.escape(ticker)}\)", headline):
        return "DIRECT"
    if "SECTOR_SCOPED_EVENT" in (event.get("reason_codes") or []):
        return "SECTOR"
    return "LOOSE"


def _event_row(event: Mapping[str, Any], link: str, detail: bool) -> dict[str, Any]:
    row = {
        "published_at": str(event.get("published_at") or "")[:16],
        "category": event.get("category"), "importance": event.get("importance"), "impact": event.get("impact"),
        "headline": str(event.get("headline") or "")[:110], "source": event.get("source"), "link": link,
        "reason_codes": list(event.get("reason_codes") or [])[: (None if detail else 4)],
    }
    if detail:
        row["source_url"] = event.get("source_url")
        row["event_id"] = event.get("event_id")
    return row


def news_context(yahoo_symbol: Optional[str], name: str, index: Optional[Mapping[str, Sequence[Mapping[str, Any]]]], *, detail: bool = False) -> dict[str, Any]:
    """Pure classification of the news around one symbol.  ``index is None`` means: no valid report -> NEWS_UNAVAILABLE.

    NEWS_HIGH_ATTENTION: at least one DIRECT HIGH event.  NEWS_ATTENTION: a DIRECT MEDIUM event, or a HIGH event that is
    only sector-/loosely linked.  NEWS_CLEAR: only LOW events, loosely linked MEDIUM events, or no events."""
    empty = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    if index is None:
        return {"status": "NEWS_UNAVAILABLE", "status_reasons": ["NO_VALID_MARKET_INTELLIGENCE_REPORT"], "counts": dict(empty), "direct_counts": dict(empty), "events": []}
    linked = [(event, event_link(event, yahoo_symbol, name)) for event in index.get(yahoo_symbol or "", [])]
    counts, direct = dict(empty), dict(empty)
    for event, link in linked:
        counts[event["importance"]] += 1
        if link == "DIRECT":
            direct[event["importance"]] += 1
    if direct["HIGH"]:
        status, reasons = "NEWS_HIGH_ATTENTION", ["DIRECT_HIGH_EVENT"]
    elif direct["MEDIUM"]:
        status, reasons = "NEWS_ATTENTION", ["DIRECT_MEDIUM_EVENT"]
    elif counts["HIGH"]:
        status, reasons = "NEWS_ATTENTION", ["SECTOR_OR_LOOSE_HIGH_EVENT"]
    elif linked:
        status, reasons = "NEWS_CLEAR", ["ONLY_LOW_OR_LOOSELY_LINKED_MEDIUM_EVENTS"]
    else:
        status, reasons = "NEWS_CLEAR", ["NO_LINKED_EVENTS"]
    ordered = sorted(linked, key=lambda pair: (_IMPORTANCE_ORDER[pair[0]["importance"]], _LINK_ORDER[pair[1]], -_epoch(pair[0].get("published_at")), pair[0].get("event_id") or ""))
    # LOW events never go into the compact list, they are only counted
    shown = [pair for pair in ordered if pair[0]["importance"] != "LOW"][: (EVENTS_PER_ROW_DETAIL if detail else EVENTS_PER_ROW_COMPACT)]
    return {"status": status, "status_reasons": reasons, "counts": counts, "direct_counts": direct, "events": [_event_row(e, l, detail) for e, l in shown]}


def _epoch(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def macro_context(mi_report: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """HIGH/MEDIUM macro events per macro category (context only)."""
    if mi_report is None:
        return {"status": "UNAVAILABLE", "categories": {}}
    categories: dict[str, Any] = {}
    for category in MACRO_ORDER:
        events = [e for e in mi_report["events"] if e["category"] == category and e["importance"] in ("HIGH", "MEDIUM")]
        if not events:
            continue
        events = sorted(events, key=lambda e: (_IMPORTANCE_ORDER[e["importance"]], 0 if e["source_type"] == "OFFICIAL_FEED" else 1, -_epoch(e["published_at"]), e["event_id"]))
        categories[category] = {
            "high": sum(e["importance"] == "HIGH" for e in events), "medium": sum(e["importance"] == "MEDIUM" for e in events),
            "official_source_events": sum(e["source_type"] == "OFFICIAL_FEED" for e in events),
            "top_events": [{"published_at": e["published_at"][:16], "importance": e["importance"], "impact": e["impact"], "source": e["source"], "headline": str(e["headline"])[:110], "regions": e.get("affected_regions", []), "portfolio_region_exposure": e.get("portfolio_region_exposure", [])} for e in events[:2]],
        }
    return {"status": "AVAILABLE", "categories": categories}


# ---------------------------------------------------------------- freshness of the two reports
def _report_state(report: Optional[Mapping[str, Any]], reason: Optional[str], freshness: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if report is None:
        return {"status": "UNAVAILABLE", "reason": reason, "generated_at": None, "evaluation_as_of": None, "age_hours": None, "stale": None}
    return {"status": "AVAILABLE", "reason": None, "generated_at": freshness["generated_at"], "evaluation_as_of": freshness["evaluation_as_of"],
            "age_hours": freshness["age_hours"], "stale": freshness["status"] == "REPORT_STALE", "freshness": freshness["status"]}


# ---------------------------------------------------------------- the view
def _quality(promotion: Optional[Mapping[str, Any]], entry: Optional[Mapping[str, Any]], discovery: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    entry_quality = (entry or {}).get("quality") or {}
    return {
        "technical_quality": (promotion or {}).get("technical_quality") or entry_quality.get("technical") or (discovery or {}).get("technical_quality"),
        "fundamental_quality": (promotion or {}).get("fundamental_quality") or entry_quality.get("fundamental") or ("not_evaluated" if discovery is not None else None),
        "event_risk_quality": (promotion or {}).get("event_risk_quality") or entry_quality.get("event_risk") or (discovery or {}).get("event_risk_quality"),
    }


def _group(entry_status: Optional[str], discovery_status: Optional[str]) -> str:
    if entry_status in ("ENTRY_READY", "WAIT_FOR_TRIGGER"):
        return entry_status
    return "DISCOVERY_READY" if discovery_status == "DISCOVERY_READY" else "OTHER"


def _sort_key(row: Mapping[str, Any]) -> tuple:
    rank = row.get("entry_rank")
    score = row.get("entry_score")
    priority = row.get("watchlist_priority")
    discovery_rank = row.get("discovery_rank")
    return (
        GROUPS.index(row["group"]),
        rank if rank is not None else 10**9,
        -(score if score is not None else -1e9),
        discovery_rank if discovery_rank is not None else 10**9,
        -(priority if priority is not None else -1e9),
        row.get("security_id") if row.get("security_id") is not None else 10**9,
        row["symbol"],
    )


def build_opportunity_view(
    *,
    engine: Optional[Mapping[str, Any]],
    symbol_map: Mapping[int, str],
    discovery: Optional[Mapping[str, Any]],
    discovery_reason: Optional[str] = None,
    mi: Optional[Mapping[str, Any]],
    mi_reason: Optional[str] = None,
    now: Optional[datetime] = None,
    engine_error: Optional[str] = None,
    discovery_max_age_days: Optional[int] = None,
    mi_max_age_hours: Optional[int] = None,
) -> dict[str, Any]:
    """Pure join of already computed results.  Inputs are never modified."""
    now = now or datetime.now(timezone.utc)
    discovery_age = DataQualityConfig().market_data_max_age_days if discovery_max_age_days is None else discovery_max_age_days
    discovery_freshness = dr._freshness(discovery["metadata"], now, discovery_age) if discovery is not None else None
    mi_freshness = ir._freshness(mi["metadata"], now, MarketIntelligenceConfig().report_max_age_hours if mi_max_age_hours is None else mi_max_age_hours) if mi is not None else None
    discovery_state = _report_state(discovery, discovery_reason, discovery_freshness)
    mi_state = _report_state(mi, mi_reason, mi_freshness)
    warnings: list[dict[str, str]] = []
    if engine is None:
        warnings.append({"code": "ENGINE_UNAVAILABLE", "message": engine_error or "the orchestrator result is not available"})
    else:
        for issue in engine.get("global_issues") or []:
            warnings.append({"code": f"ENGINE_ISSUE:{issue.get('code')}", "message": f"{issue.get('severity')}: {', '.join(map(str, issue.get('details') or []))}"})
    for label, state in (("DISCOVERY", discovery_state), ("MARKET_INTELLIGENCE", mi_state)):
        if state["status"] == "UNAVAILABLE":
            warnings.append({"code": f"{label}_REPORT_UNAVAILABLE", "message": str(state["reason"])})
        elif state["stale"]:
            warnings.append({"code": f"{label}_REPORT_STALE", "message": f"report evaluation {state['evaluation_as_of']}, generated {state['generated_at']}"})

    index = index_events(mi) if mi is not None else None
    plan = (engine or {}).get("portfolio_action_plan") or {}
    promotions = {int(p["security_id"]): p for p in (engine or {}).get("promotion_results") or [] if p.get("security_id") is not None}
    entries = {int(c["security_id"]): c for c in plan.get("entry_candidates") or [] if c.get("security_id") is not None}
    planned = {int(p["security_id"]): p for p in plan.get("planned_entries") or []}
    deferred = {int(p["security_id"]): p for p in plan.get("deferred_entries") or []}
    held_ids = {int(p["security_id"]) for p in (engine or {}).get("existing_position_results") or [] if p.get("security_id") is not None}
    discovery_by_symbol = {r["symbol"]: r for r in (discovery or {}).get("results", [])}
    known_by_yahoo = {symbol: security_id for security_id, symbol in symbol_map.items() if security_id in promotions or security_id in entries or security_id in held_ids}

    capital = _capital(plan)
    rows: list[dict[str, Any]] = []
    seen_yahoo: set[str] = set()

    def finish(row: dict[str, Any], yahoo: Optional[str]) -> None:
        row["market_intelligence_summary"] = news_context(yahoo, row["name"], index, detail=True)
        rows.append(row)

    # A) watchlist / PROMOTE candidates of the engine (entry candidates are the planner's PROMOTE set)
    for security_id in sorted(set(entries) | {i for i, p in promotions.items() if p.get("recommendation") == "PROMOTE"}):
        entry, promotion = entries.get(security_id), promotions.get(security_id)
        symbol = str((entry or promotion)["symbol"])
        yahoo = symbol_map.get(security_id, symbol)
        seen_yahoo.add(yahoo)
        disc = discovery_by_symbol.get(yahoo)
        entry_status = (entry or {}).get("entry_status")
        sizing = (planned.get(security_id) or {})
        row = {
            "symbol": symbol, "yahoo_symbol": yahoo, "security_id": security_id, "name": str((entry or promotion).get("name") or ""),
            "source": "BOTH" if disc is not None else "WATCHLIST", "group": _group(entry_status, (disc or {}).get("status")), "held": security_id in held_ids,
            "discovery_rank": (disc or {}).get("rank"), "discovery_score": (disc or {}).get("discovery_score"), "discovery_status": (disc or {}).get("status"),
            "promotion_status": (promotion or {}).get("recommendation"), "entry_status": entry_status,
            "entry_rank": (entry or {}).get("rank"), "entry_score": (entry or {}).get("entry_score"),
            "momentum_score": (entry or {}).get("momentum_score", (promotion or {}).get("momentum_score")), "confidence": (entry or {}).get("confidence"),
            "watchlist_priority": (entry or {}).get("watchlist_priority", (promotion or {}).get("watchlist_priority")),
            "current_price_eur": _round((entry or {}).get("price_eur", (promotion or {}).get("current_price_eur"))),
            **_quality(promotion, entry, disc),
            "plan_status": "PLANNED" if security_id in planned else ("DEFERRED" if security_id in deferred else "NOT_PLANNED"),
            "planned_capital_eur": _round(sizing.get("capital_eur")), "planned_quantity": sizing.get("quantity"),
            # the engine's own standalone sizing (what the candidate would need); passed through, never recomputed
            "proposed_capital_eur": _round(((entry or {}).get("sizing") or {}).get("proposed_capital_eur")), "proposed_quantity": ((entry or {}).get("sizing") or {}).get("proposed_quantity"),
            "plan_reason": (planned.get(security_id) or deferred.get(security_id) or {}).get("reason"),
            "reason_codes": list((entry or {}).get("reason_codes") or (promotion or {}).get("reasons") or []),
            "capital_fit": None,
        }
        if disc is not None:
            row["reason_codes"] = row["reason_codes"] + [f"DISCOVERY_{c}" for c in (disc.get("reason_codes") or [])]
        finish(row, yahoo)

    # B) DISCOVERY_READY candidates of the last discovery report
    for result in (discovery or {}).get("results", []):
        if result.get("status") != "DISCOVERY_READY" or result["symbol"] in seen_yahoo:
            continue
        yahoo = str(result["symbol"])
        security_id = known_by_yahoo.get(yahoo)
        known = security_id is not None
        promotion = promotions.get(security_id) if known else None
        row = {
            "symbol": yahoo, "yahoo_symbol": yahoo, "security_id": security_id, "name": str(result.get("name") or ""),
            "source": "BOTH" if known else "DISCOVERY", "group": "DISCOVERY_READY", "held": security_id in held_ids if known else False,
            "discovery_rank": result.get("rank"), "discovery_score": result.get("discovery_score"), "discovery_status": result.get("status"),
            "promotion_status": (promotion or {}).get("recommendation"), "entry_status": None, "entry_rank": None, "entry_score": None,
            "momentum_score": result.get("momentum_score"), "confidence": result.get("confidence"), "watchlist_priority": None,
            "current_price_eur": _round(result.get("current_price_eur")),
            **_quality(promotion, None, result),
            "plan_status": "NOT_APPLICABLE", "planned_capital_eur": None, "planned_quantity": None, "proposed_capital_eur": None, "proposed_quantity": None, "plan_reason": None,
            "reason_codes": [f"DISCOVERY_{c}" for c in (result.get("reason_codes") or [])] + (["DISCOVERY_SYMBOL_ALREADY_KNOWN"] if known else []),
            "capital_fit": _capital_fit(result.get("current_price_eur"), capital),
        }
        if known:
            warnings.append({"code": "DISCOVERY_SYMBOL_ALREADY_KNOWN", "message": f"{yahoo} is already in the portfolio/watchlist; the discovery report is older than that change"})
        finish(row, yahoo)

    rows.sort(key=_sort_key)
    positions = [_position_row(p, symbol_map, index) for p in (engine or {}).get("existing_position_results") or []]
    high_attention = _high_attention(rows, positions)
    watchlist_symbols = [r["symbol"] for r in rows if r["entry_status"] is not None or r["promotion_status"] is not None]
    news_counts = {status: sum(r["market_intelligence_summary"]["status"] == status for r in rows) for status in NEWS_STATUSES}
    available = [s for s in ("engine" if engine is not None else None, "discovery" if discovery is not None else None, "mi" if mi is not None else None) if s]
    status = "UNAVAILABLE" if not available else ("AVAILABLE" if len(available) == 3 else "PARTIAL")
    return {
        "metadata": {
            "schema_version": SCHEMA_VERSION, "generated_at": now.isoformat(), "evaluation_as_of": (engine or {}).get("evaluation_as_of"), "status": status,
            "read_only": True, "orders_created": False, "signals_changed": False, "deterministic": True,
            "sources": {
                "engine": {"status": "AVAILABLE" if engine is not None else "UNAVAILABLE", "global_status": (engine or {}).get("global_status"), "evaluation_as_of": (engine or {}).get("evaluation_as_of"), "error": engine_error},
                "discovery": discovery_state, "market_intelligence": mi_state,
            },
            "counts": {
                "opportunities": len(rows), "by_group": {g: sum(r["group"] == g for r in rows) for g in GROUPS},
                "by_source": {s: sum(r["source"] == s for r in rows) for s in SOURCES}, "by_news_status": news_counts, "held_positions": len(positions),
            },
            "methodology": _methodology(),
        },
        "portfolio_context": {**capital, "positions": positions},
        "watchlist_candidates": {"count": len(watchlist_symbols), "entry_summary": dict(plan.get("entry_summary") or {}), "symbols": watchlist_symbols},
        "discovery_candidates": {"status": discovery_state["status"], "counts": _pick(discovery["metadata"], dr.COUNT_BY_STATUS.values()) if discovery is not None else {},
                                 "ready_in_view": sum(r["discovery_status"] == "DISCOVERY_READY" for r in rows), "universe_size": (discovery or {}).get("metadata", {}).get("universe_size")},
        "market_context": {
            "status": mi_state["status"], "stale": mi_state["stale"], "counts": _pick(mi["metadata"], ("event_count", "high_count", "medium_count", "low_count", "macro_event_count")) if mi is not None else {},
            "macro_context": macro_context(mi), "high_attention": high_attention,
        },
        "opportunities": rows,
        "warnings": warnings,
    }


def _methodology() -> dict[str, Any]:
    return {
        "scope": "join and presentation of existing engine/discovery/market-intelligence results; no new score, rank, sizing or recommendation",
        "news": "context only (NEWS_* status); never changes discovery_score, PROMOTE, ENTRY_READY, ranks, sizing or SELL/TRIM/HOLD",
        "news_status_rules": {
            "NEWS_HIGH_ATTENTION": "at least one DIRECT HIGH event (SEC filing or headline naming the company)",
            "NEWS_ATTENTION": "a DIRECT MEDIUM event, or a HIGH event linked only through its sector or loosely by the provider",
            "NEWS_CLEAR": "only LOW events, loosely linked MEDIUM events, or no events",
            "NEWS_UNAVAILABLE": "no valid market-intelligence report",
        },
        "group_order": list(GROUPS), "order_within_group": "engine rank, entry_score (desc), discovery rank, watchlist priority (desc), security_id, symbol",
        "capital_fit": "informational comparison of one share's price with deployable cash; no sizing and no order",
    }


def _capital(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not plan:
        return {"engine_status": "UNAVAILABLE", "capital": {}, "after_actions": {}, "after_plan": {}, "planned_entries": [], "deferred_entries": []}
    cur, post, final = plan.get("current_state") or {}, plan.get("post_action_state") or {}, plan.get("final_simulated_state") or {}
    return {
        "engine_status": plan.get("status") or "AVAILABLE",
        "capital": {
            "cash_eur": _round(cur.get("cash_eur")), "deployable_cash_eur": _round(cur.get("deployable_cash_eur")), "minimum_cash_reserve_eur": _round(cur.get("minimum_cash_reserve_eur")),
            "swing_pct": _round(cur.get("swing_pct")), "swing_target_pct": cur.get("swing_target_pct"), "long_term_pct": _round(cur.get("long_term_pct")), "long_term_target_pct": cur.get("long_term_target_pct"),
            "swing_value_eur": _round(cur.get("swing_value_eur")), "total_market_value_eur": _round(cur.get("total_market_value_eur")),
            "remaining_swing_capacity_eur": _round(cur.get("remaining_swing_capacity_eur")), "allocation_guardrails": list(cur.get("allocation_guardrails") or []),
        },
        "after_actions": {
            "cash_after_actions_eur": _round(post.get("cash_after_actions_eur")), "deployable_cash_eur": _round(post.get("deployable_cash_eur")),
            "expected_proceeds_gross_eur": _round(post.get("expected_proceeds_gross_eur")), "expected_proceeds_net_conservative_eur": _round(post.get("expected_proceeds_net_conservative_eur")),
            "proceeds_basis": post.get("proceeds_basis"), "taxes_modelled": post.get("taxes_modelled"), "fees_modelled": post.get("fees_modelled"),
            "swing_pct": _round(post.get("swing_pct")), "remaining_swing_capacity_eur": _round(post.get("remaining_swing_capacity_eur")),
        },
        "after_plan": {
            "planned_entries_count": len(plan.get("planned_entries") or []), "planned_entries_total_eur": _round(final.get("planned_entries_total_eur")),
            "cash_eur": _round(final.get("cash_eur")), "remaining_buying_capacity_eur": _round(final.get("deployable_cash_eur")),
            "remaining_swing_capacity_eur": _round(final.get("remaining_swing_capacity_eur")), "swing_pct": _round(final.get("swing_pct")),
            "cash_above_reserve": final.get("cash_above_reserve"), "swing_within_max": final.get("swing_within_max"),
        },
        "planned_entries": [{"security_id": p.get("security_id"), "symbol": p.get("symbol"), "rank": p.get("rank"), "capital_eur": _round(p.get("capital_eur")), "quantity": p.get("quantity"), "entry_score": p.get("entry_score")} for p in plan.get("planned_entries") or []],
        "deferred_entries": [{"security_id": p.get("security_id"), "symbol": p.get("symbol"), "rank": p.get("rank"), "reason": p.get("reason")} for p in plan.get("deferred_entries") or []],
    }


def _capital_fit(price_eur: Any, capital: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    price = _num(price_eur)
    after_actions = _num((capital.get("after_actions") or {}).get("deployable_cash_eur"))
    after_plan = _num((capital.get("after_plan") or {}).get("remaining_buying_capacity_eur"))
    if price is None or after_actions is None or after_plan is None:
        return None
    return {"price_eur": round(price, 2), "deployable_after_actions_eur": round(after_actions, 2), "remaining_buying_capacity_after_plan_eur": round(after_plan, 2),
            "fits_one_share_after_actions": price <= after_actions, "fits_one_share_after_plan": price <= after_plan}


def fit_code(fit: Optional[Mapping[str, Any]]) -> str:
    """Compact label of a capital_fit comparison (informational only)."""
    if not fit:
        return "UNKNOWN"
    if fit["fits_one_share_after_plan"]:
        return "FITS_AFTER_PLAN"
    return "COMMITTED_BY_PLANNED_ENTRIES" if fit["fits_one_share_after_actions"] else "DOES_NOT_FIT"


def _position_row(position: Mapping[str, Any], symbol_map: Mapping[int, str], index: Optional[Mapping[str, Sequence[Mapping[str, Any]]]]) -> dict[str, Any]:
    security_id = position.get("security_id")
    symbol = str(position.get("symbol"))
    yahoo = symbol_map.get(security_id, symbol) if security_id is not None else symbol
    news = news_context(yahoo, str(position.get("name") or ""), index, detail=True)
    return {"symbol": symbol, "yahoo_symbol": yahoo, "security_id": security_id, "name": position.get("name"), "strategy": position.get("strategy"),
            "action": (position.get("decision") or {}).get("action"), "priority": position.get("priority"), "market_intelligence_summary": news}


def _high_attention(rows: Sequence[Mapping[str, Any]], positions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for scope, items in (("POSITION", positions), ("OPPORTUNITY", rows)):
        for item in items:
            summary = item["market_intelligence_summary"]
            if summary["status"] != "NEWS_HIGH_ATTENTION":
                continue
            for event in summary["events"]:
                if event["importance"] == "HIGH" and event["link"] == "DIRECT":
                    out.append({"scope": scope, "symbol": item["symbol"], "published_at": event["published_at"], "category": event["category"], "importance": event["importance"],
                                "impact": event["impact"], "headline": event["headline"], "source": event["source"]})
    return out


# ---------------------------------------------------------------- reader-side query / compacting / rendering
ROW_FIELDS = (
    "symbol", "name", "source", "group", "held", "discovery_rank", "discovery_score", "discovery_status", "promotion_status", "entry_status", "entry_rank", "entry_score",
    "momentum_score", "confidence", "current_price_eur", "technical_quality", "fundamental_quality", "event_risk_quality", "plan_status", "plan_reason", "capital_fit",
)
DETAIL_EXTRA_FIELDS = ("yahoo_symbol", "security_id", "watchlist_priority", "planned_capital_eur", "planned_quantity", "proposed_capital_eur", "proposed_quantity", "reason_codes")


def _symbol_wanted(row: Mapping[str, Any], wanted: Sequence[str]) -> bool:
    names = {str(row["symbol"]).upper(), str(row.get("yahoo_symbol") or "").upper(), str(row.get("yahoo_symbol") or "").upper().split(".")[0], str(row["symbol"]).upper().split(".")[0]}
    return bool(names & set(wanted))


def _news_wanted(summary: Mapping[str, Any], wanted: str) -> bool:
    status = summary["status"]
    if wanted == "RELEVANT":
        return status in ("NEWS_ATTENTION", "NEWS_HIGH_ATTENTION")
    if wanted == "NOT_HIGH":
        return status != "NEWS_HIGH_ATTENTION"
    return status == wanted


def query_view(
    view: Mapping[str, Any],
    *,
    source: str = "ALL",
    status: str = "ALL",
    news_status: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    detail: bool = False,
) -> dict[str, Any]:
    """Filter / compact an already built view (pure)."""
    fail = lambda error, message: {"ok": False, "operation": "get_opportunity_view", "error": error, "message": message}
    wanted_source = str(source or "ALL").strip().upper()
    if wanted_source not in ("ALL", *SOURCES):
        return fail("INVALID_SOURCE", f"source must be one of ALL, {', '.join(SOURCES)}")
    wanted_status = STATUS_ALIASES.get(str(status or "ALL").strip().upper(), str(status or "ALL").strip().upper())
    if wanted_status not in ("ALL", *GROUPS):
        return fail("INVALID_STATUS", f"status must be one of ALL, {', '.join(GROUPS)}")
    wanted_news = str(news_status).strip().upper() if news_status and str(news_status).strip() else None
    if wanted_news and wanted_news not in NEWS_FILTERS:
        return fail("INVALID_NEWS_STATUS", f"news_status must be one of {', '.join(NEWS_FILTERS)}")
    wanted_symbols = [s.strip().upper() for s in str(symbol).split(",") if s.strip()] if symbol else []

    matching = [r for r in view["opportunities"]
                if wanted_source in ("ALL", r["source"]) and wanted_status in ("ALL", r["group"])
                and (wanted_news is None or _news_wanted(r["market_intelligence_summary"], wanted_news)) and (not wanted_symbols or _symbol_wanted(r, wanted_symbols))]
    applied = max(1, min(int(limit), MAX_LIMIT_DETAIL if detail else MAX_LIMIT_COMPACT))
    if not detail:
        return _compact_response(view, matching, applied, limit, wanted_source, wanted_status, wanted_news, wanted_symbols)
    fields = ROW_FIELDS + DETAIL_EXTRA_FIELDS
    rows = []
    for row in matching[:applied]:
        out = {key: row.get(key) for key in fields}
        news = row["market_intelligence_summary"]
        out["market_intelligence_summary"] = {**news, "events": [_slim_event(e, True) for e in news["events"][:EVENTS_PER_ROW_DETAIL]]}
        rows.append(out)

    shown = [
        {"symbol": r["symbol"], "name": r["name"], "source": r["source"], "discovery_rank": r["discovery_rank"], "discovery_score": r["discovery_score"], "discovery_status": r["discovery_status"],
         "current_price_eur": r["current_price_eur"], "news_status": r["market_intelligence_summary"]["status"], "news_counts": r["market_intelligence_summary"]["counts"],
         "capital_fit": fit_code(r["capital_fit"]) if r["capital_fit"] is not None else None}
        for r in matching if r["discovery_status"] is not None
    ]
    meta = view["metadata"]
    context = view["portfolio_context"]
    positions = context["positions"]
    if wanted_symbols:
        positions = [p for p in positions if _symbol_wanted(p, wanted_symbols)]
    else:
        positions = [p for p in positions if p["action"] in ("SELL", "TRIM", "ADD") or p["market_intelligence_summary"]["status"] in ("NEWS_ATTENTION", "NEWS_HIGH_ATTENTION")]
    response: dict[str, Any] = {
        "ok": True, "operation": "get_opportunity_view", "status": meta["status"], "read_only": True, "orders_created": False, "signals_changed": False,
        "evaluation_as_of": meta["evaluation_as_of"], "generated_at": meta["generated_at"], "sources": meta["sources"], "counts": meta["counts"],
        "portfolio_context": {k: context[k] for k in ("engine_status", "capital", "after_actions", "after_plan", "planned_entries")} | ({"deferred_entries": context["deferred_entries"]} if detail or wanted_symbols else {"deferred_entries_count": len(context["deferred_entries"])})
        | {"positions": [_slim_position(p, detail, 6 if wanted_symbols else 2) for p in positions]},
        "watchlist_candidates": view["watchlist_candidates"], "discovery_candidates": {**view["discovery_candidates"], "shown": shown[:8], "shown_total": len(shown)},
        "market_context": {**view["market_context"], "high_attention": view["market_context"]["high_attention"][:HIGH_ATTENTION_LIMIT], "macro_context": _slim_macro(view["market_context"]["macro_context"], detail)},
        "filter": {"source": wanted_source, "status": wanted_status, "news_status": wanted_news, "symbol": wanted_symbols or None, "limit_requested": limit, "limit_applied": applied, "detail": bool(detail)},
        "matching": len(matching), "returned": len(rows), "truncated": len(matching) > len(rows), "opportunities": rows,
        "warnings": view["warnings"],
        "notes": [
            "Read-only join of the last orchestrator result, the last discovery report and the last market-intelligence report; no web request, no scan, no database write, no order.",
            "NEWS is context only (NEWS_* status): it changes no discovery score, PROMOTE/ENTRY_READY, rank, position size or SELL/TRIM/HOLD. There is no combined score.",
        ],
    }
    if detail:
        response["methodology"] = meta["methodology"]
    response["rendered_de"] = render_view_de(response)

    def size() -> int:
        return len(json.dumps(response, ensure_ascii=False))

    # size guard, step 1: secondary sections are reduced first (methodology, position events, macro detail, discovery list)
    for label, reduce in _EXTRA_REDUCTIONS:
        if size() <= RESPONSE_CHAR_BUDGET:
            break
        if reduce(response):
            response["notes"].append(f"RESPONSE_TRIMMED_TO_SIZE_BUDGET: {label} reduced")
            response["rendered_de"] = render_view_de(response)
    # step 2: the least important opportunities (the list is sorted by group and engine rank) are dropped
    while len(rows) > 1 and size() > RESPONSE_CHAR_BUDGET:
        current = size()
        drop = max(1, int(len(rows) * (current - RESPONSE_CHAR_BUDGET) / current) + 1)
        del rows[max(1, len(rows) - drop):]
        response.update(returned=len(rows), truncated=True)
        if not any("opportunities returned" in n for n in response["notes"]):
            response["notes"].append("RESPONSE_TRIMMED_TO_SIZE_BUDGET: fewer opportunities returned than requested to stay below the MCP result limit; narrow the filter (source, status, news_status, symbol).")
        response["rendered_de"] = render_view_de(response)
    return response


# ---------------------------------------------------------------- compact response (what an MCP client normally receives)
def _fresh(state: Mapping[str, Any]) -> dict[str, Any]:
    if state["status"] != "AVAILABLE":
        return {"status": "UNAVAILABLE", "reason": state["reason"]}
    return {"as_of": state["evaluation_as_of"], "generated_at": str(state["generated_at"])[:16], "stale": state["stale"]}


def _compact_news(news: Mapping[str, Any]) -> dict[str, Any]:
    """News status, counts (all / direct) and the single most important event of one symbol."""
    c, d = news["counts"], news["direct_counts"]
    out: dict[str, Any] = {"status": news["status"], "counts": f"H{c['HIGH']}/M{c['MEDIUM']}/L{c['LOW']}", "direct": f"H{d['HIGH']}/M{d['MEDIUM']}"}
    if news["events"]:
        e = news["events"][0]
        event = {"date": str(e["published_at"])[:10], "category": e["category"], "importance": e["importance"], "link": e["link"], "headline": str(e["headline"])[:90]}
        if e["impact"] != "UNKNOWN":
            event["impact"] = e["impact"]
        if e["reason_codes"]:
            event["reasons"] = list(e["reason_codes"])[:2]
        out["event"] = event
    return out


def _compact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One opportunity without empty fields and without the news list."""
    out: dict[str, Any] = {"symbol": row["symbol"], "name": str(row["name"])[:28], "source": row["source"]}
    if row["held"]:
        out["held"] = True
    for key in ("entry_status", "entry_rank", "entry_score", "momentum_score", "confidence", "current_price_eur", "discovery_rank", "discovery_score"):
        if row.get(key) is not None:
            out[key] = row[key]
    if row.get("discovery_status") not in (None, "DISCOVERY_READY"):
        out["discovery_status"] = row["discovery_status"]
    if row.get("promotion_status") not in (None, "PROMOTE"):
        out["promotion_status"] = row["promotion_status"]
    if row["plan_status"] != "NOT_APPLICABLE":
        out["plan"] = row["plan_status"]
        if row.get("planned_capital_eur") is not None:
            out["planned_capital_eur"] = row["planned_capital_eur"]
            out["planned_quantity"] = row["planned_quantity"]
        if row.get("plan_reason") and row["plan_reason"] != row.get("entry_status"):
            out["plan_reason"] = row["plan_reason"]
    if row.get("proposed_capital_eur") is not None:
        out["proposed_capital_eur"] = row["proposed_capital_eur"]
        out["proposed_quantity"] = row["proposed_quantity"]
    if row["capital_fit"] is not None:
        out["capital_fit"] = fit_code(row["capital_fit"])
    out["quality"] = f"tech={row['technical_quality'] or 'n/a'} fund={row['fundamental_quality'] or 'n/a'} event={row['event_risk_quality'] or 'n/a'}"
    reasons = [c for c in row["reason_codes"] if c not in (row.get("entry_status"), row.get("plan_reason"))][:4]
    if reasons:
        out["reasons"] = reasons
    out["news"] = _compact_news(row["market_intelligence_summary"])
    return out


def _compact_response(view: Mapping[str, Any], matching: Sequence[Mapping[str, Any]], applied: int, limit: Any, source: str, status: str,
                      news: Optional[str], symbols: Sequence[str]) -> dict[str, Any]:
    """The compact answer: only the requested rows, a minimal capital frame, short freshness - no side blocks, no rendered text."""
    meta, context = view["metadata"], view["portfolio_context"]
    cap, after, plan = context.get("capital") or {}, context.get("after_actions") or {}, context.get("after_plan") or {}
    rows = [_compact_row(r) for r in matching[:applied]]
    response: dict[str, Any] = {
        "ok": True, "operation": "get_opportunity_view", "status": meta["status"], "read_only": True, "orders_created": False, "signals_changed": False,
        "evaluation_as_of": meta["evaluation_as_of"],
        "freshness": {"discovery": _fresh(meta["sources"]["discovery"]), "market_intelligence": _fresh(meta["sources"]["market_intelligence"])},
    }
    if cap:
        response["capital"] = {
            "cash_eur": cap.get("cash_eur"), "cash_after_actions_eur": after.get("cash_after_actions_eur"), "net_proceeds_conservative_eur": after.get("expected_proceeds_net_conservative_eur"),
            "planned_entries_total_eur": plan.get("planned_entries_total_eur"), "remaining_buying_capacity_eur": plan.get("remaining_buying_capacity_eur"),
            "swing_pct": cap.get("swing_pct"), "swing_target_pct": cap.get("swing_target_pct"),
        }
    active = {k: v for k, v in (("source", source if source != "ALL" else None), ("status", status if status != "ALL" else None), ("news_status", news), ("symbol", list(symbols) or None)) if v}
    if active:
        response["filter"] = active
    response.update(matching=len(matching), returned=len(rows), truncated=len(matching) > len(rows), opportunities=rows)
    if symbols:
        held = [p for p in context["positions"] if _symbol_wanted(p, symbols)]
        if held:
            response["positions"] = [{"symbol": p["symbol"], "action": p["action"], "priority": p["priority"], "news": _compact_news(p["market_intelligence_summary"])} for p in held]
    codes = [w["code"] for w in view["warnings"]]
    if codes:
        response["warnings"] = codes
    if response["truncated"]:
        response["hint"] = f"{len(matching) - len(rows)} more rows match; raise limit (max {MAX_LIMIT_COMPACT}) or narrow source/status/news_status/symbol"
    while len(rows) > 1 and len(json.dumps(response, ensure_ascii=False)) > RESPONSE_CHAR_BUDGET:
        current = len(json.dumps(response, ensure_ascii=False))
        del rows[max(1, len(rows) - max(1, int(len(rows) * (current - RESPONSE_CHAR_BUDGET) / current) + 1)):]
        response.update(returned=len(rows), truncated=True)
        response["hint"] = "RESPONSE_TRIMMED_TO_SIZE_BUDGET: fewer rows returned to stay below the MCP result limit; narrow source/status/news_status/symbol"
    return response


def _drop_methodology(response: dict[str, Any]) -> bool:
    return response.pop("methodology", None) is not None


def _drop_position_events(response: dict[str, Any]) -> bool:
    changed = False
    for position in response["portfolio_context"]["positions"]:
        changed |= position.pop("events", None) is not None
    return changed


def _compact_macro(response: dict[str, Any]) -> bool:
    macro = response["market_context"]["macro_context"]
    if macro.get("status") != "AVAILABLE":
        return False
    response["market_context"]["macro_context"] = _slim_macro(macro, False)
    return True


def _drop_discovery_shown(response: dict[str, Any]) -> bool:
    shown = response["discovery_candidates"]["shown"]
    del shown[3:]
    return True


_EXTRA_REDUCTIONS = (("methodology", _drop_methodology), ("position events", _drop_position_events), ("macro context", _compact_macro), ("discovery list", _drop_discovery_shown))


def _slim_event(event: Mapping[str, Any], detail: bool) -> dict[str, Any]:
    if detail:
        return dict(event)
    return {**{k: event[k] for k in ("published_at", "category", "importance", "impact", "headline", "source", "link")}, "reason_codes": list(event["reason_codes"])[:3]}


def _slim_position(position: Mapping[str, Any], detail: bool, events_cap: int = 3) -> dict[str, Any]:
    news = position["market_intelligence_summary"]
    out = {"symbol": position["symbol"], "name": position["name"], "strategy": position["strategy"], "action": position["action"], "priority": position["priority"],
           "news_status": news["status"], "news_counts": news["counts"], "news_direct_counts": news["direct_counts"]}
    if detail:
        out["events"] = [_slim_event(e, True) for e in news["events"][:events_cap]]
    return out


def _slim_macro(macro: Mapping[str, Any], detail: bool) -> dict[str, Any]:
    if macro["status"] != "AVAILABLE":
        return dict(macro)
    return {"status": "AVAILABLE", "categories": {name: ({**cat} if detail else {"high": cat["high"], "medium": cat["medium"], "official_source_events": cat["official_source_events"], "top_events": cat["top_events"][:1]}) for name, cat in macro["categories"].items()}}


def _eur(value: Any) -> str:
    number = _num(value)
    return "-" if number is None else f"{number:,.0f}".replace(",", ".") + " EUR"


def render_view_de(response: Mapping[str, Any]) -> str:
    """Deterministic German rendering of an already computed response (formatting only)."""
    src = response["sources"]
    ctx = response["portfolio_context"]
    news_of = {r["symbol"]: r["market_intelligence_summary"]["status"] for r in response["opportunities"]}
    news_of.update({p["symbol"]: p["news_status"] for p in ctx["positions"]})

    def state(label: str, item: Mapping[str, Any]) -> str:
        if item["status"] == "UNAVAILABLE":
            return f"{label}: nicht verfügbar ({item['reason']})"
        return f"{label}: Stand {item['evaluation_as_of']}, erzeugt {item['generated_at']}, {'STALE' if item['stale'] else 'aktuell'}"

    lines = [
        f"## Opportunity View (Status {response['status']}, Engine-Stand {response['evaluation_as_of']})",
        state("Discovery", src["discovery"]), state("Market Intelligence", src["market_intelligence"]),
        "News sind nur Kontext: sie ändern keinen Score, kein Ranking, keine Positionsgröße und keine Entscheidung. Es gibt keinen kombinierten Score.",
        "",
        "### Aktuelle geplante Entries",
        "| Symbol | Entry Status | Rank | Kapital | News Context |", "|---|---|---|---|---|",
    ]
    by_symbol = {r["symbol"]: r for r in response["opportunities"]}
    for plan in ctx["planned_entries"]:
        row = by_symbol.get(plan["symbol"])
        lines.append(f"| {plan['symbol']} | {(row or {}).get('entry_status') or 'ENTRY_READY'} | {plan['rank']} | {_eur(plan['capital_eur'])} | {news_of.get(plan['symbol'], '-')} |")
    if not ctx["planned_entries"]:
        lines.append("| - | - | - | - | keine geplanten Entries |")
    others = [r for r in response["opportunities"] if r["source"] in ("WATCHLIST", "BOTH") and r["entry_status"] is not None and r["plan_status"] != "PLANNED"]
    if others:
        lines += ["", "### Weitere Watchlist-Kandidaten (nicht im Plan)", "| Symbol | Entry Status | Rank | Score | Plan | News Context |", "|---|---|---|---|---|---|"]
        for r in others:
            lines.append(f"| {r['symbol']} | {r['entry_status']} | {r['entry_rank'] if r['entry_rank'] is not None else '-'} | {r['entry_score'] if r['entry_score'] is not None else '-'} | {r['plan_status']}{(' / ' + r['plan_reason']) if r['plan_reason'] else ''} | {r['market_intelligence_summary']['status']} |")
    discovery_rows = response["discovery_candidates"]["shown"]
    lines += ["", "### Neue Discovery-Chancen", "| Symbol | Discovery Rank | Score | News Context | Status | Kapital |", "|---|---|---|---|---|---|"]
    for r in discovery_rows:
        code = r["capital_fit"] if isinstance(r.get("capital_fit"), str) or r.get("capital_fit") is None else fit_code(r["capital_fit"])
        fits = {"FITS_AFTER_PLAN": "passt ins freie Kapital", "COMMITTED_BY_PLANNED_ENTRIES": "nur aus Verkaufserlösen, durch geplante Entries bereits verplant", "DOES_NOT_FIT": "passt nicht", None: "-", "UNKNOWN": "-"}[code]
        lines.append(f"| {r['symbol']} | {r['discovery_rank']} | {r['discovery_score']} | {r['news_status']} | {r['discovery_status']} | {fits} |")
    if response["discovery_candidates"]["shown_total"] > len(discovery_rows):
        lines.append(f"(Liste gekürzt: {len(discovery_rows)} von {response['discovery_candidates']['shown_total']} Discovery-Kandidaten)")
    if not discovery_rows:
        lines.append("| - | - | - | - | keine Discovery-Kandidaten | - |")
    lines += ["", "### High-Attention", "| Symbol | Event | Kategorie | Importance |", "|---|---|---|---|"]
    for e in response["market_context"]["high_attention"]:
        lines.append(f"| {e['symbol']} ({'Depot' if e['scope'] == 'POSITION' else 'Kandidat'}) | {e['headline'][:90]} | {e['category']} | {e['importance']} |")
    if not response["market_context"]["high_attention"]:
        lines.append("| - | keine direkt symbolbezogenen HIGH-Events | - | - |")
    cap, after, plan_state = ctx["capital"], ctx["after_actions"], ctx["after_plan"]
    lines += ["", "### Kapital",
              f"- Cash aktuell: {_eur(cap.get('cash_eur'))} (frei über Reserve: {_eur(cap.get('deployable_cash_eur'))}); Swing-Anteil {cap.get('swing_pct')} % (Ziel {cap.get('swing_target_pct')})",
              f"- verfügbar nach geplanten Verkäufen: {_eur(after.get('cash_after_actions_eur'))} (frei über Reserve: {_eur(after.get('deployable_cash_eur'))}); konservativer Nettoerlös {_eur(after.get('expected_proceeds_net_conservative_eur'))}",
              f"- geplante Entries: {plan_state.get('planned_entries_count')} ({_eur(plan_state.get('planned_entries_total_eur'))}); verbleibende Kaufkapazität: {_eur(plan_state.get('remaining_buying_capacity_eur'))}; verbleibende Swing-Kapazität: {_eur(plan_state.get('remaining_swing_capacity_eur'))}",
              "", "Keine zusätzlichen Kaufvorschläge: die Ansicht plant keine Orders und erzeugt keine Signale."]
    if response["truncated"]:
        lines.append(f"(Opportunities gekürzt: {response['returned']} von {response['matching']}; mit source, status, news_status oder symbol eingrenzen)")
    return "\n".join(lines)


def unavailable(reasons: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": True, "operation": "get_opportunity_view", "status": "UNAVAILABLE", "reasons": dict(reasons),
            "hint": "Neither the orchestrator result nor a discovery or market-intelligence report is available; the reader never starts a scan or collection."}
