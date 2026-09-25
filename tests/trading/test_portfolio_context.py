from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import AvailabilityStatus, StrategyType, to_primitive  # noqa: E402
from analysis_engine import build_analysis_snapshot, build_portfolio_context  # noqa: E402
from decision_engine import decide  # noqa: E402
from portfolio_context import project_strategy_capital_use  # noqa: E402
from strategy_config import FXConfig, PortfolioTargetConfig, StrategyConfig  # noqa: E402
from _fixtures import MARKET_END, make_connection  # noqa: E402


class PortfolioContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.as_of = MARKET_END.isoformat()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_positions(self, swing_value: float, long_term_value: float) -> None:
        """Create two EUR-valued holdings with the requested market values."""

        self.conn.execute(
            "UPDATE positions SET shares = 1, avg_cost = 10, currency = 'EUR' WHERE security_id = 1"
        )
        self.conn.execute(
            "UPDATE market_snapshot SET price = ?, currency = 'EUR' WHERE security_id = 1",
            (swing_value,),
        )
        self.conn.execute(
            "INSERT INTO positions(security_id, shares, avg_cost, remaining_cost_basis, currency, invested_amount, realized_gain, transaction_count) VALUES (2, 1, 10, 10, 'EUR', 10, 0, 1)"
        )
        self.conn.execute(
            "INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (2, ?, ?, 'EUR')",
            (self.as_of + "T00:00:00+00:00", long_term_value),
        )
        self.conn.executemany(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (?, ?, ?)",
            ((1, "swing", self.as_of), (2, "long_term", self.as_of)),
        )

    def test_all_eur_mixed_strategies_have_complete_valuation_and_weights(self) -> None:
        self._set_positions(35, 65)
        context = build_portfolio_context(1, connection=self.conn)
        self.assertEqual(context.base_currency, "EUR")
        self.assertEqual(context.valuation_quality.status, AvailabilityStatus.AVAILABLE)
        self.assertEqual(context.allocation_quality.status, AvailabilityStatus.AVAILABLE)
        self.assertAlmostEqual(context.total_market_value or 0, 100)
        self.assertAlmostEqual(context.swing_market_value or 0, 35)
        self.assertAlmostEqual(context.long_term_market_value or 0, 65)
        self.assertAlmostEqual(context.swing_weight_pct or 0, 35)
        self.assertAlmostEqual(context.long_term_weight_pct or 0, 65)
        self.assertAlmostEqual(context.current_security_market_value_eur or 0, 35)
        self.assertAlmostEqual(context.current_security_weight_pct or 0, 35)
        self.assertEqual(context.allocation_guardrails, ("WITHIN_TARGET_RANGE",))
        payload = to_primitive(context)
        self.assertEqual(payload["cash_available"], None)
        self.assertEqual(payload["cash_quality"]["status"], "unavailable")

    def test_usd_market_price_is_valued_in_eur_through_existing_fx_normalization(self) -> None:
        self.conn.execute(
            "UPDATE positions SET shares = 10, avg_cost = 10, currency = 'EUR' WHERE security_id = 1"
        )
        self.conn.execute(
            "UPDATE market_snapshot SET price = 12, currency = 'USD' WHERE security_id = 1"
        )
        self.conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', 'USD', 1.2, 'ECB', ?)",
            (self.as_of, self.as_of + "T16:00:00+00:00"),
        )
        self.conn.execute(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'swing', ?)",
            (self.as_of,),
        )
        context = build_portfolio_context(1, connection=self.conn)
        self.assertAlmostEqual(context.total_market_value or 0, 100)
        exposure = context.exposures[0]
        self.assertEqual(exposure.market_price_currency, "USD")
        self.assertAlmostEqual(exposure.market_price_eur or 0, 10)
        self.assertAlmostEqual(exposure.market_value_eur or 0, 100)

    def test_missing_or_stale_fx_withholds_total_and_allocation_denominator(self) -> None:
        self.conn.execute(
            "UPDATE positions SET currency = 'EUR' WHERE security_id = 1"
        )
        self.conn.execute(
            "UPDATE market_snapshot SET price = 120, currency = 'USD' WHERE security_id = 1"
        )
        self.conn.execute(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'swing', ?)",
            (self.as_of,),
        )
        missing = build_portfolio_context(1, connection=self.conn)
        self.assertIsNone(missing.total_market_value)
        self.assertIsNone(missing.swing_weight_pct)
        self.assertNotEqual(missing.allocation_quality.status, AvailabilityStatus.AVAILABLE)
        self.conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', 'USD', 1.2, 'ECB', ?)",
            ((MARKET_END - timedelta(days=6)).isoformat(), self.as_of + "T16:00:00+00:00"),
        )
        stale = build_portfolio_context(1, connection=self.conn)
        self.assertIsNone(stale.total_market_value)
        self.assertNotEqual(stale.valuation_quality.status, AvailabilityStatus.AVAILABLE)
        self.assertNotEqual(stale.allocation_quality.status, AvailabilityStatus.AVAILABLE)

    def test_missing_market_price_is_an_incomplete_valuation(self) -> None:
        self._set_positions(35, 65)
        self.conn.execute("UPDATE market_snapshot SET price = NULL WHERE security_id = 2")
        context = build_portfolio_context(1, connection=self.conn)
        self.assertIsNone(context.total_market_value)
        self.assertIsNone(context.current_security_weight_pct)
        self.assertEqual(context.valuation_quality.status, AvailabilityStatus.PARTIAL)
        self.assertEqual(context.allocation_quality.status, AvailabilityStatus.UNAVAILABLE)
        self.assertEqual(context.allocation_guardrails, ())

    def test_allocation_guardrails_cover_each_configured_boundary(self) -> None:
        cases = (
            (45, 55, ("SWING_ALLOCATION_ABOVE_MAX", "LONG_TERM_ALLOCATION_BELOW_MIN")),
            (25, 75, ("SWING_ALLOCATION_BELOW_MIN", "LONG_TERM_ALLOCATION_ABOVE_MAX")),
        )
        for swing_value, long_term_value, expected in cases:
            with self.subTest(swing_value=swing_value):
                self._set_positions(swing_value, long_term_value)
                context = build_portfolio_context(1, connection=self.conn)
                self.assertEqual(context.allocation_guardrails, expected)
                self.conn.execute("DELETE FROM strategy_assignment")
                self.conn.execute("DELETE FROM positions WHERE security_id = 2")
                self.conn.execute("DELETE FROM market_snapshot WHERE security_id = 2")

    def test_capital_use_projection_is_informational_and_handles_unknown_amount(self) -> None:
        self._set_positions(35, 65)
        context = build_portfolio_context(1, connection=self.conn)
        config = PortfolioTargetConfig()
        swing_ok = project_strategy_capital_use(context, StrategyType.SWING, 5, config)
        swing_blocked = project_strategy_capital_use(context, StrategyType.SWING, 10, config)
        long_term = project_strategy_capital_use(context, StrategyType.LONG_TERM, 10, config)
        zero = project_strategy_capital_use(context, StrategyType.SWING, 0, config)
        unknown = project_strategy_capital_use(context, StrategyType.SWING, None, config)
        self.assertTrue(swing_ok.allocation_permitted)
        self.assertFalse(swing_blocked.allocation_permitted)
        self.assertTrue(long_term.allocation_permitted)
        self.assertTrue(zero.allocation_permitted)
        self.assertIsNone(unknown.allocation_permitted)
        self.assertEqual(unknown.quality.status, AvailabilityStatus.UNAVAILABLE)
        self.assertAlmostEqual(swing_ok.projected_strategy_weight_pct or 0, 40 / 105 * 100)

    def test_unknown_cash_is_not_a_zero_cash_balance(self) -> None:
        self._set_positions(35, 65)
        context = build_portfolio_context(connection=self.conn)
        self.assertIsNone(context.cash_available)
        self.assertEqual(context.cash_quality.status, AvailabilityStatus.UNAVAILABLE)
        self.assertIsNot(context.cash_available, 0.0)

    def test_portfolio_assembly_is_read_only_and_does_not_change_decisions(self) -> None:
        self._set_positions(45, 55)
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        context = build_portfolio_context(1, connection=self.conn)
        self.conn.set_trace_callback(None)
        self.assertTrue(context.allocation_guardrails)
        writes = [
            statement for statement in statements
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP"))
        ]
        self.assertEqual(writes, [])
        decision = decide(build_analysis_snapshot(1, connection=self.conn), StrategyConfig())
        self.assertNotIn(decision.action.value, {"BUY", "ADD"})

    def test_only_eur_base_currency_is_accepted(self) -> None:
        self._set_positions(35, 65)
        with self.assertRaises(ValueError):
            build_portfolio_context(
                connection=self.conn,
                fx_config=FXConfig(portfolio_base_currency="USD"),
            )

    def test_configured_allocation_ranges_keep_the_existing_values(self) -> None:
        config = PortfolioTargetConfig()
        self.assertEqual(config.long_term_min_pct, 0.60)
        self.assertEqual(config.long_term_max_pct, 0.70)
        self.assertEqual(config.swing_min_pct, 0.30)
        self.assertEqual(config.swing_max_pct, 0.40)
        self.assertEqual(config.etf_target_min, config.long_term_min_pct)


if __name__ == "__main__":
    unittest.main()
