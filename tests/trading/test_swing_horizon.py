"""Swing horizon (horizon_months_min/max, remainder_management) tests.

The horizon only ever gates the Phase-3B.3 runner (the still-open remainder
after TP2 execution) -- TP1/TP2, ADD, and the hard stop are all evaluated
before the runner is ever reached and must stay unaffected by campaign age.
"""

from __future__ import annotations

from dataclasses import replace
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import Action, ActionQuantityBasis  # noqa: E402
from decision_engine import decide  # noqa: E402
from strategy_config import StrategyConfig, SwingStrategyConfig  # noqa: E402
from test_runner_trend_exit import runner_snapshot  # noqa: E402
from test_swing_tp_actions import campaign_snapshot  # noqa: E402

OPENED_AT = "2026-01-01"
# Chosen so the whole-calendar-month arithmetic in decision_engine falls
# unambiguously on each side of the default 3/6-month boundaries.
LESS_THAN_3_MONTHS = "2026-03-15"  # age = 2 months
BETWEEN_3_AND_6_MONTHS = "2026-06-15"  # age = 5 months
AT_6_MONTHS = "2026-07-01"  # age = 6 months, exact boundary
BEYOND_6_MONTHS = "2026-09-27"  # age = 8 months


class SwingHorizonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()

    # --- Rule 3: before horizon_months_min, existing runner logic only ---

    def test_before_min_positive_trend_holds_as_before(self) -> None:
        result = decide(
            runner_snapshot(
                price=100, sma50=100, sma200=80,
                opened_at=OPENED_AT, evaluation_as_of=LESS_THAN_3_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(result.action, Action.HOLD)
        self.assertNotIn("SWING_MAX_HORIZON_REACHED", result.reasons)

    def test_before_min_trend_break_still_exits(self) -> None:
        result = decide(
            runner_snapshot(
                price=79.99, sma50=100, sma200=80,
                opened_at=OPENED_AT, evaluation_as_of=LESS_THAN_3_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(result.action, Action.SELL)
        self.assertIn("RUNNER_BELOW_SMA200", result.reasons)
        self.assertNotIn("SWING_MAX_HORIZON_REACHED", result.reasons)

    # --- Rule 4: between min and max, momentum_guided uses the existing
    # SMA50/SMA200 trend logic unchanged ---

    def test_between_min_and_max_positive_trend_holds(self) -> None:
        result = decide(
            runner_snapshot(
                price=100, sma50=100, sma200=80,
                opened_at=OPENED_AT, evaluation_as_of=BETWEEN_3_AND_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(result.action, Action.HOLD)
        self.assertNotIn("SWING_MAX_HORIZON_REACHED", result.reasons)

    def test_between_min_and_max_trend_break_exits(self) -> None:
        result = decide(
            runner_snapshot(
                price=79.99, sma50=100, sma200=80,
                opened_at=OPENED_AT, evaluation_as_of=BETWEEN_3_AND_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(result.action, Action.SELL)
        self.assertIn("RUNNER_BELOW_SMA200", result.reasons)
        self.assertNotIn("SWING_MAX_HORIZON_REACHED", result.reasons)

    # --- Rule 5: at/after horizon_months_max, close regardless of trend ---

    def test_at_max_horizon_closes_even_with_positive_trend(self) -> None:
        result = decide(
            runner_snapshot(
                price=120, sma50=100, sma200=80, current=20, original=77,
                opened_at=OPENED_AT, evaluation_as_of=AT_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(result.action, Action.SELL)
        self.assertIn("SWING_MAX_HORIZON_REACHED", result.reasons)
        self.assertEqual(result.action_quantity, 20)
        self.assertEqual(result.target_remaining_quantity, 0)
        self.assertEqual(
            result.action_quantity_basis, ActionQuantityBasis.RUNNER_FULL_REMAINDER
        )

    def test_beyond_max_horizon_closes_even_with_positive_trend(self) -> None:
        result = decide(
            runner_snapshot(
                price=120, sma50=100, sma200=80,
                opened_at=OPENED_AT, evaluation_as_of=BEYOND_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(result.action, Action.SELL)
        self.assertIn("SWING_MAX_HORIZON_REACHED", result.reasons)

    def test_at_max_horizon_closes_without_fresh_technical_data(self) -> None:
        # A horizon-driven close does not depend on SMA availability.
        from analysis_contracts import AvailabilityStatus

        result = decide(
            runner_snapshot(
                technical=AvailabilityStatus.UNAVAILABLE,
                opened_at=OPENED_AT, evaluation_as_of=BEYOND_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(result.action, Action.SELL)
        self.assertIn("SWING_MAX_HORIZON_REACHED", result.reasons)

    # --- Rule 6: hard stop keeps its existing, unrelated priority ---

    def test_hard_stop_still_triggers_before_runner_regardless_of_campaign_age(
        self,
    ) -> None:
        # tp2_status defaults to "not_recorded": this is the pre-runner path,
        # where the existing hard stop is evaluated before TP2 execution --
        # unaffected by (and evaluated before) any horizon concern.
        result = decide(
            campaign_snapshot(
                price=70.0, tp2_status="not_recorded",
                opened_at=OPENED_AT, evaluation_as_of=BEYOND_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(result.action, Action.SELL)
        self.assertIn("HARD_STOP_TRIGGERED", result.reasons)
        self.assertEqual(
            result.action_quantity_basis, ActionQuantityBasis.STOP_FULL_EXIT
        )
        self.assertNotIn("SWING_MAX_HORIZON_REACHED", result.reasons)

    # --- Rule 2/7: values come only from SwingStrategyConfig, no hardcoding ---

    def test_differing_config_horizon_values_change_the_outcome(self) -> None:
        # Same campaign age (5 months): default config (max=6) still holds on
        # a positive trend, but a lowered horizon_months_max=3 forces a close.
        default_result = decide(
            runner_snapshot(
                price=100, sma50=100, sma200=80,
                opened_at=OPENED_AT, evaluation_as_of=BETWEEN_3_AND_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(default_result.action, Action.HOLD)

        shorter_horizon_config = StrategyConfig(
            swing=replace(SwingStrategyConfig(), horizon_months_max=3)
        )
        shortened_result = decide(
            runner_snapshot(
                price=100, sma50=100, sma200=80,
                opened_at=OPENED_AT, evaluation_as_of=BETWEEN_3_AND_6_MONTHS,
            ),
            shorter_horizon_config,
        )
        self.assertEqual(shortened_result.action, Action.SELL)
        self.assertIn("SWING_MAX_HORIZON_REACHED", shortened_result.reasons)

    def test_horizon_months_min_is_not_a_minimum_holding_requirement(self) -> None:
        # Raising horizon_months_min must not create a new holding
        # constraint: a trend break below min still exits exactly as before.
        raised_min_config = StrategyConfig(
            swing=replace(SwingStrategyConfig(), horizon_months_min=5)
        )
        result = decide(
            runner_snapshot(
                price=79.99, sma50=100, sma200=80,
                opened_at=OPENED_AT, evaluation_as_of=LESS_THAN_3_MONTHS,
            ),
            raised_min_config,
        )
        self.assertEqual(result.action, Action.SELL)
        self.assertIn("RUNNER_BELOW_SMA200", result.reasons)

    # --- Rule 8: TP1/TP2 stay unaffected by campaign age ---

    def test_tp1_tp2_are_unaffected_by_campaign_age(self) -> None:
        tp1 = decide(
            campaign_snapshot(
                price=120, original=8, opened_at=OPENED_AT,
                evaluation_as_of=BEYOND_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(tp1.action, Action.TRIM)
        self.assertEqual(tp1.action_quantity, 2)
        self.assertEqual(
            tp1.action_quantity_basis, ActionQuantityBasis.TP1_25_PERCENT_ORIGINAL
        )

        tp2 = decide(
            campaign_snapshot(
                price=125, original=77, opened_at=OPENED_AT,
                evaluation_as_of=BEYOND_6_MONTHS,
            ),
            self.config,
        )
        self.assertEqual(tp2.action, Action.TRIM)
        self.assertEqual(tp2.action_quantity, 57)
        self.assertEqual(
            tp2.action_quantity_basis,
            ActionQuantityBasis.TP2_75_PERCENT_CUMULATIVE_ORIGINAL,
        )

    # --- Missing/unparseable campaign age never applies the new rule ---

    def test_missing_opened_at_keeps_existing_behavior(self) -> None:
        result = decide(
            runner_snapshot(price=100, sma50=100, sma200=80, opened_at=None),
            self.config,
        )
        self.assertEqual(result.action, Action.HOLD)
        self.assertNotIn("SWING_MAX_HORIZON_REACHED", result.reasons)


if __name__ == "__main__":
    unittest.main()
