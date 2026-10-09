from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import trading_analytics  # noqa: E402
from _fixtures import make_connection  # noqa: E402


class TradingAnalyticsRegressionTests(unittest.TestCase):
    def test_rank_watchlist_remains_functional(self) -> None:
        conn = make_connection()
        try:
            ranking = trading_analytics.rank_watchlist(conn)
        finally:
            conn.close()
        self.assertEqual(len(ranking), 2)
        self.assertEqual(ranking[0]["security_id"], 1)
        self.assertTrue(all(item["score"] is not None for item in ranking))

    def test_historical_helper_leaves_existing_api_intact(self) -> None:
        conn = make_connection()
        try:
            current = trading_analytics.analyze_security(conn, 1)
            historical = trading_analytics.analyze_security_as_of(conn, 1, "2025-03-01")
        finally:
            conn.close()
        self.assertEqual(current["data_points"], 260)
        self.assertLess(historical["data_points"], current["data_points"])
