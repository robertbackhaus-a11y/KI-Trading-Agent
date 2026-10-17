# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""A small stdio MCP server for the GUI tests (official SDK, same registration style as mcp-tools/server.py).

Tools: database_status, run_trading_orchestrator (returns the JSON file named by FAKE_ORCH_FILE), slow, crash, broken, sql_execute, and the five watchlist tools (the four and the security onboarding)
(FAKE_WATCHLIST_FILE holds the rows, FAKE_CALLS_FILE logs every call).
FAKE_PID_FILE receives the process id (to test shutdown and restart).  Synthetic data only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("fake-trading")
pid_file = os.environ.get("FAKE_PID_FILE")
if pid_file:
    Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")


def _text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


async def database_status() -> str:
    """Database status (fake)."""
    return _text({"ok": True, "database_path": os.environ.get("TRADING_DB_PATH"), "user": os.environ.get("TRADING_USER_ID"), "profile": os.environ.get("TRADING_TOOL_PROFILE")})


async def run_trading_orchestrator(detail: bool = False) -> str:
    """Orchestrator (fake): the fixture file as ``result``."""
    path = os.environ.get("FAKE_ORCH_FILE")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    return _text({"ok": True, "operation": "run_trading_orchestrator", "result": {}, "detail": detail})


async def slow(seconds: float = 1.0) -> str:
    """Sleeps."""
    await asyncio.sleep(seconds)
    return _text({"ok": True, "slept": seconds})


async def rank_watchlist(seconds: float = 0.0) -> str:
    """A READ tool name that can be made slow (the GUI only calls READ tools)."""
    await asyncio.sleep(seconds)
    return _text({"ok": True, "slept": seconds})


async def crash() -> str:
    """Kills the server process."""
    os._exit(3)


async def broken() -> str:
    """Returns text that is not JSON."""
    return "this is not json {"


async def failing() -> str:
    """A tool that reports its own failure."""
    return _text({"ok": False, "error": "FAKE_FAILURE", "message": "synthetic failure"})


async def sql_execute(sql: str) -> str:
    """Action tool (fake): must never be called by the GUI."""
    return _text({"ok": True, "executed": sql})


# -- watchlist tools (fake): deterministic answers by identifier, every call is logged to FAKE_CALLS_FILE (one JSON object per line) -------------
_KNOWN = {  # identifier -> (security_id, symbol, name, isin)
    "NEWA": (101, "NEWA", "New Alpha Corp", "TESTISIN-N1"), "NEWB": (102, "NEWB", "New Beta Holdings Inc", "TESTISIN-N2"), "NEWC": (103, "NEWC", "New Charlie Systems Corp", None),
    "77": (77, "HAVE", "Have Already Corp", None),  # in the Security Master, not on the watchlist (the onboarding finds it)
}
_AMBIGUOUS = {"DUP": [(104, "DUP", "Duplicate One Corp"), (105, "DUP", "Duplicate Two Corp")]}
_state: dict = {}


def _calls(name: str, arguments: dict) -> None:
    path = os.environ.get("FAKE_CALLS_FILE")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_text({"tool": name, "arguments": arguments}) + "\n")


def _template() -> dict:
    if not _state:
        path = os.environ.get("FAKE_WATCHLIST_FILE")
        _state["env"] = json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).exists() else {"ok": True, "operation": "get_watchlist", "status": "AVAILABLE", "rows": [], "warnings": []}
    return _state["env"]


def _rows() -> list:
    return _template()["rows"]


async def get_watchlist(status: str | None = None, strategy: str | None = None, symbol: str | None = None, limit: int = 150, detail: bool = False, table: bool = False) -> str:
    """Watchlist (fake, READ)."""
    _calls("get_watchlist", {"status": status, "strategy": strategy, "symbol": symbol, "limit": limit, "detail": detail, "table": table})
    env = dict(_template())
    env["counts"] = {"total": len(_rows()), "held": sum(1 for r in _rows() if r.get("held"))}
    env["matching"] = env["returned"] = len(_rows())
    env.setdefault("sector_groups", SECTOR_GROUPS)
    env["truncated"] = False
    return _text(env)


def _result(operation: str, result: str, **fields) -> str:
    return _text({"ok": result != "ERROR", "operation": operation, "result": result, "changed": result in ("ADDED", "REMOVED") or fields.pop("changed", False), **fields})


async def add_watchlist_candidate(identifier: str, priority: int | None = None, dry_run: bool = False) -> str:
    """Action tool (fake): add to the watchlist."""
    _calls("add_watchlist_candidate", {"identifier": identifier, "priority": priority, "dry_run": dry_run})
    key = identifier.strip().upper()
    if key == "ERR":
        return _result("add_watchlist_candidate", "ERROR", error="OperationalError", message="database is locked")
    if key in _AMBIGUOUS:
        return _result("add_watchlist_candidate", "AMBIGUOUS_IDENTIFIER", candidates=[{"security_id": i, "symbol": s, "name": n} for i, s, n in _AMBIGUOUS[key]])
    if key not in _KNOWN:
        return _result("add_watchlist_candidate", "SECURITY_NOT_FOUND", message="Wertpapier ist noch nicht im Security Master.")
    sid, symbol, name, isin = _KNOWN[key]
    found = {"security_id": sid, "symbol": symbol, "name": name, "isin": isin}
    if any(r["security_id"] == sid for r in _rows()):
        return _result("add_watchlist_candidate", "ALREADY_EXISTS", **found)
    if dry_run:
        return _result("add_watchlist_candidate", "WOULD_ADD", **found)
    _rows().append({"security_id": sid, "symbol": symbol, "name": name, "isin": isin, "held": False, "priority": priority, "status": "WATCH", "quality": "MARKET_DATA_MISSING",
                    "news_status": "NEWS_UNAVAILABLE", "added_at": "2026-10-13 09:00:00"})
    return _result("add_watchlist_candidate", "ADDED", **found, priority=priority)


async def remove_watchlist_candidate(security_id: int) -> str:
    """Action tool (fake): remove from the watchlist."""
    _calls("remove_watchlist_candidate", {"security_id": security_id})
    row = next((r for r in _rows() if r["security_id"] == security_id), None)
    if row is None:
        return _result("remove_watchlist_candidate", "NOT_FOUND", security_id=security_id)
    _rows().remove(row)
    return _result("remove_watchlist_candidate", "REMOVED", security_id=security_id, symbol=row["symbol"], name=row["name"], still_held=bool(row.get("held")))


async def set_watchlist_priority(security_id: int, priority: int | None) -> str:
    """Action tool (fake): set the priority."""
    _calls("set_watchlist_priority", {"security_id": security_id, "priority": priority})
    if priority is not None and priority > 2**31:
        return _result("set_watchlist_priority", "INVALID_PRIORITY", security_id=security_id, message="priority is outside the range of the database column")
    row = next((r for r in _rows() if r["security_id"] == security_id), None)
    if row is None:
        return _result("set_watchlist_priority", "NOT_FOUND", security_id=security_id)
    previous, row["priority"] = row.get("priority"), priority
    return _result("set_watchlist_priority", "PRIORITY_SET", security_id=security_id, priority=priority, previous_priority=previous, changed=previous != priority)


SECTOR_GROUPS = ["ALPHA", "BETA", "GAMMA"]


async def set_security_sector(security_id: int, sector: str | None) -> str:
    """Action tool (fake): set or remove the local sector entry of one security."""
    _calls("set_security_sector", {"security_id": security_id, "sector": sector})
    row = next((r for r in _rows() if r["security_id"] == security_id), None)
    if row is None:
        return _result("set_security_sector", "NOT_FOUND", security_id=security_id)
    if sector is not None and sector not in SECTOR_GROUPS:
        return _result("set_security_sector", "INVALID_SECTOR", security_id=security_id, valid=SECTOR_GROUPS)
    if security_id == 3 and sector == "GAMMA":  # a failing write
        return _result("set_security_sector", "LOCAL_MAP_INVALID", security_id=security_id, detail="invalid JSON")
    public = row.get("sector_public")
    wanted_local = None if sector is None or sector == public else sector
    current_local = row.get("sector") if row.get("sector_source") == "local" else None
    if wanted_local == current_local:
        return _result("set_security_sector", "UNCHANGED", security_id=security_id, sector=row.get("sector"), source=row.get("sector_source", "none"))
    if wanted_local is None:
        row["sector"], row["sector_source"] = public, ("public" if public else "none")
    else:
        row["sector"], row["sector_source"] = wanted_local, "local"
    return _result("set_security_sector", "SECTOR_SET" if wanted_local else "SECTOR_OVERRIDE_REMOVED", changed=True, security_id=security_id, sector=row["sector"], source=row["sector_source"], public_sector=public)


async def set_security_strategy(security_id: int, strategy: str | None) -> str:
    """Action tool (fake): set or remove the strategy of one security that is not held."""
    _calls("set_security_strategy", {"security_id": security_id, "strategy": strategy})
    row = next((r for r in _rows() if r["security_id"] == security_id), None)
    if row is None:
        return _result("set_security_strategy", "NOT_FOUND", security_id=security_id)
    if strategy not in (None, "swing", "long_term"):
        return _result("set_security_strategy", "INVALID_STRATEGY", security_id=security_id)
    if row.get("held"):
        return _result("set_security_strategy", "BLOCKED_HELD_POSITION", security_id=security_id, message="held")
    if security_id == 2:  # the fake's security with an open campaign
        return _result("set_security_strategy", "BLOCKED_ACTIVE_CAMPAIGN", security_id=security_id, message="campaign")
    if security_id == 3 and strategy == "long_term":  # a failing write
        return _result("set_security_strategy", "ERROR", error="OperationalError", message="database is locked")
    previous = row.get("strategy")
    if previous == strategy:
        return _result("set_security_strategy", "UNCHANGED", security_id=security_id, strategy=strategy, previous_strategy=previous)
    if strategy is None:
        row.pop("strategy", None)
    else:
        row["strategy"] = strategy
    return _result("set_security_strategy", "STRATEGY_SET" if strategy else "STRATEGY_REMOVED", changed=True, security_id=security_id, strategy=strategy, previous_strategy=previous)


_CREATED: dict[str, int] = {}


async def onboard_security(symbol: str, name: str, asset_type: str, isin: str | None = None, wkn: str | None = None, exchange: str | None = None, currency: str | None = None,
                           country: str | None = None, provider_symbols: dict[str, str] | None = None, add_to_watchlist: bool = False, watchlist_priority: int | None = None,
                           dry_run: bool = True) -> str:
    """Action tool (fake): onboard a new security (check or create)."""
    _calls("onboard_security", {"symbol": symbol, "name": name, "asset_type": asset_type, "isin": isin, "wkn": wkn, "exchange": exchange, "currency": currency, "country": country,
                                 "provider_symbols": provider_symbols, "add_to_watchlist": add_to_watchlist, "watchlist_priority": watchlist_priority, "dry_run": dry_run})
    key = symbol.strip().upper()
    if key == "ERR":
        return _result("onboard_security", "ERROR", error="OperationalError", message="database is locked")
    if key == "BADISIN":
        return _result("onboard_security", "INVALID_INPUT", errors=[{"field": "isin", "reason": "BAD_ISIN_CHECKSUM"}, {"field": "currency", "reason": "BAD_CURRENCY"}])
    if key == "HAVE":
        return _result("onboard_security", "ALREADY_EXISTS", existing={"security_id": 77, "symbol": "HAVE", "name": "Have Already Corp", "isin": None, "active": True}, on_watchlist=any(r["security_id"] == 77 for r in _rows()),
                       conflicts=[{"code": "ALREADY_EXISTS", "field": "symbol", "value": "HAVE", "security_id": 77, "symbol": "HAVE", "name": "Have Already Corp"}], warnings=[])
    if key == "CLASH":
        return _result("onboard_security", "SYMBOL_CONFLICT", conflicts=[{"code": "SYMBOL_CONFLICT", "field": "symbol", "value": "CLASH", "match": "EXACT", "security_id": 5, "symbol": "CLASH", "name": "Other Clash Ltd"},
                                                                         {"code": "ISIN_CONFLICT", "field": "isin", "value": isin, "match": "EXACT", "security_id": 6, "symbol": "SIX", "name": "Six Corp"}], warnings=[])
    planned = {"security": {"symbol": key, "name": name, "asset_type": asset_type, "isin": isin, "wkn": wkn, "exchange": exchange, "currency": currency, "country": country, "active": True},
               "provider_symbols": [{"source": s, "symbol": v} for s, v in sorted((provider_symbols or {}).items())],
               "watchlist": {"status": "WATCH", "priority": watchlist_priority} if add_to_watchlist else None, "sector": None}
    if key in _CREATED:
        return _result("onboard_security", "ALREADY_EXISTS", existing={"security_id": _CREATED[key], "symbol": key, "name": name, "isin": isin, "active": True}, on_watchlist=add_to_watchlist,
                       conflicts=[{"code": "ALREADY_EXISTS", "field": "symbol", "value": key, "security_id": _CREATED[key], "symbol": key, "name": name}], warnings=[])
    if dry_run:
        return _result("onboard_security", "READY", conflicts=[], warnings=[], planned=planned)
    sid = 900 + len(_CREATED)
    _CREATED[key] = sid
    if add_to_watchlist:
        _rows().append({"security_id": sid, "symbol": key, "name": name, "isin": isin, "held": False, "priority": watchlist_priority, "status": "WATCH", "quality": "MARKET_DATA_MISSING",
                        "news_status": "NEWS_UNAVAILABLE", "added_at": "2026-10-13 09:00:00"})
    return _result("onboard_security", "CREATED", changed=True, security_id=sid, symbol=key, name=name, provider_symbols=planned["provider_symbols"], watchlist=planned["watchlist"], warnings=[])


async def get_sector_overview(as_of: str | None = None) -> str:
    """Sector overview (fake, READ)."""
    _calls("get_sector_overview", {"as_of": as_of})
    path = os.environ.get("FAKE_SECTORS_FILE")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    return _text({"ok": True, "operation": "get_sector_overview", "as_of": "2026-10-13", "total_market_value_eur": 0.0, "limit_pct": 30.0, "sectors": [], "unvalued": [], "warnings": []})


async def get_data_freshness() -> str:
    """Data freshness (fake, READ)."""
    _calls("get_data_freshness", {})
    path = os.environ.get("FAKE_FRESHNESS_FILE")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    return _text({"ok": True, "operation": "get_data_freshness", "now": "2026-10-14T12:00:00+00:00", "sources": {}, "rules": {}})


async def get_effective_exposure(as_of: str | None = None, top: int = 25) -> str:
    """Effective exposure (fake, READ)."""
    _calls("get_effective_exposure", {"as_of": as_of, "top": top})
    path = os.environ.get("FAKE_EFFECTIVE_FILE")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    return _text({"ok": True, "operation": "get_effective_exposure", "as_of": "2026-10-13", "total_market_value_eur": 0.0, "top": [], "etfs": [], "coverage": {}, "checks": {}, "warnings": []})


def _import_token(file_path, format, create_securities, include_historical, strategies, campaign_opened_at, accept_aliases, start_campaigns) -> str:
    """The plan token of the fake: a digest of everything that makes up a plan (like the real one), so every decision gives another token."""
    import hashlib

    payload = [file_path, format, bool(create_securities), bool(include_historical), sorted(strategies or []), sorted(campaign_opened_at or []), sorted(accept_aliases or []), bool(start_campaigns)]
    return "tok-" + hashlib.sha1(_text(payload).encode("utf-8")).hexdigest()[:10]


def _decided(envelope: dict, strategies, accept_aliases) -> dict:
    """The fixture answer with the decisions applied: a decided alias / strategy is no longer open; an alias to NOSUCH is refused like the real server does."""
    diff = envelope.get("diff") or {}
    for item in accept_aliases or []:
        identifier, _, security = item.partition("=")
        diff["alias_candidates"] = [c for c in diff.get("alias_candidates", []) if c["identifier"] != identifier]
        refused = security.upper() == "NOSUCH"
        diff.setdefault("identifier_aliases", []).append({"identifier": identifier, "security": security, "security_id": None if refused else 6, "symbol": None if refused else security,
                                                           "status": "REFUSED" if refused else "NEW", "reason": "no security with that symbol, ISIN or id" if refused else ""})
        if not refused:
            diff["masterdata_conflicts"] = [c for c in diff.get("masterdata_conflicts", []) if c.get("file") != identifier]
    chosen = {item.partition("=")[0] for item in strategies or []}
    diff["strategy_needed"] = [n for n in diff.get("strategy_needed", []) if n["key"] not in chosen]
    return envelope


async def check_transaction_import(file_path: str, format: str = "parqet", create_securities: bool = False, include_historical: bool = False,
                                   strategies: list[str] | None = None, campaign_opened_at: list[str] | None = None, accept_aliases: list[str] | None = None,
                                   start_campaigns: bool = True) -> str:
    """Action tool (fake): the dry run of a transaction import; the answer is the fixture file named by FAKE_IMPORT_FILE (mode check) with the decisions applied."""
    _calls("check_transaction_import", {"file_path": file_path, "format": format, "create_securities": create_securities, "include_historical": include_historical,
                                         "strategies": strategies, "campaign_opened_at": campaign_opened_at, "accept_aliases": accept_aliases, "start_campaigns": start_campaigns})
    if file_path.endswith("broken.csv"):
        return _text({"ok": False, "operation": "check_transaction_import", "result": "ERROR", "changed": False, "error": "FileNotFoundError", "message": "file not found: broken.csv"})
    path = os.environ.get("FAKE_IMPORT_FILE")
    if not (path and Path(path).exists()):
        return _text({"ok": False, "message": "no fixture"})
    envelope = _decided(json.loads(Path(path).read_text(encoding="utf-8")), strategies, accept_aliases)
    envelope["plan_token"] = _import_token(file_path, format, create_securities, include_historical, strategies, campaign_opened_at, accept_aliases, start_campaigns)
    return _text(envelope)


async def import_transactions(file_path: str, plan_token: str, format: str = "parqet", create_securities: bool = False, include_historical: bool = False,
                              strategies: list[str] | None = None, campaign_opened_at: list[str] | None = None, accept_aliases: list[str] | None = None,
                              start_campaigns: bool = True) -> str:
    """Action tool (fake): the import; the answer is the fixture file named by FAKE_IMPORT_DONE_FILE (mode write); a token that is not the one of these arguments is refused like the real tool does."""
    _calls("import_transactions", {"file_path": file_path, "plan_token": plan_token, "format": format, "create_securities": create_securities, "include_historical": include_historical,
                                    "strategies": strategies, "campaign_opened_at": campaign_opened_at, "accept_aliases": accept_aliases, "start_campaigns": start_campaigns})
    if plan_token != _import_token(file_path, format, create_securities, include_historical, strategies, campaign_opened_at, accept_aliases, start_campaigns):
        return _text({"ok": False, "operation": "import_transactions", "result": "ERROR", "changed": False, "error": "TransactionImportError", "message": "import plan is stale; check again before writing"})
    path = os.environ.get("FAKE_IMPORT_DONE_FILE")
    return Path(path).read_text(encoding="utf-8") if path and Path(path).exists() else _text({"ok": False, "message": "no fixture"})


for fn in (database_status, run_trading_orchestrator, rank_watchlist, slow, crash, broken, failing, sql_execute, get_watchlist, get_sector_overview, get_data_freshness, get_effective_exposure, add_watchlist_candidate, remove_watchlist_candidate, set_watchlist_priority, onboard_security, set_security_strategy, set_security_sector, check_transaction_import, import_transactions):
    server.tool(name=fn.__name__, description=fn.__doc__ or fn.__name__, structured_output=False)(fn)

print("fake-mcp ready", file=sys.stderr, flush=True)
if __name__ == "__main__":
    server.run("stdio")
