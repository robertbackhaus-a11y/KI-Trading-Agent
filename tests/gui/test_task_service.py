# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Task service and task view model: JSON parsing, missing tasks, the Run-now command, running tasks, log tail (M-Q)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from gui.services import task_service as ts
from gui.viewmodels.tasks import build_task_rows


def item(name, state="Ready", last="2026-10-12T08:09:22.0000000+02:00", nxt="2026-10-12T17:15:00.0000000+02:00", result=0):
    return {"name": name, "found": True, "state": state, "last_run": last, "next_run": nxt, "last_result": result}


def six(**overrides):
    rows = [item(n) for n in ts.TASK_NAMES]
    for row in rows:
        row.update(overrides.get(row["name"], {}))
    return rows


class FakeRunner:
    """Records every command; answers the query script with ``rows`` and everything else with ``start_code``."""

    def __init__(self, rows=None, start_code=0, start_err="", query_code=0):
        self.rows = six() if rows is None else rows
        self.start_code, self.start_err, self.query_code = start_code, start_err, query_code
        self.commands: list[list[str]] = []

    def run(self, args, timeout):
        self.commands.append(args)
        script = args[-1]
        if "Get-ScheduledTask" in script:
            return self.query_code, json.dumps(self.rows), "" if self.query_code == 0 else "Access denied\nmore"
        return self.start_code, "", self.start_err

    @property
    def start_commands(self):
        return [c for c in self.commands if "Start-ScheduledTask" in c[-1]]


class ParsingTests(unittest.TestCase):
    # M
    def test_m_parses_the_json_of_the_scheduler_query(self) -> None:
        rows = six(**{"Trading-Fundamentals-SEC": {"state": "Running", "last_result": 267009, "next_run": None}, "Trading-Candidate-Discovery": {"last_result": 1, "last_run": None}})
        tasks = ts.parse_tasks(json.dumps(rows))
        self.assertEqual([t.name for t in tasks], list(ts.TASK_NAMES))  # display order, only the seven known tasks
        first = tasks[0]
        self.assertTrue(first.found and not first.running and not first.disabled)
        self.assertEqual((first.state, first.last_result, first.log_file), ("Ready", 0, "market-data-backfill.log"))
        self.assertEqual((first.last_run.year, first.last_run.minute, first.next_run.minute), (2026, 9, 15))
        sec = next(t for t in tasks if t.name == "Trading-Fundamentals-SEC")
        self.assertTrue(sec.running)
        self.assertIsNone(sec.next_run)
        self.assertIsNone(next(t for t in tasks if t.name == "Trading-Candidate-Discovery").last_run)

    def test_other_tasks_in_the_answer_are_ignored_and_a_single_object_is_accepted(self) -> None:
        text = json.dumps(six() + [item("Some-Other-Task")])
        self.assertEqual(len(ts.parse_tasks(text)), 7)
        single = ts.parse_tasks(json.dumps(item("Trading-Market-Intelligence")))
        self.assertEqual([t.found for t in single], [False] * 5 + [True, False])

    def test_invalid_scheduler_answers_raise_a_clear_error(self) -> None:
        with self.assertRaises(ValueError):
            ts.parse_tasks("Task list in German: bereit")
        self.assertEqual([t.found for t in ts.parse_tasks("")], [False] * 7)
        self.assertEqual([t.found for t in ts.parse_tasks("[]")], [False] * 7)

    def test_result_texts(self) -> None:
        self.assertEqual((ts.result_text(0), ts.result_text(None), ts.result_text(267009), ts.result_text(267011)), ("OK", "—", "läuft", "noch nie gelaufen"))
        self.assertEqual(ts.result_text(2147942402), "Fehler (0x80070002)")

    def test_the_query_script_names_exactly_the_seven_tasks_and_asks_for_json(self) -> None:
        script = ts.query_script()
        for name in ts.TASK_NAMES:
            self.assertIn(f"'{name}'", script)
        self.assertEqual(script.count("Trading-"), 7)
        self.assertIn("ConvertTo-Json", script)
        self.assertNotIn("Start-ScheduledTask", script)
        args = ts.powershell_args(script)
        self.assertEqual(args[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("OutputEncoding", args[-1])  # UTF-8 output, never localized text

    def test_query_failure_is_an_error_with_the_first_line(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            ts.query_tasks(FakeRunner(query_code=1))
        self.assertIn("Access denied", str(ctx.exception))

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
    def test_the_real_query_script_runs_and_returns_parsable_json(self) -> None:
        tasks = ts.query_tasks(ts.PowerShellRunner())  # read-only query of the scheduler; the tasks may or may not exist on this machine
        self.assertEqual([t.name for t in tasks], list(ts.TASK_NAMES))


class MissingAndRunningTests(unittest.TestCase):
    # N
    def test_n_a_missing_task_is_shown_and_cannot_be_started(self) -> None:
        rows = [{"name": "Trading-MarketData-Backfill", "found": False}] + six()[1:]
        runner = FakeRunner(rows)
        tasks = ts.query_tasks(runner)
        self.assertFalse(tasks[0].found)
        vm = build_task_rows(tasks)[0]
        self.assertEqual((vm.status, vm.can_run, vm.level), ("nicht registriert", False, "blocker"))
        ok, message = ts.start_task(runner, "Trading-MarketData-Backfill")
        self.assertFalse(ok)
        self.assertIn("nicht registriert", message)
        self.assertEqual(runner.start_commands, [])

    # O
    def test_o_run_now_starts_the_existing_task_and_nothing_else(self) -> None:
        command = ts.run_now_command("Trading-Market-Intelligence")
        self.assertEqual(command[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertTrue(command[-1].endswith("Start-ScheduledTask -TaskName 'Trading-Market-Intelligence' -ErrorAction Stop"))
        self.assertNotIn("python", command[-1].lower())  # the script behind the task is never started directly
        for forbidden in ("Register-ScheduledTask", "Set-ScheduledTask", "Unregister", "Disable-ScheduledTask", "Enable-ScheduledTask"):
            self.assertNotIn(forbidden, command[-1])
        runner = FakeRunner()
        ok, message = ts.start_task(runner, "Trading-Market-Intelligence")
        self.assertTrue(ok, message)
        self.assertEqual(len(runner.start_commands), 1)
        self.assertEqual(runner.start_commands[0], command)
        self.assertIn("Get-ScheduledTask", runner.commands[0][-1])  # the state is checked first

    def test_only_the_six_known_names_can_ever_be_started(self) -> None:
        runner = FakeRunner()
        for name in ("Trading-Other", "", "Trading-Market-Intelligence'; Remove-Item C:\\x; '", "trading-market-intelligence"):
            with self.assertRaises(ValueError):
                ts.run_now_command(name)
            self.assertFalse(ts.start_task(runner, name)[0])
        self.assertEqual(runner.commands, [])  # nothing was executed at all

    def test_a_failing_start_reports_the_first_error_line(self) -> None:
        ok, message = ts.start_task(FakeRunner(start_code=1, start_err="Access is denied.\nmore"), "Trading-FXRates-Backfill")
        self.assertFalse(ok)
        self.assertIn("Access is denied.", message)

    # P
    def test_p_a_running_task_cannot_be_started_again(self) -> None:
        rows = six(**{"Trading-EventsNews-Backfill": {"state": "Running"}, "Trading-Fundamentals-SEC": {"state": "Disabled"}})
        runner = FakeRunner(rows)
        vms = {v.name: v for v in build_task_rows(ts.query_tasks(runner))}
        self.assertEqual((vms["Trading-EventsNews-Backfill"].status, vms["Trading-EventsNews-Backfill"].can_run), ("läuft", False))
        self.assertEqual((vms["Trading-Fundamentals-SEC"].status, vms["Trading-Fundamentals-SEC"].can_run), ("deaktiviert", False))
        self.assertTrue(vms["Trading-MarketData-Backfill"].can_run)
        self.assertIn("Aktion", vms["Trading-MarketData-Backfill"].run_hint)
        for name in ("Trading-EventsNews-Backfill", "Trading-Fundamentals-SEC"):
            ok, message = ts.start_task(runner, name)  # the service refuses too (state re-read right before the start)
            self.assertFalse(ok, name)
        self.assertEqual(runner.start_commands, [])

    def test_a_failed_last_result_is_a_warning_row(self) -> None:
        rows = six(**{"Trading-Candidate-Discovery": {"last_result": 2147942402}, "Trading-FXRates-Backfill": {"last_result": 267011}})
        vms = {v.name: v for v in build_task_rows(ts.parse_tasks(json.dumps(rows)))}
        self.assertEqual(vms["Trading-Candidate-Discovery"].level, "warning")
        self.assertEqual(vms["Trading-FXRates-Backfill"].level, "")
        self.assertEqual(vms["Trading-Candidate-Discovery"].result, "Fehler (0x80070002)")


class SilentExecutionAndManualRunTests(unittest.TestCase):
    """The scheduler runs every task without a window (pythonw); "Jetzt ausführen" starts that same scheduled task and nothing else."""

    PYTHONW = "C:\\tools\\trading\\.venv\\Scripts\\pythonw.exe"
    CMD = "C:\\Windows\\System32\\cmd.exe"

    def test_the_query_reads_the_program_each_task_starts(self) -> None:
        self.assertIn("$t.Actions[0].Execute", ts.query_script())
        rows = six(**{"Trading-MarketData-Backfill": {"action_execute": self.PYTHONW}, "Trading-FXRates-Backfill": {"action_execute": self.CMD}})
        tasks = {t.name: t for t in ts.parse_tasks(json.dumps(rows))}
        self.assertIs(tasks["Trading-MarketData-Backfill"].silent, True)
        self.assertIs(tasks["Trading-FXRates-Backfill"].silent, False)  # cmd.exe opens a console window on every run
        self.assertIsNone(tasks["Trading-EventsNews-Backfill"].silent)  # not reported: nothing is claimed
        self.assertIs(ts.TaskInfo("x", True, action_execute="C:\\x\\PythonW.EXE").silent, True)
        self.assertIs(ts.TaskInfo("x", True, action_execute="C:\\x\\python.exe").silent, False)

    def test_the_manual_run_is_the_scheduled_task_itself(self) -> None:
        for name in ts.TASK_NAMES:
            command = ts.run_now_command(name)
            self.assertEqual(command[-1].split("; ")[-1], f"Start-ScheduledTask -TaskName '{name}' -ErrorAction Stop")  # the Task Scheduler runs it: same action, settings, log
            text = " ".join(command).lower()
            for other in ("python", "cmd.exe", "pythonw", ".py", "run-tradingtask", "start-process"):
                self.assertNotIn(other, text, f"{name}: {other}")  # no second way to run a script from the GUI
        with self.assertRaises(ValueError):
            ts.run_now_command("Some-Other-Task")

    def test_starting_runs_exactly_the_query_and_the_scheduler_start(self) -> None:
        for name in ts.TASK_NAMES:
            runner = FakeRunner()
            ok, message = ts.start_task(runner, name)
            self.assertTrue(ok, message)
            scripts = [c[-1] for c in runner.commands]
            self.assertEqual(len(scripts), 2)
            self.assertIn("Get-ScheduledTask", scripts[0])
            self.assertIn(f"Start-ScheduledTask -TaskName '{name}'", scripts[1])
            self.assertEqual(len(runner.start_commands), 1)

    def test_a_running_task_cannot_be_started_a_second_time_for_any_of_the_seven(self) -> None:
        for name in ts.TASK_NAMES:
            runner = FakeRunner(rows=six(**{name: {"state": "Running", "last_result": 267009}}))
            ok, message = ts.start_task(runner, name)
            self.assertFalse(ok, name)
            self.assertIn("läuft bereits", message)
            self.assertEqual(runner.start_commands, [], name)

    def test_the_runner_hides_the_console_window_of_its_own_powershell(self) -> None:
        import inspect

        source = inspect.getsource(ts.PowerShellRunner)
        self.assertIn("creationflags=flags", source)
        self.assertEqual(ts.CREATE_NO_WINDOW, 0x08000000)

    def test_exit_codes_are_shown_as_exit_codes(self) -> None:
        self.assertEqual(ts.result_text(0), "OK")
        self.assertEqual((ts.result_text(1), ts.result_text(2), ts.result_text(3)), ("Fehler (Exitcode 1)", "Fehler (Exitcode 2)", "Fehler (Exitcode 3)"))
        self.assertEqual(ts.result_text(267014), "beendet (Zeitlimit/Abbruch)")
        self.assertEqual(ts.result_text(2147942402), "Fehler (0x80070002)")


class LogTailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    # Q
    def test_q_only_the_end_of_a_large_log_is_loaded_and_the_file_is_untouched(self) -> None:
        path = self.dir / "market-data-backfill.log"
        path.write_text("\n".join(f"line {i:06d} " + "x" * 80 for i in range(20_000)) + "\n", encoding="utf-8")
        before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        tail = ts.tail_log(path, max_lines=300, max_bytes=100_000)
        self.assertTrue(tail.available and tail.truncated)
        lines = tail.text.splitlines()
        self.assertEqual(len(lines), 300)
        self.assertTrue(lines[-1].startswith("line 019999"))
        self.assertGreater(tail.size, 1_500_000)
        self.assertLess(len(tail.text), 40_000)  # nowhere near the whole file
        self.assertEqual((hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns), before)

    def test_small_log_is_complete_and_not_truncated(self) -> None:
        path = self.dir / "a.log"
        path.write_text("one\ntwo\nthree\n", encoding="utf-8")
        tail = ts.tail_log(path)
        self.assertEqual((tail.text, tail.truncated, tail.available), ("one\ntwo\nthree", False, True))

    def test_missing_empty_and_undecodable_logs(self) -> None:
        missing = ts.tail_log(self.dir / "nope.log")
        self.assertFalse(missing.available)
        self.assertIn("nicht vorhanden", missing.note)
        (self.dir / "empty.log").write_bytes(b"")
        self.assertEqual(ts.tail_log(self.dir / "empty.log").text, "")
        (self.dir / "cp1252.log").write_bytes("Umlaut \xe4\xf6\xfc und \x80\n".encode("latin-1"))
        self.assertTrue(ts.tail_log(self.dir / "cp1252.log").available)  # undecodable bytes are replaced, never an exception
        self.assertFalse(ts.tail_log(None).available)

    def test_log_paths_are_limited_to_the_known_log_files(self) -> None:
        logs = str(self.dir)
        self.assertEqual(ts.log_path(logs, "Trading-Market-Intelligence"), self.dir / "market-intelligence.log")
        self.assertIsNone(ts.log_path(logs, "Trading-Other"))
        self.assertIsNone(ts.log_path(logs, "..\\..\\evil"))
        self.assertIsNone(ts.log_path("relative\\logs", "Trading-Market-Intelligence"))
        self.assertEqual({t.log_file for t in ts.TASKS}, {"market-data-backfill.log", "fx-rates-backfill.log", "events-news-backfill.log", "fundamentals-sec-backfill.log",
                                                         "candidate-discovery.log", "market-intelligence.log", "etf-refresh.log"})

    def test_backend_tail_uses_the_configured_logs_dir(self) -> None:
        (self.dir / "candidate-discovery.log").write_text("hello\n", encoding="utf-8")
        backend = ts.TaskBackend(str(self.dir), FakeRunner())
        self.assertEqual(backend.tail("Trading-Candidate-Discovery").text, "hello")
        self.assertFalse(backend.tail("Trading-Unknown").available)
        self.assertEqual(len(backend.query()), 7)


if __name__ == "__main__":
    unittest.main()
