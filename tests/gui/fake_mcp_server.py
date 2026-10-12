"""A small stdio MCP server for the GUI tests (official SDK, same registration style as mcp-tools/server.py).

Tools: database_status, run_trading_orchestrator (returns the JSON file named by FAKE_ORCH_FILE), slow, crash, broken, sql_execute.
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


for fn in (database_status, run_trading_orchestrator, rank_watchlist, slow, crash, broken, failing, sql_execute):
    server.tool(name=fn.__name__, description=fn.__doc__ or fn.__name__, structured_output=False)(fn)

print("fake-mcp ready", file=sys.stderr, flush=True)
if __name__ == "__main__":
    server.run("stdio")
