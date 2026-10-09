from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from candidate_decision import (  # noqa: E402
    BUY,
    DEFERRED,
    INSUFFICIENT_DATA,
    WATCH,
    evaluate_watchlist_candidates,
)
from _fixtures import MARKET_END, make_connection  # noqa: E402
from datetime import timedelta  # noqa: E402


def _find(decisions, security_id: int):
    return next(d for d in decisions if d.security_id == security_id)


class CandidateDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.as_of = MARKET_END.isoformat()

    def tearDown(self) -> None:
        self.conn.close()

    def _balance_portfolio(self, swing_value: float, long_term_value: float) -> None:
        """EUR-only two-holding portfolio: security 1 = swing, security 3 = long_term."""
        self.conn.execute(
            "UPDATE positions SET shares = 1, avg_cost = 10, currency = 'EUR' WHERE security_id = 1"
        )
        self.conn.execute(
            "UPDATE market_snapshot SET price = ?, currency = 'EUR' WHERE security_id = 1",
            (swing_value,),
        )
        self.conn.execute(
            "INSERT INTO positions(security_id, shares, avg_cost, remaining_cost_basis, currency, invested_amount, realized_gain, transaction_count) VALUES (3, 1, 10, 10, 'EUR', 10, 0, 1)"
        )
        self.conn.execute(
            "INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (3, ?, ?, 'EUR')",
            (self.as_of + "T00:00:00+00:00", long_term_value),
        )
        self.conn.execute(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (3, 'long_term', ?)",
            (self.as_of,),
        )

    def _assign_swing(self, security_id: int) -> None:
        self.conn.execute(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (?, 'swing', ?)",
            (security_id, self.as_of),
        )

    def _open_campaign_for(self, security_id: int) -> None:
        self.conn.executescript(
            """
            CREATE TABLE swing_campaign(id INTEGER PRIMARY KEY, security_id INTEGER, strategy_assignment_id INTEGER,
                opened_at TEXT, original_quantity REAL, reference_avg_cost REAL, reference_currency TEXT, status TEXT, closed_at TEXT);
            """
        )
        self.conn.execute(
            "INSERT INTO swing_campaign(id, security_id, strategy_assignment_id, opened_at, original_quantity, reference_avg_cost, reference_currency, status) VALUES (1, ?, 1, ?, 1, 10, 'EUR', 'open')",
            (security_id, self.as_of),
        )

    # 1. Guter Swing-Kandidat
    def test_good_swing_candidate_is_buy(self) -> None:
        self._balance_portfolio(35, 65)  # WITHIN_TARGET_RANGE
        self._assign_swing(1)
        decisions = evaluate_watchlist_candidates(self.conn, as_of=self.as_of)
        candidate = _find(decisions, 1)
        self.assertEqual(candidate.analytics_quality, "OK")
        self.assertEqual(candidate.strategy, "swing")
        self.assertEqual(candidate.campaign_status, "NONE")
        self.assertEqual(candidate.portfolio_constraints, ("WITHIN_TARGET_RANGE",))
        self.assertEqual(candidate.decision_status, BUY)
        self.assertEqual(candidate.decision_reasons, ("SCORE_AT_OR_ABOVE_THRESHOLD",))
        self.assertGreaterEqual(candidate.analytics_score, 20.0)

    # 2. Schlechte/limitierte Daten
    def test_insufficient_analytics_data_is_insufficient_data(self) -> None:
        self.conn.execute("INSERT INTO security(id, symbol, name, asset_type) VALUES (4, 'THIN', 'Thin Data Corp', 'stock')")
        self.conn.execute("INSERT INTO watchlist(security_id, status) VALUES (4, 'WATCH')")
        for offset in range(5):
            trade_date = (MARKET_END - timedelta(days=offset)).isoformat()
            self.conn.execute(
                "INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id) VALUES (4, ?, 50, 50, 1)",
                (trade_date,),
            )
        self._balance_portfolio(35, 65)
        self._assign_swing(4)
        decisions = evaluate_watchlist_candidates(self.conn, as_of=self.as_of)
        candidate = _find(decisions, 4)
        self.assertNotEqual(candidate.analytics_quality, "OK")
        self.assertEqual(candidate.decision_status, INSUFFICIENT_DATA)
        self.assertEqual(candidate.decision_reasons, (f"ANALYTICS_QUALITY_{candidate.analytics_quality}",))

    # 3. Offene Campaign
    def test_open_swing_campaign_blocks_new_buy(self) -> None:
        self._balance_portfolio(35, 65)
        self._assign_swing(1)
        self._open_campaign_for(1)
        decisions = evaluate_watchlist_candidates(self.conn, as_of=self.as_of)
        candidate = _find(decisions, 1)
        self.assertEqual(candidate.campaign_status, "OPEN")
        self.assertEqual(candidate.decision_status, WATCH)
        self.assertEqual(candidate.decision_reasons, ("OPEN_SWING_CAMPAIGN_BLOCKS_NEW_BUY",))

    # 4. Fehlende Strategie
    def test_missing_strategy_assignment_is_deferred(self) -> None:
        self._balance_portfolio(35, 65)
        # security 1 intentionally left without any strategy_assignment row
        decisions = evaluate_watchlist_candidates(self.conn, as_of=self.as_of)
        candidate = _find(decisions, 1)
        self.assertIsNone(candidate.strategy)
        self.assertEqual(candidate.decision_status, DEFERRED)
        self.assertEqual(candidate.decision_reasons, ("NO_STRATEGY_ASSIGNMENT",))

    # 5. Portfolio-Constraint blockiert BUY
    def test_portfolio_guardrail_above_max_blocks_buy(self) -> None:
        self._balance_portfolio(45, 55)  # 45% > swing_max_pct 40% -> SWING_ALLOCATION_ABOVE_MAX
        self._assign_swing(1)
        decisions = evaluate_watchlist_candidates(self.conn, as_of=self.as_of)
        candidate = _find(decisions, 1)
        self.assertIn("SWING_ALLOCATION_ABOVE_MAX", candidate.portfolio_constraints)
        self.assertEqual(candidate.decision_status, DEFERRED)
        self.assertEqual(candidate.decision_reasons, ("SWING_ALLOCATION_ABOVE_MAX",))

    # Bonus: non-swing assignment never becomes a BUY candidate here.
    def test_long_term_assignment_is_watch_not_buy(self) -> None:
        self._balance_portfolio(35, 65)
        self.conn.execute(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'long_term', ?)",
            (self.as_of,),
        )
        decisions = evaluate_watchlist_candidates(self.conn, as_of=self.as_of)
        candidate = _find(decisions, 1)
        self.assertEqual(candidate.strategy, "long_term")
        self.assertEqual(candidate.decision_status, WATCH)
        self.assertEqual(candidate.decision_reasons, ("STRATEGY_NOT_SWING",))

    # Bonus: a swing candidate whose score is below the named threshold stays WATCH.
    def test_score_below_threshold_is_watch(self) -> None:
        self._balance_portfolio(35, 65)
        self._assign_swing(1)  # classify the priced position so allocation stays AVAILABLE
        self._assign_swing(2)  # declining price series -> strongly negative score
        decisions = evaluate_watchlist_candidates(self.conn, as_of=self.as_of)
        candidate = _find(decisions, 2)
        self.assertLess(candidate.analytics_score, 20.0)
        self.assertEqual(candidate.decision_status, WATCH)
        self.assertEqual(candidate.decision_reasons, ("SCORE_BELOW_THRESHOLD",))

    # Bonus: no implicit BUY when the portfolio allocation itself is unavailable.
    def test_unavailable_portfolio_allocation_defers_instead_of_guessing(self) -> None:
        # security 1 kept in its fixture default USD currency with no fx_rates row
        # -> allocation_quality stays unavailable.
        self._assign_swing(1)
        decisions = evaluate_watchlist_candidates(self.conn, as_of=self.as_of)
        candidate = _find(decisions, 1)
        self.assertEqual(candidate.portfolio_constraints, ())
        self.assertEqual(candidate.decision_status, DEFERRED)
        self.assertEqual(candidate.decision_reasons, ("PORTFOLIO_ALLOCATION_UNAVAILABLE",))


if __name__ == "__main__":
    unittest.main()
