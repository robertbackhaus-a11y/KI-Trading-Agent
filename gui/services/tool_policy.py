"""Central classification of the MCP tools: READ may be called by the GUI and offered to the chat, everything else is an ACTION.

The MCP tools carry no read-only annotations, so the GUI keeps its own table.  A tool that is not listed here is treated as an
ACTION and denied (deny by default); a test requires every tool of the server to be classified.

Three narrow watchlist ACTIONs and the security onboarding (``GUI_ACTIONS``) may be triggered by an explicit button of the Watchlist page, and only through
``McpService.call_action``; they are never offered to the chat and ``call`` / ``call_sync`` still refuse them.  ``sql_execute``,
``approve_swing_promotion`` and every unknown tool stay denied.
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
    "get_watchlist": READ,
    "rank_watchlist": READ,
    "run_trading_orchestrator": READ,
    "suggest_strategy_assignments": READ,
    # actions: write to the database (``sql_execute`` accepts arbitrary SQL including DROP)
    "approve_swing_promotion": ACTION,
    "sql_execute": ACTION,
    # actions: write only the watchlist table (no sql, no security creation); triggered by explicit buttons, never by the chat
    "add_watchlist_candidate": ACTION,
    "remove_watchlist_candidate": ACTION,
    "set_watchlist_priority": ACTION,
    # action: creates one new security in the Security Master (explicit dialog of the Watchlist page, dry run first); never offered to the chat
    "onboard_security": ACTION,
    # action: sets or removes the strategy of ONE security that is not held (strategy_assignment only); explicit "Übernehmen" of the Watchlist page, never the chat
    "set_security_strategy": ACTION,
    # action: sets or removes the LOCAL sector entry of ONE security (sector_map_local.json only, never the database); explicit apply of the Watchlist page, never the chat
    "set_security_sector": ACTION,
}

# the ACTIONs the GUI itself may call (explicit button + confirmation where destructive); everything else is denied
GUI_ACTIONS: frozenset[str] = frozenset({"add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "onboard_security", "set_security_strategy", "set_security_sector"})


class ToolDenied(Exception):
    """A tool outside the READ class was requested."""


def classify(name: str) -> str:
    return POLICY.get(name, ACTION)


def is_read(name: str) -> bool:
    return classify(name) == READ


def is_gui_action(name: str) -> bool:
    """A watchlist ACTION that a button of the GUI may trigger (never the chat)."""
    return name in GUI_ACTIONS and classify(name) == ACTION


def is_known(name: str) -> bool:
    return name in POLICY


def unclassified(names) -> list[str]:
    return sorted(n for n in names if n not in POLICY)


def require_read(name: str) -> None:
    if not is_read(name):
        raise ToolDenied(f"{name}: {'Aktion' if is_known(name) else 'unbekanntes Tool'} – in dieser GUI-Version nicht erlaubt")
