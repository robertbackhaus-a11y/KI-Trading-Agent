# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""GUI settings: defaults, broken files, validation, atomic save, rotating log (A, B)."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from gui.services import settings as sm


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # A
    def test_a_missing_file_gives_the_defaults_without_a_warning(self) -> None:
        loaded = sm.load_settings(self.path)
        self.assertEqual(loaded.warnings, [])
        s = loaded.settings
        self.assertEqual(s.mcp_python_path, r"C:\tools\trading\.venv\Scripts\python.exe")
        self.assertEqual(s.mcp_server_path, r"C:\tools\trading\mcp\server.py")
        self.assertEqual(s.trading_db_path, r"C:\tools\trading\data\trading.db")
        self.assertEqual(s.ollama_base_url, "http://127.0.0.1:11434")
        self.assertIsInstance(s.default_detail_mode, bool)
        self.assertIsInstance(s.think_enabled, bool)
        self.assertEqual(sm.GUI_VERSION, "dev")  # no product version is invented

    def test_defaults_are_absolute_paths(self) -> None:
        s = sm.Settings()
        for name in sm.PATH_FIELDS:
            self.assertIsNone(sm.validate_edit(name, getattr(s, name)), name)

    # B
    def test_b_invalid_json_falls_back_to_the_defaults_with_a_warning(self) -> None:
        for content in ("{ not json", "[]", '"text"', ""):
            self.path.write_text(content, encoding="utf-8")
            loaded = sm.load_settings(self.path)
            self.assertEqual(loaded.settings, sm.Settings(), content)
            self.assertEqual(len(loaded.warnings), 1, content)
            self.assertEqual(self.path.read_text(encoding="utf-8"), content)  # loading never rewrites the file

    def test_invalid_values_fall_back_individually(self) -> None:
        self.path.write_text(json.dumps({
            "ollama_model": "other:latest", "trading_db_path": "relative\\trading.db", "ollama_base_url": "ftp://x", "refresh_interval_s": 5,
            "think_enabled": "yes", "default_detail_mode": False, "unknown_key": 1, "window_geometry": 7,
        }), encoding="utf-8")
        loaded = sm.load_settings(self.path)
        s = loaded.settings
        self.assertEqual(s.ollama_model, "other:latest")  # valid values are kept
        self.assertFalse(s.default_detail_mode)
        self.assertEqual(s.trading_db_path, sm.Settings().trading_db_path)  # a relative path is refused
        self.assertEqual(s.ollama_base_url, sm.Settings().ollama_base_url)
        self.assertEqual(s.refresh_interval_s, 120)
        self.assertFalse(s.think_enabled)
        self.assertEqual(s.window_geometry, "")
        self.assertEqual(len(loaded.warnings), 5)

    def test_save_roundtrip_is_atomic_and_contains_no_secret_fields(self) -> None:
        s = sm.Settings(ollama_model="m:1", refresh_interval_s=300)
        sm.save_settings(s, self.path)
        self.assertEqual(sm.load_settings(self.path).settings, s)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["settings.json"])  # no temporary file left
        text = self.path.read_text(encoding="utf-8").lower()
        for word in ("password", "token", "secret", "api_key"):
            self.assertNotIn(word, text)

    def test_app_files_live_under_localappdata(self) -> None:
        for path in (sm.settings_path(), sm.gui_log_path(), sm.mcp_stderr_path()):
            self.assertEqual(path.parent.name, "TradingAgentGui")
        self.assertEqual((sm.settings_path().name, sm.gui_log_path().name, sm.mcp_stderr_path().name), ("settings.json", "gui.log", "mcp-stderr.log"))

    def test_logging_is_rotating_idempotent_and_writes_a_file(self) -> None:
        path = Path(self.tmp.name) / "gui.log"
        logger = sm.configure_logging(path)
        sm.configure_logging(path)
        handlers = [h for h in logger.handlers if getattr(h, "baseFilename", None) == str(path)]
        try:
            self.assertEqual(len(handlers), 1)
            self.assertEqual((handlers[0].maxBytes, handlers[0].backupCount), (1_000_000, 3))
            logger.info("hello")
            handlers[0].flush()
            self.assertIn("hello", path.read_text(encoding="utf-8"))
        finally:
            for h in handlers:
                logger.removeHandler(h)
                h.close()


if __name__ == "__main__":
    unittest.main()
