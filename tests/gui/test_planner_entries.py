"""Planned / deferred entries on Dashboard and Planner: the ranked entries of the entry evaluation plus the BUY / ADD purchases of the plan; every purchase is in exactly one list,
the counters are the table rows, nothing is lost (synthetic data only)."""

from __future__ import annotations

import copy
import unittest

import support  # noqa: F401
from gui.viewmodels import portfolio as vmp
from gui.viewmodels.portfolio import build_dashboard, build_planner

try:
    import fixtures
    from portfolio_action_planner import entries_overview

    BASE = fixtures.orchestrator_envelope(5_000.0)
    SKIP = ""
except ImportError as exc:
    BASE = None
    SKIP = str(exc)


def action(symbol, sid, kind, *, value=8_000.0, block=None):
    item = {"security_id": sid, "symbol": symbol, "action": kind, "strategy": "swing", "priority": "P3", "reasons": [f"{kind}_TEST"], "quantity": 80, "price_eur": 100.0, "estimated_purchase_eur": value}
    if block:
        item["plan_block_reason"] = block
    return item


def envelope(actions=(), planned=(), deferred=()):
    env = copy.deepcopy(BASE)
    plan = env["result"]["portfolio_action_plan"]
    plan["existing_position_actions"] = list(actions)
    plan["planned_entries"], plan["deferred_entries"] = list(planned), list(deferred)
    plan["entries_overview"] = entries_overview(plan["existing_position_actions"], plan["planned_entries"], plan["deferred_entries"])  # what the planner delivers
    return env


@unittest.skipIf(BASE is None, SKIP)
class EntriesTests(unittest.TestCase):
    def test_buy_and_add_make_the_planned_entries_non_empty(self) -> None:
        env = envelope([action("CVX", 14, "BUY"), action("LLY", 3, "ADD", value=2_234.12)], planned=[])
        for vm in (build_planner(env), build_dashboard(env)):
            self.assertEqual([r[1] for r in vm.planned.rows], ["CVX (BUY)", "LLY (ADD)"])
            self.assertEqual(vm.deferred.rows, [])
        planner = build_planner(env)
        self.assertEqual(planner.planned.rows[0][2:5], ["80", "100,00 €", "8.000 €"])  # quantity, price, capital of the purchase
        self.assertEqual(dict(build_dashboard(env).planner)["Geplante / zurückgestellte Entries"], "2 / 0")

    def test_a_blocked_purchase_is_deferred_with_its_reason(self) -> None:
        env = envelope([action("CVX", 14, "BUY"), action("IUSQ", 15, "BUY", block="BLOCKED_BY_CORRELATION")])
        for vm in (build_planner(env), build_dashboard(env)):
            self.assertEqual([r[1] for r in vm.planned.rows], ["CVX (BUY)"])
            self.assertEqual([r[1] for r in vm.deferred.rows], ["IUSQ (BUY)"])
            self.assertEqual(vm.deferred.rows[0][-1], "Zu stark korreliert")  # the reason is visible
            self.assertEqual(vm.deferred.levels, ["blocker"])

    def test_a_purchase_without_a_value_is_deferred_not_dropped(self) -> None:
        env = envelope([action("CVX", 14, "BUY", value=None)])
        vm = build_planner(env)
        self.assertEqual(vm.planned.rows, [])
        self.assertEqual([(r[1], r[-1]) for r in vm.deferred.rows], [("CVX (BUY)", "Kaufwert unbekannt")])

    def test_ranked_entries_come_first_and_purchases_follow(self) -> None:
        planned = [{"rank": 1, "security_id": 9, "symbol": "PRO", "quantity": 5, "price_eur": 10.0, "capital_eur": 50.0}]
        deferred = [{"rank": 2, "security_id": 8, "symbol": "DEF", "entry_score": 50.0, "reason": "BLOCKED_BY_SECTOR_CONCENTRATION"}]
        env = envelope([action("CVX", 14, "BUY"), action("IUSQ", 15, "ADD", block="BLOCKED_BY_CORRELATION")], planned, deferred)
        vm = build_planner(env)
        self.assertEqual([r[1] for r in vm.planned.rows], ["PRO", "CVX (BUY)"])
        self.assertEqual([r[1] for r in vm.deferred.rows], ["DEF", "IUSQ (ADD)"])

    def test_no_decision_is_lost_and_the_counters_are_the_rows(self) -> None:
        actions = [action("A", 1, "BUY"), action("B", 2, "ADD", block="BLOCKED_BY_SECTOR_CONCENTRATION"), action("C", 3, "BUY", value=None), action("D", 4, "ADD"),
                   {"security_id": 5, "symbol": "E", "action": "SELL", "estimated_proceeds_eur": 100.0}]  # sales are no entries
        env = envelope(actions)
        planned, deferred = vmp.entries_of(env["result"]["portfolio_action_plan"])
        ids = sorted(e["security_id"] for e in planned + deferred)
        self.assertEqual(ids, [1, 2, 3, 4])  # every BUY / ADD exactly once, the sale not at all
        self.assertEqual(len({e["security_id"] for e in planned} & {e["security_id"] for e in deferred}), 0)
        planner, dashboard = build_planner(env), build_dashboard(env)
        self.assertEqual((len(planner.planned.rows), len(planner.deferred.rows)), (len(planned), len(deferred)))
        self.assertEqual(dict(dashboard.planner)["Geplante / zurückgestellte Entries"], f"{len(planned)} / {len(deferred)}")
        self.assertEqual(len(dashboard.planned.rows) + len(dashboard.deferred.rows), 4)

    def test_the_pages_read_the_counts_of_the_plans_overview(self) -> None:
        env = envelope([action("A", 1, "BUY"), action("B", 2, "ADD", block="BLOCKED_BY_CORRELATION")])
        counts = env["result"]["portfolio_action_plan"]["entries_overview"]["counts"]
        planner = build_planner(env)
        self.assertEqual((len(planner.planned.rows), len(planner.deferred.rows)), (counts["planned_total"], counts["deferred_total"]))
        self.assertEqual(dict(build_dashboard(env).planner)["Geplante / zurückgestellte Entries"], f"{counts['planned_total']} / {counts['deferred_total']}")

    def test_malformed_actions_do_not_break_the_page(self) -> None:
        env = envelope([{"action": "BUY"}, {"action": "HOLD"}])
        env["result"]["portfolio_action_plan"]["entries_overview"]["planned"].extend([None, "x"])  # damaged items
        vm = build_planner(env)
        self.assertEqual(len(vm.planned.rows) + len(vm.deferred.rows), 1)  # the incomplete BUY is shown with dashes, the junk is skipped
        self.assertEqual(build_planner({"ok": True, "result": {"portfolio_action_plan": {"entries_overview": "broken"}}}).planned.rows, [])


if __name__ == "__main__":
    unittest.main()
