# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The GUI side of the MCP tool classification: READ may be called by the GUI and offered to the chat, everything else is an ACTION.

The classification itself is not kept here: it is the server's ``tool_policy.py`` (``mcp-tools`` in the repository, ``mcp`` next to ``gui`` in the deployed copy
and in the pip installation), so the server and the GUI can never disagree.  A tool that is not listed there is treated as an ACTION and denied (deny by
default); a test requires every tool of the server to be classified.

The GUI starts its server with the profile ``gui`` (READ plus ``GUI_ACTIONS``).  Those ACTIONs may be triggered by an explicit button of the Watchlist or Import
page, and only through ``McpService.call_action``; they are never offered to the chat and ``call`` / ``call_sync`` refuse them.  ``sql_execute``,
``approve_swing_promotion`` and every unknown tool stay denied (the server does not even offer them in the profile ``gui``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_NAME = "viper_ai_server_tool_policy"


def _load_server_policy():
    here = Path(__file__).resolve().parent
    root = here.parents[1]  # gui/services -> the folder that holds gui and the server folder
    for candidate in (root / "mcp" / "tool_policy.py", root / "mcp-tools" / "tool_policy.py"):  # deployed copy and pip installation / repository
        if candidate.is_file():
            if _MODULE_NAME in sys.modules:
                return sys.modules[_MODULE_NAME]
            spec = importlib.util.spec_from_file_location(_MODULE_NAME, candidate)
            module = importlib.util.module_from_spec(spec)
            sys.modules[_MODULE_NAME] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError(f"tool_policy.py of the MCP server not found next to {root}")


_server = _load_server_policy()

READ = _server.READ
ACTION = _server.ACTION
POLICY = _server.POLICY
GUI_ACTIONS = _server.GUI_ACTIONS
PROFILE_ENV = _server.PROFILE_ENV
PROFILE_GUI = _server.PROFILE_GUI
classify = _server.classify
is_read = _server.is_read
is_gui_action = _server.is_gui_action
is_known = _server.is_known
unclassified = _server.unclassified


class ToolDenied(Exception):
    """A tool outside the READ class was requested."""


def require_read(name: str) -> None:
    if not is_read(name):
        raise ToolDenied(f"{name}: {'Aktion' if is_known(name) else 'unbekanntes Tool'} – in dieser GUI-Version nicht erlaubt")
