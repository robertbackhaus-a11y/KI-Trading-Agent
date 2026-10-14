"""Architecture rules of the GUI package (static): no database or trading logic, no actions, READ-only wiring, clean layering."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import support

GUI = support.ROOT / "gui"
TRADING_MODULES = {p.stem for p in (support.ROOT / "tools" / "trading").glob("*.py")} - {"__init__"}
FORBIDDEN_EVERYWHERE = {"sqlite3", "requests", "httpx", "httpx2", "langchain", "crewai", "qasync", "PyQt5", "PyQt6", "PySide2", "ollama"}


def imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def files(sub: str = "") -> list[Path]:
    return sorted((GUI / sub).rglob("*.py")) if sub else sorted(GUI.rglob("*.py"))


class ArchitectureTests(unittest.TestCase):
    def test_the_package_is_small_and_has_the_planned_layout(self) -> None:
        names = {str(p.relative_to(GUI)).replace("\\", "/") for p in files()}
        self.assertEqual(names - {"__init__.py", "services/__init__.py", "viewmodels/__init__.py", "views/__init__.py"}, {
            "__main__.py", "app.py", "services/background.py", "services/chat_service.py", "services/mcp_client.py", "services/ollama_client.py", "services/settings.py",
            "services/task_service.py", "services/tool_loop.py", "services/tool_policy.py", "viewmodels/chat.py", "viewmodels/onboarding.py", "viewmodels/portfolio.py", "viewmodels/reason_codes.py", "viewmodels/sectors.py", "viewmodels/tasks.py", "viewmodels/treemap.py", "viewmodels/watchlist.py", "viewmodels/watchlist_edit.py",
            "views/_common.py", "views/chat.py", "views/dashboard.py", "views/diagnostics.py", "views/onboarding.py", "views/planner.py", "views/sectors.py", "views/tasks.py", "views/watchlist.py", "views/watchlist_edit.py",
        })
        self.assertFalse((GUI / "controllers").exists())
        self.assertFalse((GUI / "widgets").exists())

    def test_no_database_access_no_trading_logic_no_forbidden_frameworks(self) -> None:
        for path in files():
            found = imports(path)
            self.assertEqual(found & FORBIDDEN_EVERYWHERE, set(), path.name)
            self.assertEqual(found & TRADING_MODULES, set(), f"{path.name} imports trading logic")
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)\bselect\s+.+\s+from\b|\binsert\s+into\b|\bdelete\s+from\b", path.name)
            self.assertNotIn("trading.db", text.replace("trading_db_path", "").replace("TRADING_DB_PATH", "").replace(r"data\trading.db", ""), path.name)

    def test_view_models_are_pure_python(self) -> None:
        for path in files("viewmodels"):
            found = imports(path)
            self.assertFalse({"PySide6", "subprocess", "mcp", "asyncio", "threading"} & found, path.name)

    def test_views_do_not_talk_to_mcp_or_the_system(self) -> None:
        for path in files("views"):
            found = imports(path)
            self.assertFalse({"mcp", "mcp_types", "subprocess", "asyncio", "anyio", "urllib"} & found, path.name)
            self.assertNotIn("mcp_client", path.read_text(encoding="utf-8"), path.name)

    def test_action_tools_are_named_only_in_the_policy(self) -> None:
        for path in files():
            text = path.read_text(encoding="utf-8")
            for tool in ("sql_execute", "approve_swing_promotion"):
                if path.name == "tool_policy.py":
                    continue
                self.assertNotRegex(text, rf"call\(\s*['\"]{tool}", f"{path.name} calls {tool}")
                self.assertNotIn(f'"{tool}"', text, path.name)

    def test_the_only_tool_calls_are_read_tools(self) -> None:
        from gui.services.tool_policy import is_read

        called = set()
        for path in files():
            called |= set(re.findall(r"\.call\(\s*\"([a-z_]+)\"", path.read_text(encoding="utf-8")))
        self.assertEqual(called, {"database_status", "run_trading_orchestrator", "get_watchlist", "get_sector_overview"})
        self.assertTrue(all(is_read(n) for n in called))

    def test_the_mcp_server_is_never_imported_and_the_chat_endpoint_has_one_owner(self) -> None:
        for path in files():
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*(from|import)\s+(server|trading_sqlite|orchestrator_compact)\b", path.name)
            if path.name != "ollama_client.py":
                self.assertNotRegex(text, r"[\"']/api/chat", path.name)  # the chat endpoint is spoken by exactly one module

    def test_task_service_is_free_of_qt_and_of_task_definition_changes(self) -> None:
        path = GUI / "services" / "task_service.py"
        self.assertNotIn("PySide6", imports(path))
        text = path.read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(("#", '"""')))
        for forbidden in ("Register-ScheduledTask", "Set-ScheduledTask", "Unregister-ScheduledTask", "Disable-ScheduledTask", "Enable-ScheduledTask", "New-ScheduledTask"):
            self.assertNotIn(forbidden, code)
        self.assertIn("Start-ScheduledTask", code)

    def test_no_secrets_and_no_private_values_in_the_gui_package_or_tests(self) -> None:
        for path in [*files(), *sorted((support.ROOT / "tests" / "gui").glob("*.py"))]:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?i)\b(api[_-]?key|secret|password)\s*[:=]\s*['\"][^'\"]+", text), path.name)
            self.assertNotRegex(text, r"C:\\Users\\", path.name)
            self.assertNotRegex(text, r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", path.name)  # no ISINs


    def test_the_chat_modules_are_layered_and_never_persist_anything(self) -> None:
        loop, client, service = GUI / "services" / "tool_loop.py", GUI / "services" / "ollama_client.py", GUI / "services" / "chat_service.py"
        for path in (loop, client):
            self.assertNotIn("PySide6", imports(path), path.name)  # testable without Qt
        self.assertFalse({"mcp", "mcp_types", "anyio", "asyncio", "subprocess", "sqlite3"} & imports(loop))
        for path in (loop, client, service, GUI / "viewmodels" / "chat.py", GUI / "views" / "chat.py"):
            text = path.read_text(encoding="utf-8")
            code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
            for forbidden in ("open(", "write_text", "write_bytes", "json.dump(", "pickle", "shelve", "sqlite3", "QSettings"):
                self.assertNotIn(forbidden, code, f"{path.name} must not persist chat data ({forbidden})")

    def test_the_loop_only_reaches_mcp_through_the_injected_call_and_offers_read_tools_only(self) -> None:
        text = (GUI / "services" / "tool_loop.py").read_text(encoding="utf-8")
        self.assertIn("tool_policy.is_read(name)", text)  # the offering is filtered by the policy
        self.assertIn("if name not in self.schemas", text)  # a call to anything that was not offered is refused before the MCP call
        tree_names = {n.id for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Attribute)}
        self.assertFalse({"call_sync", "McpService", "ClientSession"} & tree_names)
        service = (GUI / "services" / "chat_service.py").read_text(encoding="utf-8")
        self.assertIn("call_sync", service)
        used = {n.id for n in ast.walk(ast.parse(service)) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(ast.parse(service)) if isinstance(n, ast.Attribute)}
        self.assertFalse({"ClientSession", "_core", "_submit"} & used)  # the ClientSession stays in the MCP thread

    def test_no_agent_framework_and_no_ollama_package(self) -> None:
        for path in files():
            self.assertEqual(imports(path) & {"langchain", "langchain_core", "crewai", "autogen", "llama_index", "ollama", "openai", "litellm"}, set(), path.name)


if __name__ == "__main__":
    unittest.main()
