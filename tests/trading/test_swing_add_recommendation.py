"""Phase-3B.5e strength-only Swing ADD eligibility and sizing tests."""

from __future__ import annotations

from dataclasses import replace
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from add_sizing import evaluate_add_recommendation  # noqa: E402
from analysis_contracts import (  # noqa: E402
    Action,
    AvailabilityStatus,
    DataQuality,
    PortfolioContext,
    PortfolioSecurityExposure,
    StrategyType,
    TechnicalAnalysis,
)
from decision_engine import decide  # noqa: E402
from strategy_config import StrategyConfig  # noqa: E402
from test_decision_engine import make_snapshot  # noqa: E402


AVAILABLE = DataQuality(AvailabilityStatus.AVAILABLE, as_of="2026-09-23")


def swing_snapshot(
    *,
    original: float = 27,
    native_price: float = 110,
    reference: float = 100,
    strategy: StrategyType = StrategyType.SWING,
    reconciliation: float = 0.0,
    tp1: str = "not_recorded",
    tp2: str = "not_recorded",
    add_events: int = 0,
    technical_status: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    sma50: float = 105,
    sma200: float = 102,
):
    snapshot = make_snapshot(
        strategy=strategy,
        price=native_price,
        position_currency="EUR",
        market_currency="EUR",
        technical_status=technical_status,
    )
    technical = TechnicalAnalysis(
        quality=DataQuality(technical_status),
        current_price=native_price,
        sma50=sma50,
        sma200=sma200,
        data_points=252,
    )
    position = replace(
        snapshot.position,
        shares=original,
        swing_campaign_id=1,
        swing_campaign_status="open",
        swing_campaign_original_quantity=original,
        swing_campaign_reference_avg_cost=reference,
        swing_campaign_reference_currency="EUR",
        tp1_lifecycle_status=tp1,
        tp2_lifecycle_status=tp2,
        add_event_count=add_events,
        lifecycle_quality=AVAILABLE,
        campaign_reconciliation_quality=AVAILABLE,
        campaign_reconciliation_delta=reconciliation,
        current_price_cost_currency=native_price,
        valuation_currency="EUR",
        fx_quality=DataQuality(AvailabilityStatus.NOT_APPLICABLE),
    )
    return replace(snapshot, technical=technical, position=position)


def portfolio(
    *,
    security_value: float = 1_000,
    swing_value: float = 3_000,
    total: float = 10_000,
    price: float = 110,
    cash: float | None = 20_000,
    buying_power: float | None = None,
    allocation_status: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
) -> PortfolioContext:
    cash_quality = DataQuality(
        AvailabilityStatus.AVAILABLE if cash is not None else AvailabilityStatus.UNAVAILABLE
    )
    buying_quality = DataQuality(
        AvailabilityStatus.AVAILABLE if buying_power is not None else AvailabilityStatus.UNAVAILABLE
    )
    exposure = PortfolioSecurityExposure(
        security_id=1,
        symbol="ABC",
        strategy=StrategyType.SWING,
        quantity=27,
        market_value_eur=security_value,
        market_price_native=price,
        market_price_currency="EUR",
        market_price_eur=price,
        valuation_quality=AVAILABLE,
        strategy_quality=AVAILABLE,
    )
    allocation = DataQuality(allocation_status)
    return PortfolioContext(
        evaluation_as_of="2026-09-23",
        base_currency="EUR",
        total_market_value=total,
        valuation_quality=AVAILABLE,
        swing_market_value=swing_value,
        swing_weight_pct=swing_value / total * 100,
        long_term_market_value=total - swing_value,
        long_term_weight_pct=(total - swing_value) / total * 100,
        cash_available=cash,
        cash_quality=cash_quality,
        buying_power=buying_power,
        buying_power_quality=buying_quality,
        capital_state_as_of="2026-09-23",
        capital_state_source="test",
        current_security_id=1,
        current_security_market_value=security_value,
        current_security_market_value_eur=security_value,
        current_security_weight_pct=security_value / total * 100,
        allocation_quality=allocation,
        exposures=(exposure,),
    )


class SwingAddRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()

    def evaluate(self, snapshot=None, context=None):
        return evaluate_add_recommendation(
            snapshot or swing_snapshot(), context or portfolio(), self.config
        )

    def test_base_quantities_are_floor_of_immutable_original_quantity(self) -> None:
        for original, expected in ((27, 6), (76, 19), (13, 3)):
            with self.subTest(original=original):
                result = self.evaluate(swing_snapshot(original=original))
                self.assertEqual(result.base_quantity, expected)

    def test_strength_only_eligibility_and_add_decision(self) -> None:
        result = self.evaluate()
        self.assertTrue(result.eligible)
        self.assertEqual(result.recommended_quantity, 6)
        self.assertEqual(result.purchase_value_eur, 660)
        decision = decide(swing_snapshot(), self.config, portfolio())
        self.assertEqual(decision.action, Action.ADD)
        self.assertEqual(decision.action_quantity, 6)
        self.assertEqual(decision.add_current_price_eur, 110)
        self.assertEqual(decision.add_reference_price_eur, 100)

    def test_campaign_and_strength_blockers_are_machine_readable(self) -> None:
        cases = (
            (swing_snapshot(strategy=StrategyType.LONG_TERM), "ADD_NOT_SWING"),
            (replace(swing_snapshot(), position=replace(swing_snapshot().position, swing_campaign_id=None)), "ADD_NO_OPEN_CAMPAIGN"),
            (swing_snapshot(tp1="executed"), "ADD_TP1_ALREADY_EXECUTED"),
            (swing_snapshot(tp2="executed"), "ADD_TP2_ALREADY_EXECUTED"),
            (swing_snapshot(add_events=1), "ADD_ALREADY_USED"),
            (swing_snapshot(native_price=99, sma50=98, sma200=97), "ADD_CAMPAIGN_NOT_PROFITABLE", portfolio(price=99)),
            (swing_snapshot(native_price=100, sma50=99, sma200=98), "ADD_BELOW_REFERENCE_COST", portfolio(price=100)),
            (swing_snapshot(native_price=105, sma50=105), "ADD_BELOW_OR_EQUAL_SMA50"),
            (swing_snapshot(native_price=102, sma50=101, sma200=102), "ADD_BELOW_OR_EQUAL_SMA200"),
            (swing_snapshot(reconciliation=1), "ADD_LIFECYCLE_UNRECONCILED"),
            (swing_snapshot(technical_status=AvailabilityStatus.STALE), "ADD_TECHNICAL_DATA_UNAVAILABLE"),
        )
        for case in cases:
            snapshot, blocker, *context = case
            with self.subTest(blocker=blocker):
                self.assertIn(blocker, self.evaluate(snapshot, *(context or [None])).block_reasons)

    def test_cash_security_swing_and_combined_caps_use_minimum_whole_quantity(self) -> None:
        self.assertEqual(self.evaluate(context=portfolio(cash=10_550)).recommended_quantity, 5)
        self.assertIn("ADD_CASH_RESERVE_LIMIT", self.evaluate(context=portfolio(cash=10_000)).block_reasons)
        self.assertEqual(self.evaluate(context=portfolio(security_value=1_850)).recommended_quantity, 1)
        self.assertEqual(self.evaluate(context=portfolio(swing_value=3_800)).recommended_quantity, 3)
        self.assertEqual(
            self.evaluate(context=portfolio(security_value=1_850, swing_value=3_800, cash=10_220)).recommended_quantity,
            1,
        )

    def test_portfolio_boundaries_and_capital_availability(self) -> None:
        self.assertIn("ADD_SWING_ALLOCATION_LIMIT", self.evaluate(context=portfolio(swing_value=4_000)).block_reasons)
        exactly = self.evaluate(context=portfolio(swing_value=3_934, security_value=1_000))
        self.assertAlmostEqual(exactly.projected_swing_allocation or 0, 0.40, places=6)
        security_exact = self.evaluate(context=portfolio(security_value=1_912, swing_value=3_000))
        self.assertAlmostEqual(security_exact.projected_security_weight or 0, 0.20, places=6)
        self.assertIn("ADD_CASH_UNAVAILABLE", self.evaluate(context=portfolio(cash=None)).block_reasons)
        restricted = self.evaluate(context=portfolio(buying_power=10_330))
        self.assertEqual(restricted.recommended_quantity, 3)
        self.assertGreaterEqual(restricted.cash_after or 0, 10_000)

    def test_sell_side_precedence_and_purity(self) -> None:
        stop = decide(swing_snapshot(native_price=84, sma50=80, sma200=79), self.config, portfolio())
        self.assertEqual(stop.action, Action.SELL)
        tp1 = decide(swing_snapshot(native_price=120, sma50=110, sma200=100), self.config, portfolio())
        self.assertEqual(tp1.action, Action.TRIM)
        tp2 = decide(swing_snapshot(native_price=125, sma50=110, sma200=100), self.config, portfolio())
        self.assertEqual(tp2.action, Action.TRIM)
        runner = decide(swing_snapshot(tp2="executed"), self.config, portfolio())
        self.assertNotEqual(runner.action, Action.ADD)
        snapshot = swing_snapshot()
        context = portfolio()
        before = (snapshot.position, context)
        self.evaluate(snapshot, context)
        self.assertEqual((snapshot.position, context), before)

    def test_zero_base_quantity_is_not_actionable(self) -> None:
        result = self.evaluate(swing_snapshot(original=3))
        self.assertFalse(result.eligible)
        self.assertIn("ADD_QUANTITY_ZERO", result.block_reasons)


if __name__ == "__main__":
    unittest.main()
