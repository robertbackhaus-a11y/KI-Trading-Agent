"""Phase-3B.4 deterministic Swing campaign hard-stop tests."""

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
from test_swing_tp_actions import campaign_snapshot  # noqa: E402


def hard_stop_snapshot(
    *,
    valuation: float = 86.0,
    current: float = 77.0,
    tp1_status: str = "not_recorded",
    tp2_status: str = "not_recorded",
    technical: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    reconciliation: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    reference_currency: str = "EUR",
    valuation_currency: str = "EUR",
    fx_status: AvailabilityStatus = AvailabilityStatus.NOT_APPLICABLE,
    campaign_status: str = "open",
    strategy: StrategyType = StrategyType.SWING,
):
    snapshot = campaign_snapshot(
        price=valuation,
        current=current,
        tp1_status=tp1_status,
        tp2_status=tp2_status,
        technical=technical,
        reconciliation=reconciliation,
        reference_currency=reference_currency,
        valuation_currency=valuation_currency,
        fx_status=fx_status,
        strategy=strategy,
    )
    return replace(
        snapshot,
        position=replace(snapshot.position, swing_campaign_status=campaign_status),
    )


class SwingHardStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()

    def test_threshold_boundaries_gap_and_full_position_quantity(self) -> None:
        above = decide(hard_stop_snapshot(valuation=85.01), self.config)
        exact = decide(hard_stop_snapshot(valuation=85), self.config)
        below = decide(hard_stop_snapshot(valuation=84.99), self.config)
        gap = decide(hard_stop_snapshot(valuation=50, current=31), self.config)

        for result in (above, exact):
            self.assertEqual(result.action, Action.HOLD)
            self.assertEqual(result.stop, 85)
            self.assertEqual(result.stop_price_currency, "EUR")
        for result, quantity in ((below, 77), (gap, 31)):
            self.assertEqual(result.action, Action.SELL)
            self.assertEqual(result.stop, 85)
            self.assertEqual(result.action_quantity, quantity)
            self.assertEqual(result.target_remaining_quantity, 0)
            self.assertEqual(result.action_quantity_basis, ActionQuantityBasis.STOP_FULL_EXIT)
            self.assertIn("HARD_STOP_TRIGGERED", result.reasons)

    def test_immutable_campaign_basis_and_valid_fx_valuation(self) -> None:
        baseline = hard_stop_snapshot(valuation=84)
        changed_position_average = replace(
            baseline,
            position=replace(baseline.position, avg_cost=1_000),
        )
        result = decide(changed_position_average, self.config)
        self.assertEqual(result.action, Action.SELL)
        self.assertEqual(result.stop, 85)

        fx_snapshot = hard_stop_snapshot(
            valuation=84,
            reference_currency="EUR",
            valuation_currency="EUR",
            fx_status=AvailabilityStatus.AVAILABLE,
        )
        fx_snapshot = replace(
            fx_snapshot,
            position=replace(
                fx_snapshot.position,
                current_price_native=100,
                current_price_currency="USD",
                current_price_cost_currency=84,
                fx_quality=DataQuality(AvailabilityStatus.AVAILABLE),
            ),
        )
        self.assertEqual(decide(fx_snapshot, self.config).action, Action.SELL)

    def test_missing_stale_fx_and_currency_mismatch_block_hard_stop(self) -> None:
        cases = [
            hard_stop_snapshot(valuation=84, fx_status=AvailabilityStatus.UNAVAILABLE),
            hard_stop_snapshot(valuation=84, fx_status=AvailabilityStatus.STALE),
            hard_stop_snapshot(
                valuation=84,
                reference_currency="EUR",
                valuation_currency="USD",
            ),
        ]
        for snapshot in cases:
            result = decide(snapshot, self.config)
            self.assertEqual(result.action, Action.HOLD)
            self.assertIn("HARD_STOP_EVALUATION_BLOCKED", result.reasons)
            self.assertTrue(result.blocking_data_gaps)

    def test_hard_stop_is_active_through_tp1_and_bypassed_after_tp2(self) -> None:
        pre_tp1 = decide(hard_stop_snapshot(valuation=84), self.config)
        after_tp1 = decide(
            hard_stop_snapshot(valuation=84, tp1_status="executed"), self.config
        )
        for result in (pre_tp1, after_tp1):
            self.assertEqual(result.action, Action.SELL)
            self.assertEqual(result.action_quantity_basis, ActionQuantityBasis.STOP_FULL_EXIT)

        after_tp2 = hard_stop_snapshot(valuation=84, tp2_status="executed")
        after_tp2 = replace(
            after_tp2,
            technical=replace(after_tp2.technical, current_price=70, sma50=100, sma200=80),
            position=replace(
                after_tp2.position,
                tp1_executed_quantity=19,
                tp2_executed_quantity=38,
            ),
        )
        runner = decide(after_tp2, self.config)
        self.assertEqual(runner.action, Action.SELL)
        self.assertEqual(runner.action_quantity_basis, ActionQuantityBasis.RUNNER_FULL_REMAINDER)
        self.assertIsNone(runner.stop)

    def test_hard_stop_precedes_pathological_tp_targets_and_ignores_technical_data(self) -> None:
        inconsistent = StrategyConfig(
            swing=replace(self.config.swing, tp1_gain_pct=-0.20, tp2_gain_pct=-0.10)
        )
        snapshot = hard_stop_snapshot(valuation=84, technical=AvailabilityStatus.STALE)
        result = decide(snapshot, inconsistent)
        self.assertEqual(result.action, Action.SELL)
        self.assertEqual(result.action_quantity_basis, ActionQuantityBasis.STOP_FULL_EXIT)
        self.assertNotIn(Action.TRIM, (result.action,))

    def test_campaign_reconciliation_and_quantity_safety(self) -> None:
        cases = [
            hard_stop_snapshot(valuation=84, reconciliation=AvailabilityStatus.PARTIAL),
            hard_stop_snapshot(valuation=84, campaign_status="closed"),
            hard_stop_snapshot(valuation=84, current=0),
            hard_stop_snapshot(valuation=84, strategy=StrategyType.LONG_TERM),
            hard_stop_snapshot(valuation=84, strategy=StrategyType.UNKNOWN),
        ]
        for snapshot in cases:
            result = decide(snapshot, self.config)
            self.assertNotEqual(result.action, Action.SELL)

    def test_hard_stop_decision_is_pure(self) -> None:
        snapshot = hard_stop_snapshot(valuation=84)
        before = repr(snapshot)
        result = decide(snapshot, self.config)
        self.assertEqual(result.action, Action.SELL)
        self.assertEqual(repr(snapshot), before)


if __name__ == "__main__":
    unittest.main()
