from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import AvailabilityStatus, StrategyType  # noqa: E402
from analysis_engine import build_analysis_snapshot  # noqa: E402
from _fixtures import make_connection  # noqa: E402


class StrategyAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.today = date.today()

    def tearDown(self) -> None:
        self.conn.close()

    def _insert(self, strategy: str, start: date, end: date | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO strategy_assignment(
                security_id, strategy_type, effective_from, effective_to, source, rationale
            ) VALUES (1, ?, ?, ?, 'test', 'fixture')
            """,
            (strategy, start.isoformat(), end.isoformat() if end else None),
        )

    def test_no_assignment_resolves_to_available_unknown(self) -> None:
        snapshot = build_analysis_snapshot(1, connection=self.conn)
        self.assertEqual(snapshot.position.strategy, StrategyType.UNKNOWN)
        self.assertEqual(snapshot.position.strategy_quality.status, AvailabilityStatus.AVAILABLE)

    def test_active_swing_and_long_term_assignments_resolve(self) -> None:
        self._insert("swing", self.today - timedelta(days=1))
        self.assertEqual(build_analysis_snapshot(1, connection=self.conn).position.strategy, StrategyType.SWING)
        self.conn.execute("DELETE FROM strategy_assignment")
        self._insert("long_term", self.today - timedelta(days=1))
        self.assertEqual(build_analysis_snapshot(1, connection=self.conn).position.strategy, StrategyType.LONG_TERM)

    def test_future_and_expired_assignments_do_not_apply(self) -> None:
        self._insert("swing", self.today + timedelta(days=1))
        self.assertEqual(build_analysis_snapshot(1, connection=self.conn).position.strategy, StrategyType.UNKNOWN)
        self.conn.execute("DELETE FROM strategy_assignment")
        self._insert("swing", self.today - timedelta(days=10), self.today - timedelta(days=1))
        self.assertEqual(build_analysis_snapshot(1, connection=self.conn).position.strategy, StrategyType.UNKNOWN)

    def test_historical_strategy_resolves_while_position_state_remains_unsupported(self) -> None:
        self._insert("tactical", date(2025, 1, 1), date(2025, 12, 31))
        snapshot = build_analysis_snapshot(1, as_of="2025-06-01", connection=self.conn)
        self.assertEqual(snapshot.position.strategy, StrategyType.TACTICAL)
        self.assertEqual(snapshot.position.strategy_quality.status, AvailabilityStatus.AVAILABLE)
        self.assertFalse(snapshot.position.state_supported)

    def test_overlapping_assignments_are_not_arbitrarily_selected(self) -> None:
        self._insert("swing", self.today - timedelta(days=5))
        self._insert("long_term", self.today - timedelta(days=1))
        snapshot = build_analysis_snapshot(1, connection=self.conn)
        self.assertEqual(snapshot.position.strategy, StrategyType.UNKNOWN)
        self.assertEqual(snapshot.position.strategy_quality.status, AvailabilityStatus.UNAVAILABLE)
        self.assertIn("multiple overlapping", snapshot.position.strategy_quality.details[0])

    def test_migration_creates_constraints_and_rejects_overlap(self) -> None:
        migration_path = Path(__file__).resolve().parents[2] / "tools" / "trading" / "Migrate-TradingStrategyAssignments.py"
        spec = importlib.util.spec_from_file_location("strategy_migration", migration_path)
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "trading.db"
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
                INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0');
                CREATE TABLE security (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                INSERT INTO security(id, name) VALUES (1, 'Test');
                """
            )
            migration.apply_migration(conn)
            self.assertTrue(migration.inspect_migration(conn)["table_exists"])
            conn.execute("INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'swing', '2025-01-01')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (1, 'long_term', '2025-06-01')")
            conn.close()
