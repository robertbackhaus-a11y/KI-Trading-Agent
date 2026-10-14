"""The chat inside the MainWindow (X): page, send flow, new chat, thinking switch, diagnostics, closing while a request runs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import support
from fake_ollama import FakeOllama, Script, call, done, say
from support import FAKE_SERVER, HAVE_QT, pump, qapp, wait_gone

if HAVE_QT:
    from gui.app import CHAT_PAGE, DIAGNOSTICS_PAGE, PAGES, MainWindow
    from gui.services import chat_service as cs
    from gui.services import mcp_client as mc
    from gui.services import ollama_client as oc
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from gui.services.task_service import TASKS, TaskInfo

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")


class Backend:
    logs_dir = "C:\\x"

    def query(self):
        return [TaskInfo(t.name, True, "Ready", None, None, 0, t.log_file) for t in TASKS]

    def start(self, name):
        raise AssertionError("the chat must never start a task")

    def tail(self, name):
        raise AssertionError("not used")


@QT
class ChatWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.fake = FakeOllama().start()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.settings = Settings(ollama_base_url=self.fake.url, ollama_model="fake-model:latest", mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER),
                                 trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.pid_file = self.tmp / "pid.txt"
        self.saved: list = []
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", extra_env={"FAKE_PID_FILE": str(self.pid_file)})
        self.runner = AsyncRunner()
        self.window = MainWindow(self.settings, self.mcp, Backend(), self.runner, [], save_fn=lambda s: self.saved.append(s),
                                 ollama_fn=lambda url: oc.check_ollama(url, 2), chat=cs.ChatService(lambda: self.window.settings, self.mcp))
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        self.window.close()
        self.runner.wait(5000)
        self.fake.stop()
        self.tmpdir.cleanup()

    def start(self) -> None:
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(30, lambda: self.mcp.state == mc.CONNECTED))

    def text(self) -> str:
        return self.window.chat.transcript.toPlainText()

    # X
    def test_x_the_window_has_the_chat_page_between_tasks_and_diagnostics(self) -> None:
        w = self.window
        self.assertEqual(PAGES, ("Dashboard", "Planner / Actions", "Watchlist", "Sektoren", "Tasks", "Chat", "Diagnostics / Settings"))
        self.assertEqual((CHAT_PAGE, DIAGNOSTICS_PAGE), (5, 6))
        w.sidebar.setCurrentRow(CHAT_PAGE)
        self.assertIs(w.stack.currentWidget(), w.chat)
        self.assertIn("fake-model:latest", w.chat.model_label.text())

    def test_opening_the_chat_page_only_checks_reachability_and_loads_no_model(self) -> None:
        self.window.show()
        self.window.sidebar.setCurrentRow(CHAT_PAGE)
        self.assertTrue(pump(10, lambda: "erreichbar" in self.window.chat.ollama_label.text()))
        self.assertEqual([r["path"] for r in self.fake.requests], ["/api/version", "/api/tags"])  # no /api/chat: nothing is loaded by opening the page
        self.assertNotIn("fehlt", self.window.chat.ollama_label.text())
        self.fake.models = ["other:1b"]
        self.window.check_ollama()
        self.assertTrue(pump(10, lambda: "fehlt" in self.window.chat.ollama_label.text()))

    def test_the_send_flow_shows_the_question_tool_status_and_streamed_answer(self) -> None:
        self.start()
        self.fake.scripts.extend([call("database_status", {}), say("Die Datenbank ", "ist in Ordnung.")])
        self.window.sidebar.setCurrentRow(CHAT_PAGE)
        self.window.chat.input.setPlainText("Wie ist der Datenbankstatus?")
        self.window.chat.send_button.click()
        self.assertTrue(pump(30, lambda: "Die Datenbank ist in Ordnung." in self.text() and not self.window.chat_service.busy))
        text = self.text()
        for expected in ("Du\nWie ist der Datenbankstatus?", "▸ Tool database_status()", "✓ database_status:", "Assistent\nDie Datenbank ist in Ordnung.", "Fertig ·"):
            self.assertIn(expected, text)
        self.assertNotIn(str(self.tmp / "trading.db"), text)  # the raw tool result (it contains the db path) is not shown
        self.assertTrue(self.window.chat.send_button.isEnabled() and not self.window.chat.stop_button.isEnabled())
        self.assertEqual(self.window.chat.status.text(), "Bereit")

    def test_stop_button_cancels_and_the_next_question_works(self) -> None:
        self.start()
        self.fake.scripts.append(Script([{"message": {"content": f"{i} "}, "done": False} for i in range(300)] + [done()], delay=0.05))
        c = self.window.chat
        c.input.setPlainText("lang")
        c.send_button.click()
        self.assertTrue(pump(10, lambda: "Assistent" in self.text()))
        self.assertTrue(c.stop_button.isEnabled() and not c.send_button.isEnabled())
        c.stop_button.click()
        self.assertTrue(pump(10, lambda: not self.window.chat_service.busy))
        self.assertIn("Abgebrochen", self.text())
        self.assertEqual(self.mcp.state, mc.CONNECTED)
        self.fake.scripts.append(say("zweite Antwort"))
        c.input.setPlainText("nochmal")
        c.send_button.click()
        self.assertTrue(pump(15, lambda: "zweite Antwort" in self.text()))

    def test_new_chat_clears_the_transcript_and_the_history(self) -> None:
        self.start()
        self.fake.scripts.append(say("antwort"))
        c = self.window.chat
        c.input.setPlainText("frage")
        c.send_button.click()
        self.assertTrue(pump(15, lambda: "antwort" in self.text() and not self.window.chat_service.busy))
        self.assertEqual(len(self.window.chat_service.history), 2)
        c.new_button.click()
        self.assertEqual((self.text(), self.window.chat_service.history), ("", []))

    def test_the_thinking_switch_updates_and_saves_the_settings(self) -> None:
        self.start()
        c = self.window.chat
        c.think_box.setChecked(True)
        self.assertTrue(self.window.settings.think_enabled)
        self.assertTrue(self.window.diagnostics.think.isChecked())
        self.assertEqual(self.saved[-1].think_enabled, True)
        self.fake.scripts.append(say("antwort", thinking=("überlege",)))
        c.input.setPlainText("x")
        c.send_button.click()
        self.assertTrue(pump(15, lambda: "Antwort" in self.text() or "antwort" in self.text()))
        pump(2, lambda: not self.window.chat_service.busy)
        self.assertIn("Denken\nüberlege", self.text())
        self.assertIs(self.fake.chat_requests[0]["think"], True)

    def test_an_error_is_shown_and_remembered_in_the_diagnostics(self) -> None:
        self.start()
        self.fake.scripts.append(Script(status=500, error="runner crashed"))
        c = self.window.chat
        c.input.setPlainText("x")
        c.send_button.click()
        self.assertTrue(pump(15, lambda: "Fehler" in self.text() and not self.window.chat_service.busy))
        self.assertIn("Ollama meldet einen Fehler", self.text())
        self.window.sidebar.setCurrentRow(DIAGNOSTICS_PAGE)
        self.assertTrue(pump(10, lambda: "HTTP_ERROR" in " ".join(l.text() for l in self.window.diagnostics.findChildren(type(self.window.lbl_mcp)))))

    def test_the_diagnostics_show_ollama_the_tool_classes_and_the_schema_hash(self) -> None:
        self.start()
        self.window.sidebar.setCurrentRow(DIAGNOSTICS_PAGE)
        self.assertTrue(pump(10, lambda: self.window._ollama is not None))
        info = " ".join(l.text() for l in self.window.diagnostics.findChildren(type(self.window.lbl_mcp)))
        for expected in ("Ollama erreichbar", "0.0.0-test", "Gewähltes Modell vorhanden", "Tool-Schema-Hash", "Letzter Chat-Fehler", "keiner",
                         "MCP-Tools (READ, dem Chat angeboten)", "MCP-Tools (ACTION oder unbekannt, gesperrt)"):
            self.assertIn(expected, info)
        rows = {l.text(): None for l in self.window.diagnostics.findChildren(type(self.window.lbl_mcp))}
        self.assertTrue(any(len(t) == 16 and all(ch in "0123456789abcdef" for ch in t) for t in rows))  # the 16 character schema hash

    def test_closing_the_window_during_a_request_ends_everything(self) -> None:
        self.start()
        self.fake.scripts.append(Script([{"message": {"content": "x"}, "done": False}] * 300 + [done()], delay=0.1))
        pid = int(self.pid_file.read_text())
        c = self.window.chat
        c.input.setPlainText("lang")
        c.send_button.click()
        self.assertTrue(pump(10, lambda: "Assistent" in self.text()))
        self.window.close()
        self.assertFalse(self.window.chat_service.busy)
        self.assertTrue(wait_gone(pid))


if __name__ == "__main__":
    unittest.main()
