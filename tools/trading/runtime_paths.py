# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Where the runtime data of VIPER-AI lives, independent of where the code was installed.

Two layouts run the same code:

* the deployed copy (``C:\\tools\\trading\\app``, written by ``deploy\\Deploy-TradingAgent.ps1``) and the repository checkout (``tools\\trading``): the private files sit in the
  ``universe`` folder next to the code, exactly as before;
* the pip installation (``site-packages\\viper_ai\\app``): the code folder belongs to pip and is replaced on every upgrade, so nothing private may live there.  The private files are
  then read from and written to the runtime root instead: ``<trading root>\\app\\universe`` (the same files the deployed copy uses) and ``<trading root>\\app\\data``.

The trading root is ``C:\\tools\\trading`` unless the environment variable ``VIPER_TRADING_ROOT`` names another folder.  The database paths are not derived here: every script keeps its
own absolute default and its ``--db`` argument.
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
    return Path(configured) if configured else DEFAULT_TRADING_ROOT


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
