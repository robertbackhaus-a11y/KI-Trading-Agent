"""External funding in the Portfolio Action Planner (planning capability, not a booking).

``metadata.external_funding_available`` lifts ONLY the cash/reserve shortage.  Swing maximum, per-security weight limits, existing
position/campaign blocks, data quality and candidate status stay binding; ``false`` (default, missing key, old DB) behaves as before.
Synthetic data only.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_portfolio_action_planner as planner_tests  # noqa: E402  (module import: its test classes must not be collected here a second time)
import test_swing_add_recommendation as add_tests  # noqa: E402
import test_swing_entry_recommendation as entry_tests  # noqa: E402
from add_sizing import evaluate_add_recommendation  # noqa: E402
from analysis_contracts import AvailabilityStatus  # noqa: E402
from capital_state import read_external_funding_available, resolve_capital_state  # noqa: E402
from entry_sizing import evaluate_entry_recommendation, internal_external_split  # noqa: E402
from opportunity_view import _capital, _capital_fit  # noqa: E402
from portfolio_action_planner import (  # noqa: E402
    BLOCKED_BY_ALLOCATION,
    BLOCKED_BY_CONCENTRATION,
    BLOCKED_BY_DATA,
    BLOCKED_EXISTING_CAMPAIGN,
    BLOCKED_EXISTING_POSITION,
    ENTRY_READY,
    WAIT_FOR_TRIGGER,
    render_plan_de,
)
from strategy_config import SizingPolicyConfig, StrategyConfig  # noqa: E402
from test_capital_state import _create_db, manager  # noqa: E402

cand, existing, promo, status_of = planner_tests.cand, planner_tests.existing, planner_tests.promo, planner_tests.status_of
RESERVE = 10_000.0

spec = importlib.util.spec_from_file_location("orchestrator_compact_ef", ROOT / "mcp-tools" / "orchestrator_compact.py")
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)


def ctx(cash, *, external=False, total=100_000.0, swing=20_000.0, **kw):
    return replace(entry_tests.context(total=total, swing=swing, cash=cash, **kw), external_funding_available=external)


def run(cash, *, external=False, promos=None, snaps=None, existing_results=(), entries=(), config=None, total=100_000.0, swing=20_000.0):
    promos = promos if promos is not None else [promo(1, "A"), promo(2, "B"), promo(3, "C")]
    if snaps is None:
        momentum = {1: 30.0, 2: 50.0, 3: 40.0}
        snaps = {int(p["security_id"]): cand(int(p["security_id"]), p["symbol"], momentum=momentum.get(int(p["security_id"]), 40.0)) for p in promos}
    return planner_tests.plan(portfolio=ctx(cash, external=external, total=total, swing=swing), promos=promos, snaps=snaps,
                              existing_results=list(existing_results), entries=list(entries), config=config)


def planned(result):
    return [(p["symbol"], p["quantity"], p["capital_eur"]) for p in result["planned_entries"]]


# --------------------------------------------------------------------------------------------------------------------------------- A
class FlagFalseKeepsTodaysBehaviourTests(unittest.TestCase):
    def test_default_context_is_false_and_the_split_is_trivial(self) -> None:
        self.assertFalse(entry_tests.context().external_funding_available)
        self.assertFalse(add_tests.portfolio().external_funding_available)
        result = run(30_000.0)
        capital = result["capital"]
        self.assertFalse(capital["external_funding_available"])
        self.assertEqual(capital["external_funding_required_eur"], 0.0)
        self.assertEqual(capital["internal_capital_used_eur"], capital["planned_capital_total_eur"])
        self.assertTrue(all(p["external_funding_eur"] == 0.0 and p["internal_capital_eur"] == p["capital_eur"] for p in result["planned_entries"]))
        self.assertNotIn("EXTERNAL_FUNDING_ASSUMED_AVAILABLE", result["warnings"])
        self.assertNotIn("EXTERNAL_FUNDING_REQUIRED_FOR_PLAN", result["warnings"])
        self.assertNotIn("Externes Kapital", render_plan_de(result))

    def test_cash_reserve_stop_is_unchanged(self) -> None:
        result = run(30_000.0)
        self.assertEqual(planned(result), [("B", 111.0, 11_100.0), ("C", 89.0, 8_900.0)])
        self.assertEqual([(d["symbol"], d["reason"]) for d in result["deferred_entries"]], [("A", "STOPPED_CASH_RESERVE_REACHED")])
        self.assertEqual(result["final_simulated_state"]["cash_eur"], RESERVE)
        self.assertEqual(result["capital"]["limiting_guard"], "STOPPED_CASH_RESERVE_REACHED")

    def test_capital_exhausted_stop_is_unchanged(self) -> None:
        promos = [promo(1, "BIG"), promo(2, "MID"), promo(3, "TINY")]
        snaps = {1: cand(1, "BIG", price=400.0, momentum=60.0), 2: cand(2, "MID", price=250.0, momentum=40.0), 3: cand(3, "TINY", price=50.0, momentum=30.0)}
        result = run(10_600.0, promos=promos, snaps=snaps)
        self.assertEqual([(p["rank"], p["symbol"], p["quantity"]) for p in result["planned_entries"]], [(1, "BIG", 1.0)])
        self.assertEqual([(d["symbol"], d["reason"]) for d in result["deferred_entries"]], [("MID", "STOPPED_CAPITAL_EXHAUSTED"), ("TINY", "STOPPED_CAPITAL_EXHAUSTED")])
        self.assertEqual(result["capital"]["limiting_guard"], "STOPPED_CAPITAL_EXHAUSTED")
        self.assertEqual(result["capital"]["remaining_buying_capacity_eur"], result["final_simulated_state"]["deployable_cash_eur"])

    def test_cash_at_the_reserve_blocks_every_entry_and_add(self) -> None:
        result = run(RESERVE)
        self.assertEqual(result["planned_entries"], [])
        self.assertEqual(status_of(result, "B")["reason_codes"], ["ENTRY_CASH_RESERVE_LIMIT"])
        self.assertIn("ENTRY_CASH_RESERVE_LIMIT", evaluate_entry_recommendation(entry_tests.candidate_snapshot(), entry_tests.context(cash=RESERVE), StrategyConfig()).block_reasons)
        self.assertIn("ADD_CASH_RESERVE_LIMIT", evaluate_add_recommendation(add_tests.swing_snapshot(), add_tests.portfolio(cash=RESERVE), StrategyConfig()).block_reasons)

    def test_final_simulated_state_keys_are_unchanged(self) -> None:
        self.assertEqual(set(run(30_000.0)["final_simulated_state"]), set(run(30_000.0, external=True)["final_simulated_state"]))


# --------------------------------------------------------------------------------------------------------------------------------- B
class FlagTrueLiftsTheCashShortageTests(unittest.TestCase):
    def test_low_cash_plans_buy_candidates_and_reports_the_requirement(self) -> None:
        result = run(10_500.0, external=True)
        self.assertEqual(planned(result), [("B", 111.0, 11_100.0), ("C", 123.0, 12_300.0), ("A", 99.0, 9_900.0)])
        self.assertFalse([d for d in result["deferred_entries"] if d["reason"].startswith("STOPPED_CASH") or d["reason"].startswith("STOPPED_CAPITAL")])
        capital = result["capital"]
        self.assertEqual(capital["cash_available_eur"], 10_500.0)
        self.assertEqual(capital["cash_reserve_min_eur"], RESERVE)
        self.assertEqual(capital["internal_deployable_capital_eur"], 500.0)
        self.assertEqual(capital["external_funding_required_eur"], 33_300.0 - 500.0)
        self.assertEqual(capital["internal_capital_used_eur"], 500.0)
        self.assertIn("EXTERNAL_FUNDING_REQUIRED_FOR_PLAN", result["warnings"])
        self.assertTrue(result["current_state"]["external_funding_available"])

    def test_split_per_entry_is_sequential_and_consistent(self) -> None:
        result = run(30_000.0, external=True)
        rows = [(p["capital_eur"], p["internal_capital_eur"], p["external_funding_eur"], p["cash_after_eur"]) for p in result["planned_entries"]]
        self.assertEqual(rows, [(11_100.0, 11_100.0, 0.0, 18_900.0), (12_300.0, 8_900.0, 3_400.0, 10_000.0), (9_900.0, 0.0, 9_900.0, 10_000.0)])
        capital = result["capital"]
        self.assertAlmostEqual(sum(p["external_funding_eur"] for p in result["planned_entries"]), capital["external_funding_required_eur"], places=6)
        self.assertAlmostEqual(capital["external_funding_required_eur"], max(0.0, capital["planned_capital_total_eur"] - capital["internal_deployable_capital_eur"]), places=6)
        self.assertGreaterEqual(result["final_simulated_state"]["cash_eur"], RESERVE)  # the reserve is never consumed

    def test_entry_sizes_do_not_depend_on_the_cash_amount(self) -> None:
        reference = planned(run(10_000.0, external=True))
        for cash in (5_000.0, 10_500.0, 30_000.0, 1_000_000.0):
            self.assertEqual(planned(run(cash, external=True)), reference, cash)

    def test_with_the_flag_nothing_is_planned_smaller_or_dropped(self) -> None:
        for cash in (10_500.0, 15_000.0, 30_000.0):
            without = {symbol: (quantity, capital) for symbol, quantity, capital in planned(run(cash))}
            with_flag = {symbol: (quantity, capital) for symbol, quantity, capital in planned(run(cash, external=True))}
            self.assertTrue(set(without) <= set(with_flag), cash)
            self.assertTrue(all(with_flag[symbol][0] >= quantity for symbol, (quantity, _) in without.items()), cash)

    def test_engine_buy_and_add_are_supported(self) -> None:
        # entry sizing (BUY): blocked without the flag, eligible with it; the externally funded part is reported
        snap = entry_tests.candidate_snapshot()
        blocked = evaluate_entry_recommendation(snap, entry_tests.context(cash=RESERVE), StrategyConfig())
        buy = evaluate_entry_recommendation(snap, replace(entry_tests.context(cash=RESERVE), external_funding_available=True), StrategyConfig())
        self.assertFalse(blocked.eligible)
        self.assertTrue(buy.eligible)
        self.assertEqual((buy.internal_capital_eur, buy.external_funding_eur), (0.0, buy.purchase_value_eur))
        self.assertEqual(buy.cash_after, RESERVE)
        # add sizing (ADD)
        add_snap = add_tests.swing_snapshot()
        blocked_add = evaluate_add_recommendation(add_snap, add_tests.portfolio(cash=RESERVE), StrategyConfig())
        add = evaluate_add_recommendation(add_snap, replace(add_tests.portfolio(cash=RESERVE), external_funding_available=True), StrategyConfig())
        self.assertIn("ADD_CASH_RESERVE_LIMIT", blocked_add.block_reasons)
        self.assertTrue(add.eligible)
        self.assertGreater(add.external_funding_eur, 0.0)
        self.assertAlmostEqual(add.internal_capital_eur + add.external_funding_eur, add.purchase_value_eur, places=6)

    def test_planner_splits_engine_add_and_buy_purchases(self) -> None:
        records = [existing("ADD", 5, "swing", 10, 100.0, "ADD", None, add_purchase_value_eur=5_000.0, add_recommended_quantity=50, add_current_price_eur=100.0)]
        buys = [existing("BUY", 6, "swing", 0, None, "BUY", None, entry_purchase_value_eur=3_000.0, entry_recommended_quantity=30, entry_current_price_eur=100.0)]
        result = run(10_500.0, external=True, promos=[], snaps={}, existing_results=records, entries=buys)
        capital, post = result["capital"], result["post_action_state"]
        self.assertEqual(post["planned_purchases_from_actions_eur"], 8_000.0)
        self.assertEqual(capital["planned_capital_total_eur"], 8_000.0)
        self.assertEqual(capital["internal_capital_used_eur"], 500.0)
        self.assertEqual(capital["external_funding_required_eur"], 7_500.0)
        self.assertEqual(post["cash_after_actions_eur"], 10_000.0)  # reserve untouched
        without = run(10_500.0, promos=[], snaps={}, existing_results=records, entries=buys)
        self.assertEqual(without["post_action_state"]["cash_after_actions_eur"], 2_500.0)  # unchanged legacy accounting without the flag
        self.assertEqual(without["capital"]["external_funding_required_eur"], 0.0)

    def test_unknown_cash_stays_a_data_problem(self) -> None:
        blocked = evaluate_entry_recommendation(entry_tests.candidate_snapshot(), replace(entry_tests.context(cash=None), external_funding_available=True), StrategyConfig())
        self.assertFalse(blocked.eligible)

    def test_split_helper(self) -> None:
        self.assertEqual(internal_external_split(cash=10_500.0, reserve=RESERVE, purchase=2_000.0), (500.0, 1_500.0))
        self.assertEqual(internal_external_split(cash=5_000.0, reserve=RESERVE, purchase=2_000.0), (0.0, 2_000.0))
        self.assertEqual(internal_external_split(cash=50_000.0, reserve=RESERVE, purchase=2_000.0), (2_000.0, 0.0))
        self.assertEqual(internal_external_split(cash=50_000.0, reserve=RESERVE, purchase=2_000.0, buying_power=10_500.0), (500.0, 1_500.0))


# --------------------------------------------------------------------------------------------------------------------------------- C
class SufficientCashNeedsNoExternalFundingTests(unittest.TestCase):
    def test_required_is_zero_and_the_plan_equals_the_internal_plan(self) -> None:
        promos = [promo(1, "A")]
        internal, external = run(30_000.0, promos=promos), run(30_000.0, external=True, promos=promos)
        self.assertEqual(planned(internal), planned(external))
        capital = external["capital"]
        self.assertTrue(capital["external_funding_available"])
        self.assertEqual(capital["external_funding_required_eur"], 0.0)
        self.assertEqual(capital["internal_capital_used_eur"], capital["planned_capital_total_eur"])
        self.assertEqual(external["planned_entries"][0]["external_funding_eur"], 0.0)
        self.assertIn("EXTERNAL_FUNDING_ASSUMED_AVAILABLE", external["warnings"])
        self.assertNotIn("EXTERNAL_FUNDING_REQUIRED_FOR_PLAN", external["warnings"])
        self.assertEqual(external["final_simulated_state"]["cash_eur"], internal["final_simulated_state"]["cash_eur"])


# --------------------------------------------------------------------------------------------------------------------------------- D
class AllocationStaysBindingTests(unittest.TestCase):
    def test_swing_maximum_is_never_exceeded_and_ends_the_plan(self) -> None:
        promos = [promo(sid, f"S{sid}") for sid in range(1, 6)]
        result = run(10_000.0, external=True, promos=promos, swing=38_000.0)
        final = result["final_simulated_state"]
        self.assertLessEqual(final["swing_pct"], 40.0 + 1e-9)
        self.assertTrue(final["swing_within_max"])
        self.assertTrue(any(d["reason"] == "STOPPED_SWING_MAX_REACHED" for d in result["deferred_entries"]))
        self.assertEqual(result["capital"]["limiting_guard"], "STOPPED_SWING_MAX_REACHED")
        capital = result["capital"]
        self.assertLessEqual(capital["planned_capital_total_eur"], capital["allocation_headroom_eur"] + 1e-6)
        self.assertEqual(capital["deployable_capital_eur"], capital["allocation_headroom_eur"])

    def test_swing_already_at_maximum_blocks_even_with_the_flag(self) -> None:
        result = run(5_000.0, external=True, promos=[promo(1, "FULL")], swing=40_000.0)
        self.assertEqual(status_of(result, "FULL")["entry_status"], BLOCKED_BY_ALLOCATION)
        self.assertEqual(status_of(result, "FULL")["reason_codes"], ["ENTRY_SWING_ALLOCATION_LIMIT"])
        self.assertEqual(result["planned_entries"], [])
        self.assertEqual(result["capital"]["external_funding_required_eur"], 0.0)

    def test_position_size_stays_limited_by_the_initial_weight(self) -> None:
        # 10 % initial weight: (0.10 * 100k) / 0.90 = 11,111 EUR -> 111 shares, no matter how much external money is assumed
        result = run(RESERVE, external=True, promos=[promo(1, "ONE")])
        self.assertEqual(planned(result), [("ONE", 111.0, 11_100.0)])

    def test_the_flag_is_not_unlimited_capital(self) -> None:
        promos = [promo(sid, f"S{sid}") for sid in range(1, 12)]
        result = run(RESERVE, external=True, promos=promos)
        self.assertLess(len(result["planned_entries"]), 11)
        self.assertLessEqual(result["final_simulated_state"]["swing_pct"], 40.0 + 1e-9)
        self.assertLessEqual(result["capital"]["planned_capital_total_eur"], result["capital"]["deployable_capital_eur"] + 1e-6)

    def test_add_sizing_keeps_its_position_and_swing_ceilings(self) -> None:
        # security already large: the weight/swing ceilings, not cash, limit the ADD
        big = add_tests.portfolio(cash=RESERVE, security_value=1_800, swing_value=3_900)
        with_flag = evaluate_add_recommendation(add_tests.swing_snapshot(), replace(big, external_funding_available=True), StrategyConfig())
        huge_cash = evaluate_add_recommendation(add_tests.swing_snapshot(), replace(big, cash_available=10_000_000.0), StrategyConfig())
        self.assertEqual((with_flag.eligible, with_flag.recommended_quantity), (huge_cash.eligible, huge_cash.recommended_quantity))


# --------------------------------------------------------------------------------------------------------------------------------- E
class OtherGuardsStayBindingTests(unittest.TestCase):
    def blocked(self, **kw):
        return run(RESERVE, external=True, **kw)

    def test_data_quality_existing_position_and_campaign_blockers(self) -> None:
        bad = self.blocked(promos=[promo(1, "BAD"), promo(2, "NOSNAP")], snaps={1: cand(1, "BAD", technical_status=AvailabilityStatus.UNAVAILABLE)})
        self.assertEqual(status_of(bad, "BAD")["entry_status"], BLOCKED_BY_DATA)
        self.assertEqual(status_of(bad, "NOSNAP")["entry_status"], BLOCKED_BY_DATA)
        owned = self.blocked(promos=[promo(1, "OWN")], existing_results=[existing("OWN", 1, "swing", 10, 100.0, "HOLD")])
        self.assertEqual(status_of(owned, "OWN")["entry_status"], BLOCKED_EXISTING_POSITION)
        snap = cand(1, "CMP")
        campaign = self.blocked(promos=[promo(1, "CMP")], snaps={1: replace(snap, position=replace(snap.position, swing_campaign_id=7, swing_campaign_status="open"))})
        self.assertEqual(status_of(campaign, "CMP")["entry_status"], BLOCKED_EXISTING_CAMPAIGN)
        for result in (bad, owned, campaign):
            self.assertEqual(result["planned_entries"], [])
            self.assertEqual(result["capital"]["external_funding_required_eur"], 0.0)

    def test_concentration_limit_and_trigger_status_remain(self) -> None:
        concentration = self.blocked(promos=[promo(1, "CON")], config=StrategyConfig(sizing=SizingPolicyConfig(max_initial_swing_weight=0.0)))
        self.assertEqual(status_of(concentration, "CON")["entry_status"], BLOCKED_BY_CONCENTRATION)
        low = self.blocked(promos=[promo(1, "LOW")], snaps={1: cand(1, "LOW", momentum=10.0)})
        self.assertEqual(status_of(low, "LOW")["entry_status"], WAIT_FOR_TRIGGER)
        self.assertEqual(concentration["planned_entries"] + low["planned_entries"], [])

    def test_unavailable_portfolio_state_stays_unavailable(self) -> None:
        result = planner_tests.plan(portfolio=ctx(30_000.0, external=True, valuation=AvailabilityStatus.UNAVAILABLE), promos=[promo(1, "NOP")])
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["planned_entries"], [])
        self.assertEqual(result["capital"], {"external_funding_available": True})  # nothing is computed without a portfolio state

    def test_ready_candidates_are_the_same_set_with_and_without_the_flag(self) -> None:
        statuses = lambda r: {c["symbol"]: c["entry_status"] for c in r["entry_candidates"]}  # noqa: E731
        for cash in (30_000.0, 15_000.0):
            ready = {s for s, st in statuses(run(cash, external=True)).items() if st == ENTRY_READY}
            self.assertEqual(ready, {"A", "B", "C"})


# --------------------------------------------------------------------------------------------------------------------------------- F
class SaleProceedsTests(unittest.TestCase):
    def test_conservative_net_proceeds_are_part_of_the_internal_capital(self) -> None:
        sell = [existing("SEL", 9, "swing", 100, 100.0, "SELL", 100)]
        result = run(RESERVE, external=True, existing_results=sell, swing=40_000.0)
        post, capital = result["post_action_state"], result["capital"]
        net = post["expected_proceeds_net_conservative_eur"]
        self.assertGreater(net, 0.0)
        self.assertLessEqual(net, post["expected_proceeds_gross_eur"])
        self.assertEqual(capital["net_sale_proceeds_conservative_eur"], net)
        self.assertAlmostEqual(capital["internal_deployable_capital_eur"], RESERVE + net - RESERVE, places=6)
        self.assertAlmostEqual(capital["internal_deployable_capital_eur"], post["cash_after_actions_eur"] - RESERVE, places=6)
        self.assertAlmostEqual(capital["external_funding_required_eur"], max(0.0, capital["planned_capital_total_eur"] - capital["internal_deployable_capital_eur"]), places=6)

    def test_proceeds_reduce_the_required_external_funding(self) -> None:
        sell = [existing("SEL", 9, "swing", 100, 100.0, "SELL", 100)]
        with_sale = run(RESERVE, external=True, existing_results=sell, swing=40_000.0)
        without_sale = run(RESERVE, external=True, swing=40_000.0)
        if with_sale["planned_entries"] and without_sale["planned_entries"]:
            self.assertLess(with_sale["capital"]["external_funding_required_eur"] / max(1.0, with_sale["capital"]["planned_capital_total_eur"]),
                            1.0)
        self.assertGreater(with_sale["capital"]["internal_deployable_capital_eur"], without_sale["capital"]["internal_deployable_capital_eur"])

    def test_buying_power_limits_the_internal_capital(self) -> None:
        context = replace(ctx(30_000.0, external=True, buying_power=12_000.0))
        result = planner_tests.plan(portfolio=context, promos=[promo(1, "A")], snaps={1: cand(1, "A")})
        self.assertEqual(result["capital"]["internal_deployable_capital_eur"], 2_000.0)
        self.assertEqual(result["capital"]["external_funding_required_eur"], result["capital"]["planned_capital_total_eur"] - 2_000.0)


# --------------------------------------------------------------------------------------------------------------------------------- G / H
class SimulationWritesNothingTests(unittest.TestCase):
    def test_orchestrator_with_the_flag_changes_no_row_and_creates_no_order(self) -> None:
        base = planner_tests.OrchestratorIntegrationTests("test_plan_is_part_of_the_result_and_promote_stays_a_candidate")
        base.setUp()
        self.addCleanup(base.tearDown)
        dump = lambda: list(base.conn.iterdump())  # noqa: E731
        before, changes = dump(), base.conn.total_changes
        decisions = {1: planner_tests.DecisionResult(planner_tests.Action.TRIM, 0.8, action_quantity=2, reasons=("TP_TRIM",))}
        with patch("trading_orchestrator.build_portfolio_context", return_value=ctx(10_500.0, external=True)), \
             patch("trading_orchestrator.build_analysis_snapshot", side_effect=lambda security_id, **_: base.snapshots[security_id]), \
             patch("trading_orchestrator.decide", side_effect=lambda snapshot, *_: decisions[snapshot.security_id]), \
             patch("trading_orchestrator.evaluate_swing_candidates", return_value=[base.promotion]):
            result = planner_tests.run_trading_orchestrator(base.conn)
        self.assertEqual(dump(), before)
        self.assertEqual(base.conn.total_changes, changes)
        plan_ = result.portfolio_action_plan
        self.assertFalse(plan_["orders_created"])
        self.assertTrue(plan_["simulation_only"])
        self.assertTrue(plan_["capital"]["external_funding_available"])
        self.assertTrue(result.capital_state_summary["external_funding_available"])
        self.assertEqual(result.action_summary["BUY"], 0)


class FlagStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = _create_db(Path(self.temp_dir.name) / "trading.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.conn.close)

    def test_missing_key_or_table_and_foreign_values_mean_false(self) -> None:
        self.assertFalse(read_external_funding_available(self.conn))
        self.assertFalse(resolve_capital_state(self.conn, "2026-09-23").external_funding_available)
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        self.assertFalse(read_external_funding_available(legacy))  # no metadata table at all
        for value in ("false", "0", "no", "", "maybe", "TRUEISH"):
            self.conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('external_funding_available', ?)", (value,))
            self.assertFalse(read_external_funding_available(self.conn), value)

    def test_true_values_are_read_case_insensitively_even_without_a_capital_state_row(self) -> None:
        for value in ("true", "TRUE", " True ", "1", "yes"):
            self.conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('external_funding_available', ?)", (value,))
            resolved = resolve_capital_state(self.conn, "2026-09-23")
            self.assertTrue(resolved.external_funding_available, value)
            self.assertIsNone(resolved.cash_available)  # the flag never invents cash
            self.assertEqual(resolved.cash_quality.status, AvailabilityStatus.UNAVAILABLE)

    def test_manager_shows_dry_runs_and_writes_only_the_flag(self) -> None:
        def snapshot():
            return {table: self.conn.execute(f"SELECT * FROM {table}").fetchall() for table in ("portfolio_capital_state",)}, [tuple(r) for r in self.conn.execute("SELECT key, value FROM metadata ORDER BY key")]

        before = snapshot()
        self.assertEqual(manager.external_funding(self.conn), {"external_funding_available": False, "key": "metadata.external_funding_available"})
        preview = manager.external_funding(self.conn, available=True)
        self.assertEqual((preview["dry_run"], preview["current"], preview["would_set"]), (True, False, True))
        self.assertEqual(snapshot(), before)  # dry-run changes nothing
        done = manager.external_funding(self.conn, available=True, write=True)
        self.assertEqual((done["dry_run"], done["previous"], done["external_funding_available"]), (False, False, True))
        self.assertTrue(read_external_funding_available(self.conn))
        after = snapshot()
        self.assertEqual(after[0], before[0])  # no capital-state / cash row, no booking
        self.assertEqual([k for k, _ in after[1]], sorted([k for k, _ in before[1]] + ["external_funding_available"]))
        manager.external_funding(self.conn, available=False, write=True)
        self.assertFalse(read_external_funding_available(self.conn))
        self.assertEqual(manager.show_capital_state(self.conn, evaluation_as_of="2026-09-23")["cash_available"], None)

    def test_the_flag_reaches_the_portfolio_context_but_not_the_cash(self) -> None:
        manager.external_funding(self.conn, available=True, write=True)
        manager.set_capital_state(self.conn, as_of="2026-09-23", currency="EUR", cash_available=10_500.0, buying_power=None, source="manual", write=True)
        resolved = resolve_capital_state(self.conn, "2026-09-23")
        self.assertEqual((resolved.cash_available, resolved.external_funding_available), (10_500.0, True))


# --------------------------------------------------------------------------------------------------------------------------------- presentation
class ViewsTests(unittest.TestCase):
    def test_opportunity_view_capital_and_fit(self) -> None:
        off, on = run(10_500.0), run(10_500.0, external=True)
        self.assertEqual(_capital(off)["funding"], {"external_funding_available": False})
        self.assertEqual(_capital(off)["after_plan"]["remaining_buying_capacity_eur"], round(off["final_simulated_state"]["deployable_cash_eur"], 2))
        funding = _capital(on)["funding"]
        self.assertTrue(funding["external_funding_available"])
        self.assertEqual(funding["external_funding_required_eur"], round(on["capital"]["external_funding_required_eur"], 2))
        fit_off, fit_on = _capital_fit(100.0, _capital(off)), _capital_fit(100.0, _capital(on))
        self.assertFalse(fit_off["fits_one_share_after_actions"] and fit_off["fits_one_share_after_plan"])
        self.assertTrue(fit_on["fits_one_share_after_actions"])  # compared with the allocation headroom, not with the missing cash

    def test_compact_view_shows_the_funding_terms_and_stays_small(self) -> None:
        base = planner_tests.OrchestratorIntegrationTests("test_plan_is_part_of_the_result_and_promote_stays_a_candidate")
        base.setUp()
        self.addCleanup(base.tearDown)
        full = base.run_orchestrator().primitive()
        full["portfolio_action_plan"] = run(10_500.0, external=True)
        compact = oc.compact_orchestrator_result(full)["entry_plan"]
        self.assertEqual(set(compact["funding"]), set(oc.FUNDING_FIELDS))
        self.assertEqual(compact["funding"]["external_funding_required_eur"], full["portfolio_action_plan"]["capital"]["external_funding_required_eur"])
        self.assertEqual(compact["remaining_buying_capacity_eur"], full["portfolio_action_plan"]["capital"]["remaining_buying_capacity_eur"])
        self.assertTrue(all({"internal_capital_eur", "external_funding_eur"} <= set(p) for p in compact["planned_entries"]))
        full["portfolio_action_plan"] = run(10_500.0)
        off = oc.compact_orchestrator_result(full)["entry_plan"]
        self.assertEqual(off["funding"], {"external_funding_available": False})
        self.assertTrue(all("external_funding_eur" not in p for p in off["planned_entries"]))
        self.assertEqual(off["remaining_buying_capacity_eur"], full["portfolio_action_plan"]["final_simulated_state"]["deployable_cash_eur"])
        self.assertLess(len(json.dumps(compact)) - len(json.dumps(off)), 1_800)

    def test_german_rendering_names_the_assumption_only_with_the_flag(self) -> None:
        self.assertIn("Externes Kapital (Annahme: verfügbar)", render_plan_de(run(10_500.0, external=True)))
        self.assertNotIn("Externes Kapital", render_plan_de(run(10_500.0)))


if __name__ == "__main__":
    unittest.main()
