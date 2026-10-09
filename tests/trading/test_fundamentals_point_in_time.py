"""Point-in-time visibility of fundamentals: filing_date, collection-date fallback, unverified quality cap."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import AvailabilityStatus  # noqa: E402
from analysis_engine import FUNDAMENTALS_PUBLICATION_DATE_UNVERIFIED, build_analysis_snapshot  # noqa: E402
from decision_engine import _confidence  # noqa: E402
from _fixtures import make_connection  # noqa: E402

AS_OF = "2025-10-01"
CODE = FUNDAMENTALS_PUBLICATION_DATE_UNVERIFIED
COLUMNS = (
    "security_id, period_end, period_type, fiscal_year, fiscal_quarter, filing_date, fetched_at, currency, "
    "revenue, operating_income, net_income, operating_cash_flow, free_cash_flow, cash, total_debt, source_id"
)


def row(period_end, period_type, fiscal_year, fiscal_quarter, filing_date, fetched_at, revenue=100.0, ocf=20.0, source_id=4):
    return (1, period_end, period_type, fiscal_year, fiscal_quarter, filing_date, fetched_at, "EUR", revenue, 10.0, 10.0, ocf, 8.0, 40.0, 15.0, source_id)


class FundamentalsPointInTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.conn.execute("DELETE FROM fundamentals")

    def tearDown(self) -> None:
        self.conn.close()

    def _rows(self, *rows) -> None:
        self.conn.execute("DELETE FROM fundamentals")
        self.conn.executemany(f"INSERT INTO fundamentals({COLUMNS}) VALUES ({','.join('?' * 16)})", rows)

    def _fundamental(self, as_of: str = AS_OF):
        return build_analysis_snapshot(1, as_of=as_of, connection=self.conn).fundamental

    def _details(self, fundamental) -> str:
        return " | ".join(fundamental.quality.details)

    def test_filing_date_on_or_before_as_of_is_used_normally(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, "2025-08-01", "2026-01-01T00:00:00+00:00"))
        for as_of in ("2025-08-01", AS_OF):
            with self.subTest(as_of=as_of):
                f = self._fundamental(as_of)
                self.assertEqual(f.period_end, "2025-06-30")
                self.assertEqual(f.quality.status, AvailabilityStatus.AVAILABLE)
                self.assertNotIn(CODE, self._details(f))

    def test_filing_date_after_as_of_is_not_visible_even_if_fetched_earlier(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, "2025-10-15", "2025-09-01T00:00:00+00:00"))
        f = self._fundamental(AS_OF)
        self.assertEqual(f.quality.status, AvailabilityStatus.UNAVAILABLE)
        self.assertIsNone(f.period_end)

    def test_null_filing_date_is_used_from_the_collection_date_and_capped_at_partial(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, None, "2025-09-20T10:04:53+00:00"))
        f = self._fundamental(AS_OF)
        self.assertEqual(f.period_end, "2025-06-30")
        self.assertEqual(f.quality.status, AvailabilityStatus.PARTIAL)  # all core fields present, still capped
        self.assertIn(CODE, self._details(f))
        self.assertIn("2025-09-20", self._details(f))
        self.assertEqual(f.revenue, 100.0)

    def test_null_filing_date_before_collection_is_not_visible(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, None, "2025-09-20T10:04:53+00:00"))
        f = self._fundamental("2025-09-19")
        self.assertEqual(f.quality.status, AvailabilityStatus.UNAVAILABLE)
        self.assertIsNone(f.period_end)

    def test_collection_date_boundary_uses_the_calendar_day(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, None, "2025-09-20T23:59:59+00:00"))
        self.assertEqual(self._fundamental("2025-09-20").period_end, "2025-06-30")
        self.assertIsNone(self._fundamental("2025-09-19").period_end)

    def test_null_filing_date_without_collection_date_is_never_visible(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, None, None))
        self.assertEqual(self._fundamental(AS_OF).quality.status, AvailabilityStatus.UNAVAILABLE)

    def test_period_end_is_never_a_publication_date(self) -> None:
        self._rows(row("2025-03-31", "quarterly", 2025, 1, None, "2025-12-01T00:00:00+00:00"))
        self.assertEqual(self._fundamental("2025-06-01").quality.status, AvailabilityStatus.UNAVAILABLE)

    def test_newer_unverified_period_wins_over_older_verified_period_when_visible(self) -> None:
        self._rows(
            row("2024-12-31", "annual", 2024, None, "2025-03-10", "2026-01-01T00:00:00+00:00", source_id=3),
            row("2025-06-30", "semiannual", 2025, None, None, "2025-09-20T10:00:00+00:00", source_id=4),
        )
        newer = self._fundamental(AS_OF)
        self.assertEqual((newer.period_end, newer.period_type), ("2025-06-30", "semiannual"))
        self.assertEqual(newer.quality.status, AvailabilityStatus.PARTIAL)
        older = self._fundamental("2025-09-19")
        self.assertEqual((older.period_end, older.period_type), ("2024-12-31", "annual"))
        self.assertEqual(older.quality.status, AvailabilityStatus.AVAILABLE)
        self.assertNotIn(CODE, self._details(older))

    def test_verified_row_wins_a_same_period_tie(self) -> None:
        self._rows(
            row("2025-06-30", "quarterly", 2025, 2, None, "2025-09-20T10:00:00+00:00", source_id=4),
            row("2025-06-30", "quarterly", 2025, 2, "2025-08-01", "2025-09-25T10:00:00+00:00", source_id=3),
        )
        f = self._fundamental(AS_OF)
        self.assertEqual(f.quality.status, AvailabilityStatus.AVAILABLE)
        self.assertNotIn(CODE, self._details(f))

    def test_missing_core_field_and_unverified_date_are_both_reported(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, None, "2025-09-20T10:00:00+00:00", ocf=None))
        f = self._fundamental(AS_OF)
        self.assertEqual(f.quality.status, AvailabilityStatus.PARTIAL)
        self.assertIn("latest period missing operating_cash_flow", self._details(f))
        self.assertIn(CODE, self._details(f))

    def test_year_over_year_uses_the_same_visibility_rule(self) -> None:
        self._rows(
            row("2024-06-30", "quarterly", 2024, 2, None, "2025-09-20T10:00:00+00:00", revenue=100.0),
            row("2025-06-30", "quarterly", 2025, 2, None, "2025-09-20T10:00:00+00:00", revenue=120.0),
        )
        self.assertAlmostEqual(self._fundamental(AS_OF).revenue_yoy_growth_pct or 0.0, 20.0)
        self.assertIsNone(self._fundamental("2025-09-19").revenue_yoy_growth_pct)

    def test_current_path_without_as_of_uses_the_same_visibility_rule_as_the_explicit_path(self) -> None:
        today = date.today()
        iso = lambda days: (today + timedelta(days=days)).isoformat()
        stamp = lambda days: iso(days) + "T10:00:00+00:00"
        self._rows(
            row("2024-12-31", "annual", 2024, None, "2025-03-10", stamp(-200), source_id=3),
            row("2025-06-30", "semiannual", 2025, None, None, stamp(-3), source_id=4),    # undated, collected: visible, unverified
            row("2025-09-30", "quarterly", 2025, 3, None, None, source_id=4),             # undated, no collection date: invisible
            row("2025-12-31", "annual", 2025, None, None, stamp(+5), source_id=4),        # undated, "collected" in the future: invisible
            row("2026-03-31", "quarterly", 2026, 1, iso(+9), stamp(-1), source_id=4),     # filing date in the future: invisible
        )
        current = build_analysis_snapshot(1, connection=self.conn).fundamental
        explicit = build_analysis_snapshot(1, as_of=today.isoformat(), connection=self.conn).fundamental
        for f in (current, explicit):
            self.assertEqual((f.period_end, f.period_type), ("2025-06-30", "semiannual"))
            self.assertEqual(f.quality.status, AvailabilityStatus.PARTIAL)
            self.assertIn(CODE, self._details(f))
        self.assertEqual(self._details(current), self._details(explicit))

    def test_current_path_hides_rows_the_strict_guard_hid_and_keeps_verified_rows_normal(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, "2025-08-01", None))
        f = build_analysis_snapshot(1, connection=self.conn).fundamental
        self.assertEqual(f.quality.status, AvailabilityStatus.AVAILABLE)
        self.assertNotIn(CODE, self._details(f))
        self._rows(row("2025-06-30", "quarterly", 2025, 2, None, None))
        self.assertEqual(build_analysis_snapshot(1, connection=self.conn).fundamental.quality.status, AvailabilityStatus.UNAVAILABLE)

    def test_unverified_rows_lower_confidence_gain_to_the_partial_value(self) -> None:
        self._rows(row("2025-06-30", "quarterly", 2025, 2, "2025-08-01", "2025-09-20T10:00:00+00:00"))
        verified = _confidence(build_analysis_snapshot(1, as_of=AS_OF, connection=self.conn))
        self._rows(row("2025-06-30", "quarterly", 2025, 2, None, "2025-09-20T10:00:00+00:00"))
        unverified = _confidence(build_analysis_snapshot(1, as_of=AS_OF, connection=self.conn))
        self._rows()
        absent = _confidence(build_analysis_snapshot(1, as_of=AS_OF, connection=self.conn))
        self.assertAlmostEqual(verified - unverified, 0.05)   # available +0.10 vs partial +0.05
        self.assertAlmostEqual(unverified - absent, 0.10)     # partial +0.05 vs unavailable -0.05


if __name__ == "__main__":
    unittest.main()
