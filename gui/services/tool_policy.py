"""Central classification of the MCP tools: READ may be called by the GUI, everything else is an ACTION.

The MCP tools carry no read-only annotations, so the GUI keeps its own table.  A tool that is not listed here is treated as an
ACTION and denied (deny by default); a test requires every tool of the server to be classified.  Phase 1 calls READ tools only.
"""

from __future__ import annotations

READ = "READ"
ACTION = "ACTION"

POLICY: dict[str, str] = {
    # reads
    "database_schema": READ,
    "database_status": READ,
    "database_tables": READ,
    "table_info": READ,
    "evaluate_swing_candidate": READ,
    "evaluate_swing_candidates": READ,
    "evaluate_watchlist_candidates": READ,
    "get_candidate_discovery": READ,
    "get_market_intelligence": READ,
    "get_opportunity_view": READ,
    "rank_watchlist": READ,
    "run_trading_orchestrator": READ,
    "suggest_strategy_assignments": READ,
    # actions: write to the database (``sql_execute`` accepts arbitrary SQL including DROP)
    "approve_swing_promotion": ACTION,
    "sql_execute": ACTION,
}


class ToolDenied(Exception):
    """A tool outside the READ class was requested."""


def classify(name: str) -> str:
    return POLICY.get(name, ACTION)


def is_read(name: str) -> bool:
    return classify(name) == READ


def is_known(name: str) -> bool:
    return name in POLICY


def unclassified(names) -> list[str]:
    return sorted(n for n in names if n not in POLICY)


def require_read(name: str) -> None:
    if not is_read(name):
        raise ToolDenied(f"{name}: {'Aktion' if is_known(name) else 'unbekanntes Tool'} – in dieser GUI-Version nicht erlaubt")
