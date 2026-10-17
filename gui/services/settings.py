# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Local GUI settings (JSON, no secrets) and the rotating GUI log.

Everything lives under ``%LOCALAPPDATA%\\TradingAgentGui``.  A missing settings file means defaults; a broken file or an invalid
value falls back to the default for that value and is reported as a warning (the file itself is never rewritten by loading).
All paths must be absolute: a relative database path breaks the read-only tools of the MCP server.

The defaults follow the installation the GUI runs from: the deployed copy and the repository use the classic runtime (``<root>\\.venv`` and ``<root>\\mcp\\server.py``),
the pip installation uses its own Python and its own ``viper_ai\\mcp\\server.py``; database and logs are the ``data`` and ``logs`` folders of the root from
``VIPER_TRADING_ROOT`` (default ``C:\\tools\\trading``), the same rule as ``tools/trading/runtime_paths.py``.  A value saved in ``settings.json`` always wins;
:func:`runtime_warnings` only reports a saved path that does not fit (missing database, a server of another installation).
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

APP_DIR_NAME = "TradingAgentGui"
LOGGER_NAME = "tradinggui"
PATH_FIELDS = ("mcp_python_path", "mcp_server_path", "trading_db_path", "trading_logs_dir")
MIN_REFRESH_S, MAX_REFRESH_S = 30, 3600
TRADING_ROOT_ENV = "VIPER_TRADING_ROOT"
DEFAULT_TRADING_ROOT = r"C:\tools\trading"
INSTALL_DIR = Path(__file__).resolve().parents[2]  # gui/services -> gui -> the repository, the trading root or the viper_ai package


def is_installed_package() -> bool:
    """True when the GUI runs from the pip installation (``site-packages\\viper_ai\\gui``), which brings its own MCP server."""
    return INSTALL_DIR.name == "viper_ai" and (INSTALL_DIR / "__init__.py").is_file() and (INSTALL_DIR / "mcp" / "server.py").is_file()


def _gui_version() -> str:
    if is_installed_package():
        try:
            from viper_ai import __version__  # the version of the installed distribution

            return str(__version__)
        except ImportError:
            pass
    return "dev"  # the deployed copy and the repository carry no version of their own


GUI_VERSION = _gui_version()


def trading_root() -> str:
    """``VIPER_TRADING_ROOT`` when it is an absolute path, else ``C:\\tools\\trading`` (a relative root is ignored and reported by :func:`runtime_warnings`)."""
    configured = os.environ.get(TRADING_ROOT_ENV, "").strip()
    return configured if configured and ntpath.isabs(configured) else DEFAULT_TRADING_ROOT


def installation_paths() -> dict[str, str]:
    """The default paths of the installation this GUI runs from (see the module docstring)."""
    root = trading_root()
    if is_installed_package():
        exe = Path(sys.executable)
        console = exe.with_name("python.exe")  # the GUI itself may run under pythonw.exe
        python, server = str(console if console.is_file() else exe), str(INSTALL_DIR / "mcp" / "server.py")
    else:
        python, server = ntpath.join(root, ".venv", "Scripts", "python.exe"), ntpath.join(root, "mcp", "server.py")
    return {"mcp_python_path": python, "mcp_server_path": server, "trading_db_path": ntpath.join(root, r"data\trading.db"), "trading_logs_dir": ntpath.join(root, "logs")}


def _default(name: str):
    return field(default_factory=lambda: installation_paths()[name])


def app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_DIR_NAME


def settings_path() -> Path:
    return app_dir() / "settings.json"


def gui_log_path() -> Path:
    return app_dir() / "gui.log"


def mcp_stderr_path() -> Path:
    return app_dir() / "mcp-stderr.log"


@dataclass
class Settings:
    ollama_base_url: str = "http://127.0.0.1:11434"  # future chat; only the reachability is shown in Phase 1
    ollama_model: str = "qwen38-uncensored:latest"
    mcp_python_path: str = _default("mcp_python_path")
    mcp_server_path: str = _default("mcp_server_path")
    trading_db_path: str = _default("trading_db_path")
    trading_logs_dir: str = _default("trading_logs_dir")
    default_detail_mode: bool = True  # reserved for later pages; Dashboard and Planner always need the detailed orchestrator result
    think_enabled: bool = False  # reserved for the chat
    refresh_interval_s: int = 120
    window_geometry: str = ""


@dataclass
class LoadedSettings:
    settings: Settings
    warnings: list[str]
    path: Path


def _valid(name: str, value: Any, default: Any) -> Optional[str]:
    """``None`` when ``value`` is acceptable for the field ``name``, else a short reason."""
    if isinstance(default, bool):
        return None if isinstance(value, bool) else "erwartet true/false"
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return "erwartet eine ganze Zahl"
        if name == "refresh_interval_s" and not MIN_REFRESH_S <= value <= MAX_REFRESH_S:
            return f"erwartet {MIN_REFRESH_S}..{MAX_REFRESH_S}"
        return None
    if not isinstance(value, str):
        return "erwartet Text"
    if name in PATH_FIELDS and not ntpath.isabs(value):
        return "erwartet einen absoluten Pfad"
    if name == "ollama_base_url" and not value.lower().startswith(("http://", "https://")):
        return "erwartet eine http(s)-URL"
    if name in ("ollama_model",) and not value.strip():
        return "darf nicht leer sein"
    return None


def load_settings(path: Optional[Path] = None) -> LoadedSettings:
    path = path or settings_path()
    defaults = Settings()
    warnings: list[str] = []
    if not path.exists():
        return LoadedSettings(defaults, warnings, path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("Wurzel ist kein Objekt")
    except (OSError, ValueError) as exc:
        return LoadedSettings(defaults, [f"settings.json ist unlesbar ({type(exc).__name__}); Standardwerte werden verwendet"], path)
    values = asdict(defaults)
    for f in fields(Settings):
        if f.name not in raw:
            continue
        problem = _valid(f.name, raw[f.name], getattr(defaults, f.name))
        if problem:
            warnings.append(f"Einstellung {f.name} ungültig ({problem}); Standardwert wird verwendet")
        else:
            values[f.name] = raw[f.name]
    return LoadedSettings(Settings(**values), warnings, path)


def runtime_warnings(settings: Settings) -> list[str]:
    """Hints about the paths in use (never blocking, nothing is changed): a missing database, a relative ``VIPER_TRADING_ROOT``, and in the pip installation a saved
    MCP server of another installation (for example the deployed copy), which would answer with other code and possibly another database."""
    out: list[str] = []
    configured = os.environ.get(TRADING_ROOT_ENV, "").strip()
    if configured and not ntpath.isabs(configured):
        out.append(f"{TRADING_ROOT_ENV} ist kein absoluter Pfad ({configured}); {DEFAULT_TRADING_ROOT} wird verwendet")
    if not Path(settings.trading_db_path).is_file():
        out.append(f"Datenbank nicht gefunden: {settings.trading_db_path}")
    if is_installed_package():
        own = installation_paths()["mcp_server_path"]
        if ntpath.normcase(ntpath.normpath(settings.mcp_server_path)) != ntpath.normcase(ntpath.normpath(own)):
            out.append(f"Der eingestellte MCP-Server gehört nicht zu dieser Installation ({settings.mcp_server_path}; diese Installation: {own})")
    return out


def save_settings(settings: Settings, path: Optional[Path] = None) -> Path:
    """Atomic write (temporary file, then replace)."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def validate_edit(name: str, value: Any) -> Optional[str]:
    """Validation for values typed into the settings form (same rules as loading)."""
    return _valid(name, value, getattr(Settings(), name))


def configure_logging(path: Optional[Path] = None, level: int = logging.INFO) -> logging.Logger:
    """Rotating GUI log (1 MB x 3).  Idempotent.  Tool results, portfolio values and prompts are never logged."""
    path = path or gui_log_path()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if not any(isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path for h in logger.handlers):
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
