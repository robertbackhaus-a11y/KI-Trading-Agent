# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Desktop GUI of VIPER-AI: main window, wiring and entry point (``python -m gui``; pip installation: ``viper-ai-gui``).

Cockpit.  Trading data comes only from the Trading MCP server (READ tools, see ``services/tool_policy.py``); the only writes are the
three narrow watchlist actions of the Watchlist page (add, remove, change priority; explicit buttons, ``McpService.call_action``); the Windows
Task Scheduler is used directly; there is no database access, no trading logic and no order function in this package.  Blocking work
never runs in the GUI thread: the MCP client lives in its own QThread with an asyncio loop, everything else in a thread pool.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QMainWindow, QStackedWidget, QWidget, QHBoxLayout

from .services import settings as settings_mod
from .services.background import AsyncRunner
from .services.chat_service import ChatService
from .services.mcp_client import CONNECTED, ERROR, McpResult, McpService
from .services.ollama_client import check_ollama
from .services.settings import GUI_VERSION, Settings, get_logger
from .services.task_service import TaskBackend
from .services.tool_loop import offered_tools, schema_hash
from .services.tool_policy import classify
from .viewmodels.onboarding import describe_onboarding
from .viewmodels.portfolio import DASH, build_dashboard, build_planner, fmt_time
from .viewmodels.effective import build_effective, direct_performance
from .viewmodels.sectors import build_sectors
from .viewmodels import transaction_import as import_vm
from .viewmodels.tasks import RunWatch, build_etf_rows, build_task_rows, run_progress, summarize
from .viewmodels.watchlist import ActionMessage, build_watchlist, describe_action
from .viewmodels.watchlist_edit import describe_sector, describe_strategy, priority_saved, sector_saved, strategy_saved
from .views.chat import ChatView
from .views.dashboard import DashboardView
from .views.diagnostics import DiagnosticsView
from .views.planner import PlannerView
from .views.sectors import SectorsView
from .views.tasks import TasksView
from .views.transaction_import import TransactionImportView
from .views.watchlist import WatchlistView

PAGES = ("Dashboard", "Planner / Actions", "Watchlist", "Sektoren", "Tasks", "Import", "Chat", "Diagnostics / Settings")
WATCHLIST_PAGE, SECTORS_PAGE, TASKS_PAGE, IMPORT_PAGE, CHAT_PAGE, DIAGNOSTICS_PAGE = 2, 3, 4, 5, 6, 7
WATCHLIST_STALE_S = 60  # opening the page reloads a list older than this; there is no polling
SECTORS_STALE_S = 60  # the same for the sector overview
STATE_TEXT = {"CONNECTED": "verbunden", "CONNECTING": "verbindet …", "DISCONNECTED": "getrennt", "ERROR": "Fehler"}


class MainWindow(QMainWindow):
    RUN_POLL_FIRST_MS, RUN_POLL_MS = 2000, 3000  # how soon and how often the scheduler is asked about a run started from the Tasks page

    def __init__(self, settings: Settings, mcp: McpService, backend: TaskBackend, runner: AsyncRunner, warnings: Optional[list[str]] = None,
                 save_fn=settings_mod.save_settings, ollama_fn=check_ollama, chat: Optional[ChatService] = None) -> None:
        super().__init__()
        self.settings, self.mcp, self.backend, self.runner = settings, mcp, backend, runner
        self.chat_service = chat or ChatService(lambda: self.settings, mcp)
        self._ollama = None  # last OllamaStatus (reachability only; no model is ever loaded by checking)
        self.warnings = list(warnings or [])
        self._save_fn, self._ollama_fn = save_fn, ollama_fn
        self.log = get_logger()
        self._inflight: set[str] = set()
        self._orch = None
        self._orch_at: Optional[datetime] = None
        self._db_status: dict = {}
        self._db_text = DASH
        self._last_refresh: Optional[datetime] = None
        self._tasks_at: Optional[datetime] = None
        self._task_infos = None  # the last scheduler answer
        self._watched: dict[str, RunWatch] = {}  # tasks started from the Tasks page and followed until the scheduler reports their end
        self._polling = False
        self._freshness = None  # the last get_data_freshness answer (None = not available)
        self._watchlist = None
        self._watchlist_at: Optional[datetime] = None
        self._sectors = None
        self._sectors_at: Optional[datetime] = None
        self._effective = None
        self.setWindowTitle(f"VIPER-AI – Desktop GUI ({GUI_VERSION})")
        self.resize(1200, 760)
        if settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(settings.window_geometry.encode("ascii", "ignore")))

        central = QWidget()
        layout = QHBoxLayout(central)
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(190)
        self.sidebar.addItems(PAGES)
        self.stack = QStackedWidget()
        self.dashboard, self.planner, self.watchlist, self.sectors, self.tasks = DashboardView(), PlannerView(), WatchlistView(), SectorsView(), TasksView()
        self.import_page = TransactionImportView()
        self.chat = ChatView(settings.ollama_model, settings.think_enabled)
        self.diagnostics = DiagnosticsView(settings)
        for page in (self.dashboard, self.planner, self.watchlist, self.sectors, self.tasks, self.import_page, self.chat, self.diagnostics):
            self.stack.addWidget(page)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.lbl_mcp, self.lbl_tools, self.lbl_db, self.lbl_refresh = QLabel("MCP: getrennt"), QLabel("Tools: —"), QLabel("DB: —"), QLabel("Letzter Refresh: —")
        for label in (self.lbl_mcp, self.lbl_tools, self.lbl_db, self.lbl_refresh):
            self.statusBar().addPermanentWidget(label)
        if self.warnings:
            self.statusBar().showMessage("⚠ " + self.warnings[0], 15000)

        self.sidebar.currentRowChanged.connect(self._on_page)
        self.dashboard.refresh_requested.connect(self.refresh_orchestrator)
        self.planner.refresh_requested.connect(self.refresh_orchestrator)
        self.watchlist.refresh_requested.connect(self.refresh_watchlist)
        self.watchlist.check_requested.connect(self.check_watchlist)
        self.watchlist.add_requested.connect(self.add_watchlist)
        self.watchlist.remove_requested.connect(self.remove_watchlist)
        self.watchlist.edit_apply_requested.connect(self.apply_watchlist_edit)
        self.watchlist.onboard_check_requested.connect(self.check_onboarding)
        self.watchlist.onboard_create_requested.connect(self.create_onboarding)
        self.watchlist.onboard_watchlist_requested.connect(self.add_existing_to_watchlist)
        self.sectors.refresh_requested.connect(self.refresh_sectors)
        self.tasks.refresh_requested.connect(self.refresh_tasks)
        self.tasks.run_requested.connect(self.run_task)
        self.tasks.log_requested.connect(self.show_task_log)
        self.import_page.check_requested.connect(self.check_import)
        self.import_page.import_requested.connect(self.run_import)
        self.diagnostics.refresh_requested.connect(self.refresh_diagnostics)
        self.diagnostics.restart_requested.connect(self.restart_mcp)
        self.diagnostics.ollama_check_requested.connect(self.check_ollama)
        self.diagnostics.save_requested.connect(self.save_settings)
        self.chat.send_requested.connect(self.send_chat)
        self.chat.stop_requested.connect(self.chat_service.cancel)
        self.chat.new_chat_requested.connect(self.new_chat)
        self.chat.think_toggled.connect(self.set_think)
        self.chat_service.event.connect(self.chat.handle_event)
        self.chat_service.event.connect(lambda ev: self.refresh_diagnostics() if ev.kind in ("error", "done") else None)
        self.chat_service.busy_changed.connect(self.chat.set_busy)
        self.mcp.state_changed.connect(self._on_mcp_state)
        self.mcp.tools_changed.connect(self._on_tools)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(settings.refresh_interval_s * 1000)
        self.sidebar.setCurrentRow(0)
        self.refresh_diagnostics()

    # -- status bar --------------------------------------------------------------------
    def _on_mcp_state(self, state: str, detail: str) -> None:
        self.lbl_mcp.setText(f"MCP: {STATE_TEXT.get(state, state)}")
        self.lbl_mcp.setToolTip(detail)
        if state == CONNECTED:
            self.refresh_all()
        elif state == ERROR:
            self.statusBar().showMessage("MCP-Fehler: " + detail, 15000)
        self.refresh_diagnostics()

    def _on_tools(self, count: int) -> None:
        self.lbl_tools.setText(f"Tools: {count}")
        self.refresh_diagnostics()

    def _mark_refresh(self) -> None:
        self._last_refresh = datetime.now()
        self.lbl_refresh.setText("Letzter Refresh: " + self._last_refresh.strftime("%H:%M:%S"))

    # -- refresh ------------------------------------------------------------------------
    def refresh_all(self) -> None:
        self.refresh_status()
        self.refresh_orchestrator()
        self.refresh_tasks()
        if self.stack.currentIndex() == WATCHLIST_PAGE:  # reconnected while the page is open
            self.refresh_watchlist()
        elif self.stack.currentIndex() == SECTORS_PAGE:
            self.refresh_sectors()

    def _tick(self) -> None:
        self.refresh_status()
        page = self.stack.currentIndex()
        if page in (0, 1):
            self.refresh_orchestrator()
        elif page == TASKS_PAGE:
            self.refresh_tasks()

    def refresh_status(self) -> None:
        if "db" in self._inflight:
            return
        self._inflight.add("db")
        self.mcp.call("database_status", timeout=15, on_done=self._on_db)

    def _on_db(self, res: McpResult) -> None:
        self._inflight.discard("db")
        self._db_status = res.data if res.tool_ok and isinstance(res.data, dict) else {}
        self._db_text = "OK" if res.tool_ok else "Fehler"
        self.lbl_db.setText("DB: " + self._db_text)
        self.lbl_db.setToolTip("" if res.tool_ok else res.message)
        if res.tool_ok:
            self._mark_refresh()
        if self._orch is not None:
            self._render_orchestrator()
        self.refresh_diagnostics()

    def refresh_orchestrator(self) -> None:
        if "orch" in self._inflight:
            return
        self._inflight.add("orch")
        self.dashboard.show_loading()
        self.planner.show_loading()
        self.mcp.call("run_trading_orchestrator", {"detail": True}, timeout=120, on_done=self._on_orchestrator)

    def _on_orchestrator(self, res: McpResult) -> None:
        self._inflight.discard("orch")
        error = ""
        if res.tool_ok:
            self._orch, self._orch_at = res.data, datetime.now()
            self._mark_refresh()
        else:
            error = res.message or "Unbekannter Fehler"
        self._render_orchestrator(error)

    def _render_orchestrator(self, error: str = "") -> None:
        if self._orch is None:
            envelope = {"ok": False, "message": error or "Noch keine Daten"}
        else:
            envelope = self._orch
        self.dashboard.show_vm(build_dashboard(envelope, self._db_status, self._orch_at))
        self.planner.show_vm(build_planner(envelope, self._orch_at))
        if error and self._orch is not None:  # keep the last good view and say so
            note = f"Aktualisierung fehlgeschlagen ({error}); gezeigt wird der Stand von {fmt_time(self._orch_at)}."
            self.dashboard.message.setText(note)
            self.planner.message.setText(note)

    def refresh_tasks(self) -> None:
        if "tasks" in self._inflight:
            return
        self._inflight.add("tasks")
        self.runner.submit(self.backend.query, self._on_tasks)
        if "freshness" not in self._inflight:
            self._inflight.add("freshness")
            self.mcp.call("get_data_freshness", timeout=30, on_done=self._on_freshness)  # the data stand of what the tasks produce (READ); without MCP the column stays empty

    def _on_tasks(self, ok: bool, value) -> None:
        self._inflight.discard("tasks")
        if not ok:
            self.log.warning("Task-Abfrage fehlgeschlagen: %s", type(value).__name__)
            self.tasks.show_error(f"Scheduler-Abfrage fehlgeschlagen: {value}")
            return
        self._tasks_at = datetime.now()
        self._task_infos = value
        self._render_tasks()
        self._follow_manual_runs()

    def _on_freshness(self, res: McpResult) -> None:
        self._inflight.discard("freshness")
        self._freshness = res.data if res.tool_ok and isinstance(res.data, dict) else None
        if not res.tool_ok:
            self.log.info("Datenstand der Tasks nicht verfügbar: %s", res.kind)
        self._render_tasks()

    def _render_tasks(self) -> None:
        if self._task_infos is None:
            return
        rows = build_task_rows(self._task_infos, self._freshness, datetime.now(), self.backend.logs_dir)
        self.tasks.show_rows(rows, fmt_time(self._tasks_at), summarize(rows, self._freshness is not None), build_etf_rows(self._freshness, datetime.now()))

    # -- watchlist ------------------------------------------------------------------------------------------
    def refresh_watchlist(self) -> None:
        if "watchlist" in self._inflight:
            return
        self._inflight.add("watchlist")
        self.watchlist.show_loading()
        self.mcp.call("get_watchlist", {"limit": 200, "table": True}, timeout=90, on_done=self._on_watchlist)

    def _on_watchlist(self, res: McpResult) -> None:
        self._inflight.discard("watchlist")
        error = ""
        if res.tool_ok:
            self._watchlist, self._watchlist_at = res.data, datetime.now()
            self._mark_refresh()
        else:
            error = res.message or "Unbekannter Fehler"
        envelope = self._watchlist if self._watchlist is not None else {"ok": False, "message": error or "Noch keine Daten"}
        self.watchlist.show_vm(build_watchlist(envelope, self._watchlist_at))
        if error and self._watchlist is not None:  # keep the last good list and say so
            self.watchlist.message.setText(f"Aktualisierung fehlgeschlagen ({error}); gezeigt wird der Stand von {fmt_time(self._watchlist_at)}.")

    def refresh_sectors(self) -> None:
        if "sectors" in self._inflight:
            return
        self._inflight.add("sectors")
        self.sectors.show_loading()
        self.mcp.call("get_sector_overview", timeout=90, on_done=self._on_sectors)
        if "effective" not in self._inflight:
            self._inflight.add("effective")
            self.sectors.show_effective_loading()
            self.mcp.call("get_effective_exposure", {"top": 40}, timeout=90, on_done=self._on_effective)

    def _on_effective(self, res: McpResult) -> None:
        self._inflight.discard("effective")
        error = ""
        if res.tool_ok:
            self._effective = res.data
        else:
            error = res.message or "Unbekannter Fehler"
        self._render_effective(error)

    def _render_effective(self, error: str = "") -> None:
        """The exposure section: the effective-exposure answer plus, for the colour of a tile, the daily change of the company's own direct share from the sector overview."""
        envelope = self._effective if self._effective is not None else {"ok": False, "message": error or "Noch keine Daten"}
        self.sectors.show_effective(build_effective(envelope, self._sectors_at, direct_performance(self._sectors)))
        if error and self._effective is not None:
            self.sectors.effective_message.setText(f"Aktualisierung fehlgeschlagen ({error}); gezeigt wird der letzte Stand.")

    def _on_sectors(self, res: McpResult) -> None:
        self._inflight.discard("sectors")
        error = ""
        if res.tool_ok:
            self._sectors, self._sectors_at = res.data, datetime.now()
            self._mark_refresh()
        else:
            error = res.message or "Unbekannter Fehler"
        envelope = self._sectors if self._sectors is not None else {"ok": False, "message": error or "Noch keine Daten"}
        self.sectors.show_vm(build_sectors(envelope, self._sectors_at))
        if error and self._sectors is not None:  # keep the last good overview and say so
            self.sectors.message.setText(f"Aktualisierung fehlgeschlagen ({error}); gezeigt wird der Stand von {fmt_time(self._sectors_at)}.")
        if self._effective is not None:
            self._render_effective()  # the tile colours depend on the daily changes of this answer

    def _watchlist_action(self, tool: str, arguments: dict, to_dialog: bool = False) -> None:
        """One watchlist ACTION for an explicit user action; the outcome is shown and, if something changed, the list is reloaded."""
        self.log.info("Watchlist-Aktion angefordert: %s", tool)

        def done(res: McpResult) -> None:
            message = describe_action(res.tool_ok, res.data, res.message)
            self.log.info("Watchlist-Aktion %s: %s", tool, res.data.get("result") if isinstance(res.data, dict) and res.data.get("result") else res.kind)
            self.watchlist.show_message(message, to_dialog)
            self.statusBar().showMessage(message.text.splitlines()[0], 8000)
            if message.done:
                self.refresh_watchlist()

        self.mcp.call_action(tool, arguments, timeout=30, on_done=done)

    def check_watchlist(self, identifier: str) -> None:
        self._watchlist_action("add_watchlist_candidate", {"identifier": identifier, "dry_run": True}, to_dialog=True)

    def add_watchlist(self, identifier: str, priority) -> None:
        arguments = {"identifier": identifier} if priority is None else {"identifier": identifier, "priority": int(priority)}
        self._watchlist_action("add_watchlist_candidate", arguments, to_dialog=True)

    def remove_watchlist(self, security_id: int) -> None:
        self._watchlist_action("remove_watchlist_candidate", {"security_id": int(security_id)})

    def apply_watchlist_edit(self, security_id: int, changes: dict) -> None:
        """"Änderung übernehmen" of one row: the strategy first, the priority only if that succeeded; a failure stops the sequence.  The list is reloaded afterwards."""
        self.log.info("Watchlist-Änderung angefordert: %s", "+".join(k for k in ("strategy", "priority", "sector") if k in changes))  # no values in the log
        steps = []
        if "strategy" in changes:
            steps.append(("strategy", "set_security_strategy", {"security_id": int(security_id), "strategy": changes["strategy"]}))
        if "priority" in changes:
            steps.append(("priority", "set_watchlist_priority", {"security_id": int(security_id), "priority": changes["priority"]}))
        if "sector" in changes:
            steps.append(("sector", "set_security_sector", {"security_id": int(security_id), "sector": changes["sector"]}))
        saved: set[str] = set()
        lines: list[str] = []

        def finish(failure: ActionMessage | None) -> None:
            if failure is None:
                message = ActionMessage("info", "\n".join(lines) or "Nichts zu speichern.", True)
            else:
                text = "\n".join([*lines, failure.text])
                message = ActionMessage(failure.level, text, False, failure.result, failure.tip)
            self.watchlist.edit_finished(int(security_id), saved, message)
            self.statusBar().showMessage(message.text.splitlines()[0], 8000)
            self.refresh_watchlist()

        def run(index: int) -> None:
            if index >= len(steps):
                finish(None)
                return
            field, tool, arguments = steps[index]

            def done(res: McpResult) -> None:
                if field == "strategy":
                    ok, message = strategy_saved(res.tool_ok, res.data), describe_strategy(res.tool_ok, res.data, res.message)
                elif field == "sector":
                    ok, message = sector_saved(res.tool_ok, res.data), describe_sector(res.tool_ok, res.data, res.message)
                else:
                    ok, message = priority_saved(res.tool_ok, res.data), describe_action(res.tool_ok, res.data, res.message)
                    if ok:
                        message = ActionMessage("info", "Priorität gespeichert.", True, "PRIORITY_SET")
                    else:
                        message = ActionMessage(message.level, f"Die Priorität wurde nicht gespeichert: {message.text}", False, message.result, message.tip)
                self.log.info("Watchlist-Änderung %s: %s", field, res.data.get("result") if isinstance(res.data, dict) and res.data.get("result") else res.kind)
                if not ok:
                    finish(message)  # the next step is not run blindly
                    return
                saved.add(field)
                lines.append(message.text)
                run(index + 1)

            self.mcp.call_action(tool, arguments, timeout=30, on_done=done)

        run(0)

    def _onboarding_action(self, arguments: dict, create: bool) -> None:
        """``onboard_security`` for an explicit click: a check (dry run) or, after a clean check, the creation; the list is reloaded after a creation."""
        self.log.info("Onboarding angefordert: %s", "Anlegen" if create else "Prüfen")  # no values in the log

        def done(res: McpResult) -> None:
            outcome = describe_onboarding(res.tool_ok, res.data, res.message)
            self.log.info("Onboarding %s: %s", "Anlegen" if create else "Prüfen", outcome.result or res.kind)
            self.watchlist.show_onboard_outcome(outcome, None if create else arguments)
            self.statusBar().showMessage(outcome.text.splitlines()[0], 8000)
            if outcome.created:
                self.refresh_watchlist()

        self.mcp.call_action("onboard_security", {**arguments, "dry_run": not create}, timeout=30, on_done=done)

    def check_onboarding(self, arguments: dict) -> None:
        self._onboarding_action(arguments, create=False)

    def create_onboarding(self, arguments: dict) -> None:
        self._onboarding_action(arguments, create=True)

    # -- transaction import (explicit buttons of the Import page) -----------------------------------------------------------------
    def check_import(self, arguments: dict) -> None:
        """``check_transaction_import``: the dry run; nothing is written."""
        self.log.info("Import-Prüfung angefordert")  # neither the path nor any value in the log

        def done(res: McpResult) -> None:
            view = import_vm.build(res.tool_ok, res.data, res.message)
            self.log.info("Import-Prüfung: %s", "ok" if view.ok else res.kind)
            self.import_page.show_view(view, arguments if view.ok else None)
            self.statusBar().showMessage(view.headline, 8000)

        self.mcp.call_action("check_transaction_import", arguments, timeout=300, on_done=done)

    def run_import(self, arguments: dict, plan_token: str) -> None:
        """``import_transactions`` with the token of the check; the cached lists are dropped afterwards (positions and securities may have changed)."""
        self.log.info("Import angefordert")

        def done(res: McpResult) -> None:
            view = import_vm.build(res.tool_ok, res.data, res.message)
            self.log.info("Import: %s", "geschrieben" if view.written else ("ok" if view.ok else res.kind))
            self.import_page.show_view(view, None)
            self.statusBar().showMessage(view.headline, 8000)
            if view.written:
                self._watchlist_at = None
                self._sectors_at = None
                self.refresh_orchestrator()

        self.mcp.call_action("import_transactions", {**arguments, "plan_token": plan_token}, timeout=300, on_done=done)

    def add_existing_to_watchlist(self, security_id: int) -> None:
        """A security the onboarding check found in the Security Master: the ordinary add action (no second copy is ever created)."""
        self.log.info("Watchlist-Aktion angefordert: add_watchlist_candidate (vorhandenes Wertpapier)")

        def done(res: McpResult) -> None:
            message = describe_action(res.tool_ok, res.data, res.message)
            self.watchlist.show_onboard_watchlist_result(message)
            self.statusBar().showMessage(message.text.splitlines()[0], 8000)
            if message.done:
                self.refresh_watchlist()

        self.mcp.call_action("add_watchlist_candidate", {"identifier": str(int(security_id))}, timeout=30, on_done=done)

    def run_task(self, name: str) -> None:
        """"Jetzt ausführen": starts the existing scheduled task (the same job, action, settings and log as the schedule; no console window - the scheduler runs it with pythonw) and
        follows it on this page until the scheduler reports its end.  A task that is running, disabled or already being started is not started again."""
        if name in self._watched:
            self.statusBar().showMessage("Dieser Task läuft oder startet gerade – kein zweiter Start.", 6000)  # a hint only: the run status above stays
            return
        self.log.info("Task-Start angefordert: %s", name)
        self._watched[name] = RunWatch(name, datetime.now())  # claimed at once: a second click before the first answer cannot start it twice
        self.tasks.show_run_status(f"Starte {name} …")

        def done(ok: bool, value) -> None:
            if not ok or not value[0]:
                self._watched.pop(name, None)
                message = f"Start fehlgeschlagen: {value}" if not ok else value[1]
                self.log.warning("Task-Start %s: %s", name, type(value).__name__ if not ok else message)
                self.tasks.show_run_status(message, "blocker")
                self.tasks.show_error(message)
                return
            message = value[1]
            self.log.info("Task-Start %s: ok", name)
            self.tasks.show_run_status(message + " – warte auf den Scheduler …")
            self.statusBar().showMessage(message, 10000)
            self._watched[name].started = datetime.now()
            if not self._polling:  # one polling chain for all followed runs
                QTimer.singleShot(self.RUN_POLL_FIRST_MS, self._poll_manual_runs)

        self.runner.submit(lambda: self.backend.start(name), done)

    def _poll_manual_runs(self) -> None:
        if not self._watched:
            self._polling = False
            return
        self._polling = True
        self.refresh_tasks()  # the answer ends in _follow_manual_runs
        QTimer.singleShot(self.RUN_POLL_MS, self._poll_manual_runs)

    def _follow_manual_runs(self) -> None:
        """Progress and result of the runs started from this page, from the scheduler answer that was just rendered (state, last run, exit code); the log follows when one ends."""
        infos = {t.name: t for t in (self._task_infos or [])}
        for name, watch in list(self._watched.items()):
            if watch.started is None:
                continue
            text, level, finished = run_progress(watch, infos.get(name), datetime.now())
            self.tasks.show_run_status(text, level)
            if finished:
                del self._watched[name]
                self.log.info("Task-Lauf %s beendet: %s", name, level)
                self.statusBar().showMessage(text, 15000)
                self.show_task_log(name)

    def show_task_log(self, name: str) -> None:
        def done(ok: bool, value) -> None:
            if ok:
                self.tasks.show_log(name, value)
            else:
                self.tasks.show_error(f"Log nicht lesbar: {value}")

        self.runner.submit(lambda: self.backend.tail(name), done)

    # -- diagnostics --------------------------------------------------------------------------
    def refresh_diagnostics(self) -> None:
        s = self.settings
        db = self._db_text
        rows = [
            ("GUI-Version", GUI_VERSION),
            ("MCP-Status", f"{STATE_TEXT.get(self.mcp.state, self.mcp.state)}" + (f" – {self.mcp.detail}" if self.mcp.detail and self.mcp.state != CONNECTED else "")),
            ("MCP-Tools", str(self.mcp.tool_count)),
            ("Datenbank-Status", db),
            ("MCP-Python", s.mcp_python_path),
            ("MCP-Server", s.mcp_server_path),
            ("Datenbank", s.trading_db_path),
            ("Log-Ordner der Tasks", s.trading_logs_dir),
            ("GUI-Log", str(settings_mod.gui_log_path())),
            ("MCP-stderr-Log", str(self.mcp.stderr_path)),
            ("Einstellungen", str(settings_mod.settings_path())),
            ("Ollama-URL", s.ollama_base_url),
            ("Ollama-Modell", s.ollama_model),
        ]
        catalog = self.mcp.tools
        offered = len(offered_tools(catalog)[0])
        ollama = self._ollama
        rows += [
            ("Ollama erreichbar", DASH if ollama is None else ("Ja" if ollama.reachable else "Nein" + (f" ({ollama.error})" if ollama.error else ""))),
            ("Ollama-Version", DASH if ollama is None or not ollama.reachable else ollama.version or DASH),
            ("Gewähltes Modell vorhanden", DASH if ollama is None or not ollama.reachable else ("Ja" if ollama.has_model(s.ollama_model) else "Nein")),
            ("MCP-Tools (READ, dem Chat angeboten)", f"{offered}"),
            ("MCP-Tools (ACTION oder unbekannt, gesperrt)", f"{len(catalog) - offered}"),
            ("Tool-Schema-Hash", schema_hash(catalog) if catalog else DASH),
            ("Letzter Chat-Fehler", self.chat_service.last_error or "keiner"),
        ]
        self.diagnostics.show_info(rows, self.warnings)
        self.diagnostics.show_tools([[name, classify(name)] for name in sorted(self.mcp.tools)])

    def restart_mcp(self) -> None:
        self.statusBar().showMessage("MCP wird neu gestartet …", 5000)
        self.mcp.restart(self.settings)

    def check_ollama(self) -> None:
        url = self.settings.ollama_base_url
        self.diagnostics.show_ollama("prüfe …", None)

        def done(ok: bool, value) -> None:
            self._ollama = value if ok else None
            if ok and value.reachable:
                models = ", ".join(value.models) or "keine Modelle"
                self.diagnostics.show_ollama(f"erreichbar, Version {value.version}; Modelle: {models}", True)
                present = value.has_model(self.settings.ollama_model)
                self.chat.set_ollama("erreichbar" + ("" if present else f", Modell {self.settings.ollama_model} fehlt"), present)
            else:
                self.diagnostics.show_ollama("nicht erreichbar" + (f": {value.error}" if ok else f": {value}"), False)
                self.chat.set_ollama("nicht erreichbar", False)
            self.refresh_diagnostics()

        self.runner.submit(lambda: self._ollama_fn(url), done)

    # -- chat -------------------------------------------------------------------------------------
    def send_chat(self, text: str) -> None:
        self.chat.add_user(text)
        self.chat_service.send(text)

    def new_chat(self) -> None:
        if self.chat_service.reset():
            self.chat.clear()

    def set_think(self, value: bool) -> None:
        self.save_settings(replace(self.settings, think_enabled=value))

    def save_settings(self, new: Settings) -> None:
        try:
            self._save_fn(new)
        except OSError as exc:
            self.diagnostics.form_message.setText(f"Speichern fehlgeschlagen: {exc}")
            self.log.warning("Einstellungen nicht gespeichert: %s", type(exc).__name__)
            return
        self.settings = new
        self.diagnostics.set_settings(new)
        self.diagnostics.think.setChecked(new.think_enabled)
        self.chat.set_model(new.ollama_model)
        self.chat.set_think(new.think_enabled)
        self.backend.logs_dir = new.trading_logs_dir
        self.timer.start(new.refresh_interval_s * 1000)
        self.log.info("Einstellungen gespeichert")
        self.refresh_diagnostics()

    # -- navigation / shutdown ------------------------------------------------------------------
    def _on_page(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        if row == WATCHLIST_PAGE:
            if self._watchlist_at is None or (datetime.now() - self._watchlist_at).total_seconds() > WATCHLIST_STALE_S:
                self.refresh_watchlist()
        elif row == SECTORS_PAGE:
            if self._sectors_at is None or (datetime.now() - self._sectors_at).total_seconds() > SECTORS_STALE_S:
                self.refresh_sectors()
        elif row == TASKS_PAGE and (self._tasks_at is None or (datetime.now() - self._tasks_at).total_seconds() > 30):
            self.refresh_tasks()
        elif row == CHAT_PAGE:
            self.check_ollama()  # two GET requests (version, model names); no model is loaded by looking
        elif row == DIAGNOSTICS_PAGE:
            self.refresh_diagnostics()
            self.check_ollama()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        try:
            self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self._save_fn(self.settings)
        except Exception:  # noqa: BLE001 - never block closing
            self.log.warning("Fenstergeometrie nicht gespeichert")
        self.timer.stop()
        self.chat_service.shutdown()
        self.mcp.shutdown()
        self.runner.wait(5000)
        super().closeEvent(event)


def main(argv: Optional[list[str]] = None) -> int:
    loaded = settings_mod.load_settings()
    log = settings_mod.configure_logging()
    log.info("GUI-Start (%s)", GUI_VERSION)
    app = QApplication(argv if argv is not None else sys.argv)

    def excepthook(kind, value, trace) -> None:
        log.error("Unbehandelter Fehler: %s", kind.__name__, exc_info=(kind, value, trace))

    sys.excepthook = excepthook
    runner = AsyncRunner()
    mcp = McpService(loaded.settings, settings_mod.mcp_stderr_path())
    window = MainWindow(loaded.settings, mcp, TaskBackend(loaded.settings.trading_logs_dir), runner, [*loaded.warnings, *settings_mod.runtime_warnings(loaded.settings)])
    window.show()
    mcp.start()
    code = app.exec()
    mcp.shutdown()
    log.info("GUI-Ende")
    return code
