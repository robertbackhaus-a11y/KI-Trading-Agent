"""Rauchtest: startet server.py per stdio gegen die TEST-Datenbank und ruft einige Tools auf."""
import asyncio, json, os, sys
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent

def short(obj, n=500):
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return s[:n] + (" ...[gekuerzt]" if len(s) > n else "")

def payload(res):
    if getattr(res, "structured_content", None):
        return res.structured_content
    return "\n".join(getattr(c, "text", "") for c in res.content)

async def main():
    params = StdioServerParameters(
        command=str(HERE.parent / ".venv" / "Scripts" / "python.exe"),
        args=[str(HERE / "server.py")],
        env={**os.environ, "TRADING_DB_PATH": str(HERE.parent / "data" / "trading.db")},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print("Tools:", len(tools), [t.name for t in tools])
            assert len(tools) == 15, f"expected 15 tools, got {len(tools)}"
            assert {"get_candidate_discovery", "get_market_intelligence", "get_opportunity_view"} <= {t.name for t in tools}
            total = sum(len(json.dumps(t.model_dump(), ensure_ascii=False)) for t in tools)
            print("Beschreibungsumfang (Zeichen):", total, "~Token:", total // 3)
            for name, args in [("database_status", {}), ("database_tables", {}), ("run_trading_orchestrator", {}), ("get_candidate_discovery", {"limit": 3}), ("get_market_intelligence", {"limit": 3}), ("get_opportunity_view", {"limit": 3})]:
                res = await s.call_tool(name, args)
                print(f"\n== {name} isError={res.is_error}")
                print(short(payload(res)))

asyncio.run(main())
