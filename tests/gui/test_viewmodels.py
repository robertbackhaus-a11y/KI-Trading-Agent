"""Dashboard and Planner view models on synthetic orchestrator results from the real orchestrator/planner (K, L)."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime

import support  # noqa: F401
from gui.viewmodels import portfolio as vmp
from gui.viewmodels.portfolio import DASH, build_dashboard, build_planner

try:
    import fixtures

    OPEN = fixtures.orchestrator_envelope(5_000.0)  # the candidate fits: planned
    BLOCKED = fixtures.orchestrator_envelope(40_000.0)  # the sector guard defers it
    SKIP = ""
except ImportError as exc:  # the trading modules are needed to build the fixtures
    OPEN = BLOCKED = None
    SKIP = str(exc)

NOW = datetime(2026, 10, 12, 14, 30, 5)


def rows(pairs):
    return dict(pairs)


@unittest.skipIf(OPEN is None, SKIP)
class DashboardViewModelTests(unittest.TestCase):
    # K
    def test_k_dashboard_from_a_synthetic_orchestrator_result(self) -> None:
        vm = build_dashboard(OPEN, {"ok": True}, NOW)
        self.assertTrue(vm.available)
        self.assertEqual(vm.refreshed, "12.10.2026 14:30:05")
        pf, pl = rows(vm.portfolio), rows(vm.planner)
        self.assertEqual(pf["Investierter Wert"], "100.000 €")
        self.assertEqual(pf["Swing"], "20,0 % (Ziel 30 % – 40 %)")
        self.assertEqual(pf["Long-Term"], "80,0 % (Ziel 60 % – 70 %)")
        self.assertTrue(pf["Cash"].startswith("30.000 €"))
        self.assertEqual(pf["External Funding verfügbar"], "Nein")
        self.assertEqual(pf["Datenbank"], "OK")
        self.assertEqual(pl["Planner-Status"], "AVAILABLE")
        self.assertEqual(pl["Simulation (simulation_only)"], "Ja")
        self.assertEqual(pl["Orders erzeugt (orders_created)"], "Nein")
        self.assertEqual(pl["Geplante / zurückgestellte Entries"], "1 / 0")
        self.assertEqual(pl["Externes Kapital für den Plan"], "nicht nötig (kein External Funding)")
        self.assertTrue(vm.simulation_only is True and vm.orders_created is False)
        self.assertEqual(vm.planned.headers[:2], ["Rang", "Symbol"])
        self.assertEqual([r[1] for r in vm.planned.rows], ["BBB"])
        self.assertEqual(vm.deferred.rows, [])
        self.assertTrue(any(text.startswith("Planner: ") for _, text in vm.issues))

    def test_blocked_candidate_is_a_deferred_entry_with_its_reason(self) -> None:
        vm = build_dashboard(BLOCKED, {"ok": True}, NOW)
        self.assertEqual(vm.planned.rows, [])
        self.assertEqual([r[1:] for r in vm.deferred.rows], [["BBB", "BLOCKED_BY_SECTOR_CONCENTRATION"]])
        self.assertEqual(vm.deferred.levels, ["blocker"])
        self.assertEqual(rows(vm.planner)["Geplante / zurückgestellte Entries"], "0 / 1")

    def test_external_funding_is_shown_from_the_result(self) -> None:
        envelope = copy.deepcopy(OPEN)
        plan = envelope["result"]["portfolio_action_plan"]
        plan["capital"]["external_funding_available"] = True
        plan["capital"]["external_funding_required_eur"] = 12_345.6
        vm = build_dashboard(envelope, {"ok": True}, NOW)
        self.assertEqual(rows(vm.portfolio)["External Funding verfügbar"], "Ja")
        self.assertEqual(rows(vm.planner)["Externes Kapital für den Plan"], "12.346 €")

    def test_global_issues_keep_their_severity(self) -> None:
        envelope = copy.deepcopy(OPEN)
        envelope["result"]["global_issues"] = [{"code": "X_BLOCK", "severity": "GLOBAL_BLOCKING"}, {"code": "Y_WARN", "severity": "NON_BLOCKING_WARNING", "symbol": "AAA"}]
        levels = {text: level for level, text in build_dashboard(envelope, None, NOW).issues}
        self.assertEqual(levels["X_BLOCK"], "blocker")
        self.assertEqual(levels["Y_WARN (AAA)"], "warning")

    def test_nothing_is_computed_the_values_are_those_of_the_result(self) -> None:
        envelope = copy.deepcopy(OPEN)
        ctx = envelope["result"]["portfolio_context"]
        ctx["total_market_value"], ctx["swing_weight_pct"] = 1234.6, 99.9  # deliberately inconsistent: the view must not "fix" it
        pf = rows(build_dashboard(envelope, None, NOW).portfolio)
        self.assertEqual((pf["Investierter Wert"], pf["Swing"].split(" (")[0]), ("1.235 €", "99,9 %"))

    def test_unavailable_inputs_give_an_error_not_an_exception(self) -> None:
        for bad in (None, {}, [], "text", {"ok": False, "message": "boom"}, {"ok": True}, {"ok": True, "result": []}, {"ok": True, "result": {"x": 1}}):
            vm = build_dashboard(bad, None, None)
            self.assertFalse(vm.available, bad)
            self.assertTrue(vm.error, bad)
            self.assertEqual(vm.refreshed, DASH)
        self.assertEqual(build_dashboard({"ok": False, "message": "boom"}).error, "boom")

    def test_malformed_values_show_a_dash(self) -> None:
        envelope = copy.deepcopy(OPEN)
        res = envelope["result"]
        res["portfolio_context"] = {"total_market_value": "n/a", "swing_weight_pct": None, "cash_available": True, "allocation_guardrails": "x"}
        res["portfolio_action_plan"]["planned_entries"] = [None, {"rank": 1, "quantity": "?", "capital_eur": [], "symbol": None}]
        res["action_summary"] = "nope"
        vm = build_dashboard(envelope, {}, NOW)
        pf = rows(vm.portfolio)
        self.assertEqual((pf["Investierter Wert"], pf["Swing"].split(" (")[0], pf["Cash"]), (DASH, DASH, DASH))
        self.assertEqual(vm.planned.rows, [["1", DASH, DASH, DASH, DASH, DASH, DASH, DASH]])
        self.assertEqual(rows(vm.planner)["Aktionen der Engine"], DASH)

    def test_formatting_helpers(self) -> None:
        self.assertEqual(vmp.fmt_eur(1234567.891), "1.234.568 €")
        self.assertEqual(vmp.fmt_eur(1234.5, 2), "1.234,50 €")
        self.assertEqual(vmp.fmt_pct(26.66), "26,7 %")
        self.assertEqual(vmp.fmt_qty(10.0), "10")
        self.assertEqual(vmp.fmt_qty(2.5), "2,50")
        for fn in (vmp.fmt_eur, vmp.fmt_pct, vmp.fmt_qty, vmp.fmt_score):
            self.assertEqual((fn(None), fn("x"), fn(True)), (DASH, DASH, DASH))


@unittest.skipIf(OPEN is None, SKIP)
class PlannerViewModelTests(unittest.TestCase):
    # L
    def test_l_planner_shows_sector_and_correlation_diagnostics(self) -> None:
        vm = build_planner(OPEN, NOW)
        self.assertTrue(vm.available)
        h = rows(vm.header)
        self.assertEqual((h["Simulation (simulation_only)"], h["Orders erzeugt (orders_created)"]), ("Ja", "Nein"))
        self.assertIn("HOLD 1", vm.counts)
        cand = next(r for r in vm.candidates.rows if r[1] == "BBB")
        header = vm.candidates.headers
        sector, status = cand[header.index("Sektor (aktuell → nach Kauf)")], cand[header.index("Sektor-Status")]
        self.assertEqual(status, "OK")
        self.assertRegex(sector, r"^TECH: 5,0 % → 14,\d % \(Limit 30 %\)$")
        self.assertRegex(cand[header.index("Stärkste Korrelation")], r"^AAA ρ=-?\d,\d\d \(\d+ Tage\)$")
        self.assertEqual(cand[header.index("Status")], "ENTRY_READY")

    def test_blocked_candidate_is_marked_and_explained(self) -> None:
        vm = build_planner(BLOCKED, NOW)
        header = vm.candidates.headers
        cand = next(r for r in vm.candidates.rows if r[1] == "BBB")
        self.assertEqual(cand[header.index("Sektor-Status")], "BLOCKED")
        self.assertRegex(cand[header.index("Sektor (aktuell → nach Kauf)")], r"^TECH: 40,0 % → 4\d,\d % \(Limit 30 %\)$")
        self.assertEqual(vm.candidates.levels[vm.candidates.rows.index(cand)], "blocker")
        self.assertEqual([r[-1] for r in vm.deferred.rows], ["BLOCKED_BY_SECTOR_CONCENTRATION"])
        self.assertEqual(vm.deferred.levels, ["blocker"])
        self.assertEqual(vm.planned.rows, [])

    def test_positions_table_lists_the_engine_actions_with_their_reasons(self) -> None:
        vm = build_planner(OPEN, NOW)
        self.assertEqual(vm.positions.headers[:3], ["Symbol", "Strategie", "Aktion"])
        row = next(r for r in vm.positions.rows if r[0] == "AAA")
        self.assertEqual((row[2], row[1]), ("HOLD", "swing"))
        self.assertIn("HOLD_TEST", row[6])

    def test_a_blocked_add_shows_its_plan_block(self) -> None:
        envelope = copy.deepcopy(OPEN)
        res = envelope["result"]
        record = next(r for r in res["existing_position_results"] if r["symbol"] == "AAA")
        record["decision"]["action"] = "ADD"
        res["portfolio_action_plan"]["existing_position_actions"] = [{
            "security_id": record["security_id"], "action": "ADD", "plan_block_reason": "BLOCKED_BY_SECTOR_CONCENTRATION",
            "risk": {"sector": {"status": "BLOCKED", "sector": "TECH", "current_weight_pct": 28.0, "projected_weight_pct": 31.4, "limit_pct": 30.0},
                     "correlation": {"status": "OK", "strongest": {"symbol": "BBB", "correlation": 0.31, "common_days": 250}}},
        }]
        vm = build_planner(envelope, NOW)
        row = next(r for r in vm.positions.rows if r[0] == "AAA")
        self.assertEqual(row[2], "ADD")
        self.assertEqual(row[7], "BLOCKED_BY_SECTOR_CONCENTRATION")
        self.assertIn("28,0 % → 31,4 %", row[8])
        self.assertIn("ρ=0,31", row[9])
        self.assertEqual(vm.positions.levels[vm.positions.rows.index(row)], "blocker")

    def test_candidates_without_risk_data_show_a_dash(self) -> None:
        envelope = copy.deepcopy(OPEN)
        for cand in envelope["result"]["portfolio_action_plan"]["entry_candidates"]:
            cand.pop("risk", None)
        vm = build_planner(envelope, NOW)
        self.assertTrue(all(DASH in row for row in vm.candidates.rows))

    def test_unavailable_planner_result(self) -> None:
        for bad in (None, {"ok": False, "error": "ORCHESTRATOR_FAILED", "message": "unable to open database file"}):
            vm = build_planner(bad)
            self.assertFalse(vm.available)
            self.assertTrue(vm.error)
        self.assertEqual(build_planner({"ok": False, "message": "m"}).error, "m")

    def test_no_action_buttons_or_order_data_in_the_model(self) -> None:
        vm = build_planner(OPEN, NOW)
        text = repr(vm).lower()
        self.assertNotIn("order ausführen", text)
        self.assertEqual(rows(vm.header)["Orders erzeugt (orders_created)"], "Nein")


if __name__ == "__main__":
    unittest.main()
