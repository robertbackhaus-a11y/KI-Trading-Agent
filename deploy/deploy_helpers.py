"""Small helpers for Deploy-TradingAgent.ps1 (stdlib only; `smoke` needs the `mcp` package of the production venv).

Every subcommand prints exactly one JSON object on the last stdout line; exit code 0 = ok, 1 = problem.
Nothing here writes next to the deployed files: the databases are opened read-only unless a command says otherwise,
and no bytecode is written.

  compile FILE...          every file compiles (syntax only)
  json FILE...             every file is valid JSON
  integrity DB             PRAGMA integrity_check on a read-only connection
  backup SRC DST           consistent copy through the SQLite backup API (DST must not exist), then integrity_check of DST
  restore SRC DST          copy the backup SRC back into DST through the SQLite backup API, then integrity_check of DST
  smoke --server PATH --db PATH --expect-tools N
                           start the MCP server over stdio, list the tools, call database_status (read-only)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path


def _emit(payload: dict, ok: bool) -> int:
    print(json.dumps({"ok": ok, **payload}, ensure_ascii=False))
    return 0 if ok else 1


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:///{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0)


def _integrity(path: Path) -> list[str]:
    conn = _ro(path)
    try:
        return [row[0] for row in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()


def cmd_compile(args) -> int:
    bad = []
    for name in args.files:
        try:
            compile(Path(name).read_bytes(), name, "exec")
        except (SyntaxError, ValueError, OSError) as exc:
            bad.append(f"{name}: {type(exc).__name__}: {exc}")
    return _emit({"checked": len(args.files), "errors": bad}, not bad)


def cmd_json(args) -> int:
    bad = []
    for name in args.files:
        try:
            json.loads(Path(name).read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            bad.append(f"{name}: {type(exc).__name__}: {exc}")
    return _emit({"checked": len(args.files), "errors": bad}, not bad)


def cmd_integrity(args) -> int:
    result = _integrity(Path(args.db))
    return _emit({"result": result}, result == ["ok"])


def cmd_backup(args) -> int:
    src, dst = Path(args.src), Path(args.dst)
    if dst.exists():
        return _emit({"error": f"destination exists: {dst}"}, False)
    source = _ro(src)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    result = _integrity(dst)
    return _emit({"bytes": dst.stat().st_size, "integrity": result}, result == ["ok"])


def cmd_restore(args) -> int:
    src, dst = Path(args.src), Path(args.dst)
    if not src.is_file():
        return _emit({"error": f"backup not found: {src}"}, False)
    if _integrity(src) != ["ok"]:
        return _emit({"error": "backup fails integrity_check; nothing restored"}, False)
    source = _ro(src)
    target = sqlite3.connect(str(dst), timeout=30.0)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    result = _integrity(dst)
    return _emit({"integrity": result}, result == ["ok"])


async def _smoke(server: Path, db: Path, expect: int) -> dict:
    from mcp import ClientSession  # production venv only
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(server)],
        env={**os.environ, "TRADING_DB_PATH": str(db), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    with open(os.devnull, "w") as quiet:  # the server logs to stderr; the verdict is the JSON line
        async with stdio_client(params, errlog=quiet) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = [tool.name for tool in (await session.list_tools()).tools]
                result = await session.call_tool("database_status", {})
                text = "\n".join(getattr(part, "text", "") for part in result.content)
                return {"tool_count": len(tools), "expected": expect, "database_status_error": bool(result.is_error), "database_status_chars": len(text), "tools_ok": len(tools) == expect}


def cmd_smoke(args) -> int:
    try:
        info = asyncio.run(asyncio.wait_for(_smoke(Path(args.server), Path(args.db), args.expect_tools), timeout=90))
    except Exception as exc:  # report, never raise: the PowerShell side prints the verdict
        return _emit({"error": f"{type(exc).__name__}: {str(exc)[:300]}"}, False)
    return _emit(info, info["tools_ok"] and not info["database_status_error"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("compile", cmd_compile), ("json", cmd_json)):
        p = sub.add_parser(name)
        p.add_argument("files", nargs="+")
        p.set_defaults(func=func)
    p = sub.add_parser("integrity")
    p.add_argument("db")
    p.set_defaults(func=cmd_integrity)
    for name, func in (("backup", cmd_backup), ("restore", cmd_restore)):
        p = sub.add_parser(name)
        p.add_argument("src")
        p.add_argument("dst")
        p.set_defaults(func=func)
    p = sub.add_parser("smoke")
    p.add_argument("--server", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--expect-tools", type=int, required=True)
    p.set_defaults(func=cmd_smoke)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
