"""Planner net-proceeds view: tax on the realized gain only, conservative planning basis, indicative offset view."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from portfolio_action_planner import build_portfolio_action_plan, render_plan_de  # noqa: E402
from strategy_config import StrategyConfig, TaxConfig  # noqa: E402
import test_portfolio_action_planner as planner_tests  # noqa: E402  (module import: its tests are not collected twice)
from test_portfolio_action_planner import cand, existing, promo  # noqa: E402
from test_swing_entry_recommendation import context  # noqa: E402

RATE = 0.25 * 1.055  # 26.375 %


def cb(mapping, currency="EUR"):
    return {sid: {"avg_cost": avg, "currency": currency} for sid, avg in mapping.items()}


def tplan(*, existing_results=(), promos=(), snaps=None, cost_basis=None, portfolio=None, config=None):
    snapshots = snaps if snaps is not None else {int(p["security_id"]): cand(int(p["security_id"]), p["symbol"]) for p in promos}
    return build_portfolio_action_plan(
        portfolio=portfolio or context(total=100_000.0, swing=20_000.0, cash=30_000.0),
        existing_position_results=list(existing_results), promotion_results=list(promos),
        candidate_snapshots=snapshots, config=config, position_cost_basis=cost_basis,
    )


def action(result, symbol):
    return next(a for a in result["existing_position_actions"] if a["symbol"] == symbol)


class TaxConfigTests(unittest.TestCase):
    def test_combined_rate_is_26_375_percent_without_church_tax(self) -> None:
        tax = TaxConfig()
        self.assertEqual((tax.capital_gains_tax_rate, tax.solidarity_surcharge_rate, tax.church_tax_rate), (0.25, 0.055, 0.0))
        self.assertAlmostEqual(tax.combined_rate, 0.26375, places=12)
        self.assertEqual(StrategyConfig().tax, tax)

    def test_church_tax_is_rejected_because_it_is_not_modelled(self) -> None:
        with self.assertRaises(ValueError):
            TaxConfig(church_tax_rate=0.08)
        for bad in (-0.1, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                TaxConfig(capital_gains_tax_rate=bad)


class RealizedGainTaxTests(unittest.TestCase):
    def test_tax_applies_to_the_gain_not_to_the_gross_proceeds(self) -> None:
        result = tplan(existing_results=[existing("GAI", 5, "swing", 100, 100.0, "SELL", 100)], cost_basis=cb({5: 60.0}))
        a = action(result, "GAI")
        self.assertEqual(a["gross_proceeds_eur"], 10_000.0)
        self.assertEqual(a["estimated_cost_basis_eur"], 6_000.0)
        self.assertEqual(a["estimated_realized_gain_eur"], 4_000.0)
        self.assertAlmostEqual(a["estimated_tax_eur_conservative"], 4_000.0 * RATE, places=9)
        self.assertNotAlmostEqual(a["estimated_tax_eur_conservative"], 10_000.0 * RATE, places=2)
        self.assertAlmostEqual(a["estimated_net_proceeds_eur_conservative"], 10_000.0 - 4_000.0 * RATE, places=9)
        self.assertEqual(a["tax_estimate_quality"], "estimated_from_average_cost")

    def test_loss_has_no_negative_tax_and_creates_no_extra_cash(self) -> None:
        result = tplan(existing_results=[existing("LOS", 5, "swing", 100, 100.0, "SELL", 100)], cost_basis=cb({5: 150.0}))
        a = action(result, "LOS")
        self.assertEqual(a["estimated_realized_gain_eur"], -5_000.0)
        self.assertEqual(a["estimated_tax_eur_conservative"], 0.0)
        self.assertEqual(a["estimated_tax_eur_with_plan_offset"], 0.0)
        self.assertEqual(a["estimated_net_proceeds_eur_conservative"], 10_000.0)
        self.assertEqual(result["post_action_state"]["cash_after_actions_eur"], 30_000.0 + 10_000.0)  # never more than gross

    def test_partial_trim_uses_the_quantity_sold(self) -> None:
        result = tplan(existing_results=[existing("TRM", 5, "swing", 100, 200.0, "TRIM", 25)], cost_basis=cb({5: 120.0}))
        a = action(result, "TRM")
        self.assertEqual((a["gross_proceeds_eur"], a["estimated_cost_basis_eur"], a["estimated_realized_gain_eur"]), (5_000.0, 3_000.0, 2_000.0))

    def test_all_required_per_action_and_total_fields_exist(self) -> None:
        result = tplan(existing_results=[existing("GAI", 5, "swing", 100, 100.0, "SELL", 100)], cost_basis=cb({5: 60.0}))
        for key in ("gross_proceeds_eur", "estimated_cost_basis_eur", "estimated_realized_gain_eur", "estimated_tax_eur_conservative",
                    "estimated_tax_eur_with_plan_offset", "estimated_net_proceeds_eur_conservative", "estimated_net_proceeds_eur_with_plan_offset"):
            self.assertIn(key, action(result, "GAI"))
        tax = result["tax_estimate"]
        for key in ("gross_proceeds_total_eur", "estimated_tax_total_conservative", "estimated_tax_total_with_plan_offset",
                    "net_proceeds_total_conservative", "net_proceeds_total_with_plan_offset"):
            self.assertIn(key, tax)
        self.assertEqual(tax["tax_estimate_quality"], "estimated_from_average_cost")
        self.assertFalse(tax["fifo_implemented"])
        self.assertEqual(tax["cost_basis_method"], "average_cost")
        self.assertEqual(tax["planning_basis"], "net_proceeds_total_conservative")
        self.assertEqual(sorted(tax["unknown_inputs"]), sorted(["loss_pot", "tax_free_allowance", "realized_gains_and_losses_current_year"]))
        self.assertAlmostEqual(tax["parameters"]["combined_rate_on_realized_gain"], 0.26375, places=12)


class ConservativeVersusOffsetTests(unittest.TestCase):
    def setUp(self) -> None:
        # gain +4,000 (A), gain +1,000 (B), loss -2,000 (C)
        self.records = [
            existing("AAA", 5, "swing", 100, 100.0, "SELL", 100),   # gross 10,000, cost 6,000
            existing("BBB", 6, "swing", 50, 100.0, "TRIM", 50),     # gross 5,000,  cost 4,000
            existing("CCC", 7, "swing", 100, 100.0, "SELL", 100),   # gross 10,000, cost 12,000
        ]
        self.basis = cb({5: 60.0, 6: 80.0, 7: 120.0})

    def test_conservative_view_taxes_each_gain_without_offset(self) -> None:
        tax = tplan(existing_results=self.records, cost_basis=self.basis)["tax_estimate"]
        self.assertAlmostEqual(tax["estimated_tax_total_conservative"], 5_000.0 * RATE, places=9)
        self.assertAlmostEqual(tax["net_proceeds_total_conservative"], 25_000.0 - 5_000.0 * RATE, places=9)

    def test_offset_view_nets_losses_within_the_plan_and_spreads_the_tax_over_the_gains(self) -> None:
        result = tplan(existing_results=self.records, cost_basis=self.basis)
        tax = result["tax_estimate"]
        self.assertAlmostEqual(tax["estimated_tax_total_with_plan_offset"], 3_000.0 * RATE, places=9)
        self.assertAlmostEqual(tax["net_proceeds_total_with_plan_offset"], 25_000.0 - 3_000.0 * RATE, places=9)
        a, b, c = action(result, "AAA"), action(result, "BBB"), action(result, "CCC")
        self.assertAlmostEqual(a["estimated_tax_eur_with_plan_offset"], 3_000.0 * RATE * 4_000.0 / 5_000.0, places=9)
        self.assertAlmostEqual(b["estimated_tax_eur_with_plan_offset"], 3_000.0 * RATE * 1_000.0 / 5_000.0, places=9)
        self.assertEqual(c["estimated_tax_eur_with_plan_offset"], 0.0)
        self.assertAlmostEqual(sum(x["estimated_tax_eur_with_plan_offset"] for x in (a, b, c)), tax["estimated_tax_total_with_plan_offset"], places=9)
        self.assertAlmostEqual(sum(x["estimated_tax_eur_conservative"] for x in (a, b, c)), tax["estimated_tax_total_conservative"], places=9)
        self.assertLess(tax["estimated_tax_total_with_plan_offset"], tax["estimated_tax_total_conservative"])

    def test_losses_larger_than_gains_cancel_the_offset_tax_but_not_the_conservative_tax(self) -> None:
        records = [existing("GAI", 5, "swing", 100, 100.0, "SELL", 100), existing("LOS", 6, "swing", 100, 100.0, "SELL", 100)]
        tax = tplan(existing_results=records, cost_basis=cb({5: 90.0, 6: 150.0}))["tax_estimate"]
        self.assertEqual(tax["estimated_tax_total_with_plan_offset"], 0.0)
        self.assertAlmostEqual(tax["estimated_tax_total_conservative"], 1_000.0 * RATE, places=9)

    def test_capital_planning_uses_the_conservative_net_proceeds_only(self) -> None:
        result = tplan(existing_results=self.records, cost_basis=self.basis)
        post, tax = result["post_action_state"], result["tax_estimate"]
        self.assertEqual(post["proceeds_basis"], "net_conservative")
        self.assertTrue(post["taxes_modelled"])
        self.assertFalse(post["fees_modelled"])
        self.assertEqual(post["expected_proceeds_gross_eur"], 25_000.0)
        self.assertAlmostEqual(post["expected_proceeds_net_conservative_eur"], tax["net_proceeds_total_conservative"], places=9)
        self.assertAlmostEqual(post["cash_after_actions_eur"], 30_000.0 + tax["net_proceeds_total_conservative"], places=9)
        # the indicative view is display only and never becomes available cash
        self.assertAlmostEqual(tax["indicative_cash_after_actions_with_plan_offset_eur"], 30_000.0 + tax["net_proceeds_total_with_plan_offset"], places=9)
        self.assertGreater(tax["indicative_cash_after_actions_with_plan_offset_eur"], post["cash_after_actions_eur"])

    def test_position_value_falls_by_gross_while_cash_rises_by_net(self) -> None:
        with_tax = tplan(existing_results=self.records, cost_basis=self.basis)["post_action_state"]
        gross = tplan(existing_results=self.records)["post_action_state"]
        self.assertEqual(with_tax["total_market_value_eur"], gross["total_market_value_eur"])
        self.assertEqual(with_tax["swing_value_eur"], 20_000.0 - 25_000.0)
        self.assertEqual(with_tax["swing_pct"], gross["swing_pct"])  # weights are unaffected by tax
        self.assertLess(with_tax["cash_after_actions_eur"], gross["cash_after_actions_eur"])


class CostBasisAvailabilityTests(unittest.TestCase):
    def test_unavailable_cost_basis_taxes_the_whole_proceeds_as_an_upper_bound(self) -> None:
        for label, basis in (("missing entry", {}), ("no avg_cost", {5: {"avg_cost": None, "currency": "EUR"}}), ("not EUR", cb({5: 60.0}, currency="USD"))):
            with self.subTest(label):
                result = tplan(existing_results=[existing("UNK", 5, "swing", 100, 100.0, "SELL", 100)], cost_basis=basis)
                a = action(result, "UNK")
                self.assertIsNone(a["estimated_cost_basis_eur"])
                self.assertIsNone(a["estimated_realized_gain_eur"])
                self.assertAlmostEqual(a["estimated_tax_eur_conservative"], 10_000.0 * RATE, places=9)
                self.assertEqual(a["tax_estimate_quality"], "cost_basis_unavailable_upper_bound")
                self.assertEqual(result["tax_estimate"]["tax_estimate_quality"], "cost_basis_unavailable_upper_bound")
                self.assertIn("TAX_COST_BASIS_UNAVAILABLE_UPPER_BOUND_USED", result["warnings"])

    def test_mixed_quality_is_reported_as_partial(self) -> None:
        records = [existing("GAI", 5, "swing", 100, 100.0, "SELL", 100), existing("UNK", 6, "swing", 10, 100.0, "SELL", 10)]
        result = tplan(existing_results=records, cost_basis=cb({5: 60.0}))
        self.assertEqual(result["tax_estimate"]["tax_estimate_quality"], "partial_cost_basis_unavailable")

    def test_without_cost_basis_input_no_estimate_is_made_and_proceeds_stay_gross(self) -> None:
        result = tplan(existing_results=[existing("GAI", 5, "swing", 100, 100.0, "SELL", 100)])
        self.assertEqual(result["tax_estimate"]["tax_estimate_quality"], "not_estimated")
        self.assertEqual(result["post_action_state"]["proceeds_basis"], "gross")
        self.assertFalse(result["post_action_state"]["taxes_modelled"])
        self.assertEqual(result["post_action_state"]["cash_after_actions_eur"], 40_000.0)
        self.assertIn("PROCEEDS_GROSS_TAXES_FEES_NOT_MODELLED", result["warnings"])
        self.assertNotIn("estimated_tax_eur_conservative", action(result, "GAI"))

    def test_no_sales_means_no_tax_and_unchanged_capital(self) -> None:
        result = tplan(existing_results=[existing("HLD", 9, "swing", 10, 100.0, "HOLD")], cost_basis=cb({9: 50.0}))
        self.assertEqual(result["tax_estimate"]["tax_estimate_quality"], "not_applicable")
        self.assertEqual(result["tax_estimate"]["estimated_tax_total_conservative"], 0.0)
        self.assertEqual(result["post_action_state"]["cash_after_actions_eur"], 30_000.0)


class NetProceedsDrivePlanningTests(unittest.TestCase):
    def test_planned_entries_are_sized_from_net_cash_and_keep_the_reserve(self) -> None:
        promos = [promo(i, f"C{i}") for i in range(1, 7)]
        snaps = {i: cand(i, f"C{i}", price=1_000.0, momentum=60.0 - i) for i in range(1, 7)}
        records = [existing("SEL", 9, "swing", 100, 100.0, "SELL", 100)]
        portfolio = context(total=200_000.0, swing=20_000.0, cash=10_000.0)
        gross = tplan(existing_results=records, promos=promos, snaps=snaps, portfolio=portfolio)
        net = tplan(existing_results=records, promos=promos, snaps=snaps, portfolio=portfolio, cost_basis=cb({9: 0.0}))
        tax = net["tax_estimate"]["estimated_tax_total_conservative"]
        self.assertAlmostEqual(tax, 10_000.0 * RATE, places=9)
        spent_gross = sum(p["capital_eur"] for p in gross["planned_entries"])
        spent_net = sum(p["capital_eur"] for p in net["planned_entries"])
        self.assertLessEqual(spent_net, spent_gross)
        self.assertLess(spent_net, spent_gross)  # the lost cash costs at least one planned share here
        self.assertLessEqual(spent_net, 10_000.0 + 10_000.0 - tax - 10_000.0 + 1e-6)  # cash + net proceeds - reserve
        self.assertGreaterEqual(net["final_simulated_state"]["cash_eur"], 10_000.0 - 1e-6)
        self.assertAlmostEqual(net["final_simulated_state"]["cash_eur"], net["post_action_state"]["cash_after_actions_eur"] - spent_net, places=6)

    def test_result_is_deterministic_json_serializable_and_inputs_are_untouched(self) -> None:
        promos = [promo(1, "C1")]
        records = [existing("SEL", 9, "swing", 100, 100.0, "SELL", 100)]
        costs = cb({9: 40.0})
        before = copy.deepcopy((records, promos, costs))
        first = tplan(existing_results=records, promos=promos, cost_basis=costs)
        second = tplan(existing_results=records, promos=promos, cost_basis=costs)
        self.assertEqual((records, promos, costs), before)
        self.assertEqual(json.dumps(first, sort_keys=True, default=str), json.dumps(second, sort_keys=True, default=str))
        self.assertEqual(json.loads(json.dumps(first))["status"], "AVAILABLE")

    def test_rendering_shows_the_sales_table_totals_and_the_required_notes(self) -> None:
        result = tplan(existing_results=[existing("GAI", 5, "swing", 100, 100.0, "SELL", 100), existing("LOS", 6, "swing", 10, 100.0, "TRIM", 10)],
                       cost_basis=cb({5: 60.0, 6: 150.0}))
        text = render_plan_de(result)
        for fragment in (
            "### Verkäufe / Reduktionen",
            "| Position | Aktion | Stück | Brutto | Steuer geschätzt | Netto |",
            "| GAI | SELL | 100 |",
            "| LOS | TRIM | 10 |",
            "- Bruttoerlös gesamt:",
            "- Geschätzte Steuer (konservativ, ohne Verlustverrechnung):",
            "- Nettoerlös (konservativ, Planungsbasis):",
            "- Zusätzlich, nur Anzeige: Netto mit planinterner Verlustverrechnung",
            "Steuerschätzung auf Basis des Durchschnitts-Einstands, kein steuerliches FIFO.",
            "Verlusttopf, Freistellungsauftrag und bereits realisierte Jahresergebnisse sind nicht bekannt.",
            "Gebühren nicht berücksichtigt",
        ):
            self.assertIn(fragment, text)
        self.assertNotIn("Obergrenze", text)  # estimated_from_average_cost needs no upper-bound hint

    def test_rendering_flags_an_upper_bound_and_keeps_the_legacy_gross_text_without_cost_basis(self) -> None:
        unknown = render_plan_de(tplan(existing_results=[existing("UNK", 5, "swing", 100, 100.0, "SELL", 100)], cost_basis={}))
        self.assertIn("Obergrenze", unknown)
        legacy = render_plan_de(tplan(existing_results=[existing("GAI", 5, "swing", 100, 100.0, "SELL", 100)]))
        self.assertNotIn("### Verkäufe / Reduktionen", legacy)
        self.assertIn("Erlös brutto, ohne Steuern/Gebühren", legacy)

    def test_rendering_lists_purchases_outside_the_sales_table(self) -> None:
        result = tplan(existing_results=[existing("GAI", 5, "swing", 100, 100.0, "SELL", 100),
                                         existing("ADD", 6, "swing", 10, 100.0, "ADD", None, add_purchase_value_eur=1_000.0, add_recommended_quantity=10, add_current_price_eur=100.0)],
                       cost_basis=cb({5: 60.0}))
        text = render_plan_de(result)
        self.assertIn("   ADD: ADD 10 (ca. 1.000,00 EUR)", text)
        self.assertNotIn("| ADD |", text)


class OrchestratorTaxIntegrationTests(unittest.TestCase):
    setUp = planner_tests.OrchestratorIntegrationTests.setUp
    tearDown = planner_tests.OrchestratorIntegrationTests.tearDown
    run_orchestrator = planner_tests.OrchestratorIntegrationTests.run_orchestrator

    def test_orchestrator_passes_the_average_cost_of_each_position(self) -> None:
        held = self.snapshots[1]
        self.snapshots[1] = replace(held, position=replace(held.position, avg_cost=40.0, cost_basis_currency="EUR", currency="EUR"))
        plan_ = self.run_orchestrator().portfolio_action_plan
        a = plan_["existing_position_actions"][0]
        self.assertEqual(a["tax_estimate_quality"], "estimated_from_average_cost")
        self.assertEqual(a["estimated_cost_basis_eur"], a["quantity"] * 40.0)
        self.assertEqual(plan_["tax_estimate"]["tax_estimate_quality"], "estimated_from_average_cost")
        self.assertEqual(plan_["post_action_state"]["proceeds_basis"], "net_conservative")

    def test_existing_decisions_are_unchanged_by_the_tax_estimate(self) -> None:
        result = self.run_orchestrator()
        self.assertEqual(result.action_summary["TRIM"], 1)
        self.assertEqual(result.existing_position_results[0]["decision"]["action_quantity"], 2)


if __name__ == "__main__":
    unittest.main()
