"""Tests for the controlled strategy-assignment management CLI."""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "trading" / "Manage-TradingStrategyAssignments.py"
SPEC = importlib.util.spec_from_file_location("manage_strategy_assignments", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


def _create_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE security (
            id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL
        );
        CREATE TABLE strategy_assignment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            security_id INTEGER NOT NULL,
            strategy_type TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            source TEXT,
            rationale TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.execute(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        (manager.FEATURE_VERSION_KEY, manager.FEATURE_VERSION),
    )
    conn.execute("INSERT INTO security (id, symbol, name) VALUES (5, 'TEST', 'Test Security')")
    return conn


class ManageStrategyAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = _create_db(Path(self.temp_dir.name) / "trading.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _set(self, *, write: bool = True, **overrides: object) -> dict:
        values: dict[str, object] = {
            "security_id": 5,
            "strategy": "swing",
            "effective_from": "2026-09-22",
            "effective_to": None,
            "source": "manual",
            "rationale": "Active swing portfolio",
        }
        values.update(overrides)
        return manager.set_assignment(self.conn, write=write, **values)

    def test_list_empty_table(self) -> None:
        self.assertEqual(manager.list_assignments(self.conn), [])

    def test_valid_set_dry_run(self) -> None:
        result = self._set(write=False)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["would_insert"]["strategy_type"], "swing")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM strategy_assignment").fetchone()[0], 0)

    def test_valid_set_write(self) -> None:
        result = self._set()

        self.assertEqual(result["inserted"]["security_id"], 5)
        self.assertEqual(result["inserted"]["symbol"], "TEST")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM strategy_assignment").fetchone()[0], 1)

    def test_set_rejects_invalid_security_id(self) -> None:
        with self.assertRaisesRegex(manager.AssignmentValidationError, "security_id 999"):
            self._set(security_id=999)

    def test_set_rejects_invalid_strategy(self) -> None:
        with self.assertRaisesRegex(manager.AssignmentValidationError, "invalid strategy"):
            self._set(strategy="income")

    def test_set_rejects_invalid_dates(self) -> None:
        with self.assertRaises(manager.AssignmentValidationError):
            self._set(effective_from="22-09-2026")
        with self.assertRaises(manager.AssignmentValidationError):
            self._set(effective_to="2026-09-21")

    def test_set_rejects_overlapping_interval(self) -> None:
        self._set(effective_to="2026-10-31")

        with self.assertRaisesRegex(manager.AssignmentValidationError, "assignment overlaps existing"):
            self._set(effective_from="2026-10-01", effective_to="2026-11-01", write=False)

    def test_close_dry_run(self) -> None:
        self._set()

        result = manager.close_assignment(
            self.conn, security_id=5, effective_to="2026-12-31", write=False
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["would_close"]["effective_to"], "2026-12-31")
        self.assertIsNone(self.conn.execute("SELECT effective_to FROM strategy_assignment").fetchone()[0])

    def test_close_write(self) -> None:
        self._set()

        result = manager.close_assignment(
            self.conn, security_id=5, effective_to="2026-12-31", write=True
        )

        self.assertEqual(result["closed"]["effective_to"], "2026-12-31")
        self.assertEqual(
            self.conn.execute("SELECT effective_to FROM strategy_assignment").fetchone()[0],
            "2026-12-31",
        )

    def test_close_rejects_no_active_assignment(self) -> None:
        with self.assertRaisesRegex(manager.AssignmentValidationError, "no open assignment"):
            manager.close_assignment(
                self.conn, security_id=5, effective_to="2026-12-31", write=False
            )

    def test_validate_clean_db(self) -> None:
        self.assertEqual(manager.validate_assignments(self.conn), [])

    def test_validate_reports_broken_overlapping_fixture(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO strategy_assignment
                (security_id, strategy_type, effective_from, effective_to, source, rationale)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (5, "swing", "2026-09-01", None, "fixture", "first"),
                (5, "tactical", "2026-10-01", None, "fixture", "second"),
            ],
        )

        errors = manager.validate_assignments(self.conn)

        self.assertTrue(any("overlapping assignments" in error for error in errors))
        self.assertTrue(any("2 open assignments" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
