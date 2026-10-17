# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Tool policy: every MCP tool is classified, unknown tools are denied (C, D)."""

from __future__ import annotations

import ast
import unittest

import support
from gui.services import tool_policy as tp

READ = {"database_schema", "database_status", "database_tables", "table_info", "evaluate_swing_candidate", "evaluate_swing_candidates", "evaluate_watchlist_candidates",
        "get_candidate_discovery", "get_data_freshness", "get_effective_exposure", "get_market_intelligence", "get_opportunity_view", "get_sector_overview", "get_watchlist", "rank_watchlist", "run_trading_orchestrator", "suggest_strategy_assignments"}
ACTION = {"approve_swing_promotion", "sql_execute", "add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "onboard_security", "set_security_strategy", "set_security_sector", "check_transaction_import", "import_transactions"}


def server_tool_names() -> set[str]:
    """Public methods of the ``Tools`` class (every one becomes an MCP tool), read statically from the server source."""
    tree = ast.parse((support.ROOT / "mcp-tools" / "trading_sqlite.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Tools")
    return {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")}


class ToolPolicyTests(unittest.TestCase):
    # C
    def test_c_every_tool_of_the_server_is_classified(self) -> None:
        names = server_tool_names()
        self.assertEqual(len(names), 27)
        self.assertEqual(tp.unclassified(names), [])
        self.assertEqual({n for n in names if tp.classify(n) == tp.READ}, READ)
        self.assertEqual({n for n in names if tp.classify(n) == tp.ACTION}, ACTION)
        self.assertEqual(set(tp.POLICY), names)  # no stale entries either

    # D
    def test_d_an_unknown_tool_is_an_action_and_denied(self) -> None:
        self.assertEqual(tp.classify("brand_new_tool"), tp.ACTION)
        self.assertFalse(tp.is_read("brand_new_tool"))
        self.assertFalse(tp.is_known("brand_new_tool"))
        with self.assertRaises(tp.ToolDenied) as ctx:
            tp.require_read("brand_new_tool")
        self.assertIn("unbekannt", str(ctx.exception))
        self.assertEqual(tp.unclassified(["database_status", "brand_new_tool"]), ["brand_new_tool"])

    def test_action_tools_are_denied_and_read_tools_pass(self) -> None:
        for name in ACTION:
            with self.assertRaises(tp.ToolDenied):
                tp.require_read(name)
        for name in READ:
            tp.require_read(name)

    def test_empty_and_odd_names_are_denied(self) -> None:
        for name in ("", " ", "SQL_EXECUTE", "sql_execute ", "database_status;drop"):
            self.assertFalse(tp.is_read(name), repr(name))


if __name__ == "__main__":
    unittest.main()
