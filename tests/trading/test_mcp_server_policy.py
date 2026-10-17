# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Server-side tool policy (``mcp-tools/tool_policy.py``): the server itself decides which tools a client process can reach.

Before v0.6.0 the server registered every public method of ``Tools`` and only the desktop GUI filtered READ / ACTION on its side; any other MCP client
(or a GUI started with ``allow_actions``) could call ``sql_execute`` and ``approve_swing_promotion``.  Now the profile of the process
(``TRADING_TOOL_PROFILE``: read by default, gui, admin) decides what is registered, an unclassified tool is never registered, and the READ tools open the
database read-only.  Synthetic data only.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


policy = _load("tool_policy_server_test", ROOT / "mcp-tools" / "tool_policy.py")
try:
    import mcp  # noqa: F401  (only the Trading venv has it)

    SERVER = _load("mcp_server_policy_test", ROOT / "mcp-tools" / "server.py")
except ImportError:
    SERVER = None
reset = _load("reset_for_policy_tests", ROOT / "tools" / "trading" / "Reset-TradingDb.py")

ADMIN_ONLY = {"sql_execute", "approve_swing_promotion"}


def tools_class_methods() -> set[str]:
    tree = ast.parse((ROOT / "mcp-tools" / "trading_sqlite.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Tools")
    return {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")}


class PolicyTableTests(unittest.TestCase):
    def test_every_server_tool_is_classified_and_nothing_stale(self) -> None:
        self.assertEqual(set(policy.POLICY), tools_class_methods())

    def test_profiles(self) -> None:
        reads = {n for n in policy.POLICY if policy.is_read(n)}
        self.assertEqual(len(reads), 17)
        self.assertEqual({n for n in policy.POLICY if policy.allowed(n, "read")}, reads)
        self.assertEqual({n for n in policy.POLICY if policy.allowed(n, "gui")}, reads | policy.GUI_ACTIONS)
        self.assertEqual({n for n in policy.POLICY if policy.allowed(n, "admin")}, set(policy.POLICY))
        for profile in ("read", "gui"):
            for name in ADMIN_ONLY:
                self.assertFalse(policy.allowed(name, profile), (name, profile))
        self.assertTrue(policy.GUI_ACTIONS.isdisjoint(ADMIN_ONLY))

    def test_an_unknown_tool_is_never_allowed(self) -> None:
        for profile in policy.PROFILES:
            for name in ("brand_new_tool", "", "SQL_EXECUTE", "sql_execute ", "database_status;drop"):
                self.assertFalse(policy.allowed(name, profile), (name, profile))

    def test_the_profile_comes_from_the_environment_and_fails_closed(self) -> None:
        self.assertEqual(policy.profile_from_env({}), "read")
        self.assertEqual(policy.profile_from_env({"TRADING_TOOL_PROFILE": "  "}), "read")
        self.assertEqual(policy.profile_from_env({"TRADING_TOOL_PROFILE": " GUI "}), "gui")
        for wrong in ("all", "write", "full", "admin,gui", "1"):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                policy.profile_from_env({"TRADING_TOOL_PROFILE": wrong})
        with self.assertRaises(ValueError):
            policy.allowed("database_status", "everything")

    def test_the_gui_uses_the_same_table(self) -> None:
        sys.path.insert(0, str(ROOT))
        from gui.services import tool_policy as gui_policy

        self.assertEqual(gui_policy.POLICY, policy.POLICY)
        self.assertEqual(gui_policy.GUI_ACTIONS, policy.GUI_ACTIONS)
        text = (ROOT / "gui" / "services" / "tool_policy.py").read_text(encoding="utf-8")
        self.assertNotIn('"sql_execute"', text)  # no second table that could drift


@unittest.skipIf(SERVER is None, "the mcp package is only in the Trading venv")
class ServerRegistrationTests(unittest.TestCase):
    def build(self, profile=None, exclude=None, tools_cls=None):
        env = {"TRADING_DB_PATH": str(ROOT / "nonexistent-test.db")}
        for key, value in (("TRADING_TOOL_PROFILE", profile), ("TRADING_EXCLUDE", exclude)):
            if value is not None:
                env[key] = value
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stderr(io.StringIO()):
            for key, value in (("TRADING_TOOL_PROFILE", profile), ("TRADING_EXCLUDE", exclude)):
                if value is None:
                    os.environ.pop(key, None)
            if tools_cls is not None:
                with mock.patch.object(SERVER, "_load_tools_class", lambda *a: tools_cls):
                    return SERVER.build_server()
            return SERVER.build_server()

    def names(self, server) -> set[str]:
        return {t.name for t in asyncio.run(server.list_tools())}

    def test_read_profile_by_default(self) -> None:
        self.assertEqual(self.names(self.build()), {n for n in policy.POLICY if policy.is_read(n)})

    def test_gui_profile_keeps_the_gui_actions_and_refuses_the_admin_tools(self) -> None:
        names = self.names(self.build("gui"))
        self.assertTrue(policy.GUI_ACTIONS <= names)
        self.assertTrue(ADMIN_ONLY.isdisjoint(names))

    def test_an_unclassified_public_method_is_never_registered(self) -> None:
        base = SERVER._load_tools_class("trading_sqlite.py", "trading_sqlite_policy_test")

        class WithNewTool(base):
            async def brand_new_tool(self) -> dict:
                return {"ok": True, "wrote": True}

        for profile in ("read", "gui", "admin"):
            with self.subTest(profile=profile):
                self.assertNotIn("brand_new_tool", self.names(self.build(profile, tools_cls=WithNewTool)))

    def test_an_invalid_profile_stops_the_server(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.build("everything")
        self.assertIn("TRADING_TOOL_PROFILE", str(ctx.exception))

    def test_the_exclude_list_narrows_but_cannot_widen(self) -> None:
        self.assertEqual(self.names(self.build("read", exclude="sql_execute,get_watchlist")), {n for n in policy.POLICY if policy.is_read(n)} - {"get_watchlist"})

    def test_calling_an_unregistered_tool_fails_in_process(self) -> None:
        server = self.build("gui")
        for name in ("sql_execute", "approve_swing_promotion", "brand_new_tool"):
            with self.subTest(name=name):
                try:
                    result = asyncio.run(server.call_tool(name, {"sql": "DELETE FROM security"} if name == "sql_execute" else {}))
                except Exception as exc:  # noqa: BLE001 - the SDK raises for an unknown tool
                    self.assertIn(name, str(exc))
                else:
                    self.assertTrue(getattr(result, "is_error", False), result)


@unittest.skipIf(SERVER is None, "the mcp package is only in the Trading venv")
class DirectClientTests(unittest.TestCase):
    """A plain MCP client over stdio (as Jan or goose connect) cannot reach a write in the default profile."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "trading.db"
        with contextlib.redirect_stdout(io.StringIO()):
            reset.create_database(self.db)

    def digest(self) -> str:
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    async def session_calls(self, profile: str | None, calls):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = {**os.environ, "TRADING_DB_PATH": str(self.db), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("TRADING_TOOL_PROFILE", None)
        env.pop("TRADING_EXCLUDE", None)
        if profile is not None:
            env["TRADING_TOOL_PROFILE"] = profile
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp-tools" / "server.py")], env=env)
        out = {}
        with open(os.devnull, "w") as quiet:
            async with stdio_client(params, errlog=quiet) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    out["tools"] = {t.name for t in (await session.list_tools()).tools}
                    for name, args in calls:
                        result = await session.call_tool(name, args)
                        out[name] = (bool(result.is_error), "\n".join(getattr(p, "text", "") for p in result.content))
        return out

    def test_a_direct_client_cannot_bypass_the_policy(self) -> None:
        before = self.digest()
        out = asyncio.run(asyncio.wait_for(self.session_calls(None, [
            ("sql_execute", {"sql": "DROP TABLE security"}),
            ("approve_swing_promotion", {"security_id": 1, "plan_token": "x", "effective_from": "2026-01-01", "approved_by": "test"}),
            ("add_watchlist_candidate", {"identifier": "EXA"}),
            ("database_status", {}),
        ]), timeout=90))
        self.assertEqual(len(out["tools"]), 17)
        for name in ("sql_execute", "approve_swing_promotion", "add_watchlist_candidate"):
            self.assertTrue(out[name][0], f"{name} must be refused: {out[name][1][:200]}")
        self.assertFalse(out["database_status"][0])
        self.assertTrue(json.loads(out["database_status"][1])["ok"])
        self.assertEqual(self.digest(), before)  # the database is byte-identical
        with contextlib.closing(sqlite3.connect(self.db)) as check:
            self.assertIsNotNone(check.execute("SELECT 1 FROM sqlite_master WHERE name = 'security'").fetchone())


class ReadToolsAreReadOnlyTests(unittest.TestCase):
    """The four database meta tools used the read-write connection; they now open the database read-only like the other READ tools."""

    def setUp(self) -> None:
        if SERVER is None:
            self.skipTest("the mcp package is only in the Trading venv")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "trading.db"
        with contextlib.redirect_stdout(io.StringIO()):
            reset.create_database(self.db)
        tools_cls = SERVER._load_tools_class("trading_sqlite.py", "trading_sqlite_readonly_test")
        self.tools = tools_cls()
        self.tools.valves.database_path = str(self.db)

    def test_the_meta_tools_never_use_the_write_connection(self) -> None:
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with mock.patch.object(type(self.tools), "_connect", side_effect=AssertionError("READ tool opened a write connection")):
            for name, args in (("database_status", {}), ("database_tables", {}), ("database_schema", {}), ("table_info", {"table_name": "security"})):
                with self.subTest(name=name):
                    result = asyncio.run(getattr(self.tools, name)(**args))
                    self.assertTrue(result["ok"], result)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_the_read_only_connection_refuses_writes(self) -> None:
        conn = self.tools._connect_readonly()
        self.addCleanup(conn.close)
        for statement in ("CREATE TABLE x(a)", "DELETE FROM security", "DROP TABLE security"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                conn.execute(statement)

    def test_table_info_with_a_hostile_name_changes_nothing(self) -> None:
        result = asyncio.run(self.tools.table_info('security"); DROP TABLE security; --'))
        self.assertTrue(result["ok"])
        self.assertEqual(result["columns"], [])
        with contextlib.closing(sqlite3.connect(self.db)) as check:
            self.assertIsNotNone(check.execute("SELECT 1 FROM sqlite_master WHERE name = 'security'").fetchone())


if __name__ == "__main__":
    unittest.main()
