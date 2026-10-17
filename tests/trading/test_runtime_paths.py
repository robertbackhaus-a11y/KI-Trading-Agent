# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""One rule for the runtime paths of every layout (deployed copy, repository, pip installation).

Before v0.6.0 ``VIPER_TRADING_ROOT`` moved only the private universe files of a pip installation; every script kept its own literal
``C:\\tools\\trading\\data\\trading.db`` and the report folders, so a custom root still read and wrote the production database.  Now every
default path is derived from the root, an explicit ``--db`` / ``--db-path`` / ``TRADING_DB_PATH`` still wins, and a relative root is refused.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "tools" / "trading"
sys.path.insert(0, str(APP))

import runtime_paths  # noqa: E402

LITERAL = re.compile(r"[A-Za-z]:\\\\?tools\\\\?trading\\\\?data")
CUSTOM = r"D:\viper-custom-root"


def load_script(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, APP / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RootTests(unittest.TestCase):
    def test_default_root_and_derived_paths(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIPER_TRADING_ROOT", None)
            self.assertEqual(runtime_paths.trading_root(), Path(r"C:\tools\trading"))
            self.assertEqual(runtime_paths.default_db_path(), Path(r"C:\tools\trading\data\trading.db"))  # unchanged for the deployed copy
            self.assertEqual(runtime_paths.data_dir(), Path(r"C:\tools\trading\data"))
            self.assertEqual(runtime_paths.logs_dir(), Path(r"C:\tools\trading\logs"))

    def test_a_custom_root_moves_every_default(self) -> None:
        with mock.patch.dict(os.environ, {"VIPER_TRADING_ROOT": CUSTOM}):
            self.assertEqual(runtime_paths.default_db_path(), Path(CUSTOM) / "data" / "trading.db")
            self.assertEqual(runtime_paths.logs_dir(), Path(CUSTOM) / "logs")

    def test_a_relative_root_is_refused_instead_of_depending_on_the_working_directory(self) -> None:
        for value in ("trading", r".\trading", "data/viper"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"VIPER_TRADING_ROOT": value}), self.assertRaisesRegex(ValueError, "absolute"):
                runtime_paths.default_db_path()


class ScriptDefaultTests(unittest.TestCase):
    def test_no_script_keeps_a_literal_production_data_path(self) -> None:
        offenders = []
        for path in sorted(APP.glob("*.py")):
            if path.name == "runtime_paths.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and LITERAL.search(node.value):
                    if path.name == "trading_analytics.py" and node.value.endswith("trading.db"):
                        continue  # DB_PATH_HINT: a text for error messages, never opened
                    offenders.append(f"{path.name}: {node.value}")
        self.assertEqual(offenders, [])

    def test_the_scripts_follow_the_root(self) -> None:
        cases = {
            "Run-TradingOrchestrator.py": ("DB_PATH",),
            "Backfill-TradingMarketData.py": ("DB_PATH",),
            "Backfill-TradingFundamentalsSEC.py": ("DB_PATH", "SEC_CACHE_DIR"),
            "Discover-TradingCandidates.py": ("DB_PATH", "DEFAULT_REPORT_DIR"),
            "Collect-TradingMarketIntelligence.py": ("DB_PATH", "DEFAULT_REPORT_DIR", "DEFAULT_DISCOVERY_DIR"),
            "Manage-TradingCapitalState.py": ("DB_PATH",),
        }
        with mock.patch.dict(os.environ, {"VIPER_TRADING_ROOT": CUSTOM}):
            for filename, names in cases.items():
                module = load_script(filename, "rp_" + filename.replace("-", "_").removesuffix(".py"))
                for name in names:
                    with self.subTest(script=filename, name=name):
                        value = Path(getattr(module, name))
                        self.assertEqual(value.parents[len(value.relative_to(CUSTOM).parts) - 1], Path(CUSTOM), value)
        import transaction_import

        self.assertEqual(transaction_import.DEFAULT_DB_PATH.name, "trading.db")

    def test_an_explicit_db_argument_wins_over_the_root(self) -> None:
        with mock.patch.dict(os.environ, {"VIPER_TRADING_ROOT": CUSTOM}):
            module = load_script("Manage-TradingCapitalState.py", "rp_capital_explicit")
        explicit = Path("E:/elsewhere/trading.db")
        with mock.patch.object(sys, "argv", ["Manage-TradingCapitalState.py", "--db-path", str(explicit), "show"]):
            self.assertEqual(module._parse_args().db_path, explicit)
        with mock.patch.object(sys, "argv", ["Manage-TradingCapitalState.py", "show"]):
            self.assertEqual(module._parse_args().db_path, Path(CUSTOM) / "data" / "trading.db")
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                module._connect(missing, write=True)
            self.assertFalse(missing.exists())  # a missing database is never created

    def test_a_missing_database_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"VIPER_TRADING_ROOT": tmp}):
            module = load_script("Discover-TradingSECIdentifiers.py", "rp_sec_identifiers")
            with self.assertRaises(FileNotFoundError):
                module.connect()
            self.assertFalse((Path(tmp) / "data" / "trading.db").exists())


class McpRuntimeFolderTests(unittest.TestCase):
    def test_a_repository_checkout_uses_its_own_helpers_not_the_deployed_copy(self) -> None:
        spec = importlib.util.spec_from_file_location("trading_sqlite_runtime_dir", ROOT / "mcp-tools" / "trading_sqlite.py")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except ImportError:
            self.skipTest("pydantic is only in the Trading venv")
        self.assertEqual(module.Tools._runtime_tools_directory(), APP)


if __name__ == "__main__":
    unittest.main()
