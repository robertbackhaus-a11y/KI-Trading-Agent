"""Compact view of run_trading_orchestrator (MCP default): all portfolio-status fields present, large duplicate blocks absent, every value identical to the
full result, the position engine and the planner kept apart, no database access, no orders.  Synthetic data only."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_portfolio_action_planner as planner_tests  # noqa: E402  (module import: its test classes must not be collected here a second time)

cand, context, existing, plan, promo = planner_tests.cand, planner_tests.context, planner_tests.existing, planner_tests.plan, planner_tests.promo

spec = importlib.util.spec_from_file_location("orchestrator_compact_under_test", ROOT / "mcp-tools" / "orchestrator_compact.py")
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)

CORE = ("symbol", "action", "action_quantity", "action_quantity_basis", "priority", "reason_codes")
DROPPED_TOP_LEVEL = ("existing_position_results", "entry_candidate_results", "promotion_results", "presentation_summary", "rendered_summary_de",
                     "highest_priority_actions", "next_review_items", "portfolio_context", "capital_state_summary", "data_quality_summary", "portfolio_action_plan")


def real_result():
    """A genuine orchestrator result (decision engine + planner) built from the synthetic fixtures of the planner tests."""
    base = planner_tests.OrchestratorIntegrationTests("test_plan_is_part_of_the_result_and_promote_stays_a_candidate")
    base.setUp()
    changes_before = base.conn.total_changes
    result = base.run_orchestrator().primitive()
    return base, result, base.conn.total_changes - changes_before


class CompactProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base, self.full, self.changes = real_result()
        self.addCleanup(self.base.tearDown)
        self.compact = oc.compact_orchestrator_result(self.full)

    def test_the_portfolio_status_core_fields_are_present(self) -> None:
        c, summary = self.compact, self.full["presentation_summary"]
        for key in ("total_market_value_eur", "swing_pct", "long_term_pct", "cash_available_eur", "allocation_warnings"):
            self.assertEqual(c["portfolio"][key], summary["portfolio"][key], key)
        self.assertEqual([{k: a[k] for k in CORE} for a in c["position_engine"]["actions"]], [{k: a[k] for k in CORE} for a in summary["actions"]])
        for key in ("promote_count", "promote_symbols", "keep_watching_count", "reject_count", "data_insufficient_count"):
            self.assertIn(key, c["promotion"])
        entry = c["entry_plan"]
        for key in ("planned_entries", "planned_count", "deferred", "deferred_count", "stop_reason", "remaining_buying_capacity_eur", "entry_status_counts"):
            self.assertIn(key, entry)
        for key in ("gross_eur", "estimated_tax_eur", "net_conservative_eur"):
            self.assertIn(key, c["proceeds"])
        self.assertIs(c["simulation_only"], True)
        self.assertIs(c["orders_created"], False)
        self.assertIn("final_simulated", c["capital"])

    def test_large_duplicate_blocks_and_rendered_texts_are_absent(self) -> None:
        for key in DROPPED_TOP_LEVEL:
            self.assertNotIn(key, self.compact, key)
        body = json.dumps({k: v for k, v in self.compact.items() if k != "omitted_for_context"})
        for needle in ("rendered_de", "rendered_summary_de", "methodology", "exposures", "valuation_quality", "strategy_quality", "keep_watching_symbols",
                       "highest_priority_actions", "next_review_items", "entry_candidates"):
            self.assertNotIn(needle, body, needle)
        for name in ("rendered_summary_de", "highest_priority_actions", "next_review_items", "existing_position_results"):
            self.assertIn(name, self.compact["omitted_for_context"])
        self.assertIn("detail=true", self.compact["hint"])

    def test_values_are_identical_to_the_full_result(self) -> None:
        plan_, summary = self.full["portfolio_action_plan"], self.full["presentation_summary"]
        c = self.compact
        self.assertEqual(c["promotion"], {k: v for k, v in summary["promotions"].items() if k != "keep_watching_symbols"})
        self.assertEqual(c["capital"]["final_simulated"], plan_["final_simulated_state"])
        self.assertEqual(c["capital"]["after_actions"], {k: plan_["post_action_state"][k] for k in c["capital"]["after_actions"]})
        self.assertEqual(c["entry_plan"]["entry_status_counts"], plan_["entry_summary"])
        for got, src in zip(c["entry_plan"]["planned_entries"], plan_["planned_entries"]):
            self.assertEqual(got, {k: src[k] for k in got})
        tax = plan_["tax_estimate"]
        self.assertEqual((c["proceeds"]["gross_eur"], c["proceeds"]["estimated_tax_eur"], c["proceeds"]["net_conservative_eur"]),
                         (tax["gross_proceeds_total_eur"], tax["estimated_tax_total_conservative"], tax["net_proceeds_total_conservative"]))
        planner_actions = {(a["security_id"], a["action"]): a for a in plan_["existing_position_actions"]}
        for src, got in zip(summary["actions"], c["position_engine"]["actions"]):
            p = planner_actions[(src["security_id"], src["action"])]
            for key in ("gross_proceeds_eur", "estimated_tax_eur_conservative", "estimated_net_proceeds_eur_conservative"):
                self.assertEqual(got[key], p[key], key)
        self.assertEqual(c["action_summary"], self.full["action_summary"])
        self.assertEqual(c["issues"]["plan_warnings"], plan_["warnings"])
        self.assertEqual(c["positions"], [{k: e[k] for k in ("symbol", "strategy", "quantity", "market_value_eur")} for e in self.full["portfolio_context"]["exposures"]])

    def test_position_engine_and_planner_are_kept_apart(self) -> None:
        engine, entry = self.compact["position_engine"], self.compact["entry_plan"]
        self.assertEqual((engine["engine_buy_count"], engine["engine_add_count"]), (0, 0))
        self.assertGreaterEqual(entry["planned_count"], 1)  # no engine BUY/ADD, but a planned new entry
        self.assertIn("does not mean", engine["note"])
        self.assertIn("entry_plan", engine["note"])

    def test_no_database_access_and_no_orders(self) -> None:
        self.assertEqual(self.changes, 0)  # the orchestrator run did not write
        before = self.base.conn.total_changes
        oc.compact_orchestrator_result(self.full)
        self.assertEqual(self.base.conn.total_changes, before)
        source = (ROOT / "mcp-tools" / "orchestrator_compact.py").read_text(encoding="utf-8")
        for forbidden in ("sqlite3", "import socket", "urllib", "subprocess", ".execute(", "open("):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertIs(self.compact["orders_created"], False)
        self.assertIs(self.compact["entry_plan"]["orders_created"], False)

    def test_input_is_not_modified_and_output_is_json(self) -> None:
        before = copy.deepcopy(self.full)
        oc.compact_orchestrator_result(self.full)
        self.assertEqual(self.full, before)
        self.assertEqual(json.loads(json.dumps(self.compact)), self.compact)

    def test_envelope_helper_keeps_ok_and_operation_and_ignores_failures(self) -> None:
        response = {"ok": True, "operation": "run_trading_orchestrator", "result": self.full}
        compact = oc.compact_orchestrator_response(response)
        self.assertEqual((compact["ok"], compact["operation"], compact["detail"]), (True, "run_trading_orchestrator", False))
        self.assertIsNone(oc.compact_orchestrator_response({"ok": False, "error": "ORCHESTRATOR_FAILED"}))
        self.assertIsNone(oc.compact_orchestrator_response("not a dict"))


class DeferredAndUnavailableTests(unittest.TestCase):
    def synthetic(self, planner: dict) -> dict:
        _base, full, _ = real_result()
        _base.tearDown()
        full = copy.deepcopy(full)
        full["portfolio_action_plan"] = planner
        return full

    def test_deferred_entries_are_grouped_by_reason_with_ranks_and_the_stop_reason(self) -> None:
        # deployable 600 EUR: BIG (400) is planned, MID (250) stops the allocation, TINY (50) must not leapfrog, LOW waits for its trigger
        promos = [promo(1, "BIG"), promo(2, "MID"), promo(3, "TINY"), promo(4, "LOW")]
        snaps = {1: cand(1, "BIG", price=400.0, momentum=60.0), 2: cand(2, "MID", price=250.0, momentum=40.0), 3: cand(3, "TINY", price=50.0, momentum=30.0), 4: cand(4, "LOW", price=10.0, momentum=5.0)}
        planner = plan(portfolio=context(total=100_000.0, swing=20_000.0, cash=10_600.0), promos=promos, snaps=snaps)
        compact = oc.compact_orchestrator_result(self.synthetic(planner))["entry_plan"]
        self.assertEqual([(p["rank"], p["symbol"], p["quantity"]) for p in compact["planned_entries"]], [(1, "BIG", 1.0)])
        self.assertEqual(compact["deferred"], [{"reason": "STOPPED_CAPITAL_EXHAUSTED", "ranks": [2, 3], "symbols": ["MID", "TINY"]}])
        self.assertEqual((compact["deferred_count"], compact["stop_reason"]), (2, "STOPPED_CAPITAL_EXHAUSTED"))
        self.assertEqual(compact["not_ready_candidates"], {"WAIT_FOR_TRIGGER": ["LOW"]})
        self.assertEqual(compact["remaining_buying_capacity_eur"], planner["final_simulated_state"]["deployable_cash_eur"])
        self.assertEqual(compact["planned_total_eur"], planner["final_simulated_state"]["planned_entries_total_eur"])

    def test_sales_carry_gross_tax_and_net(self) -> None:
        records = [existing("SEL", 9, "swing", 100, 100.0, "SELL", 100)]
        planner = plan(promos=[], snaps={}, existing_results=records)
        compact = oc.compact_orchestrator_result(self.synthetic(planner))
        tax = planner["tax_estimate"]
        self.assertEqual((compact["proceeds"]["gross_eur"], compact["proceeds"]["net_conservative_eur"]), (tax["gross_proceeds_total_eur"], tax["net_proceeds_total_conservative"]))

    def test_an_unavailable_planner_is_reported_and_the_rest_is_kept(self) -> None:
        planner = {"status": "UNAVAILABLE", "simulation_only": True, "orders_created": False, "warnings": ["PORTFOLIO_ACTION_PLAN_ERROR:RuntimeError"]}
        compact = oc.compact_orchestrator_result(self.synthetic(planner))
        self.assertEqual(compact["entry_plan"]["status"], "UNAVAILABLE")
        self.assertIn("PORTFOLIO_ACTION_PLAN_ERROR:RuntimeError", compact["entry_plan"]["warnings"])
        self.assertTrue(compact["position_engine"]["actions"])  # the position engine result is unaffected
        self.assertEqual(compact["global_status"], "AVAILABLE")

    def test_empty_or_partial_input_never_raises(self) -> None:
        for data in ({}, {"presentation_summary": None}, {"portfolio_action_plan": {"status": "AVAILABLE"}}, {"portfolio_action_plan": {"planned_entries": [None, 3]}}):
            compact = oc.compact_orchestrator_result(data)
            self.assertEqual(compact["compact_version"], oc.COMPACT_VERSION)
            json.dumps(compact)


if __name__ == "__main__":
    unittest.main()
