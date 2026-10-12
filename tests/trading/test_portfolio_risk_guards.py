"""Sector and correlation guards of the Portfolio Action Planner (synthetic data only).

One limit (``RiskGuardConfig.max_sector_weight``) covers two guards: the sector weight and the weight of a correlation cluster
(candidate plus holdings with a high return correlation), both after the purchase and evaluated sequentially in rank order.
External funding never lifts them; missing data never blocks and never becomes a 0.0 correlation.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_portfolio_action_planner as planner_tests  # noqa: E402  (module import: its test classes must not be collected twice)
import test_swing_entry_recommendation as entry_tests  # noqa: E402
from analysis_contracts import Action, AvailabilityStatus, DataQuality, DecisionResult, PortfolioSecurityExposure, StrategyType  # noqa: E402
from portfolio_action_planner import ENTRY_READY, build_portfolio_action_plan, render_plan_de  # noqa: E402
from portfolio_risk import CORRELATION_BLOCK, SECTOR_BLOCK, RiskBook, RiskInputs, load_risk_inputs, return_correlation  # noqa: E402
from strategy_config import RiskGuardConfig, StrategyConfig  # noqa: E402
from swing_promotion import PromotionDecision  # noqa: E402
from test_decision_engine import make_snapshot  # noqa: E402
from trading_orchestrator import run_trading_orchestrator  # noqa: E402

cand, existing, promo = planner_tests.cand, planner_tests.existing, planner_tests.promo

AS_OF = "2026-09-23"
LIMIT = 0.30
AVAILABLE = DataQuality(AvailabilityStatus.AVAILABLE)

spec = importlib.util.spec_from_file_location("orchestrator_compact_risk", ROOT / "mcp-tools" / "orchestrator_compact.py")
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)


def dates(n: int) -> list[str]:
    end = date.fromisoformat(AS_OF)
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def returns(seed: int, n: int = 260, vol: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, vol) for _ in range(n)]


def closes_from(rets: list[float], n: int | None = None) -> dict[str, float]:
    days = dates(len(rets) + 1)
    price, out = 100.0, {days[0]: 100.0}
    for day, r in zip(days[1:], rets):
        price *= 1.0 + r
        out[day] = price
    if n is not None:  # keep only the newest ``n`` closes
        out = dict(list(out.items())[-n:])
    return out


BASE = returns(1)
CLOSES = {
    "base": closes_from(BASE),
    "twin": closes_from([r + x for r, x in zip(BASE, returns(2, vol=0.001))]),  # correlation ~ 0.99 with "base"
    "other": closes_from(returns(3)),
    "other2": closes_from(returns(4)),
}


def exposure(sid: int, symbol: str, value: float, *, strategy: StrategyType = StrategyType.LONG_TERM) -> PortfolioSecurityExposure:
    return PortfolioSecurityExposure(sid, symbol, strategy, 10.0, value, None, None, None, AVAILABLE, AVAILABLE)


def build(
    *,
    held=(),
    sectors=None,
    closes=None,
    promos=None,
    snaps=None,
    external=False,
    cash=30_000.0,
    existing_results=(),
    entries=(),
    config=None,
    guards=True,
    sector_map_available=True,
):
    """Plan for ``promos`` (default: candidates 1 and 2, rank 1 = id 2) with the held ``exposures``; ``guards=False`` omits the risk inputs."""
    promos = promos if promos is not None else [promo(1, "AAA"), promo(2, "BBB")]
    momentum = {1: 30.0, 2: 50.0, 3: 40.0}
    snaps = snaps if snaps is not None else {int(p["security_id"]): cand(int(p["security_id"]), p["symbol"], momentum=momentum.get(int(p["security_id"]), 40.0)) for p in promos}
    portfolio = replace(entry_tests.context(total=100_000.0, swing=20_000.0, cash=cash), exposures=tuple(held), external_funding_available=external)
    risk = RiskInputs(sectors or {}, closes or {}, {1: "AAA", 2: "BBB", 3: "CCC", 101: "HLD1", 102: "HLD2"}, AS_OF, sector_map_available) if guards else None
    return build_portfolio_action_plan(
        portfolio=portfolio, existing_position_results=list(existing_results), entry_candidate_results=list(entries), promotion_results=list(promos),
        candidate_snapshots=snaps, config=config, risk_inputs=risk,
    )


def capital_of(result, symbol: str) -> float:
    return next(p["capital_eur"] for p in result["planned_entries"] if p["symbol"] == symbol)


def deferred(result) -> dict[str, str]:
    return {d["symbol"]: d["reason"] for d in result["deferred_entries"]}


def planned_symbols(result) -> list[str]:
    return [p["symbol"] for p in result["planned_entries"]]


def sector_value_for_limit(purchase: float, total: float = 100_000.0, limit: float = LIMIT) -> float:
    """Existing sector value that puts the sector exactly at ``limit`` after buying ``purchase`` (total grows by the purchase)."""
    return limit * (total + purchase) - purchase


# ------------------------------------------------------------------------------------------------------------------------------ sector
class SectorGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.free = build(guards=False)  # the same plan without guards
        self.first = capital_of(self.free, "BBB")  # rank 1
        self.second = capital_of(self.free, "AAA")  # rank 2, sized after the first purchase

    # A
    def test_a_sector_under_the_limit_is_allowed(self) -> None:
        result = build(held=[exposure(101, "HLD1", 5_000.0)], sectors={1: "TECH", 2: "TECH", 101: "TECH"})
        self.assertEqual(planned_symbols(result), planned_symbols(self.free))
        risk = status(result, "BBB")["risk"]["sector"]
        self.assertEqual(risk["status"], "OK")
        self.assertEqual(risk["sector"], "TECH")
        self.assertLess(risk["projected_weight_pct"], risk["limit_pct"])
        self.assertAlmostEqual(risk["current_weight_pct"], 5.0, places=2)

    # B
    def test_b_exactly_at_the_limit_is_allowed(self) -> None:
        result = build(held=[exposure(101, "HLD1", sector_value_for_limit(self.first))], sectors={2: "TECH", 101: "TECH"}, promos=[promo(2, "BBB")])
        self.assertEqual(planned_symbols(result), ["BBB"])
        self.assertAlmostEqual(status(result, "BBB")["risk"]["sector"]["projected_weight_pct"], 30.0, places=2)

    # C
    def test_c_over_the_limit_after_the_entry_is_blocked(self) -> None:
        result = build(held=[exposure(101, "HLD1", sector_value_for_limit(self.first) + 1.0)], sectors={2: "TECH", 101: "TECH"}, promos=[promo(2, "BBB")])
        self.assertEqual(result["planned_entries"], [])
        self.assertEqual(deferred(result), {"BBB": SECTOR_BLOCK})
        self.assertEqual(SECTOR_BLOCK, "BLOCKED_BY_SECTOR_CONCENTRATION")
        sector = status(result, "BBB")["risk"]["sector"]
        self.assertEqual(sector["status"], "BLOCKED")
        self.assertGreaterEqual(sector["projected_weight_pct"], sector["limit_pct"])  # 1 EUR above the limit rounds to 30.0
        self.assertEqual(status(result, "BBB")["entry_status"], ENTRY_READY)  # the candidate itself is unchanged; the plan defers it
        self.assertIn(SECTOR_BLOCK, render_plan_de(result))
        self.assertEqual(result["capital"]["limiting_guard"], "NO_ENTRY_PLANNED")

    # D
    def test_d_a_second_entry_is_blocked_by_the_first_planned_entry(self) -> None:
        held = [exposure(101, "HLD1", sector_value_for_limit(self.first))]  # the first entry ends exactly at the limit
        result = build(held=held, sectors={1: "TECH", 2: "TECH", 101: "TECH"})
        self.assertEqual(planned_symbols(result), ["BBB"])
        self.assertEqual(deferred(result), {"AAA": SECTOR_BLOCK})
        second = status(result, "AAA")["risk"]["sector"]
        self.assertGreater(second["current_weight_pct"], 29.99)  # measured against the state after entry 1, not the original portfolio
        self.assertEqual(planned_symbols(build(held=held, sectors={1: "TECH", 2: "ENERGY", 101: "TECH"})), ["BBB", "AAA"])  # a different sector is unaffected

    # E
    def test_e_add_counts_the_existing_position_and_is_blocked(self) -> None:
        add = lambda value: existing("HLD1", 101, "swing", 10, 100.0, "ADD", None, add_purchase_value_eur=value, add_recommended_quantity=1, add_current_price_eur=100.0)
        held = [exposure(101, "HLD1", 28_000.0, strategy=StrategyType.SWING)]
        sectors = {101: "TECH"}
        blocked = build(held=held, sectors=sectors, promos=[], existing_results=[add(5_000.0)])
        item = blocked["existing_position_actions"][0]
        self.assertEqual(item["plan_block_reason"], SECTOR_BLOCK)
        self.assertEqual(item["risk"]["sector"]["current_weight_pct"], 28.0)  # the position itself is part of the sector
        self.assertEqual(blocked["post_action_state"]["planned_purchases_from_actions_eur"], 0.0)
        self.assertEqual(blocked["post_action_state"]["cash_after_actions_eur"], 30_000.0)  # a blocked purchase does not consume cash
        allowed = build(held=held, sectors=sectors, promos=[], existing_results=[add(1_000.0)])
        self.assertNotIn("plan_block_reason", allowed["existing_position_actions"][0])
        self.assertEqual(allowed["post_action_state"]["planned_purchases_from_actions_eur"], 1_000.0)
        self.assertEqual(allowed["post_action_state"]["cash_after_actions_eur"], 29_000.0)

    # F
    def test_f_buy_considers_the_existing_sector_exposure(self) -> None:
        buy = existing("NEW", 6, "swing", 0, None, "BUY", None, entry_purchase_value_eur=8_000.0, entry_recommended_quantity=80, entry_current_price_eur=100.0)
        result = build(held=[exposure(101, "HLD1", 25_000.0)], sectors={6: "TECH", 101: "TECH"}, promos=[], entries=[buy])
        self.assertEqual(result["existing_position_actions"][0]["plan_block_reason"], SECTOR_BLOCK)  # (25 + 8) / 108 = 30.6 %
        self.assertEqual(build(held=[exposure(101, "HLD1", 25_000.0)], sectors={6: "ENERGY", 101: "TECH"}, promos=[], entries=[buy])["post_action_state"]["planned_purchases_from_actions_eur"], 8_000.0)

    def test_an_earlier_add_counts_for_the_next_purchase(self) -> None:
        add = existing("HLD1", 101, "swing", 10, 100.0, "ADD", None, add_purchase_value_eur=3_000.0, add_recommended_quantity=30, add_current_price_eur=100.0)
        first = capital_of(build(guards=False, existing_results=[add]), "BBB")  # BBB is sized after the ADD
        held = [exposure(101, "HLD1", sector_value_for_limit(first, total=103_000.0) - 3_000.0)]  # ADD(3k) then BBB end exactly at the limit; AAA would exceed it
        result = build(held=held, sectors={1: "TECH", 2: "TECH", 101: "TECH"}, existing_results=[add])
        self.assertNotIn("plan_block_reason", result["existing_position_actions"][0])
        self.assertEqual(deferred(result), {"AAA": SECTOR_BLOCK})

    # G
    def test_g_unknown_sector_is_not_assessed_and_visible_once(self) -> None:
        result = build(held=[exposure(101, "HLD1", 60_000.0)], sectors={101: "TECH"})  # candidates 1, 2 have no sector
        self.assertEqual(planned_symbols(result), planned_symbols(self.free))
        self.assertEqual(status(result, "BBB")["risk"]["sector"], {"status": "SECTOR_UNKNOWN"})
        self.assertEqual([w for w in result["warnings"] if w.startswith("RISK_SECTOR_UNKNOWN")], ["RISK_SECTOR_UNKNOWN:2"])
        self.assertNotIn(SECTOR_BLOCK, deferred(result).values())

    def test_a_missing_sector_map_switches_the_sector_guard_off_visibly(self) -> None:
        result = build(held=[exposure(101, "HLD1", 80_000.0)], sectors={}, sector_map_available=False)
        self.assertEqual(planned_symbols(result), planned_symbols(self.free))
        self.assertIn("RISK_SECTOR_MAP_UNAVAILABLE", result["warnings"])
        self.assertIn("CONCENTRATION_SINGLE_SECURITY_LIMITS_ONLY", result["warnings"])
        self.assertEqual(result["methodology"]["risk_guards"], {"active": False})

    # H
    def test_h_external_funding_does_not_bypass_the_sector_guard(self) -> None:
        held = [exposure(101, "HLD1", sector_value_for_limit(self.first) + 1.0)]
        for cash in (30_000.0, 10_500.0, 10_000.0):
            result = build(held=held, sectors={2: "TECH", 101: "TECH"}, promos=[promo(2, "BBB")], external=True, cash=cash)
            self.assertEqual(result["planned_entries"], [], cash)
            self.assertEqual(deferred(result), {"BBB": SECTOR_BLOCK}, cash)
        # the flag still does its own job for an unaffected candidate
        funded = build(held=held, sectors={2: "ENERGY", 101: "TECH"}, promos=[promo(2, "BBB")], external=True, cash=10_000.0)
        self.assertEqual(planned_symbols(funded), ["BBB"])
        self.assertGreater(funded["capital"]["external_funding_required_eur"], 0.0)


def status(result, symbol):
    return planner_tests.status_of(result, symbol)


# ------------------------------------------------------------------------------------------------------------------------- correlation
class CorrelationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = capital_of(build(guards=False), "BBB")

    # I
    def test_i_high_correlation_with_a_relevant_exposure_blocks(self) -> None:
        result = build(held=[exposure(101, "HLD1", 25_000.0)], closes={101: CLOSES["base"], 2: CLOSES["twin"]}, promos=[promo(2, "BBB")])
        self.assertEqual(deferred(result), {"BBB": CORRELATION_BLOCK})
        self.assertEqual(CORRELATION_BLOCK, "BLOCKED_BY_CORRELATION")
        corr = status(result, "BBB")["risk"]["correlation"]
        self.assertEqual(corr["status"], "BLOCKED")
        self.assertGreater(corr["strongest"]["correlation"], 0.95)
        self.assertGreaterEqual(corr["strongest"]["common_days"], 250)
        self.assertEqual(corr["cluster_members"], ["HLD1"])
        self.assertAlmostEqual(corr["cluster_current_weight_pct"], 25.0, places=2)
        self.assertGreater(corr["cluster_projected_weight_pct"], 30.0)

    def test_i_a_high_correlation_with_a_small_exposure_is_allowed(self) -> None:
        result = build(held=[exposure(101, "HLD1", 5_000.0)], closes={101: CLOSES["base"], 2: CLOSES["twin"]}, promos=[promo(2, "BBB")])
        self.assertEqual(planned_symbols(result), ["BBB"])
        self.assertEqual(status(result, "BBB")["risk"]["correlation"]["status"], "OK")

    # J
    def test_j_low_correlation_is_allowed(self) -> None:
        result = build(held=[exposure(101, "HLD1", 25_000.0)], closes={101: CLOSES["base"], 2: CLOSES["other"]}, promos=[promo(2, "BBB")])
        self.assertEqual(planned_symbols(result), ["BBB"])
        corr = status(result, "BBB")["risk"]["correlation"]
        self.assertLess(corr["strongest"]["correlation"], 0.7)
        self.assertEqual(corr["cluster_members"], [])

    # K
    def test_k_too_few_common_days_are_not_a_correlation(self) -> None:
        short = closes_from(returns(2, vol=0.001), 60)  # 59 common returns < 120
        result = build(held=[exposure(101, "HLD1", 25_000.0)], closes={101: CLOSES["base"], 2: short}, promos=[promo(2, "BBB")])
        self.assertEqual(planned_symbols(result), ["BBB"])  # not blocked
        corr = status(result, "BBB")["risk"]["correlation"]
        self.assertEqual(corr["status"], "UNAVAILABLE")
        self.assertEqual(corr["unavailable_reasons"], {"INSUFFICIENT_COMMON_DAYS": 1})
        self.assertNotIn("strongest", corr)  # no 0.0 stand-in
        self.assertIn("RISK_CORRELATION_UNAVAILABLE:1", result["warnings"])
        rho, days, why = return_correlation(CLOSES["base"], short, window=250, minimum=120)
        self.assertEqual((rho, days, why), (None, 59, "INSUFFICIENT_COMMON_DAYS"))

    # L
    def test_l_missing_prices_are_a_clean_data_quality_case(self) -> None:
        result = build(held=[exposure(101, "HLD1", 25_000.0)], closes={101: CLOSES["base"]}, promos=[promo(2, "BBB")])
        self.assertEqual(planned_symbols(result), ["BBB"])
        corr = status(result, "BBB")["risk"]["correlation"]
        self.assertEqual((corr["status"], corr["unavailable_reasons"]), ("UNAVAILABLE", {"NO_PRICE_HISTORY": 1}))
        self.assertEqual(return_correlation({}, CLOSES["base"], window=250, minimum=120), (None, 0, "NO_PRICE_HISTORY"))
        constant = {day: 100.0 for day in CLOSES["base"]}
        self.assertEqual(return_correlation(constant, CLOSES["base"], window=250, minimum=120)[0::2], (None, "ZERO_VARIANCE"))

    def test_stale_prices_are_not_used(self) -> None:
        old = {day: price for day, price in CLOSES["twin"].items() if day < "2026-09-01"}
        result = build(held=[exposure(101, "HLD1", 25_000.0)], closes={101: CLOSES["base"], 2: old}, promos=[promo(2, "BBB")])
        self.assertEqual(planned_symbols(result), ["BBB"])
        self.assertEqual(status(result, "BBB")["risk"]["correlation"]["unavailable_reasons"], {"STALE_PRICES": 1})

    # M
    def test_m_the_first_planned_entry_changes_the_correlation_exposure_of_the_second(self) -> None:
        tight = replace(StrategyConfig(), risk=RiskGuardConfig(max_sector_weight=0.15))
        closes = {1: CLOSES["twin"], 2: CLOSES["base"]}
        result = build(closes=closes, config=tight)
        self.assertEqual(planned_symbols(result), ["BBB"])
        self.assertEqual(deferred(result), {"AAA": CORRELATION_BLOCK})
        corr = status(result, "AAA")["risk"]["correlation"]
        self.assertEqual(corr["cluster_members"], ["BBB"])  # the planned entry, not a holding
        self.assertAlmostEqual(corr["cluster_current_weight_pct"], 100 * self.first / (100_000.0 + self.first), places=2)  # share of the state after entry 1
        independent = build(closes={1: CLOSES["other"], 2: CLOSES["base"]}, config=tight)
        self.assertEqual(planned_symbols(independent), ["BBB", "AAA"])

    # N
    def test_n_external_funding_does_not_bypass_the_correlation_guard(self) -> None:
        for cash in (30_000.0, 10_000.0):
            result = build(held=[exposure(101, "HLD1", 25_000.0)], closes={101: CLOSES["base"], 2: CLOSES["twin"]}, promos=[promo(2, "BBB")], external=True, cash=cash)
            self.assertEqual(result["planned_entries"], [], cash)
            self.assertEqual(deferred(result), {"BBB": CORRELATION_BLOCK}, cash)

    def test_add_includes_its_own_position_in_the_cluster(self) -> None:
        add = existing("HLD1", 101, "swing", 10, 100.0, "ADD", None, add_purchase_value_eur=6_000.0, add_recommended_quantity=60, add_current_price_eur=100.0)
        closes = {101: CLOSES["base"], 102: CLOSES["twin"]}
        blocked = build(held=[exposure(101, "HLD1", 15_000.0, strategy=StrategyType.SWING), exposure(102, "HLD2", 12_000.0)], closes=closes, promos=[], existing_results=[add])
        item = blocked["existing_position_actions"][0]
        self.assertEqual(item["plan_block_reason"], CORRELATION_BLOCK)  # (15k own + 12k correlated + 6k) / 106k = 31.1 %
        self.assertEqual(item["risk"]["correlation"]["cluster_members"], ["HLD2"])
        self.assertAlmostEqual(item["risk"]["correlation"]["cluster_current_weight_pct"], 27.0, places=2)
        allowed = build(held=[exposure(101, "HLD1", 15_000.0, strategy=StrategyType.SWING), exposure(102, "HLD2", 8_000.0)], closes=closes, promos=[], existing_results=[add])
        self.assertNotIn("plan_block_reason", allowed["existing_position_actions"][0])  # 29k / 106k = 27.4 %


# ------------------------------------------------------------------------------------------------------------------- plan invariants
class PlanInvariantTests(unittest.TestCase):
    def scenario(self, **kw):
        held = [exposure(101, "HLD1", sector_value_for_limit(capital_of(build(guards=False), "BBB")) + 1.0)]
        return build(held=held, sectors={1: "ENERGY", 2: "TECH", 101: "TECH"}, **kw)

    # O
    def test_o_ranking_is_unchanged_by_the_guards(self) -> None:
        free, guarded = build(guards=False), self.scenario()
        key = lambda r: [(c["symbol"], c["rank"], c["entry_score"], c["entry_status"]) for c in r["entry_candidates"]]
        self.assertEqual(key(free), key(guarded))
        self.assertEqual(deferred(guarded), {"BBB": SECTOR_BLOCK})  # rank 1 is blocked, rank 2 is still evaluated and planned, nobody is re-ranked
        self.assertEqual(planned_symbols(guarded), ["AAA"])
        self.assertEqual([p["rank"] for p in guarded["planned_entries"]], [2])

    def test_no_risk_inputs_means_the_previous_plan(self) -> None:
        a = build(guards=False)
        b = build(guards=True, sectors={}, closes={})  # guards on, but nothing is known: nothing is blocked
        for key in ("planned_entries", "deferred_entries", "final_simulated_state", "entry_summary"):
            self.assertEqual(a[key], b[key], key)
        self.assertIn("CONCENTRATION_SINGLE_SECURITY_LIMITS_ONLY", a["warnings"])
        self.assertNotIn("CONCENTRATION_SINGLE_SECURITY_LIMITS_ONLY", b["warnings"])

    # P / Q
    def test_p_q_the_plan_stays_a_simulation_without_orders(self) -> None:
        result = self.scenario()
        self.assertIs(result["simulation_only"], True)
        self.assertIs(result["orders_created"], False)
        self.assertIn("never lifts these guards", json.dumps(result["methodology"]["risk_guards"]))

    def test_a_blocked_candidate_does_not_stop_the_allocation(self) -> None:
        result = self.scenario()
        self.assertFalse([d for d in result["deferred_entries"] if d["reason"].startswith("STOPPED_")])
        self.assertEqual(result["capital"]["limiting_guard"], "ALL_READY_CANDIDATES_PLANNED")

    def test_plan_is_json_serializable_and_deterministic(self) -> None:
        self.assertEqual(json.dumps(self.scenario(), sort_keys=True), json.dumps(self.scenario(), sort_keys=True))


class RiskBookTests(unittest.TestCase):
    def test_sequential_state_and_no_mutation_by_assess(self) -> None:
        inputs = RiskInputs({1: "TECH", 2: "TECH", 101: "TECH"}, {}, {}, AS_OF)
        book = RiskBook(inputs, RiskGuardConfig(), exposures={101: 10_000.0})
        before = dict(book.positions)
        first = book.assess(1, 5_000.0, 100_000.0)
        self.assertEqual(book.positions, before)
        self.assertAlmostEqual(first["sector"]["projected_weight_pct"], 15_000.0 / 105_000.0 * 100.0, places=2)
        book.buy(1, 5_000.0)
        second = book.assess(2, 5_000.0, 105_000.0)
        self.assertAlmostEqual(second["sector"]["current_weight_pct"], 15_000.0 / 105_000.0 * 100.0, places=2)
        book.sell(101, 4_000.0)
        self.assertEqual(book.positions[101], 6_000.0)
        book.sell(101, 99_000.0)
        self.assertEqual(book.positions[101], 0.0)

    def test_config_is_validated(self) -> None:
        for bad in ({"max_sector_weight": 0.0}, {"max_sector_weight": 1.5}, {"correlation_threshold": 0.0}, {"correlation_min_common_days": 5}, {"correlation_window_days": 50, "correlation_min_common_days": 120}):
            with self.assertRaises(ValueError, msg=str(bad)):
                RiskGuardConfig(**bad)
        cfg = RiskGuardConfig()
        self.assertEqual((cfg.max_sector_weight, cfg.correlation_threshold, cfg.correlation_window_days, cfg.correlation_min_common_days), (0.30, 0.70, 250, 120))
        self.assertAlmostEqual(cfg.max_sector_weight, 3 * StrategyConfig().sizing.max_initial_swing_weight)  # three full initial Swing positions

    def test_returns_use_consecutive_common_days_only(self) -> None:
        a = {"2026-01-01": 100.0, "2026-01-02": 101.0, "2026-01-05": 102.0, "2026-01-06": 100.0, "2026-01-07": 103.0}
        b = {"2026-01-01": 50.0, "2026-01-03": 55.0, "2026-01-05": 51.0, "2026-01-06": 50.0, "2026-01-07": 52.0}  # holidays differ
        rho, days, why = return_correlation(a, b, window=250, minimum=3)
        self.assertEqual((days, why), (3, None))  # common days 01, 05, 06, 07 -> 3 returns
        self.assertIsNotNone(rho)
        self.assertEqual(return_correlation(a, b, window=250, minimum=4)[1:], (3, "INSUFFICIENT_COMMON_DAYS"))


# ----------------------------------------------------------------------------------------------------------- loader / orchestrator
def risk_db(*, sectors: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT, isin TEXT, wkn TEXT);
        CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE source_symbols (security_id INTEGER, source_id INTEGER, symbol TEXT);
        CREATE TABLE market_data (security_id INTEGER, trade_date TEXT, close REAL, adjusted_close REAL, source_id INTEGER);
        INSERT INTO data_sources VALUES (1, 'Yahoo Finance'), (2, 'Other');
        INSERT INTO security VALUES (1, 'AAA', 'Alpha Corp', NULL, NULL), (2, 'BBB', 'Beta Corp', NULL, NULL), (3, 'CCC', 'Gamma Corp', NULL, NULL);
        INSERT INTO source_symbols VALUES (1, 1, 'AAA.Y'), (2, 1, 'BBB.Y');
        """
    )
    for sid, key in ((1, "base"), (2, "twin")):
        conn.executemany("INSERT INTO market_data VALUES (?, ?, ?, ?, 1)", [(sid, day, price, price) for day, price in CLOSES[key].items()])
    conn.execute("INSERT INTO market_data VALUES (1, '2026-09-23', 1.0, 1.0, 2)")  # another provider is never mixed into the series
    return conn


class LoaderTests(unittest.TestCase):
    def test_loader_reads_sectors_prices_and_ignores_other_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sector_map_v1.json"
            path.write_text(json.dumps({"sectors": {"TECH": ["AAA.Y"], "ENERGY": ["BBB.Y"]}}), encoding="utf-8")
            (Path(tmp) / "sector_map_local.json").write_text(json.dumps({"sectors": {"TECH": ["CCC"]}}), encoding="utf-8")
            conn = risk_db()
            inputs = load_risk_inputs(conn, [1, 2, 3, 99], as_of=AS_OF, sector_map_path=path)
            conn.close()
        self.assertEqual(dict(inputs.sector_of), {1: "TECH", 2: "ENERGY", 3: "TECH", 99: None})  # Yahoo key first, symbol as fallback, local map merged
        self.assertTrue(inputs.sector_map_available)
        self.assertEqual(inputs.closes[1], CLOSES["base"])
        self.assertEqual(inputs.closes[3], {})
        self.assertNotIn(1.0, inputs.closes[1].values())

    def test_loader_degrades_cleanly(self) -> None:
        conn = sqlite3.connect(":memory:")  # no tables at all
        inputs = load_risk_inputs(conn, [1, 2], as_of=AS_OF, sector_map_path=Path("does-not-exist.json"))
        conn.close()
        self.assertEqual((dict(inputs.sector_of), dict(inputs.closes[1]), inputs.sector_map_available), ({1: None, 2: None}, {}, False))

    def test_shipped_sector_map_is_readable_and_unambiguous(self) -> None:
        from portfolio_risk import DEFAULT_SECTOR_MAP, read_sector_map

        mapping = read_sector_map(DEFAULT_SECTOR_MAP)
        self.assertGreater(len(mapping), 100)


class OrchestratorGuardTests(unittest.TestCase):
    """The orchestrator feeds the planner with stored daily closes and the sector map (synthetic in-memory database)."""

    def setUp(self) -> None:
        self.conn = risk_db()
        self.conn.executescript(
            """
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
        self.snapshots = {1: replace(held, security_id=1, symbol="AAA"), 2: cand(2, "BBB", momentum=40.0)}
        self.promotion = PromotionDecision(2, "BBB", "Security 2", "WATCH", 10, "test", "PROMOTE", ("PROMOTE_REASON",), "token", 100.0, "EUR", 100.0,
                                           95.0, 90.0, None, None, None, None, None, None, 40.0, "available", "available", "available", "available")
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "sector_map_v1.json"
        path.write_text(json.dumps({"sectors": {"TECH": ["AAA.Y", "BBB.Y"]}}), encoding="utf-8")
        self.map_patch = patch("portfolio_risk.DEFAULT_SECTOR_MAP", path)
        self.map_patch.start()

    def tearDown(self) -> None:
        self.map_patch.stop()
        self.tmp.cleanup()
        self.conn.close()

    def exposures(self, value):
        return replace(entry_tests.context(total=100_000.0, swing=20_000.0, cash=30_000.0), exposures=(exposure(1, "AAA", value),))

    def run_with(self, value, as_of=None):
        decisions = {1: DecisionResult(Action.HOLD, 0.8, reasons=("HOLD_TEST",))}
        with patch("trading_orchestrator.build_portfolio_context", return_value=self.exposures(value)), \
             patch("trading_orchestrator.build_analysis_snapshot", side_effect=lambda security_id, **_: self.snapshots[security_id]), \
             patch("trading_orchestrator.decide", side_effect=lambda snapshot, *_: decisions[snapshot.security_id]), \
             patch("trading_orchestrator.evaluate_swing_candidates", return_value=[self.promotion]):
            return run_trading_orchestrator(self.conn, as_of=as_of)

    def test_orchestrator_blocks_by_sector_and_stays_a_simulation(self) -> None:
        small = self.run_with(5_000.0).portfolio_action_plan
        big = self.run_with(40_000.0).portfolio_action_plan
        self.assertEqual([p["symbol"] for p in small["planned_entries"]], ["BBB"])
        self.assertEqual(big["planned_entries"], [])
        self.assertEqual([d["reason"] for d in big["deferred_entries"]], [SECTOR_BLOCK])
        for plan_ in (small, big):
            self.assertIs(plan_["simulation_only"], True)  # P
            self.assertIs(plan_["orders_created"], False)  # Q
        # the existing engine results are not touched by the guard
        self.assertEqual(json.dumps(self.run_with(5_000.0).existing_position_results, sort_keys=True, default=str), json.dumps(self.run_with(40_000.0).existing_position_results, sort_keys=True, default=str))

    def test_guard_failure_is_isolated(self) -> None:
        with patch("trading_orchestrator.load_risk_inputs", side_effect=RuntimeError("boom")):
            plan_ = self.run_with(40_000.0).portfolio_action_plan
        self.assertEqual(plan_["status"], "AVAILABLE")
        self.assertIn("CONCENTRATION_SINGLE_SECURITY_LIMITS_ONLY", plan_["warnings"])
        self.assertEqual([p["symbol"] for p in plan_["planned_entries"]], ["BBB"])


# --------------------------------------------------------------------------------------------------------------------------- compact
class CompactTests(unittest.TestCase):
    def inner(self, result):
        return {"evaluation_as_of": AS_OF, "global_status": "AVAILABLE", "portfolio_action_plan": result, "action_summary": {}}

    def test_the_reason_code_is_transported_without_inflating_the_compact_view(self) -> None:
        held = [exposure(101, "HLD1", sector_value_for_limit(capital_of(build(guards=False), "BBB")) + 1.0)]
        blocked = build(held=held, sectors={1: "ENERGY", 2: "TECH", 101: "TECH"})
        compact = oc.compact_orchestrator_result(self.inner(blocked))
        self.assertEqual([(d["reason"], d["symbols"]) for d in compact["entry_plan"]["deferred"]], [(SECTOR_BLOCK, ["BBB"])])
        text = json.dumps(compact)
        for forbidden in ("projected_weight_pct", "cluster_", "unavailable_reasons", '"risk"', "strongest"):
            self.assertNotIn(forbidden, text)
        free = oc.compact_orchestrator_result(self.inner(build(guards=False)))
        guarded_ok = oc.compact_orchestrator_result(self.inner(build(sectors={1: "ENERGY", 2: "TECH"}, closes={})))
        self.assertLessEqual(len(json.dumps(guarded_ok)) - len(json.dumps(free)), 60)  # at most the one-line data-quality warning

    def test_a_blocked_add_shows_its_reason_in_the_position_engine_block(self) -> None:
        add = existing("HLD1", 101, "swing", 10, 100.0, "ADD", None, add_purchase_value_eur=5_000.0, add_recommended_quantity=50, add_current_price_eur=100.0)
        result = build(held=[exposure(101, "HLD1", 28_000.0, strategy=StrategyType.SWING)], sectors={101: "TECH"}, promos=[], existing_results=[add])
        inner = self.inner(result)
        inner["presentation_summary"] = {"actions": [{"symbol": "HLD1", "security_id": 101, "action": "ADD", "priority": "P3", "reason_codes": ["ADD_TEST"]}]}
        action = oc.compact_orchestrator_result(inner)["position_engine"]["actions"][0]
        self.assertEqual(action["plan_block_reason"], SECTOR_BLOCK)
        self.assertNotIn("risk", action)
