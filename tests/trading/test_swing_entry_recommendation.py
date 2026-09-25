"""Phase-3B.7 pure Swing initial-entry recommendation tests."""

from __future__ import annotations

from dataclasses import replace
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import (  # noqa: E402
    Action,
    ActionQuantityBasis,
    AvailabilityStatus,
    DataQuality,
    PortfolioContext,
    StrategyType,
    TechnicalAnalysis,
)
from decision_engine import decide  # noqa: E402
from entry_sizing import evaluate_entry_recommendation  # noqa: E402
from strategy_config import SizingPolicyConfig, StrategyConfig  # noqa: E402
from test_decision_engine import make_snapshot  # noqa: E402


AVAILABLE = DataQuality(AvailabilityStatus.AVAILABLE, as_of="2026-09-23")


def candidate_snapshot(
    *,
    strategy: StrategyType = StrategyType.SWING,
    has_position: bool = False,
    campaign_id: int | None = None,
    price: float | None = 100.0,
    sma50: float | None = 95.0,
    sma200: float | None = 90.0,
    technical_status: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    momentum: float | None = -5.0,
):
    snapshot = make_snapshot(
        has_position=has_position,
        strategy=strategy,
        price=price,
        position_currency="EUR",
        market_currency="EUR",
        technical_status=technical_status,
    )
    technical = TechnicalAnalysis(
        quality=DataQuality(technical_status),
        current_price=price,
        sma50=sma50,
        sma200=sma200,
        rsi14=35.0,
        momentum_score=momentum,
        data_points=252,
    )
    position = replace(
        snapshot.position,
        has_position=has_position,
        shares=10.0 if has_position else None,
        swing_campaign_id=campaign_id,
        swing_campaign_status="open" if campaign_id is not None else None,
        current_price_cost_currency=price,
        valuation_currency="EUR" if price is not None else None,
        fx_quality=DataQuality(AvailabilityStatus.NOT_APPLICABLE),
    )
    return replace(snapshot, technical=technical, position=position)


def context(
    *,
    total: float = 100_000.0,
    swing: float = 20_000.0,
    security: float = 0.0,
    cash: float | None = 30_000.0,
    buying_power: float | None = None,
    valuation: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
    allocation: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
) -> PortfolioContext:
    return PortfolioContext(
        evaluation_as_of="2026-09-23",
        base_currency="EUR",
        total_market_value=total if valuation is AvailabilityStatus.AVAILABLE else None,
        valuation_quality=DataQuality(valuation),
        swing_market_value=swing if allocation is AvailabilityStatus.AVAILABLE else None,
        swing_weight_pct=swing / total * 100 if allocation is AvailabilityStatus.AVAILABLE else None,
        long_term_market_value=total - swing if allocation is AvailabilityStatus.AVAILABLE else None,
        long_term_weight_pct=(total - swing) / total * 100 if allocation is AvailabilityStatus.AVAILABLE else None,
        cash_available=cash,
        cash_quality=DataQuality(AvailabilityStatus.AVAILABLE if cash is not None else AvailabilityStatus.UNAVAILABLE),
        buying_power=buying_power,
        buying_power_quality=DataQuality(AvailabilityStatus.AVAILABLE if buying_power is not None else AvailabilityStatus.UNAVAILABLE),
        capital_state_as_of="2026-09-23",
        capital_state_source="test",
        current_security_id=1,
        current_security_market_value=security,
        current_security_market_value_eur=security,
        current_security_weight_pct=security / total * 100 if allocation is AvailabilityStatus.AVAILABLE else None,
        allocation_quality=DataQuality(allocation),
        exposures=(),
    )


class SwingEntryRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()

    def evaluate(self, snapshot=None, portfolio=None, config=None):
        return evaluate_entry_recommendation(
            snapshot or candidate_snapshot(), portfolio or context(), config or self.config
        )

    def test_explicit_swing_assignment_is_the_only_candidate_source(self) -> None:
        self.assertTrue(self.evaluate().eligible)
        self.assertIn("ENTRY_NOT_SWING_CANDIDATE", self.evaluate(candidate_snapshot(strategy=StrategyType.UNKNOWN)).block_reasons)
        self.assertIn("ENTRY_STRATEGY_CONFLICT", self.evaluate(candidate_snapshot(strategy=StrategyType.LONG_TERM)).block_reasons)
        unavailable = replace(candidate_snapshot(), position=replace(candidate_snapshot().position, strategy_quality=DataQuality(AvailabilityStatus.UNAVAILABLE)))
        self.assertIn("ENTRY_NOT_SWING_CANDIDATE", self.evaluate(unavailable).block_reasons)

    def test_position_and_open_campaign_are_authoritative_blocks(self) -> None:
        self.assertIn("ENTRY_EXISTING_POSITION", self.evaluate(candidate_snapshot(has_position=True)).block_reasons)
        self.assertIn("ENTRY_OPEN_CAMPAIGN_EXISTS", self.evaluate(candidate_snapshot(campaign_id=12)).block_reasons)

    def test_technical_filter_and_diagnostics_are_deterministic(self) -> None:
        cases = (
            (candidate_snapshot(price=95), "ENTRY_PRICE_BELOW_OR_EQUAL_SMA50"),
            (candidate_snapshot(price=90, sma50=89), "ENTRY_PRICE_BELOW_OR_EQUAL_SMA200"),
            (candidate_snapshot(sma50=90, sma200=90), "ENTRY_SMA50_BELOW_OR_EQUAL_SMA200"),
            (candidate_snapshot(price=None), "ENTRY_PRICE_UNAVAILABLE"),
            (candidate_snapshot(sma50=None), "ENTRY_SMA50_UNAVAILABLE"),
            (candidate_snapshot(sma200=None), "ENTRY_SMA200_UNAVAILABLE"),
            (candidate_snapshot(technical_status=AvailabilityStatus.STALE), "ENTRY_TECHNICAL_DATA_UNAVAILABLE"),
        )
        for snapshot, reason in cases:
            with self.subTest(reason=reason):
                self.assertIn(reason, self.evaluate(snapshot).block_reasons)
        # Negative momentum is diagnostic-only and cannot reject an otherwise
        # valid strength/trend setup.
        self.assertTrue(self.evaluate(candidate_snapshot(momentum=-99.0)).eligible)

    def test_initial_size_uses_post_trade_ten_percent_cap_and_floor(self) -> None:
        result = self.evaluate()
        self.assertTrue(result.eligible)
        self.assertEqual(result.recommended_quantity, 111)
        self.assertEqual(result.purchase_value_eur, 11_100)
        self.assertLessEqual(result.initial_position_weight or 1, 0.10)
        self.assertGreater((result.initial_position_weight or 0), 0.099)

    def test_explicit_weight_and_allocation_caps(self) -> None:
        policy = SizingPolicyConfig(max_initial_swing_weight=0.30)
        config = replace(self.config, sizing=policy)
        security_limited = self.evaluate(portfolio=context(security=19_900), config=config)
        self.assertLessEqual(security_limited.projected_security_weight or 0, 0.20)
        self.assertLess(security_limited.recommended_quantity or 0, 112)
        exactly_swing = self.evaluate(portfolio=context(swing=39_000))
        self.assertLessEqual(exactly_swing.projected_swing_allocation or 1, 0.40)
        self.assertIn("ENTRY_SWING_ALLOCATION_LIMIT", self.evaluate(portfolio=context(swing=40_000)).block_reasons)
        # The current global ceiling is an authoritative capital blocker,
        # even if this candidate's technical history is also stale.
        global_block = self.evaluate(
            candidate_snapshot(technical_status=AvailabilityStatus.STALE),
            context(swing=40_000),
        )
        self.assertEqual(global_block.block_reasons, ("ENTRY_SWING_ALLOCATION_LIMIT",))

    def test_cash_buying_power_and_minimum_of_caps(self) -> None:
        self.assertIn("ENTRY_CASH_UNAVAILABLE", self.evaluate(portfolio=context(cash=None)).block_reasons)
        self.assertIn("ENTRY_CASH_RESERVE_LIMIT", self.evaluate(portfolio=context(cash=10_000)).block_reasons)
        self.assertEqual(self.evaluate(portfolio=context(cash=10_550)).recommended_quantity, 5)
        self.assertEqual(self.evaluate(portfolio=context(cash=30_000, buying_power=10_350)).recommended_quantity, 3)
        # Unavailable buying power follows capital-state semantics: known cash
        # is usable and no speculative blocker is added.
        self.assertTrue(self.evaluate(portfolio=context(buying_power=None)).eligible)

    def test_portfolio_and_currency_prerequisites_are_not_inferred(self) -> None:
        self.assertIn("ENTRY_PORTFOLIO_VALUATION_UNAVAILABLE", self.evaluate(portfolio=context(valuation=AvailabilityStatus.UNAVAILABLE)).block_reasons)
        self.assertIn("ENTRY_SWING_ALLOCATION_UNAVAILABLE", self.evaluate(portfolio=context(allocation=AvailabilityStatus.UNAVAILABLE)).block_reasons)
        no_eur = replace(candidate_snapshot(), position=replace(candidate_snapshot().position, current_price_cost_currency=None, valuation_currency=None))
        self.assertIn("ENTRY_PORTFOLIO_VALUATION_UNAVAILABLE", self.evaluate(no_eur).block_reasons)

    def test_decision_promotes_only_zero_position_entry_to_buy(self) -> None:
        buy = decide(candidate_snapshot(), self.config, context())
        self.assertEqual(buy.action, Action.BUY)
        self.assertEqual(buy.action_quantity_basis, ActionQuantityBasis.INITIAL_ENTRY_SIZING)
        self.assertEqual(buy.entry_recommended_quantity, 111)
        self.assertEqual(buy.entry_sma50, 95)
        self.assertEqual(buy.entry_sma200, 90)
        self.assertEqual(buy.entry_rsi14, 35)
        self.assertEqual(buy.entry_momentum_score, -5)
        existing = decide(candidate_snapshot(has_position=True), self.config, context())
        self.assertNotEqual(existing.action, Action.BUY)
        self.assertNotEqual(existing.action, Action.ADD)

    def test_no_mutation_or_campaign_creation(self) -> None:
        snapshot = candidate_snapshot()
        portfolio = context()
        before = (snapshot, portfolio)
        result = self.evaluate(snapshot, portfolio)
        self.assertTrue(result.eligible)
        self.assertEqual((snapshot, portfolio), before)
        self.assertIsNone(snapshot.position.swing_campaign_id)


if __name__ == "__main__":
    unittest.main()
