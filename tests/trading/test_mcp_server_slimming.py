"""MCP adapter context protection: the default orchestrator answer is the compact status, detail=true is untouched, the fallback keeps the tool working,
and the tool surface (signature, 15 tools) is unchanged."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_orchestrator_compact as compact_tests  # noqa: E402  (module import, its tests are not collected twice)


def load_server():
    try:
        import mcp  # noqa: F401  (only the Trading venv has it)
    except ImportError:
        return None
    spec = importlib.util.spec_from_file_location("mcp_server_under_test", ROOT / "mcp-tools" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_sample():
    candidates = [
        {"security_id": 1, "symbol": "AAA", "name": "AAA", "entry_status": "ENTRY_READY", "reason_codes": ["ENTRY_READY"], "rank": 1,
         "entry_score": 40.0, "momentum_score": 66.0, "confidence": 0.6, "watchlist_priority": None, "price_eur": 100.0,
         "quality": {"technical": "available"}, "sizing": {"proposed_quantity": 10.0}},
        {"security_id": 2, "symbol": "BBB", "name": "BBB", "entry_status": "BLOCKED_BY_DATA", "reason_codes": ["PRICE_UNAVAILABLE"], "rank": None,
         "entry_score": None, "momentum_score": None, "confidence": None, "watchlist_priority": 5, "price_eur": None,
         "quality": None, "sizing": None},
    ]
    return {"ok": True, "result": {
        "existing_position_results": [{"symbol": "X"}], "entry_candidate_results": [], "promotion_results": [{"symbol": "AAA"}],
        "rendered_summary_de": "text",
        "portfolio_action_plan": {"status": "AVAILABLE", "entry_candidates": candidates, "planned_entries": [{"symbol": "AAA"}], "rendered_de": "plan"},
    }}


class McpServerSlimmingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        if self.server is None:
            self.skipTest("mcp package not installed (run the suite with the Trading venv)")

    # ---- the compact default
    def test_default_view_is_the_structured_compact_status_and_keeps_the_envelope(self) -> None:
        base, result, _ = compact_tests.real_result()
        self.addCleanup(base.tearDown)
        response = {"ok": True, "operation": "run_trading_orchestrator", "result": result}
        before = copy.deepcopy(response)
        slim = self.server._slim_orchestrator(response)
        self.assertEqual(response, before)  # input is never mutated
        self.assertEqual((slim["ok"], slim["operation"], slim["detail"]), (True, "run_trading_orchestrator", False))
        self.assertEqual(slim["result"]["compact_version"], 2)
        for block in ("portfolio", "position_engine", "promotion", "entry_plan", "proceeds", "capital", "issues"):
            self.assertIn(block, slim["result"], block)
        self.assertNotIn("rendered_summary_de", slim["result"])

    def test_wrapper_returns_compact_by_default_and_the_untouched_full_result_for_detail_true(self) -> None:
        base, result, _ = compact_tests.real_result()
        self.addCleanup(base.tearDown)
        full = {"ok": True, "operation": "run_trading_orchestrator", "result": result}

        async def run_trading_orchestrator(as_of: str | None = None) -> dict:
            return copy.deepcopy(full)

        wrapper = self.server._make_wrapper(object(), "run_trading_orchestrator", run_trading_orchestrator, "tester")
        compact_text, detail_text = asyncio.run(wrapper()), asyncio.run(wrapper(detail=True))
        self.assertEqual(json.loads(detail_text), json.loads(self.server._compact(full)))  # detail=true: every field, nothing removed
        self.assertLess(len(compact_text), len(detail_text))
        self.assertEqual(json.loads(compact_text)["result"]["compact_version"], 2)
        self.assertEqual(list(inspect.signature(wrapper).parameters), ["as_of", "detail"])

    # ---- fallbacks: the tool must keep working
    def test_missing_compact_module_falls_back_to_the_legacy_view(self) -> None:
        original = self.server._COMPACT
        self.addCleanup(setattr, self.server, "_COMPACT", original)
        self.server._COMPACT = None
        slim = self.server._slim_orchestrator(legacy_sample())["result"]
        self.assertTrue(slim["existing_position_results"]["omitted_for_context"])

    def test_failing_compact_view_falls_back_to_the_legacy_view(self) -> None:
        class Broken:
            @staticmethod
            def compact_orchestrator_response(_response):
                raise RuntimeError("boom")

        original = self.server._COMPACT
        self.addCleanup(setattr, self.server, "_COMPACT", original)
        self.server._COMPACT = Broken()
        slim = self.server._slim_orchestrator(legacy_sample())["result"]
        self.assertTrue(slim["promotion_results"]["omitted_for_context"])

    # ---- legacy view (fallback) unchanged
    def test_legacy_view_projects_entry_candidates_and_keeps_the_rest_of_the_plan(self) -> None:
        result = legacy_sample()
        before = copy.deepcopy(result)
        slim = self.server._slim_orchestrator_legacy(result)
        self.assertEqual(result, before)
        plan = slim["result"]["portfolio_action_plan"]
        self.assertEqual(plan["entry_candidates"], [
            {"rank": 1, "symbol": "AAA", "entry_status": "ENTRY_READY", "entry_score": 40.0, "reason_codes": ["ENTRY_READY"]},
            {"rank": None, "symbol": "BBB", "entry_status": "BLOCKED_BY_DATA", "entry_score": None, "reason_codes": ["PRICE_UNAVAILABLE"]},
        ])
        self.assertIn("detail=true", plan["entry_candidates_note"])
        self.assertEqual(slim["result"]["rendered_summary_de"], "text")
        self.assertEqual(slim["detail"], False)

    def test_legacy_view_omits_the_heavy_lists_and_handles_missing_plan_and_failures(self) -> None:
        for key in ("existing_position_results", "entry_candidate_results", "promotion_results"):
            self.assertTrue(self.server._slim_orchestrator_legacy(legacy_sample())["result"][key]["omitted_for_context"], key)
        result = {"ok": True, "result": {"existing_position_results": [], "entry_candidate_results": [], "promotion_results": []}}
        self.assertNotIn("portfolio_action_plan", self.server._slim_orchestrator_legacy(result)["result"])
        self.assertEqual(self.server._slim_orchestrator({"ok": False}), {"ok": False})

    # ---- tool surface
    def test_tool_signature_and_tool_count_are_unchanged(self) -> None:
        spec = importlib.util.spec_from_file_location("trading_sqlite_slim_test", ROOT / "mcp-tools" / "trading_sqlite.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertEqual(list(inspect.signature(module.Tools.run_trading_orchestrator).parameters), ["self", "as_of"])
        public = [name for name, _f in inspect.getmembers(module.Tools, inspect.isfunction) if not name.startswith("_")]
        self.assertEqual(len(public), 15)


if __name__ == "__main__":
    unittest.main()
