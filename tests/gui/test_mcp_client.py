"""MCP client: the official SDK against a fake stdio server (E-J), plus the Qt service (threads, callbacks, policy, responsiveness)."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import support
from support import FAKE_SERVER, HAVE_QT, pid_alive, pump, qapp, wait_gone

if HAVE_QT:
    from gui.services import mcp_client as mc
    from gui.services.settings import Settings

FAKE_TOOLS = {"database_status", "run_trading_orchestrator", "rank_watchlist", "slow", "crash", "broken", "failing", "sql_execute",
              "get_watchlist", "add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "onboard_security", "set_security_strategy", "set_security_sector"}


def make_core(tmp: Path, **kw):
    pid_file = tmp / "pid.txt"
    core = mc.McpClientCore(
        sys.executable, str(FAKE_SERVER), str(tmp / "trading.db"), tmp / "mcp-stderr.log", connect_timeout=30, ping_interval=kw.pop("ping_interval", 5.0),
        extra_env={"FAKE_PID_FILE": str(pid_file), **kw.pop("env", {})}, **kw,
    )
    return core, pid_file


def read_pid(path: Path) -> int:
    for _ in range(100):
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError("fake server wrote no pid")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")
class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.addCleanup(self.tmpdir.cleanup)

    # E
    async def test_e_starts_the_fake_stdio_server_with_absolute_paths_and_captures_stderr(self) -> None:
        core, pid_file = make_core(self.tmp)
        try:
            self.assertTrue(await core.start())
            self.assertEqual((core.state, core.detail), (mc.CONNECTED, f"{len(FAKE_TOOLS)} Tools"))
            res = await core.call("database_status")
            self.assertTrue(res.tool_ok)
            self.assertEqual(res.data["database_path"], str(self.tmp / "trading.db"))  # passed to the child unchanged and absolute
            self.assertEqual(res.data["user"], "qt-gui")
            self.assertTrue(pid_alive(read_pid(pid_file)))
        finally:
            await core.stop()
        self.assertIn("fake-mcp ready", (self.tmp / "mcp-stderr.log").read_text(encoding="utf-8"))  # stderr went to the own log file

    async def test_relative_or_missing_paths_are_an_error_state_not_a_crash(self) -> None:
        core = mc.McpClientCore(sys.executable, str(FAKE_SERVER), "relative\\trading.db", self.tmp / "e.log")
        self.assertFalse(await core.start())
        self.assertEqual(core.state, mc.ERROR)
        self.assertIn("nicht absolut", core.detail)
        core = mc.McpClientCore(sys.executable, str(self.tmp / "missing_server.py"), str(self.tmp / "t.db"), self.tmp / "e.log")
        self.assertFalse(await core.start())
        self.assertIn("nicht gefunden", core.detail)
        res = await core.call("database_status")
        self.assertEqual((res.ok, res.kind), (False, "NOT_CONNECTED"))

    # F
    async def test_f_tool_discovery(self) -> None:
        core, _ = make_core(self.tmp)
        try:
            await core.start()
            self.assertEqual(set(core.tools), FAKE_TOOLS)
            self.assertIn("input_schema", core.tools["slow"])
            self.assertEqual(core.tools["slow"]["input_schema"]["properties"]["seconds"]["type"], "number")
        finally:
            await core.stop()

    # G
    async def test_g_tool_call_results_and_errors(self) -> None:
        core, _ = make_core(self.tmp)
        try:
            await core.start()
            ok = await core.call("run_trading_orchestrator", {"detail": True})
            self.assertTrue(ok.ok and ok.tool_ok)
            self.assertEqual((ok.data["detail"], ok.kind), (True, "OK"))
            self.assertGreater(ok.chars, 0)
            self.assertEqual((await core.call("no_such_tool")).kind, "TOOL_ERROR")
            bad_args = await core.call("slow", {"seconds": "many"})
            self.assertEqual((bad_args.ok, bad_args.kind), (False, "TOOL_ERROR"))
            invalid = await core.call("broken")
            self.assertEqual((invalid.ok, invalid.kind), (False, "INVALID_RESULT"))
            failing = await core.call("failing")
            self.assertTrue(failing.ok)  # the call itself worked ...
            self.assertFalse(failing.tool_ok)  # ... but the tool reports a failure
            self.assertIn("synthetic failure", failing.message)
            many = await asyncio.gather(*[core.call("database_status") for _ in range(8)])
            self.assertTrue(all(r.tool_ok for r in many))
        finally:
            await core.stop()

    # H
    async def test_h_timeout_does_not_break_the_session(self) -> None:
        core, _ = make_core(self.tmp)
        try:
            await core.start()
            started = time.perf_counter()
            res = await core.call("slow", {"seconds": 5}, timeout=0.5)
            self.assertEqual((res.ok, res.kind), (False, "TIMEOUT"))
            self.assertLess(time.perf_counter() - started, 4)
            self.assertTrue((await core.call("database_status")).tool_ok)  # still usable
            self.assertEqual(core.state, mc.CONNECTED)
        finally:
            await core.stop()

    # I
    async def test_i_restart_starts_a_new_child_process(self) -> None:
        core, pid_file = make_core(self.tmp)
        try:
            await core.start()
            first = read_pid(pid_file)
            pid_file.unlink()
            self.assertTrue(await core.restart())
            second = read_pid(pid_file)
            self.assertNotEqual(first, second)
            self.assertTrue(wait_gone(first))  # the old child is gone
            self.assertTrue((await core.call("database_status")).tool_ok)
        finally:
            await core.stop()

    async def test_i_a_dying_child_is_reported_and_the_next_call_reconnects(self) -> None:
        core, pid_file = make_core(self.tmp)
        try:
            await core.start()
            crashed = await core.call("crash", timeout=10)
            self.assertFalse(crashed.ok)
            self.assertIn(crashed.kind, ("TRANSPORT", "TIMEOUT", "TOOL_ERROR"))
            self.assertTrue(await _until(lambda: core.state == mc.ERROR, 15), core.state)
            self.assertTrue(wait_gone(read_pid(pid_file)))
            with patch.object(mc, "RECONNECT_MIN_INTERVAL_S", 0.0):
                again = await core.call("database_status")
            self.assertTrue(again.tool_ok)  # lazy reconnect
            self.assertEqual(core.state, mc.CONNECTED)
        finally:
            await core.stop()

    async def test_i_an_idle_child_that_dies_is_detected_by_the_liveness_check(self) -> None:
        core, pid_file = make_core(self.tmp, ping_interval=0.3)
        try:
            await core.start()
            _kill(read_pid(pid_file))
            self.assertTrue(await _until(lambda: core.state == mc.ERROR, 20), core.state)
            self.assertTrue(core.detail)  # the reason is shown, e.g. "MCP-Server antwortet nicht (...)"
        finally:
            await core.stop()

    # J
    async def test_j_clean_shutdown_ends_the_child_and_is_idempotent(self) -> None:
        core, pid_file = make_core(self.tmp)
        await core.start()
        pid = read_pid(pid_file)
        started = time.perf_counter()
        await core.stop()
        self.assertLess(time.perf_counter() - started, 12)
        self.assertEqual(core.state, mc.DISCONNECTED)
        self.assertTrue(wait_gone(pid))
        await core.stop()  # again: no error
        self.assertEqual(core.state, mc.DISCONNECTED)


async def _until(predicate, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def _kill(pid: int) -> None:
    import subprocess

    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")
class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.pid_file = self.tmp / "pid.txt"
        settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"))
        self.service = mc.McpService(settings, self.tmp / "mcp-stderr.log", extra_env={"FAKE_PID_FILE": str(self.pid_file)})
        self.states: list[str] = []
        self.service.state_changed.connect(lambda s, d: self.states.append(s))
        self.addCleanup(self.tmpdir.cleanup)
        self.addCleanup(self.service.shutdown)

    def call(self, name, args=None, timeout=10.0):
        got: list = []
        thread_ids: list = []
        self.service.call(name, args, timeout, on_done=lambda r: (got.append(r), thread_ids.append(threading.get_ident())))
        self.assertTrue(pump(30, lambda: got), f"no result for {name}")
        return got[0], thread_ids[0]

    def connect(self) -> None:
        self.service.start()
        self.assertTrue(pump(30, lambda: self.service.state == mc.CONNECTED))

    def test_callbacks_run_in_the_gui_thread_and_signals_report_the_state(self) -> None:
        self.connect()
        self.assertIn(mc.CONNECTING, self.states)
        self.assertEqual(self.service.tool_count, len(FAKE_TOOLS))
        res, thread_id = self.call("database_status")
        self.assertTrue(res.tool_ok)
        self.assertEqual(thread_id, threading.get_ident())  # delivered in the GUI (main) thread

    def test_action_and_unknown_tools_are_denied_without_reaching_the_server(self) -> None:
        self.connect()
        for name in ("sql_execute", "approve_swing_promotion", "something_new"):
            res, _ = self.call(name, {"sql": "DROP TABLE x"})
            self.assertEqual((res.ok, res.kind), (False, "DENIED"), name)
        self.assertTrue(self.call("database_status")[0].tool_ok)

    def test_a_call_before_start_is_answered_not_lost(self) -> None:
        res, _ = self.call("database_status")
        self.assertEqual(res.kind, "NOT_CONNECTED")

    def test_the_gui_thread_stays_responsive_during_a_long_call(self) -> None:
        self.connect()
        got: list = []
        self.service.call("rank_watchlist", {"seconds": 2.0}, 10, on_done=got.append)
        gaps, last = [], time.perf_counter()
        end = time.time() + 6
        while time.time() < end and not got:  # a heartbeat loop standing in for the UI event loop
            qapp().processEvents()
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
            time.sleep(0.01)
        self.assertTrue(got and got[0].tool_ok)
        self.assertGreater(len(gaps), 50)  # the event loop kept turning for the whole 2 s
        self.assertLess(max(gaps), 0.25)

    def test_restart_and_shutdown(self) -> None:
        self.connect()
        first = read_pid(self.pid_file)
        self.pid_file.unlink()
        self.service.restart()
        self.assertTrue(pump(30, lambda: self.pid_file.exists() and self.service.state == mc.CONNECTED and self.states.count(mc.CONNECTED) >= 2))
        second = read_pid(self.pid_file)
        self.assertNotEqual(first, second)
        self.assertTrue(wait_gone(first))
        self.assertTrue(self.call("database_status")[0].tool_ok)
        self.service.shutdown()
        self.assertTrue(wait_gone(second))
        self.service.shutdown()  # idempotent
        self.assertEqual(self.service.state, mc.DISCONNECTED)

    def test_logging_names_tools_but_never_content(self) -> None:
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
            self.call("database_status")
        finally:
            logging.getLogger("tradinggui").removeHandler(handler)
        text = "\n".join(records)
        self.assertIn("Tool database_status: ok", text)
        self.assertNotIn(str(self.tmp), text)  # the result (it contains the db path) is not in the log


if __name__ == "__main__":
    unittest.main()
