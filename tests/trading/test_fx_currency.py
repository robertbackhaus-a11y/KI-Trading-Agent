from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import Action, AvailabilityStatus, StrategyType  # noqa: E402
from analysis_engine import build_analysis_snapshot  # noqa: E402
from decision_engine import decide  # noqa: E402
from fx_resolver import resolve_fx_rate  # noqa: E402
from strategy_config import FXConfig, StrategyConfig  # noqa: E402
from _fixtures import MARKET_END, make_connection  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "trading" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("fx_migration", "Migrate-TradingFXRates.py")
ecb = _load("ecb_backfill", "Backfill-TradingFXRatesECB.py")


def _migration_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0');
        CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, isin TEXT, wkn TEXT, name TEXT NOT NULL, exchange TEXT, currency TEXT);
        CREATE TABLE positions (security_id INTEGER PRIMARY KEY, shares REAL, avg_cost REAL, remaining_cost_basis REAL, currency TEXT, realized_gain REAL, last_transaction_at TEXT);
        CREATE TABLE market_snapshot (security_id INTEGER PRIMARY KEY, price REAL, previous_close REAL, market_cap REAL, currency TEXT, as_of_at TEXT);
        """
    )
    return conn


class FXSchemaAndResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _migration_connection()
        migration.apply_migration(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _rate(self, rate_date: str, rate: float = 1.2) -> None:
        self.conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', 'USD', ?, 'ECB', ?)",
            (rate_date, rate, rate_date + "T16:00:00+00:00"),
        )

    def test_migration_dry_run_and_feature_marker(self) -> None:
        conn = _migration_connection()
        try:
            before = migration.inspect_migration(conn)
            self.assertFalse(before["table_exists"])
            self.assertIsNone(before["feature_version"])
            result = migration.apply_migration(conn)
            self.assertTrue(result["after"]["table_exists"])
            self.assertEqual(result["after"]["feature_version"], "1")
        finally:
            conn.close()

    def test_fx_schema_enforces_unique_pair_and_ecb_orientation(self) -> None:
        self._rate("2026-09-18")
        with self.assertRaises(sqlite3.IntegrityError):
            self._rate("2026-09-18")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES ('2026-09-19', 'USD', 'EUR', 1, 'ECB', 'x')"
            )

    def test_resolver_same_currency_and_both_eur_usd_directions(self) -> None:
        self._rate("2026-09-18", 1.2)
        same = resolve_fx_rate(self.conn, "EUR", "EUR", "2026-09-18")
        self.assertEqual(same.convert(10), 10.0)
        self.assertEqual(same.quality.status, AvailabilityStatus.NOT_APPLICABLE)
        usd_eur = resolve_fx_rate(self.conn, "USD", "EUR", "2026-09-18")
        self.assertAlmostEqual(usd_eur.convert(120) or 0, 100.0)
        eur_usd = resolve_fx_rate(self.conn, "EUR", "USD", "2026-09-18")
        self.assertAlmostEqual(eur_usd.convert(100) or 0, 120.0)
        pence = resolve_fx_rate(self.conn, "GBp", "EUR", "2026-09-18")
        self.assertEqual(pence.quality.status, AvailabilityStatus.UNAVAILABLE)
        self.assertIsNone(pence.convert(100))

    def test_resolver_uses_previous_business_day_not_future_or_stale(self) -> None:
        self._rate("2026-09-18", 1.2)
        self._rate("2026-09-22", 1.3)
        weekend = resolve_fx_rate(self.conn, "USD", "EUR", "2026-09-20")
        self.assertEqual(weekend.rate_date, "2026-09-18")
        self.assertAlmostEqual(weekend.convert(120) or 0, 100.0)
        missing = resolve_fx_rate(self.conn, "USD", "EUR", "2026-09-17")
        self.assertEqual(missing.quality.status, AvailabilityStatus.UNAVAILABLE)
        stale = resolve_fx_rate(
            self.conn, "USD", "EUR", "2026-09-30", FXConfig(fx_max_age_days=5)
        )
        self.assertEqual(stale.quality.status, AvailabilityStatus.STALE)
        self.assertIsNone(stale.convert(120))

    def test_ecb_csv_parser_and_url_use_official_eur_orientation(self) -> None:
        payload = "FREQ,CURRENCY,CURRENCY_DENOM,TIME_PERIOD,OBS_VALUE\nD,USD,EUR,2026-09-18,1.1490\n"
        rows = ecb.parse_ecb_csv(payload, "USD")
        self.assertEqual(rows[0]["base_currency"], "EUR")
        self.assertEqual(rows[0]["quote_currency"], "USD")
        self.assertEqual(rows[0]["rate"], 1.149)
        self.assertIn("D.USD.EUR.SP00.A", ecb.build_ecb_url("USD", latest=True))

    def test_ecb_upsert_is_idempotent(self) -> None:
        rows = [{
            "rate_date": "2026-09-18",
            "base_currency": "EUR",
            "quote_currency": "USD",
            "rate": 1.149,
            "source": "ECB",
        }]
        self.assertEqual(ecb.upsert_rates(self.conn, rows, fetched_at="2026-09-18T16:00:00+00:00"), 1)
        rows[0]["rate"] = 1.15
        self.assertEqual(ecb.upsert_rates(self.conn, rows, fetched_at="2026-09-19T16:00:00+00:00"), 1)
        stored = self.conn.execute("SELECT rate, fetched_at FROM fx_rates").fetchone()
        self.assertEqual(stored["rate"], 1.15)
        self.assertEqual(stored["fetched_at"], "2026-09-19T16:00:00+00:00")


class FXAnalysisAndDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.today = MARKET_END.isoformat()
        self.conn.execute("UPDATE positions SET currency = 'EUR', avg_cost = 100, remaining_cost_basis = 1000 WHERE security_id = 1")
        self.conn.execute("UPDATE market_snapshot SET price = 144, currency = 'USD' WHERE security_id = 1")
        self.conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', 'USD', 1.2, 'ECB', ?)",
            (self.today, self.today + "T16:00:00+00:00"),
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_analysis_converts_native_usd_to_cost_basis_eur_with_provenance(self) -> None:
        snapshot = build_analysis_snapshot(1, connection=self.conn)
        position = snapshot.position
        self.assertEqual(position.current_price_native, 144)
        self.assertEqual(position.current_price_currency, "USD")
        self.assertAlmostEqual(position.current_price_cost_currency or 0, 120.0)
        self.assertEqual(position.valuation_currency, "EUR")
        self.assertEqual(position.fx_rate, 1.2)
        self.assertEqual(position.fx_rate_date, self.today)
        self.assertEqual(position.fx_source, "ECB")
        self.assertEqual(position.fx_quality.status, AvailabilityStatus.AVAILABLE)
        self.assertAlmostEqual(position.unrealized_gain_pct or 0, 20.0)
        self.assertAlmostEqual(position.unrealized_gain_amount or 0, 200.0)

    def test_same_currency_requires_no_fx(self) -> None:
        self.conn.execute("UPDATE market_snapshot SET price = 120, currency = 'EUR' WHERE security_id = 1")
        snapshot = build_analysis_snapshot(1, connection=self.conn)
        self.assertEqual(snapshot.position.current_price_cost_currency, 120)
        self.assertEqual(snapshot.position.fx_quality.status, AvailabilityStatus.NOT_APPLICABLE)

    def test_missing_or_stale_fx_suppresses_valuation(self) -> None:
        self.conn.execute("DELETE FROM fx_rates")
        missing = build_analysis_snapshot(1, connection=self.conn)
        self.assertIsNone(missing.position.current_price_cost_currency)
        self.assertIsNone(missing.position.unrealized_gain_pct)
        self.assertEqual(missing.position.fx_quality.status, AvailabilityStatus.UNAVAILABLE)
        self.conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', 'USD', 1.2, 'ECB', ?)",
            ((MARKET_END - timedelta(days=6)).isoformat(), self.today + "T16:00:00+00:00"),
        )
        stale = build_analysis_snapshot(1, connection=self.conn)
        self.assertIsNone(stale.position.current_price_cost_currency)
        self.assertEqual(stale.position.fx_quality.status, AvailabilityStatus.STALE)

    def test_swing_tp_uses_converted_cost_currency_and_never_trims_or_sells(self) -> None:
        self.conn.execute(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'swing', ?)",
            (date.today().isoformat(),),
        )
        snapshot = build_analysis_snapshot(1, connection=self.conn)
        result = decide(snapshot, StrategyConfig())
        self.assertEqual(result.action, Action.HOLD)
        self.assertEqual(result.tp1, 120.0)
        self.assertEqual(result.tp2, 125.0)
        self.assertIn("TP1_REACHED", result.reasons)
        self.assertNotIn("TP2_REACHED", result.reasons)
        self.assertEqual(result.target_price_currency, "EUR")
        self.assertNotIn(result.action, {Action.TRIM, Action.SELL})
        self.conn.execute("UPDATE market_snapshot SET price = 150 WHERE security_id = 1")
        tp2 = decide(build_analysis_snapshot(1, connection=self.conn), StrategyConfig())
        self.assertIn("TP1_REACHED", tp2.reasons)
        self.assertIn("TP2_REACHED", tp2.reasons)
        self.assertEqual(tp2.action, Action.HOLD)

    def test_missing_and_stale_fx_emit_no_tp_signal(self) -> None:
        self.conn.execute(
            "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'swing', ?)",
            (date.today().isoformat(),),
        )
        self.conn.execute("DELETE FROM fx_rates")
        missing = decide(build_analysis_snapshot(1, connection=self.conn), StrategyConfig())
        self.assertIsNone(missing.tp1)
        self.assertIn("SWING_TP_FX_UNAVAILABLE", missing.risks)
        self.assertTrue(missing.blocking_data_gaps)
        self.conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', 'USD', 1.2, 'ECB', ?)",
            ((MARKET_END - timedelta(days=6)).isoformat(), self.today + "T16:00:00+00:00"),
        )
        stale = decide(build_analysis_snapshot(1, connection=self.conn), StrategyConfig())
        self.assertIsNone(stale.tp1)
        self.assertIn("SWING_TP_FX_STALE", stale.risks)
        self.assertTrue(stale.blocking_data_gaps)


if __name__ == "__main__":
    unittest.main()
