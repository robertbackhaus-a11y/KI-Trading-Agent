# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The silent execution of the scheduled tasks: the launcher (``Run-TradingTask.py``, started by ``pythonw.exe``) keeps what ``cmd /c python script >> log 2>&1`` did - log content and exit
code - and the task definitions of ``Manage-TradingTasks.ps1`` all start it, without cmd.exe, a console or a second process; schedules stay as they were."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "trading"
LAUNCHER = TOOLS / "Run-TradingTask.py"
PS1_PATH = ROOT / "scheduler" / "Manage-TradingTasks.ps1"
PS1 = PS1_PATH.read_text(encoding="utf-8-sig")
PYTHONW = Path(sys.executable).with_name("pythonw.exe")

SCRIPT = textwrap.dedent('''
    import sys
    from sibling import GREETING
    print("name=" + __name__)
    print("args=" + "|".join(sys.argv[1:]))
    print(GREETING)
    print("to-stderr", file=sys.stderr)
    print("stdout again")
    sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0)
''')


class LauncherCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "sibling.py").write_text('GREETING = "sibling import works"\n', encoding="utf-8")
        self.script = self.dir / "job.py"
        self.script.write_text(SCRIPT, encoding="utf-8")

    def launch(self, log: Path, script: Path, *args: str, python: str | None = None) -> int:
        return subprocess.run([python or sys.executable, str(LAUNCHER), str(log), str(script), *args], cwd=self.dir, capture_output=True, timeout=60).returncode

    def redirect(self, log: Path, script: Path, *args: str) -> int:
        """What the old task did: ``cmd.exe /c ""python" "script" args >> "log" 2>&1"`` (the outer quotes are how cmd keeps the inner ones)."""
        line = f'""{sys.executable}" "{script}" {" ".join(args)} >> "{log}" 2>&1"'
        return subprocess.run(f"cmd.exe /c {line}", cwd=self.dir, capture_output=True, timeout=60).returncode


class EquivalenceTests(LauncherCase):
    def test_the_log_and_the_exit_code_are_those_of_the_old_redirect(self) -> None:
        for args in ((), ("3",), ("0", "--write", "--quote-currency", "USD,GBP")):
            with self.subTest(args=args):
                old, new = self.dir / "old.log", self.dir / "new.log"
                for path in (old, new):
                    path.unlink(missing_ok=True)
                code_old = self.redirect(old, self.script, *args)
                code_new = self.launch(new, self.script, *args)
                self.assertEqual(code_new, code_old)
                self.assertEqual(new.read_bytes(), old.read_bytes())  # byte for byte: same encoding, same order of stdout and stderr

    def test_the_script_runs_as_main_with_its_arguments_and_its_own_folder_on_the_path(self) -> None:
        log = self.dir / "run.log"
        self.assertEqual(self.launch(log, self.script, "5", "--write"), 5)
        text = log.read_text()
        self.assertIn("name=__main__", text)
        self.assertIn("args=5|--write", text)
        self.assertIn("sibling import works", text)

    def test_the_log_is_appended_not_truncated(self) -> None:
        log = self.dir / "run.log"
        log.write_text("earlier run\n", encoding="utf-8")
        self.launch(log, self.script)
        self.launch(log, self.script)
        text = log.read_text()
        self.assertTrue(text.startswith("earlier run\n"))
        self.assertEqual(text.count("name=__main__"), 2)

    def test_an_uncaught_exception_is_exit_code_one_with_the_traceback_in_the_log(self) -> None:
        bad = self.dir / "bad.py"
        bad.write_text("print('before')\nraise RuntimeError('boom')\n", encoding="utf-8")
        log = self.dir / "bad.log"
        self.assertEqual(self.launch(log, bad), 1)
        self.assertEqual(self.redirect(self.dir / "bad_old.log", bad), 1)
        text = log.read_text()
        self.assertIn("RuntimeError: boom", text)
        self.assertIn("before", text)

    def test_a_system_exit_with_a_message_is_exit_code_one_and_the_message_is_logged(self) -> None:
        bad = self.dir / "exit.py"
        bad.write_text("import sys\nsys.exit('something is wrong')\n", encoding="utf-8")
        log = self.dir / "exit.log"
        self.assertEqual(self.launch(log, bad), 1)
        self.assertIn("something is wrong", log.read_text())

    def test_missing_arguments_are_a_usage_error(self) -> None:
        self.assertEqual(subprocess.run([sys.executable, str(LAUNCHER)], capture_output=True, timeout=60).returncode, 2)
        self.assertEqual(subprocess.run([sys.executable, str(LAUNCHER), str(self.dir / "x.log")], capture_output=True, timeout=60).returncode, 2)


@unittest.skipUnless(os.name == "nt" and PYTHONW.is_file(), "pythonw.exe (Windows) is needed")
class RealPythonwTests(LauncherCase):
    def test_pythonw_has_no_console_and_still_logs_and_returns_the_exit_code(self) -> None:
        probe = self.dir / "probe.py"
        probe.write_text("import ctypes, sys\nprint('console_window=%d' % ctypes.windll.kernel32.GetConsoleWindow())\nprint('stdout_is_log')\nsys.exit(7)\n", encoding="utf-8")
        log = self.dir / "probe.log"
        code = self.launch(log, probe, python=str(PYTHONW))
        self.assertEqual(code, 7)  # the scheduler's last result is the exit code of the script
        text = log.read_text()
        self.assertIn("console_window=0", text)  # no console window, so nothing can pop up or take the focus
        self.assertIn("stdout_is_log", text)

    def test_the_same_job_through_pythonw_gives_the_same_log_as_the_redirect(self) -> None:
        old, new = self.dir / "old.log", self.dir / "new.log"
        code_old = self.redirect(old, self.script, "4")
        code_new = self.launch(new, self.script, "4", python=str(PYTHONW))
        self.assertEqual((code_new, new.read_bytes()), (code_old, old.read_bytes()))


class TaskDefinitionTests(unittest.TestCase):
    EXPECTED_SCHEDULE = {  # the schedules, settings and scripts as they were before the silent mechanism (and the ETF task as decided): unchanged
        "MarketData-Backfill": ("Backfill-TradingMarketData.py", "--write", "market-data-backfill.log", "Daily", ["17:15"], None, None),
        "FXRates-Backfill": ("Backfill-TradingFXRatesECB.py", "--write --quote-currency USD,GBP,AUD,KRW", "fx-rates-backfill.log", "Daily", ["17:20"], None, None),
        "EventsNews-Backfill": ("Backfill-TradingEventsNews.py", "--write", "events-news-backfill.log", "Daily", ["17:25"], None, None),
        "Fundamentals-SEC": ("Backfill-TradingFundamentalsSEC.py", "", "fundamentals-sec-backfill.log", "Weekly", ["10:00"], None, None),
        "Candidate-Discovery": ("Discover-TradingCandidates.py", "", "candidate-discovery.log", "Daily", ["17:35"], "app", "2"),
        "Market-Intelligence": ("Collect-TradingMarketIntelligence.py", "", "market-intelligence.log", "Daily", ["08:15", "17:45"], "app", "2"),
        "ETF-Refresh": ("Refresh-TradingEtfData.py", "--write", "etf-refresh.log", "Weekly", ["11:00"], "app", "1"),
    }

    @staticmethod
    def jobs() -> dict[str, tuple]:
        found = {}
        for name, body in re.findall(r"@\{ Name = '([^']+)';(.*?)\}(?:,|\s*\n\))", PS1, flags=re.S):
            field = dict(re.findall(r"(\w+) = ('[^']*'|@\([^)]*\)|\$\w+|\d+)", body))
            text = lambda key: field.get(key, "").strip("'") if key in field else None  # noqa: E731
            found[name] = (text("Script"), text("Args"), text("Log"), text("Kind"), re.findall(r"\d\d:\d\d", field["At"]), text("WorkDir"), text("LimitHours"))
        return found

    def test_schedules_scripts_arguments_and_logs_are_unchanged(self) -> None:
        self.assertEqual(self.jobs(), self.EXPECTED_SCHEDULE)

    def test_there_are_seven_tasks(self) -> None:
        self.assertEqual(len(self.jobs()), 7)

    def test_every_task_starts_pythonw_with_the_launcher_and_nothing_else(self) -> None:
        action = PS1.split("function Get-ActionParams")[1].split("function Assert-SilentRuntime")[0]
        self.assertIn("Execute = $pythonw", action)
        self.assertIn("$launcher", action)
        self.assertIn("'.venv\\Scripts\\pythonw.exe'", PS1)
        self.assertIn("'app\\Run-TradingTask.py'", PS1)
        register = PS1.split("'Register' {")[1].split("'UpdateAction' {")[0]
        update = PS1.split("'UpdateAction' {")[1].split("'Enable' {")[0]
        for forbidden in ("cmd.exe", "conhost", "powershell.exe", "wscript", ">> ", "2>&1", "-WindowStyle", "Start-Process"):
            for part in (action, register, update):
                self.assertNotIn(forbidden, part, forbidden)
        self.assertIn("$actionParams = Get-ActionParams $j", register)
        self.assertIn("New-ScheduledTaskAction @actionParams", register)

    def test_all_seven_registrations_use_the_same_action_builder(self) -> None:
        self.assertEqual(PS1.count("Get-ActionParams $j"), 2)  # Register and UpdateAction: one builder, so a new and an updated task cannot differ
        self.assertEqual(PS1.count("New-ScheduledTaskAction"), 2)

    def test_update_action_changes_only_the_action_of_existing_tasks(self) -> None:
        block = PS1.split("'UpdateAction' {")[1].split("'Enable' {")[0]
        self.assertIn("Set-ScheduledTask -TaskName $name -Action", block)
        for untouched in ("-Trigger", "-Settings", "-Principal", "Register-ScheduledTask", "Unregister", "Start-ScheduledTask"):
            self.assertNotIn(untouched, block, untouched)
        self.assertIn("nicht vorhanden", block)  # a missing task is left to Register
        self.assertIn("'UpdateAction'", PS1.split("[ValidateSet(")[1].split(")")[0])

    def test_the_settings_of_a_registration_are_unchanged(self) -> None:
        register = PS1.split("'Register' {")[1].split("'UpdateAction' {")[0]
        self.assertIn("StartWhenAvailable = $true", register)
        self.assertIn("MultipleInstances = 'IgnoreNew'", register)
        self.assertIn("New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited", register)  # unchanged: the same account and logon type
        self.assertIn("-Weekly -DaysOfWeek Sunday", register)
        self.assertIn("ExecutionTimeLimit", register)

    def test_status_tells_whether_a_task_is_silent(self) -> None:
        self.assertIn("unsichtbar (pythonw)", PS1)
        self.assertIn("zeigt ein Fenster", PS1)

    def test_the_launcher_is_deployed_and_listed_in_the_manifest(self) -> None:
        manifest = json.loads((ROOT / "deploy" / "deploy-manifest.json").read_text(encoding="utf-8"))
        listed = {name for group in manifest["groups"] for name in group.get("files", [])}
        self.assertIn("Run-TradingTask.py", listed)

    def test_no_task_script_starts_a_console_child_process(self) -> None:
        """pythonw only helps while the scripts start no console program themselves (a child would open its own window)."""
        offenders = []
        for path in TOOLS.glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^\s*(import subprocess|from subprocess import)|os\.system\(|os\.popen\(", text, flags=re.M):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_the_scheduled_scripts_exist(self) -> None:
        for script, *_rest in self.EXPECTED_SCHEDULE.values():
            self.assertTrue((TOOLS / script).is_file(), script)


@unittest.skipUnless(shutil.which("powershell") and os.name == "nt", "Windows PowerShell is needed")
class PowerShellTests(unittest.TestCase):
    def run_ps(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1_PATH), *args], capture_output=True, timeout=120, text=True, encoding="utf-8", errors="replace")

    def test_the_script_parses(self) -> None:
        command = f"$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('{PS1_PATH}',[ref]$null,[ref]$e); $e.Count"
        done = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, timeout=60, text=True)
        self.assertEqual(done.stdout.strip(), "0", done.stderr)

    def test_the_dry_run_shows_the_silent_action_for_every_existing_task_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for sub in (".venv/Scripts", "app", "logs"):
                (root / sub).mkdir(parents=True)
            for exe in ("python.exe", "pythonw.exe"):
                (root / ".venv" / "Scripts" / exe).write_text("", encoding="utf-8")
            for path in TOOLS.glob("*.py"):
                shutil.copy(path, root / "app" / path.name)
            update = self.run_ps("-Action", "UpdateAction", "-DryRun", "-Root", str(root))
            register = self.run_ps("-Action", "Register", "-DryRun", "-Root", str(root))
        self.assertEqual((update.returncode, register.returncode), (0, 0), update.stderr + register.stderr)
        lines = [l for l in (update.stdout + register.stdout).splitlines() if l.startswith("[DryRun]")]
        for line in lines:  # whichever tasks exist on this machine: each planned action is pythonw + launcher + log + script
            self.assertIn("pythonw.exe", line)
            self.assertIn("Run-TradingTask.py", line)
            self.assertNotRegex(line, r"cmd\.exe|>>|2>&1|conhost")
        for line in lines:
            self.assertRegex(line, r'Run-TradingTask\.py" "[^"]+\.log" "[^"]+\.py"')


if __name__ == "__main__":
    unittest.main()
