# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""GUI settings: defaults, broken files, validation, atomic save, rotating log (A, B)."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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



class InstallationDefaultTests(unittest.TestCase):
    """The defaults follow the installation and VIPER_TRADING_ROOT; a saved value always wins; mismatches are reported, never silently changed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def env(self, **values):
        patcher = mock.patch.dict(os.environ, {k: v for k, v in values.items() if v is not None})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)

    def fake_package(self) -> Path:
        """``<tmp>/viper_ai`` with ``__init__.py``, ``mcp/server.py`` and ``gui/services`` like a pip installation."""
        package = Path(self.tmp.name) / "viper_ai"
        (package / "mcp").mkdir(parents=True)
        (package / "gui" / "services").mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "mcp" / "server.py").write_text("", encoding="utf-8")
        return package

    def test_the_deployed_defaults_follow_the_trading_root(self) -> None:
        self.env(VIPER_TRADING_ROOT=r"D:\viper")
        s = sm.Settings()
        self.assertEqual((s.mcp_python_path, s.mcp_server_path), (r"D:\viper\.venv\Scripts\python.exe", r"D:\viper\mcp\server.py"))
        self.assertEqual((s.trading_db_path, s.trading_logs_dir), (r"D:\viper\data\trading.db", r"D:\viper\logs"))

    def test_a_relative_root_is_ignored_and_reported(self) -> None:
        self.env(VIPER_TRADING_ROOT="relative")
        self.assertEqual(sm.Settings().trading_db_path, r"C:\tools\trading\data\trading.db")
        self.assertTrue(any("kein absoluter Pfad" in w for w in sm.runtime_warnings(sm.Settings())))

    def test_the_pip_installation_uses_its_own_server_and_python(self) -> None:
        self.env(VIPER_TRADING_ROOT=None)
        package = self.fake_package()
        with mock.patch.object(sm, "INSTALL_DIR", package):
            self.assertTrue(sm.is_installed_package())
            s = sm.Settings()
            self.assertEqual(Path(s.mcp_server_path), package / "mcp" / "server.py")
            self.assertEqual(Path(s.mcp_python_path).parent, Path(sys.executable).parent)
            self.assertEqual(s.trading_db_path, r"C:\tools\trading\data\trading.db")  # the data stay at the runtime root

    def test_saved_values_win_and_a_foreign_server_is_reported(self) -> None:
        self.env(VIPER_TRADING_ROOT=None)
        package = self.fake_package()
        db = Path(self.tmp.name) / "trading.db"
        db.write_bytes(b"")
        path = Path(self.tmp.name) / "settings.json"
        path.write_text(json.dumps({"mcp_server_path": r"C:\tools\trading\mcp\server.py", "trading_db_path": str(db)}), encoding="utf-8")
        with mock.patch.object(sm, "INSTALL_DIR", package):
            loaded = sm.load_settings(path)
            self.assertEqual(loaded.settings.mcp_server_path, r"C:\tools\trading\mcp\server.py")  # the manual setting is kept
            self.assertEqual(loaded.warnings, [])
            warnings = sm.runtime_warnings(loaded.settings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("gehört nicht zu dieser Installation", warnings[0])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["mcp_server_path"], r"C:\tools\trading\mcp\server.py")  # nothing rewritten

    def test_a_missing_database_is_reported(self) -> None:
        s = sm.Settings(trading_db_path=str(Path(self.tmp.name) / "missing.db"))
        self.assertTrue(any(w.startswith("Datenbank nicht gefunden") for w in sm.runtime_warnings(s)))
        self.assertFalse((Path(self.tmp.name) / "missing.db").exists())


if __name__ == "__main__":
    unittest.main()
