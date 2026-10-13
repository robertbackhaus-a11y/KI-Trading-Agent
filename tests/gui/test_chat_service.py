"""Chat service (Qt) with a fake Ollama and a fake MCP server, the chat view offscreen, and the real MCP server's tool surface (G, J-K, Q-X)."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import support
from fake_ollama import FakeOllama, Script, call, done, say
from support import FAKE_SERVER, HAVE_QT, pump, qapp, wait_gone

if HAVE_QT:
    from gui.services import chat_service as cs
    from gui.services import mcp_client as mc
    from gui.services import ollama_client as oc
    from gui.services import tool_loop as tl
    from gui.services import tool_policy as tp
    from gui.services.settings import Settings
    from gui.views.chat import ChatView

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")


@QT
class ChatServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.fake = FakeOllama().start()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.settings = Settings(ollama_base_url=self.fake.url, ollama_model="fake-model:latest", mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER),
                                 trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.pid_file = self.tmp / "pid.txt"
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", extra_env={"FAKE_PID_FILE": str(self.pid_file)})
        self.chat = cs.ChatService(lambda: self.settings, self.mcp)
        self.events: list = []
        self.busy_log: list[bool] = []
        self.chat.event.connect(self.events.append)
        self.chat.busy_changed.connect(self.busy_log.append)
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        self.chat.shutdown()
        self.mcp.shutdown()
        self.fake.stop()
        self.tmpdir.cleanup()

    def connect(self) -> None:
        self.mcp.start()
        self.assertTrue(pump(30, lambda: self.mcp.state == mc.CONNECTED))

    def idle(self, seconds: float = 30) -> None:
        self.assertTrue(pump(seconds, lambda: not self.chat.busy and self.events and self.events[-1].kind == "done"), "request did not finish")

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    # G
    def test_g_a_full_loop_through_the_real_service_stack(self) -> None:
        self.connect()
        self.fake.scripts.extend([call("database_status", {}), say("Alles ", "ok.")])
        self.assertTrue(self.chat.send("Wie ist der Datenbankstatus?"))
        self.assertTrue(self.chat.busy)
        self.idle()
        self.assertEqual([k for k in self.kinds() if k != "status"], ["tool_call", "tool_result", "content", "content", "done"])
        result = next(e for e in self.events if e.kind == "tool_result")
        self.assertTrue(result.data["ok"] and result.data["chars"] > 0)
        self.assertEqual(self.events[-1].data["outcome"], "done")
        self.assertEqual([m["role"] for m in self.chat.history], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(self.busy_log, [True, False])
        sent = [t["function"]["name"] for t in self.fake.chat_requests[0]["tools"]]
        self.assertEqual(len(sent), 4)  # the fake server's READ-class names; its other tools (sql_execute, slow, crash ...) are never offered
        self.assertTrue(set(sent) <= {n for n in self.mcp.tools if tp.is_read(n)})

    def test_the_tools_offered_are_the_discovered_read_tools_only(self) -> None:
        self.connect()
        self.fake.scripts.append(say("ok"))
        self.chat.send("x")
        self.idle()
        sent = {t["function"]["name"] for t in self.fake.chat_requests[0]["tools"]}
        self.assertEqual(sent, {"database_status", "run_trading_orchestrator", "rank_watchlist", "get_watchlist"})  # the fake's READ names; sql_execute (ACTION) and the made-up ones are not offered
        self.assertNotIn("sql_execute", sent)

    def test_m_n_o_an_action_call_never_reaches_the_mcp_server(self) -> None:
        self.connect()
        self.fake.scripts.extend([call("sql_execute", {"sql": "DROP TABLE x"}), call("slow", {"seconds": 0}), say("ok")])
        self.chat.send("lösche alles")
        self.idle()
        results = [e for e in self.events if e.kind == "tool_result"]
        self.assertEqual([(e.text, e.data["kind"]) for e in results], [("sql_execute", "TOOL_NOT_ALLOWED"), ("slow", "TOOL_NOT_ALLOWED")])  # "slow" is not a READ tool either
        text = json.dumps(self.fake.chat_requests[1:])
        self.assertNotIn("executed", text)  # the fake sql_execute would have answered {"executed": ...}
        self.assertTrue(all("TOOL_NOT_ALLOWED" in m["content"] for r in self.fake.chat_requests[1:2] for m in r["messages"] if m["role"] == "tool"))

    # Q
    def test_q_an_mcp_timeout_is_reported_and_the_chat_continues(self) -> None:
        self.connect()
        self.fake.scripts.extend([call("rank_watchlist", {"seconds": 5}), say("trotzdem fertig")])
        with patch.object(cs, "TOOL_TIMEOUT_S", 0.5):
            self.chat.send("rank")
            started = time.perf_counter()
            self.idle()
        self.assertLess(time.perf_counter() - started, 8)
        result = next(e for e in self.events if e.kind == "tool_result")
        self.assertEqual((result.data["ok"], result.data["kind"]), (False, "TIMEOUT"))
        self.assertEqual(self.events[-1].data["outcome"], "done")

    def test_mcp_not_connected_is_a_clear_error_and_the_chat_stays_usable(self) -> None:
        self.assertFalse(self.chat.send("hallo"))
        self.assertEqual([e.kind for e in self.events], ["error", "done"])
        self.assertEqual(self.events[0].data["kind"], "MCP_NOT_CONNECTED")
        self.assertFalse(self.chat.busy)
        self.assertIn("MCP_NOT_CONNECTED", self.chat.last_error)
        self.assertEqual(self.fake.chat_requests, [])  # Ollama was not even asked

    def test_ollama_unreachable_is_reported_without_crashing(self) -> None:
        self.connect()
        chat = cs.ChatService(lambda: self.settings, self.mcp, client_factory=lambda url: oc.OllamaClient("http://127.0.0.1:1", timeout=1))
        events: list = []
        chat.event.connect(events.append)
        self.assertTrue(chat.send("hallo"))
        self.assertTrue(pump(15, lambda: events and events[-1].kind == "done"))
        self.assertEqual(next(e for e in events if e.kind == "error").data["kind"], oc.UNREACHABLE)
        self.assertEqual((chat.busy, chat.history), (False, []))
        self.assertIn("UNREACHABLE", chat.last_error)
        chat.shutdown()

    # R
    def test_r_http_500_ends_the_request_and_the_next_one_works(self) -> None:
        self.connect()
        self.fake.scripts.extend([Script(status=500, error="runner crashed"), say("zweiter Versuch")])
        self.chat.send("eins")
        self.idle()
        error = next(e for e in self.events if e.kind == "error")
        self.assertEqual((error.data["kind"], error.data["status"]), (oc.HTTP_ERROR, 500))
        self.assertEqual(self.chat.history, [])
        self.assertIn("HTTP_ERROR", self.chat.last_error)
        self.events.clear()
        self.chat.send("zwei")
        self.idle()
        self.assertEqual(self.chat.history[-1]["content"], "zweiter Versuch")

    # J / K / T
    def test_j_k_t_stop_cancels_only_this_request_and_one_request_runs_at_a_time(self) -> None:
        self.connect()
        self.fake.scripts.append(Script([{"message": {"content": f"{i} "}, "done": False} for i in range(300)] + [done()], delay=0.05))
        self.assertTrue(self.chat.send("lang"))
        self.assertTrue(pump(10, lambda: any(e.kind == "content" for e in self.events)))
        self.assertFalse(self.chat.send("noch eine"))  # T: only one active request
        self.assertTrue(any(e.kind == "warning" and "bereits" in e.text for e in self.events))
        self.assertFalse(self.chat.reset())  # not while running
        started = time.perf_counter()
        self.chat.cancel()
        self.idle(10)
        self.assertLess(time.perf_counter() - started, 3)
        self.assertEqual(self.events[-1].data["outcome"], "cancelled")
        self.assertEqual(self.chat.history, [])  # the cancelled turn is not part of the history
        self.assertEqual(self.mcp.state, mc.CONNECTED)  # MCP keeps running
        self.assertEqual(len(self.fake.chat_requests), 1)
        # K: a follow-up works
        self.events.clear()
        self.fake.scripts.append(say("wieder da"))
        self.assertTrue(self.chat.send("nochmal"))
        self.idle()
        self.assertEqual(self.chat.history[-1]["content"], "wieder da")
        self.assertEqual(self.busy_log, [True, False, True, False])

    # S
    def test_s_history_lives_in_memory_and_new_chat_resets_it(self) -> None:
        self.connect()
        before = sorted(p.name for p in self.tmp.iterdir())
        self.fake.scripts.extend([say("a"), say("b")])
        self.chat.send("1")
        self.idle()
        self.events.clear()
        self.chat.send("2")
        self.idle()
        self.assertEqual([m["role"] for m in self.chat.history], ["user", "assistant", "user", "assistant"])
        self.assertEqual([m["role"] for m in self.fake.chat_requests[1]["messages"]], ["system", "user", "assistant", "user"])  # the second request carried the first turn
        self.assertTrue(self.chat.reset())
        self.assertEqual((self.chat.history, self.chat.last_error), ([], ""))
        self.fake.scripts.append(say("c"))
        self.events.clear()
        self.chat.send("3")
        self.idle()
        self.assertEqual([m["role"] for m in self.fake.chat_requests[2]["messages"]], ["system", "user"])  # nothing of the old chat
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before)  # the chat wrote nothing to disk

    def test_thinking_switch_and_no_context_option(self) -> None:
        self.connect()
        self.settings = replace(self.settings, think_enabled=True)
        self.fake.scripts.append(say("antwort", thinking=("überlege",)))
        self.chat.send("x")
        self.idle()
        self.assertIn("thinking", self.kinds())
        self.assertIs(self.fake.chat_requests[0]["think"], True)
        self.assertNotIn("options", self.fake.chat_requests[0])
        self.assertNotIn("keep_alive", self.fake.chat_requests[0])
        self.assertEqual([r["path"] for r in self.fake.requests], ["/api/chat"])  # nothing else was requested: no model loading, no pull

    def test_the_gui_thread_stays_responsive_while_streaming(self) -> None:
        self.connect()
        self.fake.scripts.append(Script([{"message": {"content": "x"}, "done": False} for _ in range(40)] + [done()], delay=0.05))
        self.chat.send("stream")
        gaps, last = [], time.perf_counter()
        end = time.time() + 10
        while self.chat.busy and time.time() < end:
            qapp().processEvents()
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
            time.sleep(0.01)
        self.assertFalse(self.chat.busy)
        self.assertGreater(len(gaps), 50)
        self.assertLess(max(gaps), 0.25)

    def test_the_loop_runs_in_a_worker_thread_and_events_arrive_in_the_gui_thread(self) -> None:
        self.connect()
        seen: list = []
        self.chat.event.connect(lambda e: seen.append(threading.get_ident()))
        self.fake.scripts.append(say("a"))
        self.chat.send("x")
        self.idle()
        self.assertTrue(seen and set(seen) == {threading.get_ident()})

    def test_logging_names_models_and_rounds_but_never_the_content(self) -> None:
        import logging

        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logging.getLogger("tradinggui").addHandler(handler)
        logging.getLogger("tradinggui").setLevel(logging.INFO)
        try:
            self.connect()
            self.fake.scripts.extend([call("database_status", {}), say("GEHEIMER-ANTWORTTEXT")])
            self.chat.send("GEHEIME-FRAGE")
            self.idle()
        finally:
            logging.getLogger("tradinggui").removeHandler(handler)
        text = "\n".join(records)
        self.assertIn("Ollama-Anfrage gestartet: Modell fake-model:latest", text)
        self.assertIn("Tool database_status: ok", text)
        self.assertIn("Ollama-Anfrage beendet: done, 1 Tool-Runden", text)
        for secret in ("GEHEIME-FRAGE", "GEHEIMER-ANTWORTTEXT", str(self.tmp / "trading.db")):
            self.assertNotIn(secret, text)

    def test_shutdown_while_a_request_runs_does_not_hang(self) -> None:
        self.connect()
        self.fake.scripts.append(Script([{"message": {"content": "x"}, "done": False}] * 200 + [done()], delay=0.1))
        self.chat.send("lang")
        pump(5, lambda: any(e.kind == "content" for e in self.events))
        started = time.perf_counter()
        self.chat.shutdown()
        self.assertLess(time.perf_counter() - started, 5)


@QT
class ChatViewTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.view = ChatView("fake-model:latest", think=False)

    def text(self) -> str:
        return self.view.transcript.toPlainText()

    def test_u_message_types_are_shown_distinctly_and_streaming_is_one_block(self) -> None:
        v = self.view
        v.add_user("Frage?")
        v.handle_event(tl.LoopEvent("tool_call", "database_status", {"arguments": {}, "allowed": True}))
        v.handle_event(tl.LoopEvent("tool_result", "database_status", {"ok": True, "kind": "OK", "seconds": 0.14, "chars": 9600}))
        for chunk in ("Hal", "lo ", "Welt"):
            v.handle_event(tl.LoopEvent("content", chunk))
        v.handle_event(tl.LoopEvent("error", "boom", {"kind": "HTTP_ERROR"}))
        v.handle_event(tl.LoopEvent("done", data={"outcome": "error", "seconds": 1.5, "rounds": 1, "tool_calls": 1, "prompt_tokens": 100, "eval_tokens": 20}))
        text = self.text()
        for expected in ("Du\nFrage?", "▸ Tool database_status()", "✓ database_status: 0,14 s, 9,6k Zeichen", "Assistent\nHallo Welt", "Fehler", "Ollama meldet einen Fehler", "Fehler · 1,5 s · 1 Tool-Runde, 1 Aufruf · 120 Tokens"):
            self.assertIn(expected, text)
        self.assertEqual(text.count("Assistent"), 1)  # three chunks, one block

    def test_thinking_is_only_shown_when_switched_on_and_is_separate(self) -> None:
        v = self.view
        v.handle_event(tl.LoopEvent("thinking", "geheimes Überlegen"))
        self.assertNotIn("Überlegen", self.text())
        v.set_think(True)
        v.handle_event(tl.LoopEvent("thinking", "ich überlege"))
        v.handle_event(tl.LoopEvent("content", "Antwort"))
        self.assertIn("Denken\nich überlege", self.text())
        self.assertLess(self.text().index("Denken"), self.text().index("Assistent"))

    def test_refused_tool_calls_and_big_results_are_marked(self) -> None:
        v = self.view
        v.handle_event(tl.LoopEvent("tool_call", "sql_execute", {"arguments": {"sql": "x" * 200}, "allowed": False}))
        v.handle_event(tl.LoopEvent("tool_result", "sql_execute", {"ok": False, "kind": "TOOL_NOT_ALLOWED", "seconds": 0, "chars": 0, "note": "Aktion – nicht erlaubt"}))
        v.handle_event(tl.LoopEvent("tool_result", "rank_watchlist", {"ok": False, "kind": "TOO_LARGE", "seconds": 0.2, "chars": 50_000, "note": "zu groß"}))
        text = self.text()
        self.assertIn("nicht erlaubt", text)
        self.assertIn("✗ sql_execute: TOOL_NOT_ALLOWED", text)
        self.assertIn("…", text)  # a long argument is shortened
        self.assertNotIn("x" * 100, text)
        self.assertNotIn('"rows"', text)  # no raw tool result anywhere

    def test_controls_send_stop_busy_new_chat_and_think(self) -> None:
        v = self.view
        sent, stops, news, thinks = [], [], [], []
        v.send_requested.connect(sent.append)
        v.stop_requested.connect(lambda: stops.append(1))
        v.new_chat_requested.connect(lambda: news.append(1))
        v.think_toggled.connect(thinks.append)
        self.assertFalse(v.stop_button.isEnabled())
        v.input.setPlainText("  Hallo  ")
        v.send_button.click()
        self.assertEqual((sent, v.input.toPlainText()), (["Hallo"], ""))
        v.send_button.click()  # empty input: nothing is sent
        self.assertEqual(sent, ["Hallo"])
        v.set_busy(True)
        self.assertEqual((v.send_button.isEnabled(), v.stop_button.isEnabled(), v.new_button.isEnabled()), (False, True, False))
        v.input.setPlainText("zweite")
        v.send_button.click()
        self.assertEqual(sent, ["Hallo"])  # no send while busy
        v.stop_button.click()
        v.set_busy(False)
        v.new_button.click()
        v.think_box.setChecked(True)
        self.assertEqual((stops, news, thinks), ([1], [1], [True]))
        self.assertIn("fake-model:latest", v.model_label.text())
        v.set_ollama("nicht erreichbar", False)
        self.assertIn("nicht erreichbar", v.ollama_label.text())
        v.add_user("x")
        v.clear()
        self.assertEqual(self.text(), "")


@QT
class RealServerToolSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """The tool surface of the REAL Trading MCP server (repository copy; it only lists tools, no database is touched)."""

    async def test_v_w_the_19_real_tools_map_to_14_offered_ones(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        core = mc.McpClientCore(sys.executable, str(support.ROOT / "mcp-tools" / "server.py"), str(Path(tmp.name) / "none.db"), Path(tmp.name) / "e.log")
        try:
            self.assertTrue(await core.start(), core.detail)
            catalog = core.tools
            self.assertEqual(len(catalog), 19)
            tools, schemas = tl.offered_tools(catalog)
            names = [t["function"]["name"] for t in tools]
            self.assertEqual(len(names), 14)  # the 13 READ tools of before and get_watchlist
            self.assertEqual(sorted(names), sorted(n for n in catalog if tp.is_read(n)))
            self.assertFalse({"sql_execute", "approve_swing_promotion", "add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority"} & set(names))
            for tool in tools:
                fn = tool["function"]
                original = catalog[fn["name"]]
                self.assertEqual(fn["description"], original["description"] or fn["name"])  # passed through
                expected = json.loads(json.dumps(original["input_schema"]))
                expected.get("properties", {}).pop("detail", None)
                if "required" in expected:
                    expected["required"] = [r for r in expected["required"] if r != "detail"]
                self.assertEqual(fn["parameters"], expected, fn["name"])
            self.assertEqual({n for n in catalog if "detail" in catalog[n]["input_schema"].get("properties", {})}, {"run_trading_orchestrator", "get_market_intelligence", "get_opportunity_view", "get_candidate_discovery", "get_watchlist"})
            # the validator accepts what the real schemas describe and rejects what they do not
            errors: list[str] = []
            self.assertEqual(tl.validate_arguments(schemas["get_market_intelligence"], {"symbol": "AAA", "limit": "3", "importance": None}, errors), {"symbol": "AAA", "limit": 3, "importance": None})
            self.assertEqual(errors, [])
            tl.validate_arguments(schemas["table_info"], {}, errors)
            tl.validate_arguments(schemas["get_market_intelligence"], {"limit": "many"}, errors)
            self.assertEqual(len(errors), 2)
            self.assertEqual(len(tl.schema_hash(catalog)), 16)
        finally:
            await core.stop()


if __name__ == "__main__":
    unittest.main()
