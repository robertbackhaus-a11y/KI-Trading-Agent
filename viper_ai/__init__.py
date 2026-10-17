# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""VIPER-AI: local, MCP-based trading agent (portfolio analysis and simulation only, no order execution).

The pip installation maps the existing source folders into this package without moving them in the repository::

    viper_ai.app   <- tools/trading   domain modules and the CLI scripts (flat modules, run with their own folder on sys.path)
    viper_ai.mcp   <- mcp-tools       MCP server adapter (27 tools)
    viper_ai.gui   <- gui             optional desktop GUI (extra ``gui``)

Runtime data (database, logs, scheduler, private universe files) never lives in the package; see ``viper_ai.app.runtime_paths``.
"""

__version__ = "0.6.0"
