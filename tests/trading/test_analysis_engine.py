from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import AvailabilityStatus  # noqa: E402
from analysis_engine import build_analysis_snapshot  # noqa: E402
from strategy_config import DataQualityConfig  # noqa: E402
from _fixtures import MARKET_END, MARKET_START, make_connection  # noqa: E402


class AnalysisEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()

    def tearDown(self) -> None:
        self.conn.close()

    def _replace_fundamentals(self, rows: list[tuple]) -> None:
        self.conn.execute("DELETE FROM fundamentals WHERE security_id = 1")
        self.conn.executemany(
            """
            INSERT INTO fundamentals(
                security_id, period_end, period_type, fiscal_year, fiscal_quarter,
                filing_date, currency, revenue, operating_income, net_income,
                operating_cash_flow, free_cash_flow, cash, total_debt, source_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def test_position_snapshot_and_annual_comparison_without_quarterly_mixing(self) -> None:
        snapshot = build_analysis_snapshot(1, connection=self.conn)
        self.assertEqual(snapshot.technical.quality.status, AvailabilityStatus.AVAILABLE)
        self.assertTrue(snapshot.position.has_position)
        self.assertEqual(snapshot.position.unrealized_gain_pct, 30.0)
        self.assertEqual(snapshot.fundamental.period_type, "annual")
        self.assertAlmostEqual(snapshot.fundamental.revenue_yoy_growth_pct or 0, 20.0)
        self.assertAlmostEqual(snapshot.fundamental.net_income_yoy_growth_pct or 0, 100.0)
        self.assertAlmostEqual(snapshot.fundamental.operating_margin_pct or 0, 10.0)
        self.assertAlmostEqual(snapshot.fundamental.fcf_margin_pct or 0, 100.0 / 12.0)

    def test_quarterly_compares_same_quarter_only(self) -> None:
        self._replace_fundamentals([
            (1, "2024-03-31", "quarterly", 2024, 1, "2024-05-01", "USD", 100, 10, 10, 20, 8, 40, 15, 1),
            (1, "2024-06-30", "quarterly", 2024, 2, "2024-08-01", "USD", 900, 90, 90, 90, 90, 90, 90, 1),
            (1, "2025-03-31", "quarterly", 2025, 1, "2025-05-01", "USD", 120, 12, 20, 30, 10, 50, 20, 1),
        ])
        snapshot = build_analysis_snapshot(1, connection=self.conn)
        self.assertEqual(snapshot.fundamental.period_type, "quarterly")
        self.assertAlmostEqual(snapshot.fundamental.revenue_yoy_growth_pct or 0, 20.0)

    def test_semiannual_compares_semiannual_only(self) -> None:
        self._replace_fundamentals([
            (1, "2024-06-30", "semiannual", 2024, None, "2024-08-01", "USD", 100, 10, 10, 20, 8, 40, 15, 1),
            (1, "2024-12-31", "annual", 2024, None, "2025-02-01", "USD", 900, 90, 90, 90, 90, 90, 90, 1),
            (1, "2025-06-30", "semiannual", 2025, None, "2025-08-01", "USD", 120, 12, 20, 30, 10, 50, 20, 1),
        ])
        snapshot = build_analysis_snapshot(1, connection=self.conn)
        self.assertEqual(snapshot.fundamental.period_type, "semiannual")
        self.assertAlmostEqual(snapshot.fundamental.revenue_yoy_growth_pct or 0, 20.0)

    def test_historical_fundamentals_require_filing_date(self) -> None:
        as_of = "2025-04-01"
        late = (1, "2025-03-31", "quarterly", 2025, 1, "2025-05-10", "USD", 100, 10, 10, 10, 10, 10, 10, 1)
        self._replace_fundamentals([late])
        self.assertEqual(build_analysis_snapshot(1, as_of=as_of, connection=self.conn).fundamental.quality.status, AvailabilityStatus.UNAVAILABLE)
        null_filing = (*late[:5], None, *late[6:])
        self._replace_fundamentals([null_filing])
        self.assertEqual(build_analysis_snapshot(1, as_of=as_of, connection=self.conn).fundamental.quality.status, AvailabilityStatus.UNAVAILABLE)
        timely = (*late[:5], "2025-03-31", *late[6:])
        self._replace_fundamentals([timely])
        self.assertEqual(build_analysis_snapshot(1, as_of=as_of, connection=self.conn).fundamental.quality.status, AvailabilityStatus.AVAILABLE)

    def test_technical_unavailable_insufficient_fresh_and_stale(self) -> None:
        self.assertEqual(build_analysis_snapshot(3, connection=self.conn).technical.quality.status, AvailabilityStatus.UNAVAILABLE)
        self.conn.execute("DELETE FROM market_data WHERE security_id = 2")
        self.conn.executemany(
            "INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id) VALUES (2, ?, 100, 100, 1)",
            [((MARKET_END - timedelta(days=day)).isoformat(),) for day in range(10)],
        )
        self.assertEqual(build_analysis_snapshot(2, connection=self.conn).technical.quality.status, AvailabilityStatus.INSUFFICIENT)
        self.conn.execute("DELETE FROM market_data WHERE security_id = 2")
        self.conn.executemany(
            "INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id) VALUES (2, ?, 100, 100, 1)",
            [((MARKET_END - timedelta(days=10 + day)).isoformat(),) for day in range(40)],
        )
        self.assertEqual(build_analysis_snapshot(2, connection=self.conn).technical.quality.status, AvailabilityStatus.STALE)
        self.assertEqual(
            build_analysis_snapshot(
                2,
                connection=self.conn,
                data_quality_config=DataQualityConfig(market_data_max_age_days=20),
            ).technical.quality.status,
            AvailabilityStatus.PARTIAL,
        )
        self.assertEqual(build_analysis_snapshot(1, connection=self.conn).technical.quality.status, AvailabilityStatus.AVAILABLE)

    def test_historical_technical_cutoff_hides_current_position_and_watchlist_state(self) -> None:
        as_of = (MARKET_START + timedelta(days=59)).isoformat()
        snapshot = build_analysis_snapshot(1, as_of=as_of, connection=self.conn)
        self.assertEqual(snapshot.as_of, as_of)
        self.assertLess(snapshot.technical.data_points, 260)
        self.assertEqual(snapshot.technical.quality.status, AvailabilityStatus.PARTIAL)
        self.assertFalse(snapshot.position.state_supported)
        self.assertIsNone(snapshot.position.has_position)
        self.assertIsNone(snapshot.position.in_watchlist)
        self.assertEqual(snapshot.position.quality.status, AvailabilityStatus.UNAVAILABLE)

    def test_etf_fundamentals_are_not_applicable(self) -> None:
        snapshot = build_analysis_snapshot(3, connection=self.conn)
        self.assertEqual(snapshot.fundamental.quality.status, AvailabilityStatus.NOT_APPLICABLE)
