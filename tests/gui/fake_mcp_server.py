"""A small stdio MCP server for the GUI tests (official SDK, same registration style as mcp-tools/server.py).

Tools: database_status, run_trading_orchestrator (returns the JSON file named by FAKE_ORCH_FILE), slow, crash, broken, sql_execute, and the four watchlist tools
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
    return _text({"ok": True, "database_path": os.environ.get("TRADING_DB_PATH"), "user": os.environ.get("TRADING_USER_ID")})


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


for fn in (database_status, run_trading_orchestrator, rank_watchlist, slow, crash, broken, failing, sql_execute, get_watchlist, add_watchlist_candidate, remove_watchlist_candidate, set_watchlist_priority):
    server.tool(name=fn.__name__, description=fn.__doc__ or fn.__name__, structured_output=False)(fn)

print("fake-mcp ready", file=sys.stderr, flush=True)
if __name__ == "__main__":
    server.run("stdio")
