"""Reset-TradingDb.py is destructive: it must never fall back to a default DB path."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[2] / "tools" / "trading"
sys.path.insert(0, str(TOOLS))

_spec = importlib.util.spec_from_file_location("reset_tradingdb", TOOLS / "Reset-TradingDb.py")
reset = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset)


def make_marker_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE old_marker(id INTEGER)")
        conn.commit()
    finally:
        conn.close()


def table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


class ResetTradingDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "trading.db"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_no_hardcoded_default_db_path_exists(self) -> None:
        self.assertFalse(hasattr(reset, "DB_PATH"))

    def test_missing_db_path_aborts_cleanly_without_prompt_or_changes(self) -> None:
        make_marker_db(self.db_path)
        stderr = io.StringIO()
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")) as prompt:
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                reset.main([])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--db-path is required", stderr.getvalue())
        prompt.assert_not_called()
        self.assertEqual(table_names(self.db_path), {"old_marker"})

    def test_explicit_db_path_with_reset_confirmation_recreates_database(self) -> None:
        make_marker_db(self.db_path)
        output = io.StringIO()
        with mock.patch("builtins.input", return_value="RESET") as prompt, contextlib.redirect_stdout(output):
            reset.main(["--db-path", str(self.db_path)])
        prompt.assert_called_once()
        self.assertIn(str(self.db_path), output.getvalue())
        tables = table_names(self.db_path)
        self.assertNotIn("old_marker", tables)
        self.assertTrue({"metadata", "security", "transactions"} <= tables)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_reset_confirmation_is_still_required(self) -> None:
        make_marker_db(self.db_path)
        output = io.StringIO()
        with mock.patch("builtins.input", return_value="yes") as prompt, contextlib.redirect_stdout(output):
            reset.main(["--db-path", str(self.db_path)])
        prompt.assert_called_once()
        self.assertIn("Cancelled.", output.getvalue())
        self.assertEqual(table_names(self.db_path), {"old_marker"})


if __name__ == "__main__":
    unittest.main()
