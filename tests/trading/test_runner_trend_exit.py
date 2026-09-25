"""Phase-3B.3 deterministic Swing runner trend-exit tests."""

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


def runner_snapshot(
    *,
    price: float = 90.0,
    sma50: float | None = 100.0,
    sma200: float | None = 80.0,
    original: float = 77.0,
    current: float = 20.0,
    tp2_status: str = "executed",
    tp1_executed: float = 19.0,
    tp2_executed: float = 38.0,
    technical: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    reconciliation: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    campaign_status: str = "open",
    post_tp2_add: bool = False,
    strategy: StrategyType = StrategyType.SWING,
):
    snapshot = campaign_snapshot(
        price=price,
        original=original,
        current=current,
        tp2_status=tp2_status,
        technical=technical,
        reconciliation=reconciliation,
        strategy=strategy,
    )
    technical_context = replace(
        snapshot.technical,
        current_price=price,
        sma50=sma50,
        sma200=sma200,
    )
    position = replace(
        snapshot.position,
        swing_campaign_status=campaign_status,
        tp1_executed_quantity=tp1_executed,
        tp2_executed_quantity=tp2_executed,
        post_tp2_add_detected=post_tp2_add,
    )
    return replace(snapshot, technical=technical_context, position=position)


class RunnerTrendExitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()

    def test_tp2_not_recorded_or_signaled_does_not_activate_runner(self) -> None:
        for status in ("not_recorded", "signaled"):
            result = decide(
                runner_snapshot(price=90, sma50=100, sma200=80, tp2_status=status),
                self.config,
            )
            self.assertNotEqual(result.action, Action.SELL)
            self.assertNotIn("RUNNER_BELOW_SMA200", result.reasons)

    def test_incomplete_tp2_execution_blocks_runner(self) -> None:
        result = decide(
            runner_snapshot(tp1_executed=10, tp2_executed=10), self.config
        )
        self.assertEqual(result.action, Action.HOLD)
        self.assertIn("TP2_EXECUTION_INCOMPLETE", result.reasons)
        self.assertIn("TP2_EXECUTION_INCOMPLETE", result.blocking_data_gaps)

    def test_runner_sma_thresholds_and_precedence(self) -> None:
        above = decide(runner_snapshot(price=100, sma50=100, sma200=80), self.config)
        self.assertEqual(above.action, Action.HOLD)
        self.assertNotIn("RUNNER_BELOW_SMA50", above.reasons)

        warning = decide(runner_snapshot(price=90, sma50=100, sma200=80), self.config)
        self.assertEqual(warning.action, Action.HOLD)
        self.assertIn("RUNNER_BELOW_SMA50", warning.reasons)
        self.assertIn("RUNNER_BELOW_SMA50", warning.risks)

        at_sma200 = decide(runner_snapshot(price=80, sma50=100, sma200=80), self.config)
        self.assertEqual(at_sma200.action, Action.HOLD)
        self.assertIn("RUNNER_BELOW_SMA50", at_sma200.reasons)

        below = decide(runner_snapshot(price=79.99, sma50=100, sma200=80), self.config)
        self.assertEqual(below.action, Action.SELL)
        self.assertIn("RUNNER_BELOW_SMA200", below.reasons)

    def test_runner_sell_uses_current_reconciled_remainder(self) -> None:
        manual_reduction = decide(
            runner_snapshot(price=70, sma50=100, sma200=80, current=17), self.config
        )
        over_target = decide(
            runner_snapshot(
                price=70, sma50=100, sma200=80, current=10,
                tp1_executed=19, tp2_executed=50,
            ),
            self.config,
        )
        for result, quantity in ((manual_reduction, 17), (over_target, 10)):
            self.assertEqual(result.action, Action.SELL)
            self.assertEqual(result.action_quantity, quantity)
            self.assertEqual(result.target_remaining_quantity, 0)
            self.assertEqual(
                result.action_quantity_basis,
                ActionQuantityBasis.RUNNER_FULL_REMAINDER,
            )

    def test_post_tp2_add_is_blocked_without_automatic_inclusion(self) -> None:
        result = decide(
            runner_snapshot(price=70, sma50=100, sma200=80, post_tp2_add=True),
            self.config,
        )
        self.assertEqual(result.action, Action.HOLD)
        self.assertIn("POST_TP2_ADD_POLICY_UNDEFINED", result.reasons)
        self.assertIn("POST_TP2_ADD_POLICY_UNDEFINED", result.blocking_data_gaps)

    def test_invalid_technical_values_hold_not_watch_or_sell(self) -> None:
        for status in (
            AvailabilityStatus.PARTIAL,
            AvailabilityStatus.STALE,
            AvailabilityStatus.INSUFFICIENT,
            AvailabilityStatus.UNAVAILABLE,
        ):
            result = decide(runner_snapshot(technical=status), self.config)
            self.assertEqual(result.action, Action.HOLD)
            self.assertIn("RUNNER_TECHNICAL_INPUT_UNAVAILABLE", result.reasons)
        for kwargs in ({"sma50": None}, {"sma200": None}, {"price": None}):
            result = decide(runner_snapshot(**kwargs), self.config)
            self.assertEqual(result.action, Action.HOLD)
            self.assertIn("RUNNER_TECHNICAL_INPUT_UNAVAILABLE", result.reasons)

    def test_campaign_strategy_reconciliation_and_quantity_safety(self) -> None:
        cases = [
            runner_snapshot(reconciliation=AvailabilityStatus.PARTIAL),
            runner_snapshot(campaign_status="closed"),
            runner_snapshot(current=0),
            runner_snapshot(strategy=StrategyType.LONG_TERM),
            runner_snapshot(strategy=StrategyType.UNKNOWN),
        ]
        for snapshot in cases:
            self.assertNotEqual(decide(snapshot, self.config).action, Action.SELL)

    def test_runner_decision_is_pure(self) -> None:
        snapshot = runner_snapshot(price=70, sma50=100, sma200=80)
        before = repr(snapshot)
        result = decide(snapshot, self.config)
        self.assertEqual(result.action, Action.SELL)
        self.assertEqual(repr(snapshot), before)


if __name__ == "__main__":
    unittest.main()
