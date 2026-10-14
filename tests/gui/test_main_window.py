"""MainWindow offscreen smoke (R) with a fake MCP server and a fake task backend: pages, wiring, run-now flow, errors, shutdown."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp, wait_gone

if HAVE_QT:
    from PySide6.QtWidgets import QPushButton

    from gui.app import DIAGNOSTICS_PAGE, PAGES, TASKS_PAGE, WATCHLIST_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from gui.services.task_service import LogTail, TaskInfo, TASKS
    from gui.viewmodels.portfolio import DASH

    import fixtures

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")


class FakeBackend:
    def __init__(self, tasks=None, start_result=(True, "gestartet")):
        self.tasks = tasks if tasks is not None else [TaskInfo(t.name, True, "Ready", None, None, 0, t.log_file) for t in TASKS]
        self.start_result, self.started, self.logs_dir, self.fail_query = start_result, [], "C:\\x", False

    def query(self):
        if self.fail_query:
            raise RuntimeError("PowerShell kaputt")
        return self.tasks

    def start(self, name):
        self.started.append(name)
        return self.start_result

    def tail(self, name):
        return LogTail(True, f"letzte Zeile von {name}", False, 2048)


@QT
class MainWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.pid_file = self.tmp / "pid.txt"
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        self.settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.saved: list = []
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", extra_env={"FAKE_PID_FILE": str(self.pid_file), "FAKE_ORCH_FILE": str(self.tmp / "orch.json")}, ping_interval=2)
        self.backend, self.runner = FakeBackend(), AsyncRunner()
        self.window = MainWindow(self.settings, self.mcp, self.backend, self.runner, ["Einstellung x ungültig"], save_fn=lambda s: self.saved.append(s),
                                 ollama_fn=lambda url: __import__("gui.services.ollama_client", fromlist=["x"]).OllamaStatus(True, "0.35.0", ["m1", "m2"]))
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        self.window.close()
        self.runner.wait(5000)
        self.tmpdir.cleanup()

    def start(self) -> None:
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None), "dashboard data did not arrive")

    # R
    def test_r_the_window_builds_with_seven_pages_including_watchlist_and_chat_and_a_status_bar(self) -> None:
        w = self.window
        w.show()
        self.assertEqual(w.sidebar.count(), 7)
        self.assertEqual([w.sidebar.item(i).text() for i in range(7)], list(PAGES))
        self.assertEqual((PAGES[WATCHLIST_PAGE], PAGES[3], PAGES[4], PAGES[5]), ("Watchlist", "Sektoren", "Tasks", "Chat"))
        self.assertEqual(w.stack.count(), 7)
        self.assertEqual((w.lbl_mcp.text(), w.lbl_tools.text(), w.lbl_db.text(), w.lbl_refresh.text()), ("MCP: getrennt", "Tools: —", "DB: —", "Letzter Refresh: —"))
        self.assertIn("Desktop GUI (dev)", w.windowTitle())
        for row in range(7):
            w.sidebar.setCurrentRow(row)
            self.assertEqual(w.stack.currentIndex(), row)
        pixmap = w.grab()
        self.assertFalse(pixmap.isNull())
        self.assertIn("Einstellung x ungültig", w.diagnostics.warnings.text())

    def test_connecting_loads_dashboard_planner_and_status(self) -> None:
        self.start()
        w = self.window
        self.assertTrue(pump(20, lambda: w.lbl_db.text() == "DB: OK"))
        self.assertEqual((w.lbl_mcp.text(), w.lbl_tools.text()), ("MCP: verbunden", f"Tools: {self.mcp.tool_count}"))
        self.assertNotEqual(w.lbl_refresh.text(), "Letzter Refresh: —")
        self.assertEqual(w.dashboard.planned.rowCount(), 1)
        self.assertEqual(w.dashboard.planned.item(0, 1).text(), "BBB")
        self.assertEqual(w.dashboard.message.text(), "")
        self.assertGreater(w.planner.tables["candidates"].rowCount(), 0)
        self.assertIn("Geplante Entries (1)", w.planner.tabs.tabText(1))
        self.assertTrue(w.dashboard.button.isEnabled() and w.planner.button.isEnabled())

    def test_the_planner_page_has_no_action_buttons(self) -> None:
        self.start()
        texts = [b.text().lower() for b in self.window.planner.findChildren(QPushButton)]
        self.assertEqual(texts, ["aktualisieren"])
        for page in (self.window.dashboard, self.window.diagnostics):
            for button in page.findChildren(QPushButton):
                for word in ("kaufen", "verkaufen", "order", "approve", "promote"):
                    self.assertNotIn(word, button.text().lower())

    def test_tasks_page_run_now_needs_the_confirmation(self) -> None:
        self.start()
        w = self.window
        w.sidebar.setCurrentRow(TASKS_PAGE)
        self.assertTrue(pump(10, lambda: w.tasks.table.rowCount() == 6 and len(w.tasks.run_buttons) == 6))
        name = TASKS[5].name
        button = w.tasks.run_buttons[name]
        self.assertTrue(button.isEnabled())
        asked: list[str] = []
        w.tasks.confirm = lambda n: (asked.append(n), False)[1]  # the user declines
        button.click()
        pump(0.5)
        self.assertEqual((asked, self.backend.started), ([name], []))
        w.tasks.confirm = lambda n: (asked.append(n), True)[1]  # the user confirms
        button.click()
        self.assertTrue(pump(10, lambda: self.backend.started == [name]))
        self.assertEqual(self.backend.started, [name])
        self.assertTrue(pump(10, lambda: "gestartet" in w.tasks.message.text()))

    def test_the_confirmation_dialog_names_the_task_and_defaults_to_cancel(self) -> None:
        from gui.views.tasks import TasksView

        text = TasksView._ask_text("Trading-Market-Intelligence")
        self.assertIn("Trading-Market-Intelligence", text)
        self.assertIn("Aktion", text)

    def test_a_running_task_has_a_disabled_button(self) -> None:
        self.backend.tasks = [TaskInfo(t.name, True, "Running" if i == 1 else "Ready", None, None, 267009 if i == 1 else 0, t.log_file) for i, t in enumerate(TASKS)]
        self.start()
        w = self.window
        w.sidebar.setCurrentRow(TASKS_PAGE)
        self.assertTrue(pump(10, lambda: len(w.tasks.run_buttons) == 6))
        self.assertFalse(w.tasks.run_buttons[TASKS[1].name].isEnabled())
        self.assertTrue(w.tasks.run_buttons[TASKS[0].name].isEnabled())
        self.assertTrue(w.tasks.table.item(1, 2).text().startswith("läuft"))

    def test_a_failed_scheduler_query_is_shown_not_raised(self) -> None:
        self.backend.fail_query = True
        self.start()
        self.window.sidebar.setCurrentRow(TASKS_PAGE)
        self.assertTrue(pump(10, lambda: "Scheduler-Abfrage fehlgeschlagen" in self.window.tasks.message.text()))

    def test_log_viewer_loads_in_the_background(self) -> None:
        self.start()
        self.window.show_task_log(TASKS[0].name)
        self.assertTrue(pump(10, lambda: "letzte Zeile" in self.window.tasks.log.toPlainText()))
        self.assertIn(TASKS[0].name, self.window.tasks.log_title.text())

    def test_diagnostics_shows_paths_tools_and_ollama(self) -> None:
        self.start()
        w = self.window
        w.sidebar.setCurrentRow(DIAGNOSTICS_PAGE)
        pump(1)
        info = " ".join(label.text() for label in w.diagnostics.findChildren(type(w.lbl_mcp)))
        for expected in (str(FAKE_SERVER), sys.executable, str(self.tmp / "trading.db"), "dev", "settings.json", "gui.log", "mcp-stderr.log"):
            self.assertIn(expected, info)
        classes = {w.diagnostics.tools.item(r, 0).text(): w.diagnostics.tools.item(r, 1).text() for r in range(w.diagnostics.tools.rowCount())}
        self.assertEqual(classes["database_status"], "READ")
        self.assertEqual(classes["sql_execute"], "ACTION")
        w.check_ollama()
        self.assertTrue(pump(10, lambda: "0.35.0" in w.diagnostics.ollama.text()))
        self.assertIn("m1, m2", w.diagnostics.ollama.text())

    def test_restart_mcp_from_diagnostics_starts_a_new_child(self) -> None:
        self.start()
        first = int(self.pid_file.read_text())
        self.pid_file.unlink()
        self.window.diagnostics.restart.click()
        self.assertTrue(pump(30, lambda: self.pid_file.exists() and self.mcp.state == mc.CONNECTED))
        self.assertNotEqual(int(self.pid_file.read_text()), first)
        self.assertTrue(wait_gone(first))

    def test_saving_settings_validates_and_applies(self) -> None:
        w = self.window
        d = w.diagnostics
        d.edits["trading_db_path"].setText("relative\\db.sqlite")
        d._save()
        self.assertEqual(self.saved, [])
        self.assertIn("absoluten Pfad", d.form_message.text())
        d.edits["trading_db_path"].setText(str(self.tmp / "other.db"))
        d.interval.setValue(300)
        d._save()
        self.assertEqual(len(self.saved), 1)
        self.assertEqual((w.settings.trading_db_path, w.settings.refresh_interval_s), (str(self.tmp / "other.db"), 300))
        self.assertEqual(w.timer.interval(), 300_000)

    def test_a_dead_mcp_server_shows_an_error_and_no_crash(self) -> None:
        w = self.window
        bad = Settings(mcp_python_path=sys.executable, mcp_server_path=str(self.tmp / "missing.py"), trading_db_path=str(self.tmp / "t.db"))
        service = mc.McpService(bad, self.tmp / "e.log")
        other = MainWindow(bad, service, FakeBackend(), AsyncRunner(), [], save_fn=lambda s: None)
        other.show()
        service.start()
        self.assertTrue(pump(20, lambda: other.lbl_mcp.text() == "MCP: Fehler"))
        other.refresh_orchestrator()
        self.assertTrue(pump(10, lambda: "Keine Daten" in other.dashboard.message.text()))
        self.assertIn("nicht gefunden", other.dashboard.message.text())
        self.assertTrue(other.dashboard.button.isEnabled())  # retry is possible
        other.close()

    def test_a_failing_refresh_keeps_the_last_good_view(self) -> None:
        self.start()
        w = self.window
        self.assertEqual(w.dashboard.planned.rowCount(), 1)
        w._on_orchestrator(mc.McpResult("run_trading_orchestrator", False, error="Zeitüberschreitung nach 120 s", kind="TIMEOUT"))
        self.assertEqual(w.dashboard.planned.rowCount(), 1)  # still showing the last data
        self.assertIn("Zeitüberschreitung", w.dashboard.message.text())
        self.assertIn("Stand von", w.planner.message.text())

    def test_closing_the_window_ends_the_child_process(self) -> None:
        self.start()
        pid = int(self.pid_file.read_text())
        self.window.close()
        self.assertTrue(wait_gone(pid))
        self.assertEqual(self.mcp.state, mc.DISCONNECTED)
        self.assertTrue(self.saved)  # the window geometry was saved


if __name__ == "__main__":
    unittest.main()
