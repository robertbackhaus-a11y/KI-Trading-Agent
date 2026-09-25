from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))

from analysis_contracts import Action, AnalysisSnapshot, AvailabilityStatus, DataQuality, EventRiskAnalysis, FundamentalAnalysis, PositionContext, RiskAnalysis, SnapshotDataQuality, StrategyType, TechnicalAnalysis, ValuationAnalysis  # noqa: E402
from decision_engine import decide  # noqa: E402
from strategy_config import StrategyConfig  # noqa: E402


def make_snapshot(*, has_position: bool | None = True, strategy: StrategyType = StrategyType.UNKNOWN, technical_status: AvailabilityStatus = AvailabilityStatus.AVAILABLE, price: float | None = 130.0, position_currency: str | None = "USD", market_currency: str | None = "USD", state_supported: bool = True) -> AnalysisSnapshot:
    technical_quality = DataQuality(technical_status)
    fundamental_quality = DataQuality(AvailabilityStatus.AVAILABLE)
    unavailable = DataQuality(AvailabilityStatus.UNAVAILABLE)
    position_quality = DataQuality(AvailabilityStatus.AVAILABLE if state_supported else AvailabilityStatus.UNAVAILABLE)
    currencies_match = position_currency is not None and position_currency == market_currency
    fx_quality = DataQuality(
        AvailabilityStatus.NOT_APPLICABLE if currencies_match else AvailabilityStatus.UNAVAILABLE
    )
    return AnalysisSnapshot(
        security_id=1, symbol="ABC", name="Example", as_of="2026-09-22", asset_type="stock",
        technical=TechnicalAnalysis(quality=technical_quality, current_price=price, data_points=252),
        fundamental=FundamentalAnalysis(quality=fundamental_quality),
        valuation=ValuationAnalysis(quality=unavailable), event_risk=EventRiskAnalysis(quality=unavailable),
        risk=RiskAnalysis(quality=technical_quality),
        position=PositionContext(quality=position_quality, has_position=has_position, strategy=strategy, strategy_quality=DataQuality(AvailabilityStatus.AVAILABLE), state_supported=state_supported, avg_cost=100.0 if has_position else None, currency=position_currency, cost_basis_currency=position_currency, current_price=price if has_position else None, current_price_native=price if has_position else None, current_price_currency=market_currency, current_price_cost_currency=price if has_position and currencies_match else None, valuation_currency=position_currency if has_position and currencies_match else None, fx_quality=fx_quality),
        data_quality=SnapshotDataQuality(technical=technical_quality, fundamental=fundamental_quality, valuation=unavailable, event_risk=unavailable, risk=technical_quality, position=position_quality, strategy_assignment=DataQuality(AvailabilityStatus.AVAILABLE)),
    )


class DecisionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()

    def test_existing_position_defaults_to_hold(self) -> None:
        result = decide(make_snapshot(), self.config)
        self.assertEqual(result.action, Action.HOLD)
        self.assertIn("EXISTING_POSITION_DEFAULT_HOLD", result.reasons)
        self.assertIsNone(result.tp1)

    def test_no_position_defaults_to_watch(self) -> None:
        result = decide(make_snapshot(has_position=False), self.config)
        self.assertEqual(result.action, Action.WATCH)
        self.assertIn("NO_POSITION_DEFAULT_WATCH", result.reasons)

    def test_unavailable_insufficient_and_stale_technical_block(self) -> None:
        for status in (AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.INSUFFICIENT, AvailabilityStatus.STALE):
            result = decide(make_snapshot(technical_status=status), self.config)
            self.assertEqual(result.action, Action.WATCH)
            self.assertEqual(result.confidence, 0.10)
            self.assertTrue(result.blocking_data_gaps)
        insufficient = decide(make_snapshot(technical_status=AvailabilityStatus.INSUFFICIENT), self.config)
        self.assertIn("technical history is insufficient", insufficient.blocking_data_gaps)

    def test_historical_position_state_blocks_decision(self) -> None:
        result = decide(make_snapshot(has_position=None, state_supported=False), self.config)
        self.assertEqual(result.action, Action.WATCH)
        self.assertIn("historical position and watchlist state are unsupported", result.blocking_data_gaps)

    def test_tp1_and_tp2_boundaries_keep_hold(self) -> None:
        tp1 = decide(make_snapshot(strategy=StrategyType.SWING, price=120.0), self.config)
        self.assertEqual(tp1.action, Action.HOLD)
        self.assertEqual(tp1.tp1, 120.0)
        self.assertEqual(tp1.tp2, 125.0)
        self.assertIn("TP1_REACHED", tp1.reasons)
        self.assertNotIn("TP2_REACHED", tp1.reasons)
        self.assertEqual(tp1.target_price_currency, "USD")
        tp2 = decide(make_snapshot(strategy=StrategyType.SWING, price=125.0), self.config)
        self.assertEqual(tp2.action, Action.HOLD)
        self.assertIn("TP1_REACHED", tp2.reasons)
        self.assertIn("TP2_REACHED", tp2.reasons)

    def test_non_swing_strategies_receive_no_swing_targets(self) -> None:
        for strategy in (StrategyType.LONG_TERM, StrategyType.TACTICAL, StrategyType.UNKNOWN):
            result = decide(make_snapshot(strategy=strategy, price=130.0), self.config)
            self.assertEqual(result.action, Action.HOLD)
            self.assertIsNone(result.tp1)
            self.assertIsNone(result.tp2)
            self.assertNotIn("TP1_REACHED", result.reasons)

    def test_currency_mismatch_or_missing_currency_emits_no_tp_signal(self) -> None:
        mismatch = decide(make_snapshot(strategy=StrategyType.SWING, position_currency="EUR", market_currency="USD"), self.config)
        missing = decide(make_snapshot(strategy=StrategyType.SWING, position_currency=None, market_currency="USD"), self.config)
        for result, risk in ((mismatch, "SWING_TP_FX_UNAVAILABLE"), (missing, "SWING_TP_FX_UNAVAILABLE")):
            self.assertEqual(result.action, Action.HOLD)
            self.assertIsNone(result.tp1)
            self.assertIsNone(result.tp2)
            self.assertNotIn("TP1_REACHED", result.reasons)
            self.assertNotIn("TP2_REACHED", result.reasons)
            self.assertIn(risk, result.risks)

    def test_confidence_is_always_bounded(self) -> None:
        for status in AvailabilityStatus:
            result = decide(make_snapshot(technical_status=status), self.config)
            self.assertGreaterEqual(result.confidence, 0.0)
            self.assertLessEqual(result.confidence, 1.0)
