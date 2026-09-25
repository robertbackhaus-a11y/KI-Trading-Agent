"""Tests for deterministic zero-position quote currency provenance."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRADING = ROOT / 'tools' / 'trading'
sys.path.insert(0, str(TRADING))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixtures import make_connection  # noqa: E402
from analysis_contracts import AvailabilityStatus, StrategyType  # noqa: E402
from analysis_engine import build_analysis_snapshot  # noqa: E402


def load_backfill():
    path = TRADING / 'Backfill-TradingMarketSnapshotCurrency.py'
    spec = importlib.util.spec_from_file_location('snapshot_currency_backfill', path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MarketSnapshotCurrencyBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_backfill()
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE security(id INTEGER PRIMARY KEY, currency TEXT);
            CREATE TABLE market_snapshot(security_id INTEGER PRIMARY KEY, as_of_at TEXT, price REAL, currency TEXT, source_id INTEGER);
            CREATE TABLE market_data(id INTEGER PRIMARY KEY, security_id INTEGER, source_id INTEGER, trade_date TEXT, currency TEXT);
            CREATE TABLE source_symbols(id INTEGER PRIMARY KEY, security_id INTEGER, source_id INTEGER, currency TEXT);
            CREATE TABLE watchlist(security_id INTEGER PRIMARY KEY, status TEXT);
        ''')
        self.conn.executemany('INSERT INTO security(id, currency) VALUES (?, ?)', [(1, 'EUR'), (2, 'USD'), (3, None), (4, 'EUR'), (5, 'USD'), (6, 'GBP')])
        self.conn.executemany('INSERT INTO market_snapshot(security_id, as_of_at, price, currency, source_id) VALUES (?, "2026-09-21T12:00:00+00:00", 10, ?, 1)', [(1, 'GBP'), (2, None), (3, None), (4, None), (5, None), (6, 'GBp')])
        self.conn.execute('INSERT INTO market_data(security_id, source_id, trade_date, currency) VALUES (2, 1, "2026-09-21", "USD")')
        self.conn.execute('INSERT INTO source_symbols(security_id, source_id, currency) VALUES (3, 1, "AUD")')
        self.conn.executemany('INSERT INTO market_data(security_id, source_id, trade_date, currency) VALUES (5, 1, "2026-09-21", ?)', [('USD',), ('EUR',)])
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_resolution_hierarchy_and_conflict_safety(self) -> None:
        rows = {item.security_id: item for item in self.module.build_plan(self.conn)}
        self.assertEqual((rows[1].currency, rows[1].status, rows[1].provenance), ('GBP', 'CURRENCY_ALREADY_PRESENT', 'market_snapshot.currency'))
        self.assertEqual((rows[2].currency, rows[2].provenance), ('USD', 'market_data.currency'))
        self.assertEqual((rows[3].currency, rows[3].provenance), ('AUD', 'source_symbols.currency'))
        self.assertEqual((rows[4].currency, rows[4].provenance), ('EUR', 'security.currency'))
        self.assertEqual((rows[5].currency, rows[5].status), (None, 'CURRENCY_AMBIGUOUS'))
        self.assertEqual((rows[6].currency, rows[6].status), (None, 'CURRENCY_UNRESOLVED'))

    def test_dry_plan_is_read_only_and_apply_is_idempotent(self) -> None:
        plan = self.module.build_plan(self.conn)
        self.assertIsNone(self.conn.execute('SELECT currency FROM market_snapshot WHERE security_id=2').fetchone()[0])
        self.assertEqual(self.module.apply_plan(self.conn, plan), 3)
        self.assertEqual(self.conn.execute('SELECT currency FROM market_snapshot WHERE security_id=2').fetchone()[0], 'USD')
        rerun = self.module.build_plan(self.conn)
        self.assertEqual(self.module.apply_plan(self.conn, rerun), 0)
        self.assertIsNone(self.conn.execute('SELECT currency FROM market_snapshot WHERE security_id=5').fetchone()[0])

    def test_zero_position_preserves_native_quote_and_date_safe_fx_conversion(self) -> None:
        conn = make_connection()
        try:
            conn.execute("ALTER TABLE security ADD COLUMN currency TEXT")
            conn.execute("UPDATE security SET currency='USD' WHERE id=2")
            conn.execute("INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (2, '2026-09-21T12:00:00+00:00', 100, 'USD')")
            conn.execute("INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES ('2026-09-21', 'EUR', 'USD', 1.25, 'ECB', '2026-09-21T12:00:00+00:00')")
            snapshot = build_analysis_snapshot(2, connection=conn)
            self.assertFalse(snapshot.position.has_position)
            self.assertEqual(snapshot.position.current_price_native, 100)
            self.assertEqual(snapshot.position.current_price_currency, 'USD')
            self.assertEqual(snapshot.position.current_price_cost_currency, 80)
            self.assertEqual(snapshot.position.fx_rate_date, '2026-09-21')
            self.assertEqual(snapshot.position.fx_quality.status, AvailabilityStatus.AVAILABLE)
            # A later-only rate cannot leak into an earlier snapshot.
            conn.execute("DELETE FROM fx_rates")
            conn.execute("INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES ('2026-09-22', 'EUR', 'USD', 1.25, 'ECB', '2026-09-22T12:00:00+00:00')")
            old = build_analysis_snapshot(2, as_of='2026-09-21', connection=conn)
            self.assertIsNone(old.position.current_price_cost_currency)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
