# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The pip installation end to end: build, inspect, install into a fresh virtual environment, run, upgrade.

Static checks of the package definition always run.  The end-to-end class builds the wheel and the sdist, creates fresh virtual environments and
installs the dependencies from the package index, so it runs only on request:

    set VIPER_INSTALL_TESTS=1
    python -m unittest tests.trading.test_packaging_install

Everything happens in temporary folders with a synthetic database; the production root is never read or written (``VIPER_TRADING_ROOT`` always points
to a temporary folder).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_E2E = os.environ.get("VIPER_INSTALL_TESTS") == "1"
PRIVATE = re.compile(r"(_local\.json|\.db|\.sqlite3?|\.log|\.bak|\.csv|latest\.json)$|(^|/)(data|logs|backup)/")


def _version() -> str:
    text = (ROOT / "viper_ai" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


class PackageDefinitionTests(unittest.TestCase):
    def test_the_mcp_policy_and_the_runtime_paths_are_packaged_and_private_files_are_not(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"viper_ai.mcp" = "mcp-tools"', text)  # mcp-tools/tool_policy.py is a module of viper_ai.mcp
        self.assertIn('"viper_ai.app" = ["universe/*_v1.json"]', text)
        self.assertIn("include-package-data = false", text)
        self.assertTrue((ROOT / "mcp-tools" / "tool_policy.py").is_file())

    def test_every_console_script_points_to_an_existing_function(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        targets = re.findall(r'^[a-z-]+ = "viper_ai\.cli:([a-z_]+)"', text, flags=re.M)
        self.assertGreaterEqual(len(targets), 14)
        source = (ROOT / "viper_ai" / "cli.py").read_text(encoding="utf-8")
        for name in targets:
            self.assertRegex(source, rf"(?m)^def {name}\(", name)


def _run(args, *, env=None, cwd=None, timeout=900, check=True) -> subprocess.CompletedProcess:
    cwd = cwd or tempfile.gettempdir()  # never the repository: "python -c" would import its viper_ai folder instead of the installed package
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(map(str, args))} -> {result.returncode}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    return result


def _digest(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


@unittest.skipUnless(RUN_E2E, "end-to-end install test: set VIPER_INSTALL_TESTS=1 (builds, creates virtual environments, needs the package index)")
class InstallEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="viper-install-")
        base = Path(cls.tmp.name)
        cls.dist = base / "dist"
        _run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", cls.dist, ROOT], cwd=base)
        cls.wheel = next(cls.dist.glob("*.whl"))
        cls.sdist = next(cls.dist.glob("*.tar.gz"))
        cls.venv = base / "venv"
        _run([sys.executable, "-m", "venv", cls.venv])
        cls.py = cls.venv / "Scripts" / "python.exe" if os.name == "nt" else cls.venv / "bin" / "python"
        cls.scripts = cls.py.parent
        _run([cls.py, "-m", "pip", "install", "--disable-pip-version-check", "-q", f"{cls.wheel}[gui]"])
        # a runtime root with a synthetic database and private files (what pip must never touch)
        cls.root = base / "runtime root"
        (cls.root / "data").mkdir(parents=True)
        (cls.root / "app" / "universe").mkdir(parents=True)
        spec = importlib.util.spec_from_file_location("reset_for_install_test", ROOT / "tools" / "trading" / "Reset-TradingDb.py")
        reset = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reset)
        with contextlib.redirect_stdout(io.StringIO()):
            reset.create_database(cls.root / "data" / "trading.db")
        (cls.root / "app" / "universe" / "sector_map_local.json").write_text('{"private": true}\n', encoding="utf-8")
        (cls.root / "app" / "universe" / "etf_holdings_local.json").write_text('{"private": true}\n', encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def env(self, **extra) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in ("TRADING_DB_PATH", "TRADING_TOOL_PROFILE", "TRADING_EXCLUDE", "PYTHONPATH")}
        env.update(VIPER_TRADING_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen")
        env.update(extra)
        return env

    def exe(self, name: str) -> Path:
        return self.scripts / (name + (".exe" if os.name == "nt" else ""))

    # build artefacts -----------------------------------------------------------------------------------------------------------------
    def test_wheel_and_sdist_contain_the_code_and_no_private_runtime_data(self) -> None:
        names = zipfile.ZipFile(self.wheel).namelist()
        self.assertIn("viper_ai/mcp/tool_policy.py", names)
        self.assertIn("viper_ai/app/runtime_paths.py", names)
        self.assertIn("viper_ai/gui/services/settings.py", names)
        self.assertEqual([n for n in names if PRIVATE.search(n)], [])
        self.assertFalse([n for n in names if n.startswith("tests/") or "/tests/" in n])
        self.assertIn(f"-{_version()}-", self.wheel.name)
        with tarfile.open(self.sdist) as archive:
            members = [m.name for m in archive.getmembers()]
        self.assertEqual([n for n in members if PRIVATE.search(n.split("/", 1)[-1])], [])

    # installed commands ---------------------------------------------------------------------------------------------------------------
    def test_version_help_and_every_entry_point(self) -> None:
        out = _run([self.exe("viper-ai"), "--version"], env=self.env()).stdout
        self.assertEqual(out.strip(), f"VIPER-AI {_version()}")
        self.assertIn("console commands", _run([self.exe("viper-ai"), "--help"], env=self.env()).stdout)
        commands = ["viper-ai-mcp", "viper-ai-orchestrator", "viper-ai-import-parqet", "viper-ai-import-transactions", "viper-ai-refresh-etf",
                    "viper-ai-backfill-market-data", "viper-ai-backfill-fx-rates", "viper-ai-backfill-events-news", "viper-ai-backfill-fundamentals-sec",
                    "viper-ai-discover-candidates", "viper-ai-market-intelligence"]
        for name in commands:
            with self.subTest(command=name):
                result = _run([self.exe(name), "--help"], env=self.env(), timeout=120, check=False)
                self.assertEqual(result.returncode, 0, result.stderr[-500:])
                self.assertIn("usage", (result.stdout + result.stderr).lower())
        self.assertEqual(_run([self.exe("viper-ai-gui"), "--help"], env=self.env(), check=False).returncode, 0)

    def test_paths_follow_the_runtime_root(self) -> None:
        out = _run([self.exe("viper-ai"), "paths"], env=self.env()).stdout
        self.assertIn(str(self.root / "data" / "trading.db") + "  (exists", out)
        self.assertIn(f"private universe: {self.root / 'app' / 'universe'}", out)
        bad = _run([self.exe("viper-ai"), "paths"], env=self.env(VIPER_TRADING_ROOT="relative"), check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("absolute", bad.stderr)

    def test_the_orchestrator_reads_the_database_of_the_root_and_an_explicit_path_wins(self) -> None:
        result = _run([self.exe("viper-ai-orchestrator"), "--json"], env=self.env(), check=False)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertIsInstance(json.loads(result.stdout), dict)
        missing = Path(self.tmp.name) / "nowhere" / "trading.db"
        explicit = _run([self.exe("viper-ai-orchestrator"), "--db-path", missing, "--json"], env=self.env(), check=False)
        self.assertNotEqual(explicit.returncode, 0)  # the explicit (missing) database is used, not the root's, and not created
        self.assertFalse(missing.exists())

    # MCP server ------------------------------------------------------------------------------------------------------------------------
    async def _mcp(self, env: dict[str, str]) -> dict:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=str(self.exe("viper-ai-mcp")), args=[], env=env)
        with open(os.devnull, "w") as quiet:
            async with stdio_client(params, errlog=quiet) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    status = await session.call_tool("database_status", {})
                    return {"tools": tools, "status": json.loads("".join(getattr(p, "text", "") for p in status.content))}

    def test_the_installed_mcp_server_uses_the_root_database_and_the_read_profile(self) -> None:
        out = asyncio.run(asyncio.wait_for(self._mcp(self.env()), 120))
        self.assertEqual(len(out["tools"]), 17)
        self.assertNotIn("sql_execute", out["tools"])
        self.assertEqual(Path(out["status"]["database_path"]), self.root / "data" / "trading.db")
        other = Path(self.tmp.name) / "explicit.db"
        shutil.copyfile(self.root / "data" / "trading.db", other)
        explicit = asyncio.run(asyncio.wait_for(self._mcp(self.env(TRADING_DB_PATH=str(other), TRADING_TOOL_PROFILE="gui")), 120))
        self.assertEqual(Path(explicit["status"]["database_path"]), other)
        self.assertEqual(len(explicit["tools"]), 25)

    def test_the_mcp_server_refuses_a_missing_database(self) -> None:
        empty = Path(self.tmp.name) / "empty root"
        empty.mkdir(exist_ok=True)
        result = _run([self.exe("viper-ai-mcp")], env=self.env(VIPER_TRADING_ROOT=str(empty)), check=False, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("database not found", result.stderr)
        self.assertFalse((empty / "data" / "trading.db").exists())

    # GUI -----------------------------------------------------------------------------------------------------------------------------
    def test_the_installed_gui_starts_with_its_own_server_and_the_root_database(self) -> None:
        code = ("import json; from viper_ai.gui.services import settings as s; import viper_ai.gui.app as app; "
                "x = s.Settings(); print(json.dumps({'server': x.mcp_server_path, 'python': x.mcp_python_path, 'db': x.trading_db_path, "
                "'version': s.GUI_VERSION, 'installed': s.is_installed_package(), 'warnings': s.runtime_warnings(x)}))")
        info = json.loads(_run([self.py, "-c", code], env=self.env()).stdout)
        site = Path(_run([self.py, "-c", "import viper_ai, pathlib; print(pathlib.Path(viper_ai.__file__).parent)"], env=self.env()).stdout.strip())
        self.assertTrue(info["installed"])
        self.assertEqual(Path(info["server"]), site / "mcp" / "server.py")
        self.assertEqual(Path(info["python"]).parent, self.py.parent)
        self.assertEqual(Path(info["db"]), self.root / "data" / "trading.db")
        self.assertEqual(info["version"], _version())
        self.assertEqual(info["warnings"], [])
        window = "\n".join([  # the real window and its MCP child for 10 s, then a normal close
            "import sys",
            "from PySide6.QtCore import QTimer",
            "from PySide6.QtWidgets import QApplication",
            "import viper_ai.gui.app as app",
            "show = app.MainWindow.show",
            "def shown(self):",
            "    show(self)",
            "    QTimer.singleShot(10000, QApplication.instance().quit)",
            "app.MainWindow.show = shown",
            "sys.exit(app.main(['viper-ai-gui']))",
        ])
        env = self.env(LOCALAPPDATA=str(Path(self.tmp.name) / "localappdata"))
        result = _run([self.py, "-c", window], env=env, check=False, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        log = (Path(self.tmp.name) / "localappdata" / "TradingAgentGui" / "gui.log").read_text(encoding="utf-8")
        self.assertIn("GUI-Start", log)
        self.assertIn("MCP CONNECTED", log)

    # upgrade -------------------------------------------------------------------------------------------------------------------------
    def test_an_upgrade_and_an_uninstall_leave_the_runtime_root_untouched(self) -> None:
        before = _digest(self.root)
        source = Path(self.tmp.name) / "next-source"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__"))
        init = source / "viper_ai" / "__init__.py"
        upgraded = re.sub(r"\.dev\d+$", "", _version()) + ".post1"  # later than the release and than its pre-release
        init.write_text(init.read_text(encoding="utf-8").replace(f'__version__ = "{_version()}"', f'__version__ = "{upgraded}"'), encoding="utf-8")
        next_dist = Path(self.tmp.name) / "next-dist"
        _run([sys.executable, "-m", "build", "--wheel", "--outdir", next_dist, source], cwd=self.tmp.name)
        wheel = next(next_dist.glob("*.whl"))
        _run([self.py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", wheel])
        self.assertEqual(_run([self.exe("viper-ai"), "--version"], env=self.env()).stdout.strip(), f"VIPER-AI {upgraded}")
        self.assertEqual(_digest(self.root), before)  # database and private files unchanged
        site = Path(_run([self.py, "-c", "import viper_ai, pathlib; print(pathlib.Path(viper_ai.__file__).parent)"], env=self.env()).stdout.strip())
        self.assertEqual(sorted(p.name for p in (site / "app" / "universe").iterdir()), sorted(p.name for p in (ROOT / "tools" / "trading" / "universe").glob("*_v1.json")))
        _run([self.py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--force-reinstall", "--no-deps", self.wheel])  # back to the release build
        self.assertEqual(_digest(self.root), before)


if __name__ == "__main__":
    unittest.main()
