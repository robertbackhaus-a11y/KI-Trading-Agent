# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Console entry points of the VIPER-AI pip installation.

Each entry point runs one existing script of the package exactly like ``python <script> <args>`` did: as ``__main__``, with the script's own folder first on
``sys.path`` and the script path in ``sys.argv[0]``, so its arguments, output, exit code and defaults (database path, ``--db``) are unchanged.
"""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__

PACKAGE_DIR = Path(__file__).resolve().parent
APP_DIR = PACKAGE_DIR / "app"
MCP_DIR = PACKAGE_DIR / "mcp"

# console script -> (folder, script); the folder of every script stays its import root
COMMANDS: dict[str, tuple[Path, str]] = {
    "viper-ai-mcp": (MCP_DIR, "server.py"),
    "viper-ai-orchestrator": (APP_DIR, "Run-TradingOrchestrator.py"),
    "viper-ai-import-parqet": (APP_DIR, "Import-ParqetTransactions.py"),
    "viper-ai-import-transactions": (APP_DIR, "Import-TradingTransactions.py"),
    "viper-ai-refresh-etf": (APP_DIR, "Refresh-TradingEtfData.py"),
    "viper-ai-backfill-market-data": (APP_DIR, "Backfill-TradingMarketData.py"),
    "viper-ai-backfill-fx-rates": (APP_DIR, "Backfill-TradingFXRatesECB.py"),
    "viper-ai-backfill-events-news": (APP_DIR, "Backfill-TradingEventsNews.py"),
    "viper-ai-backfill-fundamentals-sec": (APP_DIR, "Backfill-TradingFundamentalsSEC.py"),
    "viper-ai-discover-candidates": (APP_DIR, "Discover-TradingCandidates.py"),
    "viper-ai-market-intelligence": (APP_DIR, "Collect-TradingMarketIntelligence.py"),
}


# Scripts without a command line of their own: they ignore every argument and start their job, so ``-h`` / ``--help`` is answered here and the
# script is not run (a call without help arguments is passed through unchanged).
NO_ARGPARSE: dict[str, str] = {
    "server.py": "MCP server over stdio (27 tools). Configuration only through the environment: TRADING_DB_PATH (required), TRADING_EXCLUDE, TRADING_USER_ID, TRADING_MAX_RESULT_CHARS.",
    "Run-TradingTask.py": "Run-TradingTask.py <log> <script> [args...]: runs one script without a console window, appends stdout/stderr to <log>, returns the script's exit code.",
    "Backfill-TradingFundamentalsIR.py": "Fundamentals from the company IR reference parsers; takes no arguments and writes on every run.",
    "Discover-TradingSECIdentifiers.py": "SEC CIK identifier discovery; takes no arguments and writes on every run.",
    "Resolve-TradingSecurities.py": "Security master data resolution (OpenFIGI); takes no arguments and writes on every run.",
}
HELP_FLAGS = ("-h", "--help")


def run_script(script: Path, args: Sequence[str]) -> int:
    """Run ``script`` as ``python script args`` would; returns its exit code (a script that ends without ``SystemExit`` gives 0)."""
    script = Path(script).resolve()
    if not script.is_file():
        print(f"viper-ai: script not found: {script}", file=sys.stderr)
        return 2
    own_args = args[:1] if script.name == "Run-TradingTask.py" else args  # the launcher passes the arguments after <log> <script> to the script
    if script.name in NO_ARGPARSE and any(arg in HELP_FLAGS for arg in own_args):
        print(f"usage: {script.name}", "", NO_ARGPARSE[script.name], "", "This script has no options of its own; nothing was started.", sep="\n")
        return 0
    sys.argv = [str(script), *args]
    sys.path.insert(0, str(script.parent))  # the script's folder first, like "python script.py"
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


def app_scripts() -> list[str]:
    """The runnable scripts of ``viper_ai.app`` (``Verb-Trading*.py``)."""
    return sorted(p.name for p in APP_DIR.glob("*-*.py"))


def _command(name: str) -> int:
    folder, script = COMMANDS[name]
    return run_script(folder / script, sys.argv[1:])


def mcp_server() -> int:
    return _command("viper-ai-mcp")


def orchestrator() -> int:
    return _command("viper-ai-orchestrator")


def import_parqet() -> int:
    return _command("viper-ai-import-parqet")


def import_transactions() -> int:
    return _command("viper-ai-import-transactions")


def refresh_etf() -> int:
    return _command("viper-ai-refresh-etf")


def backfill_market_data() -> int:
    return _command("viper-ai-backfill-market-data")


def backfill_fx_rates() -> int:
    return _command("viper-ai-backfill-fx-rates")


def backfill_events_news() -> int:
    return _command("viper-ai-backfill-events-news")


def backfill_fundamentals_sec() -> int:
    return _command("viper-ai-backfill-fundamentals-sec")


def discover_candidates() -> int:
    return _command("viper-ai-discover-candidates")


def market_intelligence() -> int:
    return _command("viper-ai-market-intelligence")


def task() -> int:
    """``Run-TradingTask.py <log> <script> [args]`` for the Task Scheduler (installed as a GUI script: no console window).  A bare script name is taken from ``viper_ai.app``."""
    args = sys.argv[1:]
    if len(args) >= 2 and not Path(args[1]).is_file() and (APP_DIR / args[1]).is_file():
        args = [args[0], str(APP_DIR / args[1]), *args[2:]]
    return run_script(APP_DIR / "Run-TradingTask.py", args)


def gui() -> int:
    """The desktop GUI (installed as a GUI script).  ``-h`` / ``--help`` only prints the usage, the window is not opened."""
    if any(arg in HELP_FLAGS for arg in sys.argv[1:]):
        print("usage: viper-ai-gui", "", "Starts the VIPER-AI desktop GUI (needs the extra 'gui'); settings are read from the GUI settings file.", sep="\n")
        return 0
    from .gui.app import main as gui_main

    return gui_main()


def main(argv: Optional[Sequence[str]] = None) -> int:
    commands = "\n".join(f"  {name:<36} {folder.name}/{script}" for name, (folder, script) in COMMANDS.items())
    parser = argparse.ArgumentParser(
        prog="viper-ai",
        description="VIPER-AI: local MCP-based trading agent (analysis and simulation only, no order execution).",
        epilog=f"console commands:\n{commands}\n  {'viper-ai-task':<36} app/Run-TradingTask.py (no console window)\n  {'viper-ai-gui':<36} desktop GUI (extra 'gui', no console window)\n\n"
               "Every command accepts -h / --help without starting a job. Runtime data (database, logs, scheduler, *_local.json) is never part of the package.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"VIPER-AI {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="list the console commands and the runnable scripts of the package")
    sub.add_parser("paths", help="show the package folders and the runtime folders the installation uses")
    run = sub.add_parser("run", help="run one script of the package with its own arguments, e.g. viper-ai run Audit-TradingDataQuality.py --help")
    run.add_argument("script", help="file name of the script in viper_ai.app (e.g. Manage-TradingCapitalState.py)")
    run.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the script unchanged")
    args = parser.parse_args(argv)
    if args.command == "list":
        print("Console commands:")
        for name, (folder, script) in COMMANDS.items():
            print(f"  {name:<36} {folder.name}/{script}")
        print(f"  {'viper-ai-task':<36} app/Run-TradingTask.py (no console window)")
        print(f"  {'viper-ai-gui':<36} desktop GUI (extra 'gui', no console window)")
        print("\nScripts for 'viper-ai run':")
        for name in app_scripts():
            print(f"  {name}")
        return 0
    if args.command == "paths":
        sys.path.insert(0, str(APP_DIR))
        import runtime_paths

        print(f"package:          {PACKAGE_DIR}")
        print(f"app scripts:      {APP_DIR}")
        print(f"mcp server:       {MCP_DIR / 'server.py'}")
        print(f"trading root:     {runtime_paths.trading_root()}  (environment variable {runtime_paths.TRADING_ROOT_ENV})")
        print(f"private universe: {runtime_paths.private_universe_dir()}")
        print(f"private data:     {runtime_paths.private_data_dir()}")
        return 0
    if args.command == "run":
        name = Path(args.script).name
        if name not in app_scripts():
            print(f"viper-ai: unknown script {args.script!r}; see 'viper-ai list'", file=sys.stderr)
            return 2
        return run_script(APP_DIR / name, args.args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
