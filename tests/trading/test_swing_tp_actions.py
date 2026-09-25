"""Phase-3B.2 deterministic Swing TP recommendation tests."""

from __future__ import annotations

from dataclasses import replace
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import (  # noqa: E402
    Action,
    ActionQuantityBasis,
    AvailabilityStatus,
    DataQuality,
    StrategyType,
)
from decision_engine import decide  # noqa: E402
from strategy_config import StrategyConfig  # noqa: E402
from test_decision_engine import make_snapshot  # noqa: E402


def campaign_snapshot(
    *,
    price: float,
    original: float = 77,
    current: float | None = None,
    tp1_status: str = "not_recorded",
    tp2_status: str = "not_recorded",
    technical: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    lifecycle: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    reconciliation: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    reference_currency: str = "USD",
    valuation_currency: str = "USD",
    fx_status: AvailabilityStatus = AvailabilityStatus.NOT_APPLICABLE,
    strategy: StrategyType = StrategyType.SWING,
):
    current = original if current is None else current
    snapshot = make_snapshot(
        strategy=strategy,
        technical_status=technical,
        price=price,
        position_currency=valuation_currency,
        market_currency=valuation_currency,
    )
    position = replace(
        snapshot.position,
        shares=current,
        swing_campaign_id=1,
        swing_campaign_status="open",
        swing_campaign_original_quantity=original,
        swing_campaign_reference_avg_cost=100.0,
        swing_campaign_reference_currency=reference_currency,
        tp1_lifecycle_status=tp1_status,
        tp2_lifecycle_status=tp2_status,
        lifecycle_quality=DataQuality(lifecycle),
        campaign_reconciliation_quality=DataQuality(reconciliation),
        current_price_cost_currency=(price if fx_status in {AvailabilityStatus.AVAILABLE, AvailabilityStatus.NOT_APPLICABLE} else None),
        valuation_currency=valuation_currency,
        fx_quality=DataQuality(fx_status),
    )
    return replace(snapshot, position=position)


class SwingTpActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()

    def test_tp1_thresholds_and_signaled_state_recommend_trim(self) -> None:
        below = decide(campaign_snapshot(price=119.99, original=8), self.config)
        self.assertEqual(below.action, Action.HOLD)

        exact = decide(campaign_snapshot(price=120, original=8), self.config)
        self.assertEqual(exact.action, Action.TRIM)
        self.assertEqual(exact.action_quantity, 2)
        self.assertEqual(exact.target_remaining_quantity, 6)
        self.assertEqual(exact.action_quantity_basis, ActionQuantityBasis.TP1_25_PERCENT_ORIGINAL)

        between = decide(campaign_snapshot(price=124, original=77, tp1_status="signaled"), self.config)
        self.assertEqual(between.action, Action.TRIM)
        self.assertEqual(between.action_quantity, 19)
        self.assertIn("TP1_REACHED", between.reasons)

    def test_tp1_executed_is_not_recommended_again(self) -> None:
        result = decide(campaign_snapshot(price=124, tp1_status="executed"), self.config)
        self.assertEqual(result.action, Action.HOLD)
        self.assertIsNone(result.action_quantity)

    def test_tp2_is_cumulative_and_takes_precedence(self) -> None:
        exact = decide(campaign_snapshot(price=125, original=77), self.config)
        above = decide(campaign_snapshot(price=140, original=77), self.config)
        for result in (exact, above):
            self.assertEqual(result.action, Action.TRIM)
            self.assertEqual(result.action_quantity, 57)
            self.assertEqual(result.target_remaining_quantity, 20)
            self.assertEqual(
                result.action_quantity_basis,
                ActionQuantityBasis.TP2_75_PERCENT_CUMULATIVE_ORIGINAL,
            )
            self.assertIn("TP2_REACHED", result.reasons)

        runner = decide(campaign_snapshot(price=125, original=8), self.config)
        self.assertEqual(runner.action_quantity, 6)
        self.assertEqual(runner.target_remaining_quantity, 2)

        # The campaign reference, not a subsequently changed position average,
        # remains the immutable TP basis.
        fixed_baseline = campaign_snapshot(price=125, original=77)
        fixed_baseline = replace(
            fixed_baseline,
            position=replace(fixed_baseline.position, avg_cost=1_000),
        )
        self.assertEqual(decide(fixed_baseline, self.config).action_quantity, 57)

    def test_tp2_catches_up_after_explicit_tp1_and_stops_after_tp2_execution(self) -> None:
        catch_up = decide(
            campaign_snapshot(price=125, original=77, current=58, tp1_status="executed"),
            self.config,
        )
        self.assertEqual(catch_up.action, Action.TRIM)
        self.assertEqual(catch_up.action_quantity, 38)
        self.assertEqual(catch_up.target_remaining_quantity, 20)

        executed = decide(
            campaign_snapshot(price=140, tp1_status="not_recorded", tp2_status="executed"),
            self.config,
        )
        self.assertEqual(executed.action, Action.HOLD)
        # An explicit TP2 execution closes both TP recommendation paths, even
        # when the current price has fallen back into the TP1-only range.
        executed_below_tp2 = decide(
            campaign_snapshot(price=124, tp1_status="not_recorded", tp2_status="executed"),
            self.config,
        )
        self.assertEqual(executed_below_tp2.action, Action.HOLD)

    def test_floor_rounding_zero_target_and_current_position_cap(self) -> None:
        original_33_tp1 = decide(campaign_snapshot(price=120, original=33), self.config)
        self.assertEqual(original_33_tp1.action_quantity, 8)
        self.assertEqual(original_33_tp1.target_remaining_quantity, 25)
        original_33_tp2 = decide(campaign_snapshot(price=125, original=33), self.config)
        self.assertEqual(original_33_tp2.action_quantity, 24)
        self.assertEqual(original_33_tp2.target_remaining_quantity, 9)

        zero = decide(campaign_snapshot(price=120, original=3), self.config)
        self.assertEqual(zero.action, Action.HOLD)
        self.assertIsNone(zero.action_quantity)

        capped = decide(campaign_snapshot(price=125, original=77, current=47), self.config)
        self.assertEqual(capped.action_quantity, 27)
        self.assertEqual(capped.target_remaining_quantity, 20)

    def test_manual_reduction_is_not_inferred_as_tp_execution(self) -> None:
        # A reconciled current quantity below original is not a TP execution
        # without an explicit lifecycle status change.
        result = decide(campaign_snapshot(price=125, original=77, current=47), self.config)
        self.assertEqual(result.action, Action.TRIM)
        self.assertEqual(result.action_quantity, 27)

    def test_safety_conditions_suppress_trim(self) -> None:
        no_campaign = campaign_snapshot(price=125)
        cases = [
            replace(no_campaign, position=replace(no_campaign.position, swing_campaign_id=None)),
            campaign_snapshot(price=125, technical=AvailabilityStatus.PARTIAL),
            campaign_snapshot(price=125, lifecycle=AvailabilityStatus.UNAVAILABLE),
            campaign_snapshot(price=125, reconciliation=AvailabilityStatus.PARTIAL),
            campaign_snapshot(price=125, fx_status=AvailabilityStatus.UNAVAILABLE),
            campaign_snapshot(price=125, fx_status=AvailabilityStatus.STALE),
            campaign_snapshot(price=125, reference_currency="EUR", valuation_currency="USD"),
            campaign_snapshot(price=125, strategy=StrategyType.LONG_TERM),
            campaign_snapshot(price=125, strategy=StrategyType.UNKNOWN),
        ]
        for snapshot in cases:
            result = decide(snapshot, self.config)
            self.assertNotEqual(result.action, Action.TRIM)

        missing_reference = campaign_snapshot(price=125)
        missing_reference = replace(
            missing_reference,
            position=replace(missing_reference.position, swing_campaign_reference_avg_cost=None),
        )
        result = decide(missing_reference, self.config)
        self.assertEqual(result.action, Action.HOLD)
        self.assertIn("SWING_TP_CAMPAIGN_REFERENCE_UNAVAILABLE", result.risks)

    def test_stale_technical_keeps_watch_and_decision_does_not_mutate_snapshot(self) -> None:
        stale = decide(campaign_snapshot(price=125, technical=AvailabilityStatus.STALE), self.config)
        self.assertEqual(stale.action, Action.WATCH)
        self.assertNotIn(stale.action, {Action.BUY, Action.ADD, Action.SELL})

        snapshot = campaign_snapshot(price=125)
        before = repr(snapshot)
        result = decide(snapshot, self.config)
        self.assertEqual(result.action, Action.TRIM)
        self.assertEqual(repr(snapshot), before)


if __name__ == "__main__":
    unittest.main()
