# Desktop GUI

Deutsche Version: [gui.de.md](gui.de.md)

The desktop GUI is a read-only cockpit for the Trading Agent. It shows what the Trading MCP server reports and controls the scheduled tasks. It contains no trading logic, no database access and no order function. Development status: there is no product version yet; the GUI reports `dev`.

## 1. Architecture

```
Desktop GUI (PySide6)
  |-- MCP client ----- stdio child process ----- Trading MCP server ----- SQLite
  |-- Task service --- PowerShell -------------- Windows Task Scheduler
  `-- settings file and log files
```

- Trading data comes only from the READ tools of the Trading MCP server. The view models only format what the tools return and compute nothing.
- The MCP server runs as a separate child process of the GUI. It is never imported, and it can run next to another client that starts its own server process.
- The Windows Task Scheduler is used directly and not through MCP, so that no language model can start a task.
- The chat with the local language model is not part of this version; the diagnostics only show whether Ollama is reachable.

## 2. Installation and start

The GUI has its own virtual environment; the runtime environment of the Trading Agent is not changed. `requirements-gui.txt` pins `PySide6-Essentials` (not the `PySide6` meta package, which also pulls WebEngine and the Addons) and `mcp` (the same version as the runtime).

```
py -3.12 -m venv .venv-gui
.venv-gui\Scripts\python.exe -m pip install -r requirements-gui.txt
.venv-gui\Scripts\python.exe -m gui
```

The commands run from the repository root. The defaults point to the production installation; they can be changed in the settings.

## 3. Pages

| Page | Content | Source |
|---|---|---|
| Dashboard | portfolio, allocation, cash, External Funding, planner status, planned and deferred entries, blockers and warnings | `run_trading_orchestrator` (detail), `database_status` |
| Planner / Actions | engine actions per position, planned and deferred entries with reason codes, sector and correlation risk, `simulation_only`, `orders_created` | `run_trading_orchestrator` (detail) |
| Tasks | the six scheduled Trading tasks: state, last and next run, result, Run now, log tail | Windows Task Scheduler, log files |
| Diagnostics / Settings | MCP and database status, paths, tool classification, Ollama reachability, settings form, MCP restart | `database_status`, settings file |

## 4. MCP client

- The official `mcp` SDK starts the server as a stdio child process. The client session belongs to its own QThread with its own asyncio loop.
- All paths are absolute, in particular `TRADING_DB_PATH`; a relative database path breaks the read-only tools of the server.
- Every call has a timeout. A cheap liveness check runs about every five seconds; if the child process dies, the status shows an error and the next call reconnects. Restart MCP restarts only the child process and changes no trading data.
- The stderr output of the server goes to its own log file.
- Nothing depends on `capabilities.experimental`.

## 5. Read and action tools

| Class | Tools | GUI |
|---|---|---|
| READ | the 13 read tools, for example `database_status`, `run_trading_orchestrator`, `get_opportunity_view` | allowed; this version calls only `database_status` and `run_trading_orchestrator` |
| ACTION | `approve_swing_promotion`, `sql_execute` | denied |
| unknown | every tool that is not in the table | denied (deny by default) |

The table lives in `gui/services/tool_policy.py`; a test requires that every tool of the server is classified. Actions are visibly separate from reads in the user interface and are never hidden behind a read.

## 6. Task control

- Only the six Trading tasks are queried and started: `Trading-MarketData-Backfill`, `Trading-FXRates-Backfill`, `Trading-EventsNews-Backfill`, `Trading-Fundamentals-SEC`, `Trading-Candidate-Discovery` and `Trading-Market-Intelligence`.
- The state is read with PowerShell as JSON, never as localized text.
- Run now asks for a confirmation and then starts the existing task with `Start-ScheduledTask`. It never starts the script behind the task and never changes a task definition. A running or disabled task cannot be started; the setting `IgnoreNew` of the scheduler stays in force.
- A started task can use the network and write to the database exactly as scheduled; therefore Run now is marked as an action.
- The log view loads only the end of the log file from the log folder and never changes the file.

## 7. Settings and logging

- The settings file is `%LOCALAPPDATA%\TradingAgentGui\settings.json`. A missing file means defaults; a broken file or an invalid value falls back to the default with a warning. There are no secrets.
- Settings: `ollama_base_url`, `ollama_model`, `mcp_python_path`, `mcp_server_path`, `trading_db_path`, `trading_logs_dir`, `default_detail_mode`, `think_enabled`, `refresh_interval_s` and `window_geometry`.
- The GUI log is `%LOCALAPPDATA%\TradingAgentGui\gui.log` (rotating). It records start and stop, MCP connect and disconnect, tool name, duration and result size, errors and task starts. It never records tool results, portfolio values or prompts.
- The stderr output of the MCP server is written to `%LOCALAPPDATA%\TradingAgentGui\mcp-stderr.log`.

## 8. Threading

- The GUI thread never blocks. MCP calls run in the asyncio thread; the result is delivered by a queued signal to a callback in the GUI thread.
- PowerShell, log reading and the Ollama check run in a thread pool (`QThreadPool`).
- There is no `qasync` and no `QtAsyncio`.

## 9. Tests

```
.venv-gui\Scripts\python.exe -m unittest discover -s tests/gui -p "test_*.py"
```

- The Qt tests run offscreen (`QT_QPA_PLATFORM=offscreen`). Without PySide6 they are skipped; the tests of the policy, the settings, the view models and the task service run in any environment.
- The MCP client is tested against a small fake stdio server that uses the same SDK: start, discovery, call, timeout, restart, crash and shutdown.
- The view models are tested with results of the real orchestrator and planner on synthetic data.

## 10. Known limits

- Read-only: there is no chat, no import, no campaigns page, no order function and no strategy change in this version.
- The detailed orchestrator result is not versioned like the compact view; the view models read it defensively and show a dash for anything that is missing.
- Run now needs PowerShell; without it the page shows the error and nothing is started.
- The refresh is manual plus a moderate interval; the status bar shows the last successful refresh.

## 11. Deployment

The GUI is deployed with the same script as the runtime. The manifest has the groups `gui`, `gui-services`, `gui-viewmodels`, `gui-views` and `gui-requirements`; they copy the package to `gui\` of the production root and `requirements-gui.txt` to the production root. Tests, documentation and the environment are never copied. The environment `.venv-gui` is created in the production root from `requirements-gui.txt` (the deploy does not create it); the runtime environment stays untouched.

```
C:\tools\trading\.venv-gui\Scripts\python.exe -m gui
C:\tools\trading\.venv-gui\Scripts\pythonw.exe -m gui
```

The first command starts the GUI with a console so that errors stay visible, the second one without a console window. There is no desktop shortcut, no installer and no autostart.
