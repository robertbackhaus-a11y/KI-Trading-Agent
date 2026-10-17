# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Read-only watchlist view for the ``get_watchlist`` tool: every watchlist entry with its existing ranking, decision and news context.

``WATCHLIST ROWS + analytics ranking + candidate decision + planner entry status + news context -> one row per entry``.

This module only *joins and presents* results that already exist (the same ones ``rank_watchlist``, ``evaluate_watchlist_candidates``, the
orchestrator and ``get_opportunity_view`` deliver); it computes no score, no rank (beyond the position in the existing score ordering), no
recommendation and writes nothing.  Missing sources degrade to ``None`` / ``NEWS_UNAVAILABLE`` and a warning, never to an exception.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

import opportunity_view as ov
import sector_assignment
from security_aliases import AliasIndex, split_symbol, symbol_key

DEFAULT_LIMIT = 150
MAX_LIMIT = 200
RESPONSE_CHAR_BUDGET = 34_000  # the MCP server refuses results above 40,000 characters (RESULT_TOO_LARGE)
STRATEGY_NONE = "NONE"  # filter value: entries without an active strategy assignment
NO_MARKET_DATA = "MARKET_DATA_MISSING"
NOTE_FIELDS = ("entry_reason", "thesis", "target_entry", "notes")  # free-text / target fields of a watchlist row
_TABLE_KEYS = ("security_id", "symbol", "name", "isin", "strategy", "held", "priority", "status", "rank", "score", "quality", "as_of",
               "assessment", "entry_status", "promotion", "news_status", "annotated", "added_at", "sector", "sector_source", "sector_public")  # the desktop GUI's table rows (``table=true``)
_COMPACT_KEYS = ("symbol", "name", "strategy", "held", "priority", "status", "rank", "score", "quality", "assessment", "entry_status", "news_status")  # chat / LLM rows


def load_rows(conn: Any, today: str) -> list[dict[str, Any]]:
    """Watchlist rows joined to the Security Master, plus the held flag and the strategy active on ``today`` (read-only SELECTs)."""
    rows = conn.execute(
        """
        SELECT w.security_id, w.status, w.priority, w.added_at, w.updated_at,
               s.symbol, s.name, s.isin, s.wkn, s.exchange, s.currency,
               EXISTS (SELECT 1 FROM positions p WHERE p.security_id = w.security_id AND p.shares > 0),
               w.entry_reason, w.thesis, w.target_entry, w.notes
        FROM watchlist w LEFT JOIN security s ON s.id = w.security_id
        ORDER BY w.security_id
        """
    ).fetchall()
    strategies: dict[int, list[str]] = {}
    for sid, kind in conn.execute(
        "SELECT security_id, strategy_type FROM strategy_assignment WHERE effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)", (today, today)
    ).fetchall():
        strategies.setdefault(int(sid), []).append(str(kind))
    out = []
    for r in rows:
        kinds = strategies.get(int(r[0]), [])
        out.append({
            "security_id": int(r[0]), "status": r[1], "priority": r[2], "added_at": r[3], "updated_at": r[4], "symbol": r[5], "name": r[6] or "", "isin": r[7],
            "wkn": r[8], "exchange": r[9], "currency": r[10], "held": bool(r[11]),
            "entry_reason": r[12], "thesis": r[13], "target_entry": r[14], "notes": r[15],
            "strategy": kinds[0] if len(kinds) == 1 else None,  # the engine's rule: exactly one active assignment, otherwise unknown
        })
    return out


def _by_id(items: Optional[Sequence[Mapping[str, Any]]]) -> dict[int, Mapping[str, Any]]:
    return {int(i["security_id"]): i for i in items or [] if isinstance(i, Mapping) and i.get("security_id") is not None}


def build_watchlist(
    *,
    rows: Sequence[Mapping[str, Any]],
    ranking: Optional[Sequence[Mapping[str, Any]]],
    decisions: Optional[Sequence[Mapping[str, Any]]],
    engine: Optional[Mapping[str, Any]],
    mi: Optional[Mapping[str, Any]],
    symbol_map: Optional[Mapping[int, str]] = None,
    alias_index: Optional[AliasIndex] = None,
    sector_layers: Optional[sector_assignment.Layers] = None,
) -> dict[str, Any]:
    """Pure join; the inputs are never modified.  ``sector_layers`` (the two sector-map layers) adds the sector of each entry and where it comes from."""
    warnings: list[dict[str, str]] = []
    if ranking is None:
        warnings.append({"code": "RANKING_UNAVAILABLE", "message": "the analytics ranking could not be computed"})
    if decisions is None:
        warnings.append({"code": "DECISIONS_UNAVAILABLE", "message": "the candidate decisions could not be computed"})
    if engine is None:
        warnings.append({"code": "ENGINE_UNAVAILABLE", "message": "the orchestrator result is not available (no entry status)"})
    if mi is None:
        warnings.append({"code": "MARKET_INTELLIGENCE_REPORT_UNAVAILABLE", "message": "no valid market-intelligence report (news status unavailable)"})
    rank_of: dict[int, tuple[int, Mapping[str, Any]]] = {}
    scored = 0
    for item in ranking or []:
        sid = int(item["security_id"])
        if item.get("score") is not None:
            scored += 1
            rank_of[sid] = (scored, item)
        else:
            rank_of[sid] = (0, item)
    decision_of = _by_id(decisions)
    plan = (engine or {}).get("portfolio_action_plan") or {}
    entry_of = _by_id(plan.get("entry_candidates"))
    promotion_of = _by_id((engine or {}).get("promotion_results"))
    event_index = ov.index_events(mi) if mi is not None else None
    id_index = ov.index_events_by_id(mi) if mi is not None else None
    result_rows: list[dict[str, Any]] = []
    for row in rows:
        sid = int(row["security_id"])
        rank, analytics = rank_of.get(sid, (0, None))
        decision, entry, promotion = decision_of.get(sid), entry_of.get(sid), promotion_of.get(sid)
        yahoo = (symbol_map or {}).get(sid) or row.get("symbol")
        news = ov.news_context(yahoo, str(row.get("name") or ""), event_index, detail=True, security_id=sid, id_index=id_index)
        points = (analytics or {}).get("data_points")
        quality = None if analytics is None else (NO_MARKET_DATA if not points else analytics.get("quality"))
        result_rows.append({
            "security_id": sid, "symbol": row.get("symbol"), "name": row.get("name") or "", "isin": row.get("isin"), "strategy": row.get("strategy"), "held": bool(row.get("held")),
            "priority": row.get("priority"), "status": row.get("status"), "rank": rank or None,
            "score": None if analytics is None or analytics.get("score") is None else round(float(analytics["score"]), 2),
            "quality": quality, "as_of": (analytics or {}).get("as_of"),
            "assessment": (decision or {}).get("decision_status"), "entry_status": (entry or {}).get("entry_status"),
            "promotion": (promotion or {}).get("recommendation"), "news_status": news["status"], "added_at": row.get("added_at"),
            **_sector_fields(sector_layers, (alias_index.key_for(sid) if alias_index is not None else None) or row.get("symbol")),
            "annotated": True if any(row.get(k) not in (None, "") for k in NOTE_FIELDS) else None,  # notes would be lost by a removal
            # detail only
            "wkn": row.get("wkn"), "yahoo_symbol": yahoo, "exchange": row.get("exchange"), "currency": row.get("currency"),
            "updated_at": row.get("updated_at"), **{k: row.get(k) for k in NOTE_FIELDS}, "data_points": points, "current_price": (analytics or {}).get("current_price"),
            "assessment_reasons": list((decision or {}).get("decision_reasons") or []), "portfolio_constraints": list((decision or {}).get("portfolio_constraints") or []),
            "entry_rank": (entry or {}).get("rank"), "entry_score": (entry or {}).get("entry_score"), "news": {"counts": news["counts"], "events": news["events"][:2]},
            "aliases": alias_index.aliases_of(sid) if alias_index is not None else [],
        })
    result_rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, -(r["priority"] if isinstance(r["priority"], (int, float)) else -1), str(r["symbol"] or ""), r["security_id"]))
    return {
        "status": "AVAILABLE" if not warnings else "PARTIAL",
        "counts": {"total": len(result_rows),
                   "by_status": _count(r["status"] for r in result_rows),
                   "by_strategy": _count(r["strategy"] or STRATEGY_NONE for r in result_rows),
                   "held": sum(1 for r in result_rows if r["held"])},
        "order": "rank (scored entries first, by the analytics score), then priority (higher first), symbol",
        "rows": result_rows,
        "sector_groups": list(sector_layers.groups) if sector_layers is not None else [],  # the existing groups a sector may be chosen from
        "warnings": warnings,
    }


def _sector_fields(layers: Optional[sector_assignment.Layers], key: Optional[str]) -> dict[str, Any]:
    """The effective sector of a listing key, where it comes from (public / local / none) and the public value (shown when a local entry overrides it)."""
    if layers is None:
        return {"sector": None, "sector_source": None, "sector_public": None}
    found = sector_assignment.resolve(layers, key)
    return {"sector": found["sector"], "sector_source": found["source"], "sector_public": found["public"]}


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))


def _wanted(row: Mapping[str, Any], wanted: Sequence[str]) -> bool:
    names = {symbol_key(row.get("symbol")), split_symbol(row.get("symbol"))[0], symbol_key(row.get("yahoo_symbol")), split_symbol(row.get("yahoo_symbol"))[0],
             str(row.get("isin") or "").upper(), str(row.get("wkn") or "").upper(), *[str(a).upper() for a in row.get("aliases") or []]}
    return any(w in names for w in wanted)


def _table(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in _TABLE_KEYS if row.get(k) is not None}  # absent = no value; keeps the answer small


_UNIFORM_KEYS = ("status", "news_status")  # stated once in the summary when every matching entry has the same value


def _uniform(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out = {}
    for key in _UNIFORM_KEYS:
        values = {r.get(key) for r in rows}
        if len(values) == 1 and None not in values and len(rows) > 1:
            out[key] = next(iter(values))
    return out


def _compact(row: Mapping[str, Any], same: Mapping[str, Any] = {}) -> dict[str, Any]:
    out = {k: row[k] for k in _COMPACT_KEYS if row.get(k) is not None and k not in same}
    if not out.get("held"):
        out.pop("held", None)  # ``held`` appears only as true; the summary carries the number of held entries
    return out


def _summary(total: int, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Small aggregation of the matching entries (before the limit), so a trimmed row list still describes the whole selection."""
    dates = [str(r["as_of"]) for r in rows if r.get("as_of")]
    return {"total": total, "held": sum(1 for r in rows if r.get("held")),
            "strategies": _count(r["strategy"] or STRATEGY_NONE for r in rows), "statuses": _count(r["status"] for r in rows),
            "data_quality": _count(r["quality"] or "NONE" for r in rows), "news": _count(r["news_status"] or "NONE" for r in rows),
            "as_of": max(dates) if dates else None, "same_for_all": _uniform(rows)}  # same_for_all: fields left out of every row because they never differ


def _text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def query_watchlist(view: Mapping[str, Any], *, status: Optional[str] = None, strategy: Optional[str] = None, symbol: Optional[str] = None, limit: Any = DEFAULT_LIMIT,
                    detail: bool = False, table: bool = False) -> dict[str, Any]:
    """Filter and shape the view.  Filters are exact (status, strategy ``swing`` / ``long_term`` / ``NONE``) or a comma-separated symbol / ISIN / WKN list.

    Three shapes: the default is the small chat / LLM answer (a summary plus a few fields per entry); ``table`` is the row shape the desktop GUI needs
    (ids, ISIN, dates, the notes flag); ``detail`` is the diagnosis view (at most 10 complete rows) and wins over ``table``."""
    try:
        cap = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        cap = DEFAULT_LIMIT
    rows = list(view["rows"])
    if status:
        rows = [r for r in rows if str(r["status"]).upper() == str(status).strip().upper()]
    if strategy:
        wanted = str(strategy).strip().lower()
        rows = [r for r in rows if (r["strategy"] or STRATEGY_NONE.lower()) == wanted or (wanted in ("none", "unassigned") and not r["strategy"])]
    if symbol:
        wanted_symbols = [symbol_key(s) for s in str(symbol).split(",") if s.strip()]
        rows = [r for r in rows if _wanted(r, wanted_symbols)]
    matching = len(rows)
    shown = rows[:cap]
    chat = not detail and not table
    same = _uniform(rows) if chat else {}
    response = {
        "ok": True, "operation": "get_watchlist", "read_only": True, "status": view["status"],
        ("summary" if chat else "counts"): _summary(len(view["rows"]), rows) if chat else view["counts"], "order": view["order"],
        "filter": {"status": status, "strategy": strategy, "symbol": symbol, "limit": cap, "detail": bool(detail), "table": bool(table)},
        "matching": matching, "returned": len(shown), "truncated": matching > len(shown),
        "rows": [dict(r) if detail else (_compact(r, same) if chat else _table(r)) for r in shown], "warnings": list(view["warnings"]),
    }
    if not chat:
        response["sector_groups"] = list(view.get("sector_groups") or [])
    while not detail and len(_text(response)) > RESPONSE_CHAR_BUDGET and response["rows"]:  # the oldest rank is kept; the tail is dropped, never half a row
        response["rows"].pop()
        response["returned"], response["truncated"] = len(response["rows"]), True
    if detail and len(response["rows"]) > 10:
        response["rows"] = response["rows"][:10]  # detail is a diagnosis view: a few complete rows
        response["returned"], response["truncated"] = 10, matching > 10
    return response
