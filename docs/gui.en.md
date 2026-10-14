# Desktop GUI

Deutsche Version: [gui.de.md](gui.de.md)

The desktop GUI is a cockpit for the Trading Agent that mostly reads. It shows what the Trading MCP server reports, controls the scheduled tasks and offers a chat with a local language model that can read the same data. It contains no trading logic, no database access and no order function. It writes only through six narrow, explicit MCP actions of the Watchlist page (sections 3 and 5); the chat can only read. Development status: the GUI has no version number of its own; it reports `dev`.

## 1. Architecture

```
Desktop GUI (PySide6)
  |-- MCP client ----- stdio child process ----- Trading MCP server ----- SQLite
  |-- Chat ----------- Ollama HTTP API (local)   tool calls go through the MCP client
  |-- Task service --- PowerShell -------------- Windows Task Scheduler
  `-- settings file and log files
```

- Trading data comes only from the READ tools of the Trading MCP server. The view models only format what the tools return and compute nothing.
- The MCP server runs as a separate child process of the GUI. It is never imported, and it can run next to another client that starts its own server process.
- The Windows Task Scheduler is used directly and not through MCP, so that no language model can start a task.
- The chat talks to the local Ollama server directly; the GUI never starts or stops Ollama.

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
| Watchlist | the whole watchlist with strategy, priority, rank, score, status, data quality, news status and evaluation date; filters and sorting; add, remove, and edit strategy and priority in the table | `get_watchlist`, `add_watchlist_candidate`, `remove_watchlist_candidate`, `set_watchlist_priority`, `onboard_security`, `set_security_strategy`, `set_security_sector` |
| Sektoren | the portfolio by sector: treemap (area = market value, grouped by sector, colour = daily change), sector table (market value, share, number of positions, daily change, guard status and limit) and a position table; positions without a sector as their own area "Nicht zugeordnet" | `get_sector_overview` |
| Tasks | the six scheduled Trading tasks: state, last and next run, result, Run now, log tail | Windows Task Scheduler, log files |
| Chat | questions to a local language model that reads through the READ tools; streamed answer, optional thinking, tool status, Stop, New chat | Ollama, MCP tools |
| Diagnostics / Settings | MCP, database and Ollama status, paths, tool classification, schema hash, last chat error, settings form, MCP restart | `database_status`, settings file |

The Watchlist page changes the watchlist only through narrow, explicit actions: add (a security that is already in the Security Master; the optional check only resolves it), remove (after a confirmation whose default is Cancel; a held position stays untouched) and editing strategy and priority in the table (below; there is no second dialog that writes at once). A security that is not in the Security Master is never created by the add action; the dialog says so and then offers the button "Neues Wertpapier aufnehmen" (add a new security). The action tools are not available in the chat.

**Strategy and priority in the table.** Exactly two columns are editable: strategy (choice: Keine, Swing, Long-Term, only the values that exist) and priority (a whole number, empty = none; the existing semantics). A double click opens the editor; choosing a value writes **nothing**, it only marks the row as changed (bold, marker in the last column). Only the apply button saves the marked row: first the strategy through `set_security_strategy`, and only if that succeeded the priority through `set_watchlist_priority`; on an error nothing is written blindly, the part that was not saved stays marked and the list is reloaded (the backend is the truth). The discard button resets the row. Reloading with unsaved changes asks whether to discard them (default: Cancel); filters, sorting and page changes keep them. **Held positions** have no strategy editor and an **active campaign** blocks the change (readable message; the original code is in the tooltip), because the strategy there is tied to the campaign lifecycle. A strategy only sets the assignment: no campaign is created and no order is generated; a not held security set to Swing is afterwards treated as an entry candidate by the planner (simulation only). The tool is not available in the chat.

**Sector page.** Read-only (`get_sector_overview`, no write action, no change to the risk guard). The open positions are grouped by their effective sector - the same sector map resolution as the risk guard and the news mapping (a local entry before a public one). The market value is the EUR value of the portfolio context (quantity x EUR price); the share refers to the invested value (the sum of the valued positions, the basis of the sector guard); cash is not part of it. The treemap shows one area per sector with its positions: area = market value, colour = daily change (green/red, full colour from +-3 %, grey = no data - a missing daily change is never shown as 0 %). The daily change of a position is `close / previous_close - 1` of the two latest daily bars (trading currency, without currency effect; a missing or stale bar means "no data"); that of a sector is the value-weighted change of its positions that have data (with partial coverage the covered share is shown). Positions without a sector entry form their own area "Nicht zugeordnet" (dashed); they are never dropped and never added to another sector. The column "Guard / Limit" shows the limit of the sector guard (30 % of the invested value) and whether the sector is below it, above it (shown as "over limit" in German) or not assessed (not assigned); the guard only blocks purchases that would lift the sector above the limit. **ETF look-through.** An ETF with an official provider sector allocation is split into its categories (market value x weight; the weights are normalised to 100 % so that rounded provider figures cannot create value). A category that fits exactly one sector group (Financials, Health Care, Energy, Utilities, Materials, Consumer Staples) adds to that group as an "ETF-Look-through" contribution. A category that fits no single group (for example Information Technology, which spans semiconductors, software and hardware; likewise Industrials, Communication, Consumer Discretionary, Real Estate, cash/derivatives, "Other") stays its own group "... (ETF, nicht zuordenbar)" - it is never put into a group by name or size. An ETF without a usable allocation (no data, invalid or incomplete weights, no ISIN) stays one unresolved position and is visible with its status in the ETF table; an old allocation (older than 120 days) is used and marked. The table shows per sector the effective exposure (direct + ETF look-through), the direct share, the look-through share, the number of direct positions and ETF contributions, and the guard limit and status; the header line shows the look-through coverage (share of the ETF value with data and the share of it that lands in sector groups). Treemap, tooltip and position table say for every entry whether it is direct or from the ETF look-through (dashed border). The allocations live in `etf_lookthrough_local.json` (private, not versioned, per ETF ISIN with provider, as-of date and source), the mapping of the provider categories in `etf_sector_mapping_v1.json` (versioned); `sector_map_local.json` is neither used nor changed. **The risk guard still reads the direct assignment only**; the limit and status on the page refer to the effective exposure and block nothing. A direct assignment is still changed on the Watchlist page.

**Sector in the table.** The column "Sektor" shows the sector group from the sector map (which the risk guard and the news mapping use, not a database column). The suffix "(lokal)" and the tooltip show where it comes from: `sector_map_v1.json` (public), `sector_map_local.json` (a local entry, which may replace a public value - the replaced value is in the tooltip) or "not assigned" (the sector guard then does not assess the security). The choice offers only existing groups; for a local entry also "Lokale Zuordnung entfernen" (remove the local entry; the public value applies again if there is one). The flow is the same as for strategy and priority: choose, the row is marked as changed, only the apply button saves (after strategy and priority) through `set_security_sector`. Only `sector_map_local.json` is written (the previous version stays as `.bak`); `sector_map_v1.json` and the database are untouched. A held position or an open campaign does not block the sector change, because it is a classification, not a lifecycle change. The tool is not available in the chat.

**Adding a new security.** The dialog of its own creates exactly one security, solely from the values entered there: symbol, name and type (stock or ETF) are required; ISIN, WKN, exchange, currency, country and a Yahoo and an FMP symbol are optional. Nothing is researched, completed or guessed (no ISIN, no exchange suffix, no Yahoo symbol, no sector, no CIK). The check button runs a dry run: it normalizes the input, checks ISIN (with check digit), symbol, WKN and provider symbols against the Security Master and shows either "Keine Konflikte gefunden." (no conflicts) with a summary or every conflict in plain words; the technical codes are in the tooltip only. The create button is enabled only after a check without conflicts and is locked again whenever the input changes. Creating runs in a single transaction (security, provider symbols and, on request, the watchlist entry) and is rolled back completely on any error. If the security already exists, no second one is created and the existing one is not changed; the dialog shows symbol, name and `security_id` and offers to add it to the watchlist through the ordinary action. The sector stays "not assigned". No strategy, campaign, position or order is created and no market data, fundamentals or news is fetched; market data is added by the next regular run. The tool `onboard_security` is not available in the chat.

Technical reason codes (for example `STOPPED_SWING_MAX_REACHED`) are shown as a short German text; the tooltip holds the explanation and the original code. The backend codes stay unchanged, and an unknown code is shown in a readable form.

## 4. MCP client

- The official `mcp` SDK starts the server as a stdio child process. The client session belongs to its own QThread with its own asyncio loop.
- All paths are absolute, in particular `TRADING_DB_PATH`; a relative database path breaks the read-only tools of the server.
- Every call has a timeout. A cheap liveness check runs about every five seconds; if the child process dies, the status shows an error and the next call reconnects. Restart MCP restarts only the child process and changes no trading data.
- The stderr output of the server goes to its own log file.
- Nothing depends on `capabilities.experimental`.

## 5. Read and action tools

| Class | Tools | GUI |
|---|---|---|
| READ | the 15 read tools, for example `database_status`, `run_trading_orchestrator`, `get_watchlist`, `get_sector_overview` | allowed; the pages call only `database_status`, `run_trading_orchestrator`, `get_watchlist` and `get_sector_overview`, the chat offers all 15 to the model |
| ACTION | `approve_swing_promotion`, `sql_execute` | denied, and never offered to the model |
| ACTION (watchlist) | `add_watchlist_candidate`, `remove_watchlist_candidate`, `set_watchlist_priority` | only through the explicit buttons of the Watchlist page (`call_action`); never offered to the model, refused by `call` |
| ACTION (strategy) | `set_security_strategy` | only through the apply button of the Watchlist page (`call_action`); never offered to the model, refused by `call` |
| ACTION (sector) | `set_security_sector` | only through the apply button of the Watchlist page (`call_action`); writes only the local sector map; never offered to the model, refused by `call` |
| ACTION (Security Master) | `onboard_security` | only through the dialog "Neues Wertpapier aufnehmen" of the Watchlist page (`call_action`, dry run first, then create); never offered to the model, refused by `call` |
| unknown | every tool that is not in the table | denied (deny by default) |

The table lives in `gui/services/tool_policy.py`; a test requires that every tool of the server is classified. Actions are visibly separate from reads in the user interface and are never hidden behind a read.

## 6. Chat

- The Ollama client uses only the standard library. It streams `/api/chat` (answer text, thinking text, tool calls), and Stop closes the HTTP connection of that one request. The GUI never starts, stops, loads or unloads Ollama or a model, and it sets no context size, so it can share one Ollama instance with other clients.
- The model and the thinking switch come from the settings `ollama_model` and `think_enabled`; the model is changed only there. Looking at the page asks Ollama only for its version and model names; the model is loaded by the first question.
- The tool loop is a small own implementation without any framework: question, `/api/chat`, tool calls, check, MCP call, result as a tool message, `/api/chat` again, until the answer. At most 6 tool rounds per question; then the request ends with a notice.
- Only the READ tools of the MCP discovery are offered, with their name, description and schema unchanged. A call to anything else is answered with `TOOL_NOT_ALLOWED` and never reaches the MCP server. Arguments are validated first (`TOOL_ARGS_INVALID`). The parameters `detail` and `table` are removed from the offered schemas and forced to false (only where the tool has them), so the compact views fit the context. A result over 40,000 characters (`TOOL_RESULT_TOO_LARGE`) or beyond 80,000 characters per question (`TOOL_RESULT_BUDGET`) is not handed to the model.
- The transcript shows user, assistant, thinking, tool call, tool status and error separately. Tool results are never shown in full: only the tool name, the duration and the size.
- Only one request runs at a time. The history stays in memory, New chat resets it, and nothing is written to disk; in follow-up questions, older tool results are replaced by a one-line placeholder. The system prompt is short and lives in the code (`SYSTEM_PROMPT`).

## 7. Task control

- Only the six Trading tasks are queried and started: `Trading-MarketData-Backfill`, `Trading-FXRates-Backfill`, `Trading-EventsNews-Backfill`, `Trading-Fundamentals-SEC`, `Trading-Candidate-Discovery` and `Trading-Market-Intelligence`.
- The state is read with PowerShell as JSON, never as localized text.
- Run now asks for a confirmation and then starts the existing task with `Start-ScheduledTask`. It never starts the script behind the task and never changes a task definition. A running or disabled task cannot be started; the setting `IgnoreNew` of the scheduler stays in force.
- A started task can use the network and write to the database exactly as scheduled; therefore Run now is marked as an action.
- The log view loads only the end of the log file from the log folder and never changes the file.

## 8. Settings and logging

- The settings file is `%LOCALAPPDATA%\TradingAgentGui\settings.json`. A missing file means defaults; a broken file or an invalid value falls back to the default with a warning. There are no secrets.
- Settings: `ollama_base_url`, `ollama_model`, `mcp_python_path`, `mcp_server_path`, `trading_db_path`, `trading_logs_dir`, `default_detail_mode`, `think_enabled`, `refresh_interval_s` and `window_geometry`.
- The GUI log is `%LOCALAPPDATA%\TradingAgentGui\gui.log` (rotating). It records start and stop, MCP connect and disconnect, tool name, duration and result size, Ollama requests (model, duration, tool rounds, abort, errors), and task starts. It never records questions, answers, tool results or portfolio values.
- The stderr output of the MCP server is written to `%LOCALAPPDATA%\TradingAgentGui\mcp-stderr.log`.

## 9. Threading

- The GUI thread never blocks. MCP calls run in the asyncio thread; the result is delivered by a queued signal to a callback in the GUI thread.
- PowerShell, log reading and the Ollama check run in a thread pool (`QThreadPool`).
- The Ollama stream and the tool loop run in their own worker thread; a tool call is handed to the MCP thread and the worker waits for it, so the client session is only used by its own thread.
- There is no `qasync` and no `QtAsyncio`.

## 10. Tests

```
.venv-gui\Scripts\python.exe -m unittest discover -s tests/gui -p "test_*.py"
```

- The Qt tests run offscreen (`QT_QPA_PLATFORM=offscreen`). Without PySide6 they are skipped; the tests of the policy, the settings, the view models, the task service, the Ollama client and the tool loop run in any environment.
- The MCP client is tested against a small fake stdio server that uses the same SDK: start, discovery, call, timeout, restart, crash and shutdown.
- The Ollama client and the chat are tested against a scripted fake Ollama HTTP server: streaming, thinking, tool calls, rounds, limits, errors, Stop and the follow-up request, with synthetic data only.
- The view models are tested with results of the real orchestrator and planner on synthetic data.

## 11. Known limits

- Read-only: there is no import, no campaigns page, no order function and no strategy change in this version; the chat cannot call any action tool.
- The chat sees the compact views only; drill-down results (`detail`) are not available to the model.
- The detailed orchestrator result is not versioned like the compact view; the view models read it defensively and show a dash for anything that is missing.
- Run now needs PowerShell; without it the page shows the error and nothing is started.
- The refresh is manual plus a moderate interval; the status bar shows the last successful refresh.

## 12. Deployment

The GUI is deployed with the same script as the runtime. The manifest has the groups `gui`, `gui-services`, `gui-viewmodels`, `gui-views` and `gui-requirements`; they copy the package to `gui\` of the production root and `requirements-gui.txt` to the production root. Tests, documentation and the environment are never copied. The environment `.venv-gui` is created in the production root from `requirements-gui.txt` (the deploy does not create it); the runtime environment stays untouched.

```
C:\tools\trading\.venv-gui\Scripts\python.exe -m gui
C:\tools\trading\.venv-gui\Scripts\pythonw.exe -m gui
```

The first command starts the GUI with a console so that errors stay visible, the second one without a console window. There is no desktop shortcut, no installer and no autostart.

## 13. Desktop launcher

The desktop shortcut KI TA starts the GUI directly, with no batch file and no script in between. The chat is available in production and is read-only.

- Target: `C:\tools\trading\.venv-gui\Scripts\pythonw.exe` with the argument `-m gui`, so no console window appears.
- Working directory: `C:\tools\trading`.
- Icon: `C:\tools\ki\ki-ta-icon.ico`, next to the icon of the existing KI start and stop shortcuts. It follows their look (teal circle, white bold lettering) and shows KI and TA on two lines, so it is easy to tell apart. It is a multi-resolution icon (16 to 256 pixels, 32 bit with transparency).
- The GUI never starts or stops Ollama; it only talks to a running instance, so start Ollama first as usual.
- The chat keeps no history on disk, and Stop cancels only the current request.
