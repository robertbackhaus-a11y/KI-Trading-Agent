"""MCP adapter context protection: the compact orchestrator output stays small, detail is untouched."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_server():
    try:
        import mcp  # noqa: F401  (only the Trading venv has it)
    except ImportError:
        return None
    spec = importlib.util.spec_from_file_location("mcp_server_under_test", ROOT / "mcp-tools" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_result():
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

    def test_compact_mode_projects_entry_candidates_and_keeps_the_rest_of_the_plan(self) -> None:
        result = sample_result()
        before = copy.deepcopy(result)
        slim = self.server._slim_orchestrator(result)
        self.assertEqual(result, before)  # input is never mutated
        plan = slim["result"]["portfolio_action_plan"]
        self.assertEqual(plan["entry_candidates"], [
            {"rank": 1, "symbol": "AAA", "entry_status": "ENTRY_READY", "entry_score": 40.0, "reason_codes": ["ENTRY_READY"]},
            {"rank": None, "symbol": "BBB", "entry_status": "BLOCKED_BY_DATA", "entry_score": None, "reason_codes": ["PRICE_UNAVAILABLE"]},
        ])
        self.assertIn("detail=true", plan["entry_candidates_note"])
        for key in ("status", "planned_entries", "rendered_de"):
            self.assertEqual(plan[key], before["result"]["portfolio_action_plan"][key])
        self.assertEqual(slim["detail"], False)

    def test_existing_heavy_lists_are_still_omitted_and_summary_text_is_kept(self) -> None:
        slim = self.server._slim_orchestrator(sample_result())["result"]
        for key in ("existing_position_results", "entry_candidate_results", "promotion_results"):
            self.assertTrue(slim[key]["omitted_for_context"], key)
        self.assertEqual(slim["rendered_summary_de"], "text")

    def test_result_without_a_plan_is_handled(self) -> None:
        result = {"ok": True, "result": {"existing_position_results": [], "entry_candidate_results": [], "promotion_results": []}}
        slim = self.server._slim_orchestrator(result)
        self.assertNotIn("portfolio_action_plan", slim["result"])
        self.assertEqual(self.server._slim_orchestrator({"ok": False}), {"ok": False})


if __name__ == "__main__":
    unittest.main()
