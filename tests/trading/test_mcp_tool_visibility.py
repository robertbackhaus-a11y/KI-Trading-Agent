"""Which MCP tools a client sees: the server registers every public method of ``Tools``; ``TRADING_EXCLUDE`` hides tools per client process.

The desktop GUI starts the server without an exclude (27 tools; its chat is offered the READ tools only).  The Jan front end is configured with the
three watchlist actions, ``onboard_security``, ``set_security_strategy``, ``set_security_sector``, ``check_transaction_import`` and ``import_transactions`` excluded, so it sees the 15 tools of before plus the four READ tools ``get_watchlist``, ``get_sector_overview``, ``get_data_freshness`` and ``get_effective_exposure`` (19).  The 15 tools of before keep their exact description and schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
JAN_EXCLUDE = "add_watchlist_candidate,remove_watchlist_candidate,set_watchlist_priority,onboard_security,set_security_strategy,set_security_sector,check_transaction_import,import_transactions"  # the value in Jan's MCP configuration (documented in the architecture docs)
WATCHLIST_ACTIONS = {"add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "onboard_security", "set_security_strategy", "set_security_sector", "check_transaction_import", "import_transactions"}  # the three watchlist writes, the onboarding, the strategy write, the sector write and the two transaction import tools
READ_ADDITIONS = {"get_watchlist", "get_sector_overview", "get_data_freshness", "get_effective_exposure"}  # the read tools added after the 15 original ones
NEW_TOOLS = WATCHLIST_ACTIONS | READ_ADDITIONS
# sha256 prefix of {"description", "input_schema"} of each tool that existed before the watchlist tools; a changed description or parameter fails this test
BASELINE = {
    "approve_swing_promotion": "2be6f8055b2bc03f",
    "database_schema": "a07e2a4cc0e0f252",
    "database_status": "caae007d5c2c7a79",
    "database_tables": "9104b6ba4fff608d",
    "evaluate_swing_candidate": "8e2d78b7da0db173",
    "evaluate_swing_candidates": "0b03d12de0a73124",
    "evaluate_watchlist_candidates": "b488a6093948a13a",
    "get_candidate_discovery": "883c70b875476827",
    "get_market_intelligence": "560820ac243bd2af",
    "get_opportunity_view": "2181fb73bc6a3948",
    "rank_watchlist": "9cbc1391a65b3915",
    "run_trading_orchestrator": "efc3fcd18624270f",
    "sql_execute": "2d33a873de33d7dd",
    "suggest_strategy_assignments": "15213cf5430125be",
    "table_info": "9c90d84cde4bc413",
}


def load_server():
    try:
        import mcp  # noqa: F401  (only the Trading venv has it)
    except ImportError:
        return None
    spec = importlib.util.spec_from_file_location("mcp_server_visibility_test", ROOT / "mcp-tools" / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SERVER = load_server()


def listing(exclude: str | None = None) -> dict:
    env = {"TRADING_DB_PATH": str(ROOT / "nonexistent-test.db")}
    if exclude is not None:
        env["TRADING_EXCLUDE"] = exclude
    with mock.patch.dict(os.environ, env, clear=False):
        if exclude is None:
            os.environ.pop("TRADING_EXCLUDE", None)
        return {t.name: t for t in asyncio.run(SERVER.build_server().list_tools())}


def fingerprint(tool) -> str:
    return hashlib.sha256(json.dumps({"description": tool.description or "", "input_schema": tool.input_schema}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


@unittest.skipIf(SERVER is None, "the mcp package is only in the Trading venv")
class ToolVisibilityTests(unittest.TestCase):
    def test_the_qt_gui_process_gets_all_27_tools(self) -> None:
        tools = listing()
        self.assertEqual(len(tools), 27)
        self.assertTrue(NEW_TOOLS <= set(tools))
        self.assertEqual(set(tools) - NEW_TOOLS, set(BASELINE))

    def test_jans_exclude_hides_exactly_the_eight_write_actions(self) -> None:
        self.assertEqual(set(JAN_EXCLUDE.split(",")), WATCHLIST_ACTIONS)
        everything, jan = listing(), listing(JAN_EXCLUDE)
        self.assertEqual(len(jan), 19)
        self.assertEqual(set(everything) - set(jan), WATCHLIST_ACTIONS)
        self.assertTrue(READ_ADDITIONS <= set(jan))  # Jan keeps the read tools
        self.assertTrue(WATCHLIST_ACTIONS.isdisjoint(jan))
        self.assertEqual(set(jan), set(BASELINE) | READ_ADDITIONS)  # exactly the tools of before plus the four read tools

    def test_the_exclude_is_tolerant_and_changes_nothing_else(self) -> None:
        self.assertEqual(set(listing(" add_watchlist_candidate , ,nonexistent_tool,")) , set(listing()) - {"add_watchlist_candidate"})
        self.assertEqual(len(listing("")), 27)
        for name, tool in listing(JAN_EXCLUDE).items():  # excluding does not touch the remaining tools
            if name in BASELINE:
                self.assertEqual(fingerprint(tool), BASELINE[name], name)

    def test_the_15_existing_tools_are_schema_identical(self) -> None:
        tools = listing()
        for name, expected in BASELINE.items():
            self.assertEqual(fingerprint(tools[name]), expected, f"{name}: description or parameters changed")

    def test_the_server_code_is_not_client_specific(self) -> None:
        text = (ROOT / "mcp-tools" / "server.py").read_text(encoding="utf-8").lower()
        for client in ("jan", "qt-gui", "ollama", "claude", "codex"):
            self.assertNotRegex(text.replace("janeiro", ""), rf"(?<![a-z]){client}(?![a-z])", client)

    def test_the_new_tools_have_narrow_signatures_without_sql_or_security_fields(self) -> None:
        tools = listing()
        params = {n: sorted(tools[n].input_schema.get("properties", {})) for n in NEW_TOOLS}
        self.assertEqual(params["get_watchlist"], ["detail", "limit", "status", "strategy", "symbol", "table"])
        self.assertEqual(params["add_watchlist_candidate"], ["dry_run", "identifier", "priority"])
        self.assertEqual(params["remove_watchlist_candidate"], ["security_id"])
        self.assertEqual(params["set_watchlist_priority"], ["priority", "security_id"])
        self.assertEqual(params["onboard_security"], sorted(["symbol", "name", "asset_type", "isin", "wkn", "exchange", "currency", "country", "provider_symbols", "add_to_watchlist", "watchlist_priority", "dry_run"]))
        self.assertEqual(params["set_security_strategy"], ["security_id", "strategy"])
        self.assertEqual(params["set_security_sector"], ["sector", "security_id"])
        for forbidden in ("sql", "query", "security_id", "sector", "cik", "active"):  # the onboarding takes no SQL, no id, no sector and no CIK
            self.assertNotIn(forbidden, tools["onboard_security"].input_schema.get("properties", {}), forbidden)
        self.assertEqual(params["check_transaction_import"], sorted(["file_path", "format", "create_securities", "include_historical", "strategies", "campaign_opened_at", "accept_aliases", "start_campaigns"]))
        self.assertEqual(params["import_transactions"], sorted(["file_path", "plan_token", "format", "create_securities", "include_historical", "strategies", "campaign_opened_at", "accept_aliases", "start_campaigns"]))
        for name in ("check_transaction_import", "import_transactions"):  # a file path and options, never SQL
            for forbidden in ("sql", "query", "security_id", "table"):
                self.assertNotIn(forbidden, tools[name].input_schema.get("properties", {}), f"{name}: {forbidden}")
        for name in WATCHLIST_ACTIONS - {"onboard_security", "set_security_strategy", "set_security_sector", "check_transaction_import", "import_transactions"}:
            for forbidden in ("sql", "query", "name", "isin", "wkn", "symbol", "exchange", "currency", "sector"):  # nothing that could describe a new security
                self.assertNotIn(forbidden, tools[name].input_schema.get("properties", {}), f"{name}: {forbidden}")


if __name__ == "__main__":
    unittest.main()
