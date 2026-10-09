"""decision_engine._confidence() weighting tests.

valuation/event_risk are backed by estimates/ratings/price_targets/events/
news -- tables with no populating importer today (known inconsistency #4 in
docs/trading-agent-architecture.en.md). Their UNAVAILABLE state now reflects a
missing data pipeline, not a diagnostic finding, and carries no penalty.
fundamental (backed by the actively populated `fundamentals` table) is
unchanged.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import Action, AvailabilityStatus  # noqa: E402
from decision_engine import decide, _confidence  # noqa: E402
from strategy_config import StrategyConfig  # noqa: E402
from test_decision_engine import make_snapshot  # noqa: E402


class ConfidenceScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()

    # --- fundamental: unchanged behavior ---

    def test_fundamental_unavailable_still_penalized(self) -> None:
        snapshot = make_snapshot(fundamental_status=AvailabilityStatus.UNAVAILABLE)
        # 0.50 - 0.05 (fundamental unavailable) + 0.00 + 0.00 (valuation/event_risk
        # unavailable, both neutral by default in make_snapshot)
        self.assertEqual(_confidence(snapshot), 0.45)

    def test_fundamental_available_and_partial_bonuses_unchanged(self) -> None:
        available = make_snapshot(fundamental_status=AvailabilityStatus.AVAILABLE)
        partial = make_snapshot(fundamental_status=AvailabilityStatus.PARTIAL)
        # baseline 0.50 + fundamental bonus (valuation/event_risk unavailable -> 0.00 each)
        self.assertEqual(_confidence(available), 0.60)
        self.assertEqual(_confidence(partial), 0.55)

    # --- valuation: UNAVAILABLE is now neutral, AVAILABLE/PARTIAL unchanged ---

    def test_valuation_unavailable_is_neutral(self) -> None:
        snapshot = make_snapshot(
            valuation_status=AvailabilityStatus.UNAVAILABLE,
            event_risk_status=AvailabilityStatus.AVAILABLE,
        )
        # 0.50 + 0.10 (fundamental available, default) + 0.00 (valuation neutral) + 0.05 (event_risk available)
        self.assertEqual(_confidence(snapshot), 0.65)

    def test_valuation_available_and_partial_bonuses_unchanged(self) -> None:
        available = make_snapshot(valuation_status=AvailabilityStatus.AVAILABLE)
        partial = make_snapshot(valuation_status=AvailabilityStatus.PARTIAL)
        # baseline 0.50 + fundamental 0.10 + valuation bonus (event_risk unavailable -> 0.00)
        self.assertEqual(_confidence(available), 0.65)
        self.assertEqual(_confidence(partial), 0.62)

    # --- event_risk: UNAVAILABLE is now neutral, AVAILABLE/PARTIAL unchanged ---

    def test_event_risk_unavailable_is_neutral(self) -> None:
        snapshot = make_snapshot(
            event_risk_status=AvailabilityStatus.UNAVAILABLE,
            valuation_status=AvailabilityStatus.AVAILABLE,
        )
        # 0.50 + 0.10 (fundamental) + 0.05 (valuation available) + 0.00 (event_risk neutral)
        self.assertEqual(_confidence(snapshot), 0.65)

    def test_event_risk_available_and_partial_bonuses_unchanged(self) -> None:
        available = make_snapshot(event_risk_status=AvailabilityStatus.AVAILABLE)
        partial = make_snapshot(event_risk_status=AvailabilityStatus.PARTIAL)
        self.assertEqual(_confidence(available), 0.65)
        self.assertEqual(_confidence(partial), 0.62)

    # --- both unavailable together: no cumulative malus ---

    def test_both_valuation_and_event_risk_unavailable_produce_no_malus(self) -> None:
        snapshot = make_snapshot(
            valuation_status=AvailabilityStatus.UNAVAILABLE,
            event_risk_status=AvailabilityStatus.UNAVAILABLE,
        )
        # 0.50 + 0.10 (fundamental available, default) + 0.00 + 0.00
        self.assertEqual(_confidence(snapshot), 0.60)
        # Explicitly not the pre-change value (0.50), proving the malus is gone.
        self.assertNotEqual(_confidence(snapshot), 0.50)

    # --- ETF / NOT_APPLICABLE fundamental: unaffected by this change ---

    def test_not_applicable_fundamental_contributes_nothing(self) -> None:
        snapshot = make_snapshot(fundamental_status=AvailabilityStatus.NOT_APPLICABLE)
        # 0.50 + 0.00 (fundamental not_applicable, no branch matches) + 0.00 + 0.00
        self.assertEqual(_confidence(snapshot), 0.50)

    # --- existing TECHNICAL_DATA_BLOCKER path: untouched ---

    def test_technical_data_blocker_confidence_stays_fixed_at_0_10(self) -> None:
        for status in (
            AvailabilityStatus.UNAVAILABLE,
            AvailabilityStatus.INSUFFICIENT,
            AvailabilityStatus.STALE,
        ):
            result = decide(make_snapshot(technical_status=status), self.config)
            self.assertEqual(result.action, Action.WATCH)
            self.assertEqual(result.confidence, 0.10)
            self.assertIn("TECHNICAL_DATA_BLOCKER", result.reasons)

    # --- Action/Reason/Quantity are unaffected by the confidence reweighting ---

    def test_action_reason_quantity_unaffected_by_valuation_event_risk_status(
        self,
    ) -> None:
        low_confidence_inputs = make_snapshot(
            valuation_status=AvailabilityStatus.UNAVAILABLE,
            event_risk_status=AvailabilityStatus.UNAVAILABLE,
        )
        high_confidence_inputs = make_snapshot(
            valuation_status=AvailabilityStatus.AVAILABLE,
            event_risk_status=AvailabilityStatus.AVAILABLE,
        )
        low = decide(low_confidence_inputs, self.config)
        high = decide(high_confidence_inputs, self.config)

        self.assertEqual(low.action, high.action)
        self.assertEqual(low.reasons, high.reasons)
        self.assertEqual(low.action_quantity, high.action_quantity)
        self.assertEqual(low.action_quantity_basis, high.action_quantity_basis)
        self.assertEqual(low.target_remaining_quantity, high.target_remaining_quantity)
        self.assertNotEqual(low.confidence, high.confidence)
        self.assertEqual(round(high.confidence - low.confidence, 4), 0.10)


if __name__ == "__main__":
    unittest.main()
