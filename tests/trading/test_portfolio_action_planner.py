"""v0.2.0 Portfolio Action Planner: deterministic, simulation-only capital/entry plan."""

from __future__ import annotations

import copy
import json
import random
import sqlite3
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import Action, AvailabilityStatus, DecisionResult, StrategyType  # noqa: E402
from entry_sizing import evaluate_entry_recommendation  # noqa: E402
from portfolio_action_planner import (  # noqa: E402
    BLOCKED_BY_ALLOCATION,
    BLOCKED_BY_CONCENTRATION,
    BLOCKED_BY_DATA,
    BLOCKED_EXISTING_CAMPAIGN,
    BLOCKED_EXISTING_POSITION,
    ENTRY_READY,
    WAIT_FOR_TRIGGER,
    build_portfolio_action_plan,
)
from strategy_config import SizingPolicyConfig, StrategyConfig  # noqa: E402
from swing_promotion import PromotionDecision  # noqa: E402
from test_decision_engine import make_snapshot  # noqa: E402
from test_swing_entry_recommendation import candidate_snapshot, context  # noqa: E402
from trading_orchestrator import run_trading_orchestrator  # noqa: E402


def existing(symbol, sid, strategy, qty, price, action, action_qty=None, *, campaign_id=None, **decision_extra):
    return {
        "security_id": sid, "symbol": symbol, "strategy": strategy, "position_quantity": qty,
        "valuation_price_eur": price, "campaign_id": campaign_id,
        "campaign_status": "open" if campaign_id else None, "priority": {"SELL": "P1", "TRIM": "P2", "ADD": "P3", "BUY": "P4"}.get(action, "P6"),
        "decision": {"action": action, "action_quantity": action_qty, "reasons": [f"{action}_TEST"], **decision_extra},
    }


def promo(sid, symbol, *, priority=None, recommendation="PROMOTE"):
    return {"security_id": sid, "symbol": symbol, "name": symbol, "recommendation": recommendation, "watchlist_priority": priority, "current_price_eur": 100.0}


def cand(sid, symbol, *, price=100.0, momentum=40.0, **kw):
    snap = candidate_snapshot(strategy=StrategyType.UNKNOWN, has_position=False, price=price, sma50=price * 0.95, sma200=price * 0.90, momentum=momentum, **kw)
    return replace(snap, security_id=sid, symbol=symbol, name=symbol)


def plan(*, portfolio=None, existing_results=(), entries=(), promos=(), snaps=None, config=None):
    snapshots = snaps if snaps is not None else {int(p["security_id"]): cand(int(p["security_id"]), p["symbol"]) for p in promos}
    return build_portfolio_action_plan(
        portfolio=portfolio or context(total=100_000.0, swing=20_000.0, cash=30_000.0),
        existing_position_results=list(existing_results), entry_candidate_results=list(entries),
        promotion_results=list(promos), candidate_snapshots=snapshots, config=config,
    )


def status_of(result, symbol):
    return next(c for c in result["entry_candidates"] if c["symbol"] == symbol)


class PostActionCapitalTests(unittest.TestCase):
    # 1
    def test_no_sell_or_trim_leaves_capital_unchanged(self) -> None:
        result = plan(existing_results=[existing("HLD", 9, "swing", 10, 100.0, "HOLD")])
        post = result["post_action_state"]
        self.assertEqual(post["expected_proceeds_gross_eur"], 0.0)
        self.assertEqual(post["cash_after_actions_eur"], 30_000.0)
        self.assertEqual(post["swing_value_eur"], 20_000.0)
        self.assertEqual(result["existing_position_actions"], [])
        self.assertNotIn("PROCEEDS_GROSS_TAXES_FEES_NOT_MODELLED", result["warnings"])

    # 2
    def test_sell_creates_free_swing_capacity(self) -> None:
        result = plan(portfolio=context(total=100_000.0, swing=40_000.0, cash=30_000.0),
                      existing_results=[existing("SEL", 5, "swing", 100, 100.0, "SELL", 100)])
        post = result["post_action_state"]
        self.assertEqual(post["expected_proceeds_gross_eur"], 10_000.0)
        self.assertEqual(post["proceeds_basis"], "gross")
        self.assertFalse(post["taxes_modelled"])
        self.assertFalse(post["fees_modelled"])
        self.assertEqual(post["cash_after_actions_eur"], 40_000.0)
        self.assertEqual(post["total_market_value_eur"], 90_000.0)
        self.assertEqual(post["swing_value_eur"], 30_000.0)
        self.assertAlmostEqual(post["swing_pct"], 33.3333, places=3)
        self.assertAlmostEqual(post["long_term_pct"], 66.6667, places=3)
        self.assertAlmostEqual(post["remaining_swing_capacity_eur"], 10_000.0, places=6)  # (0.4*90k - 30k) / 0.6
        self.assertAlmostEqual(post["remaining_swing_capacity_pct"], 6.6667, places=3)
        self.assertIn("PROCEEDS_GROSS_TAXES_FEES_NOT_MODELLED", result["warnings"])

    # 3
    def test_trim_creates_free_capacity(self) -> None:
        result = plan(existing_results=[existing("TRM", 5, "swing", 100, 100.0, "TRIM", 25)])
        post = result["post_action_state"]
        self.assertEqual(post["expected_proceeds_gross_eur"], 2_500.0)
        self.assertEqual(post["cash_after_actions_eur"], 32_500.0)
        self.assertEqual(post["swing_value_eur"], 17_500.0)

    # 4
    def test_multiple_actions_accumulate_per_strategy(self) -> None:
        result = plan(existing_results=[
            existing("SEL", 5, "swing", 100, 100.0, "SELL", 100),
            existing("TRM", 6, "swing", 50, 200.0, "TRIM", 10),
            existing("LTT", 7, "long_term", 20_000, 5.0, "TRIM", 1_000),
        ])
        post = result["post_action_state"]
        self.assertEqual(post["expected_proceeds_gross_eur"], 10_000.0 + 2_000.0 + 5_000.0)
        self.assertEqual(post["swing_value_eur"], 20_000.0 - 12_000.0)
        self.assertEqual(post["long_term_value_eur"], 80_000.0 - 5_000.0)
        self.assertEqual(post["cash_after_actions_eur"], 30_000.0 + 17_000.0)
        self.assertEqual(post["total_market_value_eur"], 100_000.0 - 17_000.0)

    def test_trim_is_never_larger_than_the_held_quantity(self) -> None:
        result = plan(existing_results=[existing("OVR", 5, "swing", 10, 100.0, "TRIM", 50)])
        self.assertEqual(result["post_action_state"]["expected_proceeds_gross_eur"], 1_000.0)

    def test_missing_price_is_reported_not_invented(self) -> None:
        result = plan(existing_results=[existing("NOP", 5, "swing", 10, None, "SELL", 10)])
        self.assertEqual(result["post_action_state"]["expected_proceeds_gross_eur"], 0.0)
        self.assertIn("PROCEEDS_UNAVAILABLE:NOP", result["warnings"])

    def test_existing_add_and_buy_purchases_consume_cash_and_swing_capacity(self) -> None:
        result = plan(existing_results=[existing("ADD", 5, "swing", 10, 100.0, "ADD", None, add_purchase_value_eur=5_000.0, add_recommended_quantity=50, add_current_price_eur=100.0)],
                      entries=[existing("BUY", 6, "swing", 0, None, "BUY", None, entry_purchase_value_eur=3_000.0, entry_recommended_quantity=30, entry_current_price_eur=100.0)])
        post = result["post_action_state"]
        self.assertEqual(post["planned_purchases_from_actions_eur"], 8_000.0)
        self.assertEqual(post["cash_after_actions_eur"], 22_000.0)
        self.assertEqual(post["swing_value_eur"], 28_000.0)
        self.assertEqual(post["total_market_value_eur"], 108_000.0)


class EntryEvaluationTests(unittest.TestCase):
    # 5
    def test_promote_with_bad_data_is_blocked_by_data(self) -> None:
        stale = cand(1, "BAD", technical_status=AvailabilityStatus.UNAVAILABLE)
        result = plan(promos=[promo(1, "BAD"), promo(2, "NOSNAP")], snaps={1: stale})
        self.assertEqual(status_of(result, "BAD")["entry_status"], BLOCKED_BY_DATA)
        self.assertTrue(any(code.startswith("TECHNICAL_DATA_") for code in status_of(result, "BAD")["reason_codes"]))
        self.assertEqual(status_of(result, "NOSNAP")["entry_status"], BLOCKED_BY_DATA)
        self.assertEqual(status_of(result, "NOSNAP")["reason_codes"], ["CANDIDATE_SNAPSHOT_UNAVAILABLE"])
        self.assertEqual(result["planned_entries"], [])

    # 6
    def test_existing_position_is_blocked(self) -> None:
        held = [existing("OWN", 1, "swing", 10, 100.0, "HOLD")]
        result = plan(existing_results=held, promos=[promo(1, "OWN")])
        self.assertEqual(status_of(result, "OWN")["entry_status"], BLOCKED_EXISTING_POSITION)
        position_snapshot = cand(2, "POS")
        position_snapshot = replace(position_snapshot, position=replace(position_snapshot.position, has_position=True, shares=5.0))
        result = plan(promos=[promo(2, "POS")], snaps={2: position_snapshot})
        self.assertEqual(status_of(result, "POS")["entry_status"], BLOCKED_EXISTING_POSITION)
        self.assertEqual(result["planned_entries"], [])

    # 7
    def test_open_campaign_is_blocked(self) -> None:
        result = plan(promos=[promo(1, "CMP")], snaps={1: replace(cand(1, "CMP"), position=replace(cand(1, "CMP").position, swing_campaign_id=7, swing_campaign_status="open"))})
        self.assertEqual(status_of(result, "CMP")["entry_status"], BLOCKED_EXISTING_CAMPAIGN)
        self.assertEqual(result["planned_entries"], [])

    # 8
    def test_swing_already_at_maximum_is_blocked_by_allocation(self) -> None:
        result = plan(portfolio=context(total=100_000.0, swing=40_000.0, cash=30_000.0), promos=[promo(1, "FULL")])
        candidate = status_of(result, "FULL")
        self.assertEqual(candidate["entry_status"], BLOCKED_BY_ALLOCATION)
        self.assertEqual(candidate["reason_codes"], ["ENTRY_SWING_ALLOCATION_LIMIT"])
        self.assertEqual(result["planned_entries"], [])

    # 9
    def test_candidate_is_entry_ready_with_sizing_fields(self) -> None:
        result = plan(promos=[promo(1, "RDY")])
        candidate = status_of(result, "RDY")
        self.assertEqual(candidate["entry_status"], ENTRY_READY)
        self.assertEqual(candidate["rank"], 1)
        sizing = candidate["sizing"]
        # initial-weight cap 10%: (0.10 * 100k) / 0.90 = 11,111.11 -> floor(111.11) = 111 shares
        self.assertEqual(sizing["proposed_quantity"], 111.0)
        self.assertEqual(sizing["proposed_capital_eur"], 11_100.0)
        self.assertAlmostEqual(sizing["resulting_portfolio_weight_pct"], 11_100 / 111_100 * 100, places=6)
        self.assertAlmostEqual(sizing["resulting_swing_allocation_pct"], 31_100 / 111_100 * 100, places=6)
        self.assertEqual(sizing["resulting_position_value_eur"], 11_100.0)
        self.assertEqual(result["entry_summary"][ENTRY_READY], 1)

    def test_promote_is_not_buy_only_promote_items_are_evaluated(self) -> None:
        result = plan(promos=[promo(1, "KW", recommendation="KEEP_WATCHING"), promo(2, "REJ", recommendation="REJECT"), promo(3, "OK")],
                      snaps={3: cand(3, "OK")})
        self.assertEqual([c["symbol"] for c in result["entry_candidates"]], ["OK"])
        self.assertFalse(result["orders_created"])
        self.assertTrue(result["simulation_only"])

    def test_wait_for_trigger_on_low_score_or_lost_trend(self) -> None:
        low = plan(promos=[promo(1, "LOW")], snaps={1: cand(1, "LOW", momentum=10.0)})
        self.assertEqual(status_of(low, "LOW")["entry_status"], WAIT_FOR_TRIGGER)
        self.assertEqual(status_of(low, "LOW")["reason_codes"], ["SCORE_BELOW_THRESHOLD"])
        no_score = plan(promos=[promo(1, "NOS")], snaps={1: cand(1, "NOS", momentum=None)})
        self.assertEqual(status_of(no_score, "NOS")["reason_codes"], ["SCORE_UNAVAILABLE"])
        below = cand(2, "TRD")
        below = replace(below, technical=replace(below.technical, current_price=90.0))  # <= SMA50 (95)
        lost = plan(promos=[promo(2, "TRD")], snaps={2: below})
        self.assertEqual(status_of(lost, "TRD")["entry_status"], WAIT_FOR_TRIGGER)
        self.assertEqual(status_of(lost, "TRD")["reason_codes"], ["ENTRY_PRICE_BELOW_OR_EQUAL_SMA50"])

    def test_security_weight_limit_is_blocked_by_concentration(self) -> None:
        config = StrategyConfig(sizing=SizingPolicyConfig(max_initial_swing_weight=0.0))
        result = plan(promos=[promo(1, "CON")], config=config)
        self.assertEqual(status_of(result, "CON")["entry_status"], BLOCKED_BY_CONCENTRATION)

    def test_unavailable_portfolio_state_yields_unavailable_plan_and_blocked_candidates(self) -> None:
        result = plan(portfolio=context(valuation=AvailabilityStatus.UNAVAILABLE), promos=[promo(1, "NOP")])
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(status_of(result, "NOP")["entry_status"], BLOCKED_BY_DATA)
        self.assertEqual(status_of(result, "NOP")["reason_codes"], ["PORTFOLIO_STATE_UNAVAILABLE"])
        self.assertEqual(result["planned_entries"], [])
        self.assertIn("PORTFOLIO_STATE_UNAVAILABLE", result["warnings"])


class RankingAndAllocationTests(unittest.TestCase):
    def three(self):
        promos = [promo(1, "A"), promo(2, "B"), promo(3, "C")]
        snaps = {1: cand(1, "A", momentum=30.0), 2: cand(2, "B", momentum=50.0), 3: cand(3, "C", momentum=40.0)}
        return promos, snaps

    # 10
    def test_ranking_is_by_entry_score_and_deterministic(self) -> None:
        promos, snaps = self.three()
        result = plan(promos=promos, snaps=snaps)
        ranked = [c["symbol"] for c in sorted((c for c in result["entry_candidates"] if c["rank"]), key=lambda c: c["rank"])]
        self.assertEqual(ranked, ["B", "C", "A"])  # 50, 40, 30 x identical confidence
        self.assertEqual(result["entry_candidates"][0]["symbol"], "B")
        again = plan(promos=promos, snaps=snaps)
        self.assertEqual(json.dumps(result, sort_keys=True), json.dumps(again, sort_keys=True))
        shuffled = list(promos)
        random.Random(7).shuffle(shuffled)
        self.assertEqual({c["symbol"]: c["rank"] for c in plan(promos=shuffled, snaps=snaps)["entry_candidates"]}, {c["symbol"]: c["rank"] for c in result["entry_candidates"]})

    def test_rank_ties_break_on_watchlist_priority_then_security_id(self) -> None:
        promos = [promo(1, "P1"), promo(2, "P2", priority=80), promo(3, "P3", priority=50), promo(4, "P4")]
        snaps = {sid: cand(sid, f"P{sid}", momentum=40.0) for sid in (1, 2, 3, 4)}
        result = plan(promos=promos, snaps=snaps)
        order = [c["symbol"] for c in sorted((c for c in result["entry_candidates"] if c["rank"]), key=lambda c: c["rank"])]
        self.assertEqual(order, ["P2", "P3", "P1", "P4"])

    # 11
    def test_position_sizing_matches_the_existing_entry_logic_and_uses_whole_shares(self) -> None:
        portfolio = context(total=100_000.0, swing=20_000.0, cash=30_000.0)
        direct = evaluate_entry_recommendation(candidate_snapshot(price=100.0, sma50=95.0, sma200=90.0, momentum=40.0), portfolio, StrategyConfig())
        planned = plan(portfolio=portfolio, promos=[promo(1, "SIZ")])["planned_entries"][0]
        self.assertTrue(direct.eligible)
        self.assertEqual(planned["quantity"], direct.recommended_quantity)
        self.assertEqual(planned["capital_eur"], direct.purchase_value_eur)
        self.assertEqual(planned["quantity"], int(planned["quantity"]))
        self.assertAlmostEqual(planned["expected_weight_pct"], direct.projected_security_weight * 100, places=9)

    # 12
    def test_cash_reserve_prevents_over_allocation(self) -> None:
        tight = plan(portfolio=context(total=100_000.0, swing=20_000.0, cash=10_500.0), promos=[promo(1, "CSH")])
        entry = tight["planned_entries"][0]
        self.assertEqual(entry["quantity"], 5.0)  # only 500 EUR above the 10,000 reserve
        self.assertGreaterEqual(tight["final_simulated_state"]["cash_eur"], 10_000.0)
        self.assertTrue(tight["final_simulated_state"]["cash_above_reserve"])
        at_reserve = plan(portfolio=context(total=100_000.0, swing=20_000.0, cash=10_000.0), promos=[promo(1, "CSH")])
        self.assertEqual(status_of(at_reserve, "CSH")["entry_status"], BLOCKED_BY_ALLOCATION)
        self.assertEqual(status_of(at_reserve, "CSH")["reason_codes"], ["ENTRY_CASH_RESERVE_LIMIT"])
        self.assertEqual(at_reserve["planned_entries"], [])

    # 13
    def test_swing_maximum_is_never_exceeded(self) -> None:
        promos = [promo(sid, f"S{sid}") for sid in range(1, 6)]
        result = plan(portfolio=context(total=100_000.0, swing=38_000.0, cash=100_000.0), promos=promos)
        self.assertLessEqual(result["final_simulated_state"]["swing_pct"], 40.0 + 1e-9)
        self.assertTrue(result["final_simulated_state"]["swing_within_max"])
        for entry in result["planned_entries"]:
            self.assertLessEqual(entry["resulting_swing_allocation_pct"], 40.0 + 1e-9)

    # 14
    def test_multiple_entries_are_distributed_sequentially_in_rank_order(self) -> None:
        promos, snaps = self.three()
        result = plan(promos=promos, snaps=snaps)
        planned = result["planned_entries"]
        self.assertEqual([(p["rank"], p["symbol"]) for p in planned], [(1, "B"), (2, "C")])
        self.assertEqual(planned[0]["capital_eur"], 11_100.0)
        # 2nd entry sees the reduced state: cash 18,900 - 10,000 reserve = 8,900 -> 89 shares
        self.assertEqual(planned[1]["quantity"], 89.0)
        self.assertEqual(planned[1]["cash_after_eur"], 10_000.0)
        self.assertGreater(planned[1]["resulting_swing_allocation_pct"], planned[0]["resulting_swing_allocation_pct"])
        self.assertEqual([(d["symbol"], d["reason"]) for d in result["deferred_entries"]], [("A", "STOPPED_CASH_RESERVE_REACHED")])

    def test_candidate_unaffordable_even_alone_is_blocked_and_does_not_compete(self) -> None:
        # deployable 600 EUR: a 700 EUR share cannot be bought at all -> BLOCKED_BY_ALLOCATION, never ranked.
        promos = [promo(1, "BIG"), promo(2, "CHEAP")]
        snaps = {1: cand(1, "BIG", price=700.0, momentum=60.0), 2: cand(2, "CHEAP", price=50.0, momentum=30.0)}
        result = plan(portfolio=context(total=100_000.0, swing=20_000.0, cash=10_600.0), promos=promos, snaps=snaps)
        self.assertEqual(status_of(result, "BIG")["entry_status"], BLOCKED_BY_ALLOCATION)
        self.assertIsNone(status_of(result, "BIG")["rank"])
        self.assertEqual([p["symbol"] for p in result["planned_entries"]], ["CHEAP"])

    def test_allocation_stops_in_strict_rank_order_without_crumb_entries(self) -> None:
        # deployable 600 EUR. Each candidate is fundable alone, but rank 1 (400) leaves 200:
        # rank 2 (250) can no longer be funded -> STOP; the cheap rank 3 (50) must not leapfrog into the crumbs.
        promos = [promo(1, "BIG"), promo(2, "MID"), promo(3, "TINY")]
        snaps = {1: cand(1, "BIG", price=400.0, momentum=60.0), 2: cand(2, "MID", price=250.0, momentum=40.0), 3: cand(3, "TINY", price=50.0, momentum=30.0)}
        result = plan(portfolio=context(total=100_000.0, swing=20_000.0, cash=10_600.0), promos=promos, snaps=snaps)
        self.assertEqual([(p["rank"], p["symbol"], p["quantity"]) for p in result["planned_entries"]], [(1, "BIG", 1.0)])
        self.assertEqual([(d["symbol"], d["reason"]) for d in result["deferred_entries"]], [("MID", "STOPPED_CAPITAL_EXHAUSTED"), ("TINY", "STOPPED_CAPITAL_EXHAUSTED")])
        self.assertEqual(result["final_simulated_state"]["cash_eur"], 10_200.0)

    # 15
    def test_final_simulated_state_is_consistent(self) -> None:
        promos, snaps = self.three()
        result = plan(promos=promos, snaps=snaps, existing_results=[existing("SEL", 9, "swing", 100, 100.0, "SELL", 100)])
        post, final, planned = result["post_action_state"], result["final_simulated_state"], result["planned_entries"]
        spent = sum(p["capital_eur"] for p in planned)
        self.assertAlmostEqual(final["planned_entries_total_eur"], spent, places=6)
        self.assertAlmostEqual(final["cash_eur"], post["cash_after_actions_eur"] - spent, places=6)
        self.assertAlmostEqual(final["swing_value_eur"], post["swing_value_eur"] + spent, places=6)
        self.assertAlmostEqual(final["total_market_value_eur"], post["total_market_value_eur"] + spent, places=6)
        self.assertAlmostEqual(final["swing_pct"], final["swing_value_eur"] / final["total_market_value_eur"] * 100, places=9)
        self.assertAlmostEqual(final["swing_pct"] + final["long_term_pct"], 100.0, places=9)
        self.assertTrue(final["swing_within_max"] and final["cash_above_reserve"])
        self.assertGreaterEqual(final["cash_eur"], 0.0)

    # 16
    def test_inputs_and_existing_actions_are_never_modified(self) -> None:
        promos, snaps = self.three()
        records = [existing("SEL", 9, "swing", 100, 100.0, "SELL", 100), existing("TRM", 8, "swing", 50, 200.0, "TRIM", 10)]
        before = copy.deepcopy((records, promos))
        result = plan(promos=promos, snaps=snaps, existing_results=records)
        self.assertEqual((records, promos), before)
        self.assertEqual([(a["symbol"], a["action"], a["quantity"]) for a in result["existing_position_actions"]], [("SEL", "SELL", 100.0), ("TRM", "TRIM", 10.0)])
        self.assertEqual(json.loads(json.dumps(result))["status"], "AVAILABLE")  # JSON-serializable


class OrchestratorIntegrationTests(unittest.TestCase):
    """The plan is added at the end of the orchestrator and never alters existing results."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT);
            CREATE TABLE positions (security_id INTEGER, shares REAL);
            CREATE TABLE strategy_assignment (security_id INTEGER, strategy_type TEXT, effective_from TEXT, effective_to TEXT);
            CREATE TABLE watchlist (security_id INTEGER, status TEXT, priority INTEGER, entry_reason TEXT);
            CREATE TABLE metadata (key TEXT, value TEXT);
            CREATE TABLE candidate_promotion (id INTEGER PRIMARY KEY);
            CREATE TABLE swing_campaign (security_id INTEGER, status TEXT);
            INSERT INTO metadata VALUES ('candidate_promotion_schema_version', '1');
            INSERT INTO positions VALUES (1, 10);
            """
        )
        held = make_snapshot(has_position=True, strategy=StrategyType.SWING, position_currency="EUR", market_currency="EUR")
        self.snapshots = {1: replace(held, security_id=1, symbol="S1"), 3: cand(3, "S3", momentum=40.0)}
        self.promotion = PromotionDecision(3, "S3", "Security 3", "WATCH", 10, "test", "PROMOTE", ("PROMOTE_REASON",), "token", 100.0, "EUR", 100.0,
                                           95.0, 90.0, None, None, None, None, None, None, 40.0, "available", "available", "available", "available")

    def tearDown(self) -> None:
        self.conn.close()

    def run_orchestrator(self, **overrides):
        decisions = {1: DecisionResult(Action.TRIM, 0.8, action_quantity=2, reasons=("TP_TRIM",))}
        with patch("trading_orchestrator.build_portfolio_context", return_value=context(total=100_000.0, swing=20_000.0, cash=30_000.0)), \
             patch("trading_orchestrator.build_analysis_snapshot", side_effect=lambda security_id, **_: self.snapshots[security_id]), \
             patch("trading_orchestrator.decide", side_effect=lambda snapshot, *_: decisions[snapshot.security_id]), \
             patch("trading_orchestrator.evaluate_swing_candidates", return_value=[self.promotion]):
            if overrides:
                with patch("trading_orchestrator.build_portfolio_action_plan", **overrides):
                    return run_trading_orchestrator(self.conn)
            return run_trading_orchestrator(self.conn)

    def test_plan_is_part_of_the_result_and_promote_stays_a_candidate(self) -> None:
        result = self.run_orchestrator()
        plan_ = result.portfolio_action_plan
        self.assertEqual(plan_["status"], "AVAILABLE")
        self.assertEqual([a["symbol"] for a in plan_["existing_position_actions"]], ["S1"])
        self.assertEqual(status_of(plan_, "S3")["entry_status"], ENTRY_READY)
        self.assertEqual(result.action_summary["PROMOTE"], 1)  # unchanged: PROMOTE is not turned into BUY
        self.assertEqual(result.action_summary["BUY"], 0)
        self.assertIn("portfolio_action_plan", result.primitive())

    def test_existing_results_are_identical_with_and_without_the_planner(self) -> None:
        with_plan = self.run_orchestrator()
        without = self.run_orchestrator(return_value={})
        for field in ("existing_position_results", "entry_candidate_results", "promotion_results", "action_summary", "next_review_items", "global_status", "rendered_summary_de"):
            self.assertEqual(json.dumps(getattr(with_plan, field), sort_keys=True, default=str), json.dumps(getattr(without, field), sort_keys=True, default=str), field)

    def test_planner_failure_is_isolated_and_visible(self) -> None:
        good = self.run_orchestrator(return_value={})
        failed = self.run_orchestrator(side_effect=RuntimeError("boom"))
        self.assertEqual(failed.portfolio_action_plan["status"], "UNAVAILABLE")
        self.assertIn("PORTFOLIO_ACTION_PLAN_ERROR:RuntimeError", failed.portfolio_action_plan["warnings"])
        self.assertEqual(json.dumps(failed.existing_position_results, sort_keys=True), json.dumps(good.existing_position_results, sort_keys=True))
        self.assertEqual(failed.global_status, "AVAILABLE")


if __name__ == "__main__":
    unittest.main()
