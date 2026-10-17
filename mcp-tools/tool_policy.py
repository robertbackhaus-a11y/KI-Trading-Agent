# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The one classification of the MCP tools, enforced by the server and used by the desktop GUI.

Every public method of ``trading_sqlite.Tools`` is either READ (reads the database or a report, writes nothing) or an ACTION (writes).  A name that is not in
``POLICY`` is unknown and is never offered: a new tool has to be classified here before any client can see it.

The server registers only the tools of one profile (environment variable ``TRADING_TOOL_PROFILE``):

* ``read`` (default): the READ tools.  Every analysis and chat client runs with it; it cannot write anything.
* ``gui``: READ plus ``GUI_ACTIONS``, the narrow writes behind explicit buttons of the desktop GUI (watchlist, onboarding, strategy, sector, transaction import).
  The GUI starts its own server process with this profile; its chat is still offered the READ tools only.
* ``admin``: every classified tool, including ``sql_execute`` (arbitrary SQL) and ``approve_swing_promotion``.  Only for a deliberate manual session.

``TRADING_EXCLUDE`` still removes single tools from a profile; it can narrow a profile, never widen it.  This module has no dependencies, so the server
(``mcp`` venv) and the GUI (its own venv) load the same file.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

READ = "READ"
ACTION = "ACTION"

POLICY: dict[str, str] = {
    # reads (read-only database connection or a report file; no write, no web access)
    "database_schema": READ,
    "database_status": READ,
    "database_tables": READ,
    "table_info": READ,
    "evaluate_swing_candidate": READ,
    "evaluate_swing_candidates": READ,
    "evaluate_watchlist_candidates": READ,
    "get_candidate_discovery": READ,
    "get_data_freshness": READ,
    "get_effective_exposure": READ,
    "get_market_intelligence": READ,
    "get_opportunity_view": READ,
    "get_sector_overview": READ,
    "get_watchlist": READ,
    "rank_watchlist": READ,
    "run_trading_orchestrator": READ,
    "suggest_strategy_assignments": READ,
    # actions: write to the database (``sql_execute`` accepts arbitrary SQL including DROP); profile admin only
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
    # actions: the transaction import. check_transaction_import reads a local CSV and writes nothing (a dry run), import_transactions writes in ONE transaction after a check
    # (token); both are explicit buttons of the Import page, never the chat (a file path is not something an LLM may be given)
    "check_transaction_import": ACTION,
    "import_transactions": ACTION,
}

# the ACTIONs the GUI itself may call (explicit button + confirmation where destructive); profile gui
GUI_ACTIONS: frozenset[str] = frozenset({"add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "onboard_security", "set_security_strategy", "set_security_sector",
                                         "check_transaction_import", "import_transactions"})

PROFILE_ENV = "TRADING_TOOL_PROFILE"
PROFILE_READ, PROFILE_GUI, PROFILE_ADMIN = "read", "gui", "admin"
PROFILES: tuple[str, ...] = (PROFILE_READ, PROFILE_GUI, PROFILE_ADMIN)
DEFAULT_PROFILE = PROFILE_READ


def classify(name: str) -> str:
    return POLICY.get(name, ACTION)


def is_read(name: str) -> bool:
    return classify(name) == READ


def is_gui_action(name: str) -> bool:
    """An ACTION that a button of the GUI may trigger (never the chat)."""
    return name in GUI_ACTIONS and classify(name) == ACTION


def is_known(name: str) -> bool:
    return name in POLICY


def unclassified(names: Iterable[str]) -> list[str]:
    return sorted(n for n in names if n not in POLICY)


def allowed(name: str, profile: str) -> bool:
    """Whether the server may offer ``name`` in ``profile``; an unknown name never."""
    if profile not in PROFILES:
        raise ValueError(f"unknown tool profile {profile!r} (allowed: {', '.join(PROFILES)})")
    if not is_known(name):
        return False
    if profile == PROFILE_ADMIN:
        return True
    return is_read(name) or (profile == PROFILE_GUI and is_gui_action(name))


def profile_from_env(environ: Mapping[str, str]) -> str:
    """The profile named by ``TRADING_TOOL_PROFILE`` (empty or unset: ``read``); any other value is an error, never a silent fallback to more rights."""
    value: Optional[str] = environ.get(PROFILE_ENV)
    text = (value or "").strip().lower()
    if not text:
        return DEFAULT_PROFILE
    if text not in PROFILES:
        raise ValueError(f"{PROFILE_ENV}={value!r} is not a tool profile (allowed: {', '.join(PROFILES)})")
    return text
