"""BUY / ADD purchases of the engine in the plan: they are ``existing_position_actions`` (folded into the capital state before any new entry), they are never lost, and the
capital summary never claims that candidates were planned when only such purchases exist (synthetic data only)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_portfolio_risk_guards as guards  # noqa: E402
from portfolio_action_planner import render_plan_de  # noqa: E402

existing, promo, build, exposure = guards.existing, guards.promo, guards.build, guards.exposure


def buy(symbol, sid, value=8_000.0):
    return existing(symbol, sid, "swing", 0, None, "BUY", None, entry_purchase_value_eur=value, entry_recommended_quantity=80, entry_current_price_eur=100.0)


def add(symbol, sid, value=3_000.0):
    return existing(symbol, sid, "swing", 10, 100.0, "ADD", None, add_purchase_value_eur=value, add_recommended_quantity=30, add_current_price_eur=100.0)


class PurchaseTests(unittest.TestCase):
    def test_buy_and_add_without_a_promotion_are_actions_not_ranked_entries(self) -> None:
        result = build(guards=False, promos=[], entries=[buy("NEW", 6)], existing_results=[add("HLD1", 101)])
        self.assertEqual([(a["symbol"], a["action"], a["estimated_purchase_eur"]) for a in result["existing_position_actions"]], [("HLD1", "ADD", 3_000.0), ("NEW", "BUY", 8_000.0)])
        self.assertEqual((result["planned_entries"], result["deferred_entries"]), ([], []))
        self.assertEqual(result["post_action_state"]["planned_purchases_from_actions_eur"], 11_000.0)  # the purchases are in the capital state

    def test_the_summary_does_not_claim_planned_candidates_when_only_purchases_exist(self) -> None:
        result = build(guards=False, promos=[], entries=[buy("NEW", 6)], existing_results=[add("HLD1", 101)])
        self.assertEqual(result["capital"]["limiting_guard"], "NO_RANKED_ENTRY_PLANNED")  # says ranked entries; the purchases are in the overview
        self.assertGreater(result["capital"]["planned_capital_total_eur"], 0)  # the purchases are counted in the capital ...
        self.assertEqual(result["final_simulated_state"]["planned_entries_total_eur"], 0)  # ... but they are no planned entry

    def test_the_summary_still_reports_planned_candidates(self) -> None:
        result = build(guards=False, cash=300_000.0)  # enough capital for both promotions
        self.assertEqual(len(result["planned_entries"]), 2)
        self.assertEqual(result["capital"]["limiting_guard"], "ALL_READY_CANDIDATES_PLANNED")

    def test_every_buy_and_add_is_planned_or_blocked_and_none_is_lost(self) -> None:
        entries = [buy("NEW", 6), buy("NEW2", 7, 9_000.0)]
        result = build(held=[exposure(101, "HLD1", 25_000.0)], sectors={6: "TECH", 7: "ENERGY", 101: "TECH"}, promos=[], entries=entries, existing_results=[add("HLD1", 101)])
        actions = result["existing_position_actions"]
        self.assertEqual(sorted(a["symbol"] for a in actions), ["HLD1", "NEW", "NEW2"])
        blocked = [a["symbol"] for a in actions if a.get("plan_block_reason")]
        folded = [a["symbol"] for a in actions if not a.get("plan_block_reason")]
        self.assertEqual(blocked, ["NEW"])  # the sector guard stops it (25k + 3k + 8k of 100k > 30 %)
        self.assertEqual(sorted(blocked + folded), ["HLD1", "NEW", "NEW2"])
        self.assertEqual(result["post_action_state"]["planned_purchases_from_actions_eur"], 3_000.0 + 9_000.0)  # only the folded ones


class OverviewTests(unittest.TestCase):
    """``entries_overview``: one view of everything the plan buys or holds back, next to the unchanged PROMOTE lists."""

    def scenario(self):
        entries = [buy("CVX", 6), buy("IUSQ", 7, 9_000.0), buy("NOVAL", 8, 5_000.0)]
        entries[2]["decision"]["entry_purchase_value_eur"] = None
        return build(held=[exposure(101, "HLD1", 25_000.0)], sectors={6: "ENERGY", 7: "TECH", 101: "TECH"}, promos=[], entries=entries, existing_results=[add("HLD1", 101)])

    def test_planned_and_deferred_purchases_are_listed_with_kind_and_reason(self) -> None:
        overview = self.scenario()["entries_overview"]
        self.assertEqual([(i["kind"], i["symbol"], i["capital_eur"]) for i in overview["planned"]], [("ADD", "HLD1", 3_000.0), ("BUY", "CVX", 8_000.0)])
        self.assertEqual([(i["kind"], i["symbol"], i["reason"]) for i in overview["deferred"]], [("BUY", "IUSQ", guards.SECTOR_BLOCK), ("BUY", "NOVAL", "PURCHASE_VALUE_UNAVAILABLE")])

    def test_the_totals_and_the_promote_counters_are_separate(self) -> None:
        counts = self.scenario()["entries_overview"]["counts"]
        self.assertEqual((counts["planned_total"], counts["deferred_total"]), (2, 2))
        self.assertEqual((counts["planned_buy"], counts["planned_add"], counts["deferred_buy"], counts["deferred_add"]), (1, 1, 2, 0))
        self.assertEqual((counts["planned_promote"], counts["deferred_promote"]), (0, 0))

    def test_the_promote_lists_keep_their_meaning_and_join_the_overview(self) -> None:
        result = build(guards=False, cash=300_000.0)
        self.assertTrue(result["planned_entries"])
        self.assertFalse(any("kind" in e for e in result["planned_entries"]))  # the plan's own lists are not changed
        overview = result["entries_overview"]
        promote = [i for i in overview["planned"] if i["kind"] == "PROMOTE"]
        self.assertEqual([i["symbol"] for i in promote], [e["symbol"] for e in result["planned_entries"]])
        self.assertEqual(overview["counts"]["planned_promote"], len(result["planned_entries"]))
        self.assertEqual(overview["counts"]["planned_total"], len(overview["planned"]))

    def test_no_decision_is_lost_or_listed_twice(self) -> None:
        result = self.scenario()
        overview = result["entries_overview"]
        listed = [i["security_id"] for i in overview["planned"] + overview["deferred"]]
        decided = [a["security_id"] for a in result["existing_position_actions"] if a["action"] in ("BUY", "ADD")]
        self.assertEqual(sorted(listed), sorted(decided))
        self.assertEqual(len(listed), len(set(listed)))

    def test_the_funding_and_the_state_are_the_ones_without_the_overview(self) -> None:
        result = self.scenario()
        self.assertEqual(result["post_action_state"]["planned_purchases_from_actions_eur"], 3_000.0 + 8_000.0)  # the overview only re-sorts what the planner folded

    def test_the_label_names_ranked_entries_and_says_nothing_planned_only_when_nothing_is(self) -> None:
        self.assertEqual(self.scenario()["capital"]["limiting_guard"], "NO_RANKED_ENTRY_PLANNED")
        nothing = build(guards=False, promos=[], entries=[], existing_results=[])
        self.assertEqual(nothing["capital"]["limiting_guard"], "NO_ENTRY_PLANNED")
        self.assertEqual(nothing["entries_overview"]["counts"]["planned_total"], 0)

    def test_the_rendered_plan_does_not_say_none_when_purchases_are_planned(self) -> None:
        text = render_plan_de(self.scenario())
        self.assertIn("keine rangierten Entries (PROMOTE); 1 BUY und 1 ADD siehe oben", text)

    def test_the_mcp_view_has_the_overview_and_keeps_the_promote_counters_named(self) -> None:
        inner = {"evaluation_as_of": guards.AS_OF, "global_status": "AVAILABLE", "portfolio_action_plan": self.scenario(), "action_summary": {}}
        plan = guards.oc.compact_orchestrator_result(inner)["entry_plan"]
        self.assertEqual((plan["planned_count"], plan["deferred_count"]), (0, 0))  # ranked PROMOTE entries, as before
        view = plan["overview"]
        self.assertEqual((view["counts"]["planned_total"], view["counts"]["deferred_total"]), (2, 2))
        self.assertEqual([(i["kind"], i["symbol"]) for i in view["planned"]], [("ADD", "HLD1"), ("BUY", "CVX")])
        self.assertEqual([(i["kind"], i["symbol"], i["reason"]) for i in view["deferred"]][0], ("BUY", "IUSQ", guards.SECTOR_BLOCK))
        self.assertLess(len(json.dumps(view)), 2_500)  # small enough for Jan


if __name__ == "__main__":
    unittest.main()
