"""Phase-3B.1 schema, manual lifecycle, and read-only derivation tests."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import AvailabilityStatus  # noqa: E402
from analysis_engine import build_analysis_snapshot  # noqa: E402
from _fixtures import make_connection  # noqa: E402
from swing_lifecycle import derive_open_lifecycle  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "trading" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("swing_lifecycle_migration", "Migrate-TradingSwingLifecycle.py")
manager = _load("swing_campaign_manager", "Manage-TradingSwingCampaigns.py")


def _create_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0');
        INSERT INTO metadata(key, value) VALUES ('strategy_assignment_schema_version', '1');
        CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL);
        CREATE TABLE strategy_assignment (
            id INTEGER PRIMARY KEY, security_id INTEGER NOT NULL, strategy_type TEXT NOT NULL,
            effective_from TEXT NOT NULL, effective_to TEXT
        );
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, security_id INTEGER NOT NULL, transaction_type TEXT NOT NULL,
            shares REAL
        );
        CREATE TABLE positions (security_id INTEGER PRIMARY KEY, shares REAL NOT NULL);
        INSERT INTO security(id, symbol, name) VALUES (1, 'SWING', 'Swing Security');
        INSERT INTO security(id, symbol, name) VALUES (2, 'LONG', 'Long Security');
        INSERT INTO strategy_assignment(id, security_id, strategy_type, effective_from)
        VALUES (10, 1, 'swing', '2026-01-01');
        INSERT INTO strategy_assignment(id, security_id, strategy_type, effective_from)
        VALUES (20, 2, 'long_term', '2026-01-01');
        INSERT INTO transactions(id, security_id, transaction_type, shares) VALUES (100, 1, 'BUY', 10);
        INSERT INTO transactions(id, security_id, transaction_type, shares) VALUES (101, 1, 'SELL', 10);
        INSERT INTO transactions(id, security_id, transaction_type, shares) VALUES (200, 2, 'SELL', 10);
        INSERT INTO positions(security_id, shares) VALUES (1, 10);
        """
    )
    migration.apply_migration(conn)
    return conn


class SwingCampaignMigrationTests(unittest.TestCase):
    def test_migration_dry_run_write_and_feature_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trading.db"
            conn = sqlite3.connect(path, isolation_level=None)
            conn.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
                INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0');
                INSERT INTO metadata(key, value) VALUES ('strategy_assignment_schema_version', '1');
                CREATE TABLE security (id INTEGER PRIMARY KEY);
                CREATE TABLE strategy_assignment (id INTEGER PRIMARY KEY, security_id INTEGER, strategy_type TEXT);
                CREATE TABLE transactions (id INTEGER PRIMARY KEY, security_id INTEGER, transaction_type TEXT);
                """
            )
            before = migration.inspect_migration(conn)
            self.assertFalse(before["campaign_table_exists"])
            applied = migration.apply_migration(conn)
            self.assertTrue(applied["after"]["campaign_table_exists"])
            self.assertTrue(applied["after"]["event_table_exists"])
            self.assertEqual(applied["after"]["feature_version"], "1")
            conn.close()


class SwingCampaignManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = _create_db(Path(self.temp_dir.name) / "trading.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _open(self, *, write: bool = True, **overrides: object) -> dict:
        values: dict[str, object] = {
            "security_id": 1,
            "opened_at": "2026-09-22",
            "original_quantity": 10,
            "reference_avg_cost": 100,
            "reference_currency": "EUR",
            "source": "manual",
            "rationale": "test baseline",
        }
        values.update(overrides)
        return manager.open_campaign(self.conn, write=write, **values)

    def test_valid_open_dry_run_and_write_create_atomic_baseline(self) -> None:
        dry = self._open(write=False)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM swing_campaign").fetchone()[0], 0)

        written = self._open()
        self.assertEqual(written["inserted_campaign"]["original_quantity"], 10.0)
        baseline = written["inserted_baseline_event"]
        self.assertEqual(baseline["event_type"], "baseline")
        self.assertEqual(baseline["quantity"], 10.0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM swing_campaign_event").fetchone()[0], 1)

    def test_open_rejects_missing_or_long_term_assignment_and_duplicate_open(self) -> None:
        with self.assertRaisesRegex(manager.CampaignValidationError, "exactly one active Swing"):
            self._open(security_id=2)
        with self.assertRaisesRegex(manager.CampaignValidationError, "security_id 99"):
            self._open(security_id=99)
        self._open()
        with self.assertRaisesRegex(manager.CampaignValidationError, "already has open"):
            self._open(write=False)

    def test_close_dry_run_write_and_already_closed_rejected(self) -> None:
        campaign_id = self._open()["inserted_campaign"]["id"]
        dry = manager.close_campaign(
            self.conn, campaign_id=campaign_id, closed_at="2026-10-01", write=False
        )
        self.assertTrue(dry["dry_run"])
        self.assertEqual(self.conn.execute("SELECT status FROM swing_campaign").fetchone()[0], "open")
        closed = manager.close_campaign(
            self.conn, campaign_id=campaign_id, closed_at="2026-10-01", write=True
        )
        self.assertEqual(closed["closed_campaign"]["status"], "closed")
        with self.assertRaisesRegex(manager.CampaignValidationError, "already closed"):
            manager.close_campaign(self.conn, campaign_id=campaign_id, closed_at="2026-10-02")

    def test_explicit_events_and_transaction_linkage(self) -> None:
        campaign_id = self._open()["inserted_campaign"]["id"]
        signal = manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="tp1_signal",
            event_at="2026-09-23", source="analysis", write=True,
        )
        self.assertEqual(signal["inserted_event"]["event_type"], "tp1_signal")
        execution = manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="tp1_execution",
            event_at="2026-09-24", quantity=2, price=120, currency="EUR",
            transaction_id=101, source="manual", write=True,
        )
        self.assertEqual(execution["inserted_event"]["transaction_id"], 101)
        manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="tp2_execution",
            event_at="2026-09-25", quantity=3, source="manual", write=True,
        )
        manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="manual_reduction",
            event_at="2026-09-26", quantity=1, source="manual", write=True,
        )
        lifecycle = manager.list_campaigns(self.conn)[0]["lifecycle"]
        self.assertEqual(lifecycle["tp1_status"], "executed")
        self.assertEqual(lifecycle["tp2_status"], "executed")
        self.assertEqual(lifecycle["tp1_executed_quantity"], 2.0)
        self.assertEqual(lifecycle["tp2_executed_quantity"], 3.0)
        self.assertEqual(lifecycle["manual_reduction_quantity"], 1.0)

    def test_wrong_security_transaction_and_signal_quantity_are_rejected(self) -> None:
        campaign_id = self._open()["inserted_campaign"]["id"]
        with self.assertRaisesRegex(manager.CampaignValidationError, "different security"):
            manager.add_event(
                self.conn, campaign_id=campaign_id, event_type="tp1_execution",
                event_at="2026-09-23", quantity=1, transaction_id=200, source="manual",
            )
        with self.assertRaisesRegex(manager.CampaignValidationError, "must not contain a quantity"):
            manager.add_event(
                self.conn, campaign_id=campaign_id, event_type="tp1_signal",
                event_at="2026-09-23", quantity=1, source="analysis",
            )

    def test_generic_sell_and_signal_do_not_create_tp_execution_or_change_quantity(self) -> None:
        campaign_id = self._open()["inserted_campaign"]["id"]
        before = manager.list_campaigns(self.conn)[0]["lifecycle"]
        self.assertEqual(before["tp1_status"], "not_recorded")
        self.assertEqual(before["event_derived_quantity"], 10.0)
        # Transaction 101 is a generic SELL until an explicit lifecycle event links it.
        manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="tp1_signal",
            event_at="2026-09-23", source="analysis", write=True,
        )
        after = manager.list_campaigns(self.conn)[0]["lifecycle"]
        self.assertEqual(after["tp1_status"], "signaled")
        self.assertEqual(after["tp1_executed_quantity"], 0.0)
        self.assertEqual(after["event_derived_quantity"], 10.0)

    def test_tp2_signal_is_recorded_without_execution(self) -> None:
        campaign_id = self._open()["inserted_campaign"]["id"]
        manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="tp2_signal",
            event_at="2026-09-23", source="analysis", write=True,
        )
        lifecycle = manager.list_campaigns(self.conn)[0]["lifecycle"]
        self.assertEqual(lifecycle["tp1_status"], "not_recorded")
        self.assertEqual(lifecycle["tp2_status"], "signaled")
        self.assertEqual(lifecycle["tp2_executed_quantity"], 0.0)

    def test_post_tp2_add_is_derived_from_explicit_event_order(self) -> None:
        campaign_id = self._open()["inserted_campaign"]["id"]
        manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="tp1_execution",
            event_at="2026-09-23", quantity=3, source="manual", write=True,
        )
        manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="tp2_execution",
            event_at="2026-09-24", quantity=4, source="manual", write=True,
        )
        manager.add_event(
            self.conn, campaign_id=campaign_id, event_type="add",
            event_at="2026-09-25", quantity=1, source="manual", write=True,
        )
        self.conn.execute("UPDATE positions SET shares = 4 WHERE security_id = 1")
        lifecycle = manager.list_campaigns(self.conn)[0]["lifecycle"]
        self.assertEqual(lifecycle["tp1_executed_quantity"], 3.0)
        self.assertEqual(lifecycle["tp2_executed_quantity"], 4.0)
        self.assertEqual(lifecycle["total_add_quantity"], 1.0)
        self.assertEqual(lifecycle["add_event_count"], 1)
        self.assertTrue(lifecycle["post_tp2_add_detected"])
        self.assertEqual(lifecycle["event_derived_quantity"], 4.0)
        self.assertEqual(lifecycle["reconciliation_quality"], "available")

    def test_reconciliation_reports_mismatch_without_repair(self) -> None:
        self._open()
        matching = manager.list_campaigns(self.conn)[0]["lifecycle"]
        self.assertEqual(matching["reconciliation_quality"], "available")
        self.conn.execute("UPDATE positions SET shares = 8 WHERE security_id = 1")
        mismatch = manager.list_campaigns(self.conn)[0]["lifecycle"]
        self.assertEqual(mismatch["reconciliation_quality"], "partial")
        self.assertEqual(mismatch["reconciliation_delta"], -2.0)
        self.assertEqual(self.conn.execute("SELECT original_quantity FROM swing_campaign").fetchone()[0], 10.0)

    def test_validate_clean_and_broken_baseline_fixture(self) -> None:
        self._open()
        self.assertEqual(manager.validate_campaigns(self.conn), [])
        self.conn.execute("DELETE FROM swing_campaign_event")
        errors = manager.validate_campaigns(self.conn)
        self.assertTrue(any("0 baseline events" in error for error in errors))


class SwingCampaignSnapshotIntegrationTests(unittest.TestCase):
    def test_current_swing_snapshot_exposes_missing_and_recorded_lifecycle_context(self) -> None:
        conn = make_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
                INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0');
                INSERT INTO metadata(key, value) VALUES ('strategy_assignment_schema_version', '1');
                CREATE TABLE transactions (id INTEGER PRIMARY KEY, security_id INTEGER, transaction_type TEXT, shares REAL);
                """
            )
            migration.apply_migration(conn)
            conn.execute(
                "INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'swing', ?)",
                (date.today().isoformat(),),
            )
            conn.commit()
            missing = build_analysis_snapshot(1, connection=conn)
            self.assertEqual(missing.position.lifecycle_quality.status, AvailabilityStatus.UNAVAILABLE)
            self.assertIsNone(missing.position.swing_campaign_id)
            opened = manager.open_campaign(
                conn, security_id=1, opened_at=date.today().isoformat(), original_quantity=10,
                reference_avg_cost=100, reference_currency="USD", source="manual", write=True,
            )
            current = build_analysis_snapshot(1, connection=conn)
            self.assertEqual(current.position.swing_campaign_id, opened["inserted_campaign"]["id"])
            self.assertEqual(current.position.tp1_lifecycle_status, "not_recorded")
            self.assertEqual(current.position.lifecycle_quality.status, AvailabilityStatus.AVAILABLE)
            manager.add_event(
                conn, campaign_id=opened["inserted_campaign"]["id"],
                event_type="stop_execution", event_at=date.today().isoformat(),
                quantity=1, source="manual", write=True,
            )
            after_stop_event = build_analysis_snapshot(1, connection=conn)
            self.assertEqual(after_stop_event.position.stop_execution_quantity, 1.0)
        finally:
            conn.close()

    def test_evaluation_and_market_dates_are_distinct_without_future_state_leaks(self) -> None:
        """A 22 Sep live evaluation may use 21 Sep market/FX data only."""

        class EvaluationDate(date):
            @classmethod
            def today(cls) -> date:
                return cls(2026, 9, 22)

        conn = make_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
                INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0');
                INSERT INTO metadata(key, value) VALUES ('strategy_assignment_schema_version', '1');
                CREATE TABLE transactions (id INTEGER PRIMARY KEY, security_id INTEGER, transaction_type TEXT, shares REAL);
                """
            )
            migration.apply_migration(conn)
            conn.execute("DELETE FROM market_data WHERE trade_date > '2026-09-21'")
            conn.execute(
                """INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id)
                   VALUES (1, '2026-09-23', 9999, 9999, 1)"""
            )
            conn.execute("UPDATE market_snapshot SET as_of_at = '2026-09-21T16:00:00+00:00', price = 144, currency = 'USD' WHERE security_id = 1")
            conn.execute("UPDATE positions SET currency = 'EUR', avg_cost = 100 WHERE security_id = 1")
            conn.execute("INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES ('2026-09-21', 'EUR', 'USD', 1.2, 'ECB', '2026-09-21T16:00:00+00:00')")
            # This row must not be selected for the 21 Sep market valuation.
            conn.execute("INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES ('2026-09-22', 'EUR', 'USD', 1.3, 'ECB', '2026-09-22T16:00:00+00:00')")
            conn.execute("INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'swing', '2026-09-22')")
            conn.commit()
            opened = manager.open_campaign(
                conn, security_id=1, opened_at="2026-09-22", original_quantity=10,
                reference_avg_cost=100, reference_currency="EUR", source="manual", write=True,
            )

            with patch("analysis_engine.date", EvaluationDate):
                live = build_analysis_snapshot(1, connection=conn)
                historical = build_analysis_snapshot(1, as_of="2026-09-21", connection=conn)

            self.assertEqual(live.evaluation_as_of, "2026-09-22")
            self.assertEqual(live.as_of, "2026-09-22")
            self.assertEqual(live.market_data_as_of, "2026-09-21")
            self.assertEqual(live.position.strategy.value, "swing")
            self.assertEqual(live.position.swing_campaign_id, opened["inserted_campaign"]["id"])
            self.assertEqual(live.position.fx_rate_date, "2026-09-21")
            self.assertAlmostEqual(live.position.current_price_cost_currency or 0, 120.0)

            self.assertEqual(historical.evaluation_as_of, "2026-09-21")
            self.assertEqual(historical.market_data_as_of, "2026-09-21")
            self.assertEqual(historical.position.strategy.value, "unknown")
            self.assertIsNone(historical.position.swing_campaign_id)
            self.assertEqual(historical.position.lifecycle_quality.status, AvailabilityStatus.UNAVAILABLE)

            manager.close_campaign(
                conn, campaign_id=opened["inserted_campaign"]["id"], closed_at="2026-09-22", write=True
            )
            after_close = derive_open_lifecycle(
                conn, 1, current_quantity=10, evaluation_as_of="2026-09-23"
            )
            self.assertIsNone(after_close.campaign_id)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
