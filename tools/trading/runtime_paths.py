# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Where the runtime data of VIPER-AI lives, independent of where the code was installed.

Two layouts run the same code:

* the deployed copy (``C:\\tools\\trading\\app``, written by ``deploy\\Deploy-TradingAgent.ps1``) and the repository checkout (``tools\\trading``): the private files sit in the
  ``universe`` folder next to the code, exactly as before;
* the pip installation (``site-packages\\viper_ai\\app``): the code folder belongs to pip and is replaced on every upgrade, so nothing private may live there.  The private files are
  then read from and written to the runtime root instead: ``<trading root>\\app\\universe`` (the same files the deployed copy uses) and ``<trading root>\\app\\data``.

The trading root is ``C:\\tools\\trading`` unless the environment variable ``VIPER_TRADING_ROOT`` names another folder (it must be absolute: a relative root would depend on the
working directory of whoever starts a script, for example the Task Scheduler).  Every default runtime path is derived from it: the database ``<root>\\data\\trading.db``
(:func:`default_db_path`), the data folder with reports and caches (:func:`data_dir`) and the logs (:func:`logs_dir`).  An explicit ``--db`` / ``--db-path`` of a script,
``TRADING_DB_PATH`` of the MCP server and a path saved in the GUI settings always win over the default.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

TRADING_ROOT_ENV = "VIPER_TRADING_ROOT"
DEFAULT_TRADING_ROOT = Path(r"C:\tools\trading")
CODE_DIR = Path(__file__).resolve().parent
PACKAGED_UNIVERSE = CODE_DIR / "universe"
PACKAGE_NAME = "viper_ai"


def trading_root() -> Path:
    """The runtime root (database, logs, scheduler, private files)."""
    configured = os.environ.get(TRADING_ROOT_ENV, "").strip()
    if not configured:
        return DEFAULT_TRADING_ROOT
    root = Path(configured)
    if not root.is_absolute():
        raise ValueError(f"{TRADING_ROOT_ENV} must be an absolute path, got {configured!r}")
    return root


def data_dir() -> Path:
    """``<root>\\data``: the database, the reports (candidate discovery, market intelligence), caches and audits."""
    return trading_root() / "data"


def default_db_path() -> Path:
    """``<root>\\data\\trading.db``: the database every script uses unless it is given ``--db`` / ``--db-path``."""
    return data_dir() / "trading.db"


def logs_dir() -> Path:
    """``<root>\\logs``: the logs of the scheduled tasks."""
    return trading_root() / "logs"


def is_installed_package(code_dir: Path = CODE_DIR) -> bool:
    """True when this code runs from the pip installation (``viper_ai/app``), not from the deployed copy or the repository."""
    return code_dir.name == "app" and code_dir.parent.name == PACKAGE_NAME and (code_dir.parent / "__init__.py").is_file()


def private_universe_dir() -> Path:
    """The folder of the private universe files (``sector_map_local.json``, ``etf_lookthrough_local.json``, ``etf_holdings_local.json``)."""
    return trading_root() / "app" / "universe" if is_installed_package() else PACKAGED_UNIVERSE


def private_data_dir() -> Path:
    """The folder of private hand-maintained data next to the code (``bae_systems_snapshot.json``)."""
    return trading_root() / "app" / "data" if is_installed_package() else CODE_DIR / "data"


def private_file(directory: Union[str, Path], name: str) -> Path:
    """``directory / name``; only when ``directory`` is the packaged universe folder of a pip installation, the private file of the runtime root instead."""
    folder = Path(directory)
    if is_installed_package() and folder.resolve() == PACKAGED_UNIVERSE:
        return private_universe_dir() / name
    return folder / name
