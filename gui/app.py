"""Desktop GUI of the Trading Agent: main window, wiring and entry point (``python -m gui``).

Read-only cockpit.  Trading data comes only from the Trading MCP server (READ tools, see ``services/tool_policy.py``); the Windows
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
from .viewmodels.portfolio import DASH, build_dashboard, build_planner, fmt_time
from .viewmodels.tasks import build_task_rows
from .views.chat import ChatView
from .views.dashboard import DashboardView
from .views.diagnostics import DiagnosticsView
from .views.planner import PlannerView
from .views.tasks import TasksView

PAGES = ("Dashboard", "Planner / Actions", "Tasks", "Chat", "Diagnostics / Settings")
CHAT_PAGE, DIAGNOSTICS_PAGE = 3, 4
STATE_TEXT = {"CONNECTED": "verbunden", "CONNECTING": "verbindet …", "DISCONNECTED": "getrennt", "ERROR": "Fehler"}


class MainWindow(QMainWindow):
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
        self.setWindowTitle(f"Trading Agent – Desktop GUI ({GUI_VERSION})")
        self.resize(1200, 760)
        if settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(settings.window_geometry.encode("ascii", "ignore")))

        central = QWidget()
        layout = QHBoxLayout(central)
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(190)
        self.sidebar.addItems(PAGES)
        self.stack = QStackedWidget()
        self.dashboard, self.planner, self.tasks = DashboardView(), PlannerView(), TasksView()
        self.chat = ChatView(settings.ollama_model, settings.think_enabled)
        self.diagnostics = DiagnosticsView(settings)
        for page in (self.dashboard, self.planner, self.tasks, self.chat, self.diagnostics):
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
        self.tasks.refresh_requested.connect(self.refresh_tasks)
        self.tasks.run_requested.connect(self.run_task)
        self.tasks.log_requested.connect(self.show_task_log)
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

    def _tick(self) -> None:
        self.refresh_status()
        page = self.stack.currentIndex()
        if page in (0, 1):
            self.refresh_orchestrator()
        elif page == 2:
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

    def _on_tasks(self, ok: bool, value) -> None:
        self._inflight.discard("tasks")
        if not ok:
            self.log.warning("Task-Abfrage fehlgeschlagen: %s", type(value).__name__)
            self.tasks.show_error(f"Scheduler-Abfrage fehlgeschlagen: {value}")
            return
        self._tasks_at = datetime.now()
        self.tasks.show_rows(build_task_rows(value), fmt_time(self._tasks_at))

    def run_task(self, name: str) -> None:
        self.log.info("Task-Start angefordert: %s", name)
        self.tasks.show_message(f"Starte {name} …")

        def done(ok: bool, value) -> None:
            if not ok:
                self.log.warning("Task-Start %s fehlgeschlagen: %s", name, type(value).__name__)
                self.tasks.show_error(f"Start fehlgeschlagen: {value}")
                return
            started, message = value
            self.log.info("Task-Start %s: %s", name, "ok" if started else message)
            self.tasks.show_message(message) if started else self.tasks.show_error(message)
            self.statusBar().showMessage(message, 10000)
            QTimer.singleShot(3000, self.refresh_tasks)

        self.runner.submit(lambda: self.backend.start(name), done)

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
        if row == 2 and (self._tasks_at is None or (datetime.now() - self._tasks_at).total_seconds() > 30):
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
    window = MainWindow(loaded.settings, mcp, TaskBackend(loaded.settings.trading_logs_dir), runner, loaded.warnings)
    window.show()
    mcp.start()
    code = app.exec()
    mcp.shutdown()
    log.info("GUI-Ende")
    return code
