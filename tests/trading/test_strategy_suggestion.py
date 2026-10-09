from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy_suggestion import (  # noqa: E402
    CONFIDENCE_ETF_LONG_TERM,
    CONFIDENCE_OPEN_CAMPAIGN,
    CONFIDENCE_SWING_MOMENTUM,
    CONFIDENCE_UNKNOWN,
    LONG_TERM,
    SWING,
    UNKNOWN,
    suggest_strategy_assignments,
)
from _fixtures import MARKET_END, make_connection  # noqa: E402


def _find(suggestions, security_id: int):
    return next((s for s in suggestions if s.security_id == security_id), None)


class StrategySuggestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.as_of = MARKET_END.isoformat()

    def tearDown(self) -> None:
        self.conn.close()

    def _open_campaign_for(self, security_id: int) -> None:
        self.conn.executescript(
            """
            CREATE TABLE swing_campaign(id INTEGER PRIMARY KEY, security_id INTEGER, strategy_assignment_id INTEGER,
                opened_at TEXT, original_quantity REAL, reference_avg_cost REAL, reference_currency TEXT, status TEXT, closed_at TEXT);
            """
        )
        self.conn.execute(
            "INSERT INTO swing_campaign(id, security_id, strategy_assignment_id, opened_at, original_quantity, reference_avg_cost, reference_currency, status) VALUES (1, ?, NULL, ?, 1, 10, 'EUR', 'open')",
            (security_id, self.as_of),
        )

    # 1. aktives Assignment wird übersprungen
    def test_active_assignment_is_skipped(self) -> None:
        self.conn.execute(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'swing', ?)",
            (self.as_of,),
        )
        suggestions = suggest_strategy_assignments(self.conn, as_of=self.as_of)
        self.assertIsNone(_find(suggestions, 1))

    # 2. hohe Swing-Eignung
    def test_high_swing_suitability_suggests_swing(self) -> None:
        suggestions = suggest_strategy_assignments(self.conn, as_of=self.as_of)
        candidate = _find(suggestions, 1)  # security 1: monotonically rising price series
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.suggested_strategy, SWING)
        self.assertEqual(candidate.confidence, CONFIDENCE_SWING_MOMENTUM)
        self.assertEqual(candidate.reasons, ("MOMENTUM_SCORE_ABOVE_THRESHOLD",))

    # 3. unzureichende Daten -> unknown
    def test_insufficient_data_is_unknown(self) -> None:
        self.conn.execute("INSERT INTO security(id, symbol, name, asset_type) VALUES (4, 'THIN', 'Thin Data Corp', 'stock')")
        self.conn.execute("INSERT INTO watchlist(security_id, status) VALUES (4, 'WATCH')")
        for offset in range(5):
            trade_date = (MARKET_END - timedelta(days=offset)).isoformat()
            self.conn.execute(
                "INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id) VALUES (4, ?, 50, 50, 1)",
                (trade_date,),
            )
        suggestions = suggest_strategy_assignments(self.conn, as_of=self.as_of)
        candidate = _find(suggestions, 4)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.suggested_strategy, UNKNOWN)
        self.assertEqual(candidate.confidence, CONFIDENCE_UNKNOWN)
        self.assertEqual(candidate.reasons, ("ANALYTICS_QUALITY_INSUFFICIENT",))

    # 4. offene Campaign
    def test_open_campaign_suggests_swing(self) -> None:
        self._open_campaign_for(1)  # no active strategy_assignment, but an open campaign exists
        suggestions = suggest_strategy_assignments(self.conn, as_of=self.as_of)
        candidate = _find(suggestions, 1)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.suggested_strategy, SWING)
        self.assertEqual(candidate.confidence, CONFIDENCE_OPEN_CAMPAIGN)
        self.assertEqual(candidate.reasons, ("OPEN_SWING_CAMPAIGN_INDICATES_SWING",))

    # 5. keine falsche Long-Term-Ableitung aus reinem Momentum
    def test_no_long_term_inference_from_momentum_alone(self) -> None:
        # security 1 is a stock (asset_type='stock') with a strongly bullish
        # momentum score -- it must never resolve to long_term from that
        # score alone, regardless of how high it is.
        suggestions = suggest_strategy_assignments(self.conn, as_of=self.as_of)
        candidate = _find(suggestions, 1)
        self.assertIsNotNone(candidate)
        self.assertGreater(candidate.confidence, 0.0)
        self.assertNotEqual(candidate.suggested_strategy, LONG_TERM)
        self.assertEqual(candidate.suggested_strategy, SWING)

    # Bonus: the one non-momentum long-term basis this module uses (asset_type).
    def test_etf_asset_type_suggests_long_term(self) -> None:
        self.conn.execute("UPDATE security SET asset_type = 'etf' WHERE id = 3")
        self.conn.execute("INSERT INTO watchlist(security_id, status) VALUES (3, 'WATCH')")
        rows = []
        for index in range(260):
            trade_date = (MARKET_END - timedelta(days=259 - index)).isoformat()
            rows.append((3, trade_date, 50.0, 50.0, 1))
        self.conn.executemany(
            "INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        suggestions = suggest_strategy_assignments(self.conn, as_of=self.as_of)
        candidate = _find(suggestions, 3)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.suggested_strategy, LONG_TERM)
        self.assertEqual(candidate.confidence, CONFIDENCE_ETF_LONG_TERM)
        self.assertEqual(candidate.reasons, ("ASSET_TYPE_ETF_INDICATES_LONG_TERM",))

    # Bonus: a swing score below the named threshold does not force a strategy.
    def test_score_below_threshold_is_unknown(self) -> None:
        suggestions = suggest_strategy_assignments(self.conn, as_of=self.as_of)
        candidate = _find(suggestions, 2)  # security 2: monotonically declining price series
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.suggested_strategy, UNKNOWN)
        self.assertEqual(candidate.confidence, CONFIDENCE_UNKNOWN)
        self.assertEqual(candidate.reasons, ("NO_SUFFICIENT_STRATEGY_BASIS",))


if __name__ == "__main__":
    unittest.main()
