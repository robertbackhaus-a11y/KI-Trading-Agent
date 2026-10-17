# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Windows Task Scheduler access of the GUI (PowerShell with JSON output; no MCP, no Qt).

Only the six known Trading tasks are ever queried or started.  "Run now" starts the *existing* scheduled task (``Start-ScheduledTask``);
the underlying script is never launched directly and no task definition is changed.  The scheduler's own ``MultipleInstances = IgnoreNew``
stays in effect; the GUI additionally refuses to start a task that is already running or disabled.  Output is JSON, never localized text.

Besides the run state the query reads the *definition* (triggers, ``StartWhenAvailable``, ``MultipleInstances``, time limit, missed runs) and, from the log
file next to it, when it was last written and the last error-looking line of its end -- read-only, nothing is changed.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol


@dataclass(frozen=True)
class TaskDef:
    name: str
    log_file: str


# order of the evening run
TASKS: tuple[TaskDef, ...] = (
    TaskDef("Trading-MarketData-Backfill", "market-data-backfill.log"),
    TaskDef("Trading-FXRates-Backfill", "fx-rates-backfill.log"),
    TaskDef("Trading-EventsNews-Backfill", "events-news-backfill.log"),
    TaskDef("Trading-Fundamentals-SEC", "fundamentals-sec-backfill.log"),
    TaskDef("Trading-Candidate-Discovery", "candidate-discovery.log"),
    TaskDef("Trading-Market-Intelligence", "market-intelligence.log"),
    TaskDef("Trading-ETF-Refresh", "etf-refresh.log"),
)
TASK_NAMES = tuple(t.name for t in TASKS)
RESULT_TEXT = {0: "OK", 267009: "läuft", 267010: "nicht geplant", 267011: "noch nie gelaufen", 267014: "beendet (Zeitlimit/Abbruch)", 1: "Fehler (Exitcode 1)"}
CREATE_NO_WINDOW = 0x08000000


class CommandRunner(Protocol):
    def run(self, args: list[str], timeout: float) -> tuple[int, str, str]: ...


class PowerShellRunner:
    """Runs ``args`` as a subprocess without a console window and decodes the output as UTF-8."""

    def run(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        done = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)  # noqa: S603 - fixed argument list
        return done.returncode, done.stdout.decode("utf-8-sig", "replace"), done.stderr.decode("utf-8", "replace")


DAY_NAMES = ((1, "So"), (2, "Mo"), (4, "Di"), (8, "Mi"), (16, "Do"), (32, "Fr"), (64, "Sa"))  # the DaysOfWeek bit mask of the scheduler


@dataclass(frozen=True)
class TriggerInfo:
    """One trigger of a task: ``kind`` is ``Daily``, ``Weekly``, ``Logon`` or ``Other``; ``time`` is the local ``HH:MM`` of the start."""

    kind: str
    time: Optional[str] = None
    days_interval: int = 1
    weeks_interval: int = 1
    days_of_week: int = 0
    enabled: bool = True


@dataclass
class TaskInfo:
    name: str
    found: bool = False
    state: str = ""
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    last_result: Optional[int] = None
    log_file: str = ""
    triggers: list[TriggerInfo] = field(default_factory=list)
    start_when_available: Optional[bool] = None
    multiple_instances: str = ""  # IgnoreNew, Parallel, Queue, StopExisting
    time_limit: str = ""  # ISO 8601 duration of the scheduler, e.g. PT2H
    missed_runs: Optional[int] = None
    log_modified: Optional[datetime] = None  # when the log file was last written (about the end of the last run)
    log_size: Optional[int] = None
    log_hint: str = ""  # the last error-looking line at the end of the log
    action_execute: str = ""  # the program the task starts (pythonw.exe = no window; cmd.exe / python.exe = a console window on every run)

    @property
    def silent(self) -> Optional[bool]:
        """True if the task starts a program without a console (pythonw), False if it starts a console program, None if the action is not known."""
        if not self.action_execute:
            return None
        return ntpath.basename(self.action_execute).lower() == "pythonw.exe"

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"

    @property
    def disabled(self) -> bool:
        return self.state.lower() == "disabled"


@dataclass
class LogTail:
    available: bool
    text: str = ""
    truncated: bool = False
    size: int = 0
    modified: Optional[datetime] = None
    note: str = ""


def powershell_args(script: str) -> list[str]:
    prefix = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $ErrorActionPreference = 'Stop'; "
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", prefix + script]


def query_script() -> str:
    names = ",".join(f"'{n}'" for n in TASK_NAMES)  # constants only, never user input
    return (
        f"$names = @({names}); "
        "$rows = foreach ($n in $names) { "
        "$t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; "
        "if ($null -eq $t) { [pscustomobject]@{ name = $n; found = $false } } else { "
        "$i = $t | Get-ScheduledTaskInfo; "
        "[pscustomobject]@{ name = $n; found = $true; state = [string]$t.State; "
        "last_run = $(if ($i.LastRunTime -gt [datetime]'2001-01-01') { $i.LastRunTime.ToString('o') } else { $null }); "
        "next_run = $(if ($i.NextRunTime -gt [datetime]'2001-01-01') { $i.NextRunTime.ToString('o') } else { $null }); "
        "last_result = [int64]$i.LastTaskResult; missed_runs = [int]$i.NumberOfMissedRuns; action_execute = [string]$t.Actions[0].Execute; "
        "start_when_available = [bool]$t.Settings.StartWhenAvailable; multiple_instances = [string]$t.Settings.MultipleInstances; time_limit = [string]$t.Settings.ExecutionTimeLimit; "
        "triggers = @($t.Triggers | ForEach-Object { [pscustomobject]@{ type = [string]$_.CimClass.CimClassName; start = [string]$_.StartBoundary; "
        "days_interval = [int]$_.DaysInterval; weeks_interval = [int]$_.WeeksInterval; days_of_week = [int]$_.DaysOfWeek; enabled = [bool]$_.Enabled } }) } } }; "
        "ConvertTo-Json -InputObject @($rows) -Compress -Depth 5"
    )


def run_now_command(name: str) -> list[str]:
    """The exact command for "Run now": the scheduled task itself is started (``Start-ScheduledTask``), so the manual run is the same job with the same action, settings and log as
    the scheduled one -- no second way to run a script.  ``name`` must be one of the seven known tasks."""
    if name not in TASK_NAMES:
        raise ValueError(f"unbekannter Task: {name}")
    return powershell_args(f"Start-ScheduledTask -TaskName '{name}' -ErrorAction Stop")


def _when(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def _trigger(raw) -> Optional[TriggerInfo]:
    if not isinstance(raw, dict):
        return None
    kind_raw = str(raw.get("type") or "")
    kind = "Daily" if "Daily" in kind_raw else "Weekly" if "Weekly" in kind_raw else "Logon" if "Logon" in kind_raw else "Other"
    start = _when(raw.get("start"))
    interval = raw.get("days_interval")
    weeks = raw.get("weeks_interval")
    mask = raw.get("days_of_week")
    return TriggerInfo(kind, start.strftime("%H:%M") if start else None, int(interval) if isinstance(interval, (int, float)) and interval else 1,
                       int(weeks) if isinstance(weeks, (int, float)) and weeks else 1, int(mask) if isinstance(mask, (int, float)) else 0, raw.get("enabled") is not False)


def parse_tasks(text: str) -> list[TaskInfo]:
    """JSON of :func:`query_script` -> one :class:`TaskInfo` per known task (in display order); an absent task is ``found=False``."""
    try:
        raw = json.loads(text) if text.strip() else []
    except ValueError as exc:
        raise ValueError("Ungültige Scheduler-Antwort (kein JSON)") from exc
    if isinstance(raw, dict):
        raw = [raw]
    by_name = {str(item.get("name")): item for item in raw if isinstance(item, dict)}
    out: list[TaskInfo] = []
    for task in TASKS:
        item = by_name.get(task.name)
        if not item or not item.get("found"):
            out.append(TaskInfo(task.name, log_file=task.log_file))
            continue
        result = item.get("last_result")
        triggers = [t for t in (_trigger(raw) for raw in (item.get("triggers") if isinstance(item.get("triggers"), list) else [])) if t is not None]
        missed = item.get("missed_runs")
        out.append(TaskInfo(
            task.name, True, str(item.get("state") or ""), _when(item.get("last_run")), _when(item.get("next_run")),
            int(result) if isinstance(result, (int, float)) else None, task.log_file,
            triggers, item.get("start_when_available") if isinstance(item.get("start_when_available"), bool) else None, str(item.get("multiple_instances") or ""),
            str(item.get("time_limit") or ""), int(missed) if isinstance(missed, (int, float)) else None,
            action_execute=str(item.get("action_execute") or ""),
        ))
    return out


def _days_text(mask: int) -> str:
    names = [name for bit, name in DAY_NAMES if mask & bit]
    return ", ".join(names) if names else "?"


def trigger_line(trigger: TriggerInfo) -> str:
    """One trigger in words, for the details: ``täglich 17:15``, ``alle 2 Tage 10:00``, ``wöchentlich So 10:00``, ``bei Anmeldung``."""
    suffix = "" if trigger.enabled else " (deaktiviert)"
    at = f" {trigger.time}" if trigger.time else ""
    if trigger.kind == "Daily":
        return (("täglich" if trigger.days_interval <= 1 else f"alle {trigger.days_interval} Tage") + at) + suffix
    if trigger.kind == "Weekly":
        return (("wöchentlich" if trigger.weeks_interval <= 1 else f"alle {trigger.weeks_interval} Wochen") + f" {_days_text(trigger.days_of_week)}" + at) + suffix
    if trigger.kind == "Logon":
        return "bei Anmeldung" + suffix
    return "anderer Auslöser" + suffix


def rhythm_text(triggers: list[TriggerInfo]) -> str:
    """The rhythm of a task from its (enabled) triggers: ``täglich 17:15 + bei Anmeldung``, ``2× täglich 08:15, 17:45``, ``wöchentlich So 10:00 + bei Anmeldung``."""
    active = [t for t in triggers if t.enabled]
    if not active:
        return "kein Auslöser" if not triggers else "Auslöser deaktiviert"
    parts: list[str] = []
    daily = sorted((t for t in active if t.kind == "Daily"), key=lambda t: t.time or "")
    if len(daily) == 1:
        parts.append(trigger_line(daily[0]))
    elif len(daily) > 1:
        parts.append(f"{len(daily)}× täglich " + ", ".join(t.time or "?" for t in daily))
    parts += [trigger_line(t) for t in active if t.kind == "Weekly"]
    if any(t.kind == "Logon" for t in active):
        parts.append("bei Anmeldung")
    parts += [trigger_line(t) for t in active if t.kind == "Other"]
    return " + ".join(parts)


def result_text(code: Optional[int]) -> str:
    if code is None:
        return "—"
    if code in RESULT_TEXT:
        return RESULT_TEXT[code]
    if 0 < code < 256:
        return f"Fehler (Exitcode {code})"  # the exit code of the script (the launcher hands it on to the scheduler)
    return f"Fehler (0x{code & 0xFFFFFFFF:08X})"


def query_tasks(runner: CommandRunner, timeout: float = 30.0) -> list[TaskInfo]:
    code, out, err = runner.run(powershell_args(query_script()), timeout)
    if code != 0:
        raise RuntimeError(f"PowerShell-Fehler ({code}): {err.strip().splitlines()[0] if err.strip() else 'keine Meldung'}")
    return parse_tasks(out)


def start_task(runner: CommandRunner, name: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Start the existing scheduled task.  Checks the current state first; never starts a running or disabled task."""
    if name not in TASK_NAMES:
        return False, f"Unbekannter Task: {name}"
    info = next((t for t in query_tasks(runner, timeout) if t.name == name), None)
    if info is None or not info.found:
        return False, f"Task {name} ist nicht registriert"
    if info.running:
        return False, f"Task {name} läuft bereits"
    if info.disabled:
        return False, f"Task {name} ist deaktiviert"
    code, _out, err = runner.run(run_now_command(name), timeout)
    if code != 0:
        return False, f"Start fehlgeschlagen ({code}): {err.strip().splitlines()[0] if err.strip() else 'keine Meldung'}"
    return True, f"Task {name} gestartet"


_ERROR_LINE = re.compile(r"traceback|\berror\b|\bfailed\b|\bfehler\b|getaddrinfo|forbidden|timed? ?out", re.I)
_BENIGN_LINE = re.compile(r"failed[^:=\n]{0,24}[:=]\s*0\b|failed:\s*\[\s*\]|\"failed\":\s*0|errors\"?:\s*(\{\s*\}|0\b)|no errors|keine fehler", re.I)
HINT_LINES, HINT_BYTES = 80, 40_000


def log_hint(tail_text: str) -> str:
    """The last error-looking line of the end of a log (a line that says ``0 failed`` or ``errors: {}`` does not count), at most 200 characters; empty if none."""
    for line in reversed(tail_text.splitlines()[-HINT_LINES:]):
        if _ERROR_LINE.search(line) and not _BENIGN_LINE.search(line):
            text = line.strip()
            return text if len(text) <= 200 else text[:197] + "…"
    return ""


def enrich_with_logs(tasks: list[TaskInfo], logs_dir: str) -> list[TaskInfo]:
    """Adds the write time and size of every task's log file and the last error-looking line at its end (read-only; a missing log leaves the fields empty)."""
    for task in tasks:
        path = log_path(logs_dir, task.name)
        if path is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        task.log_modified, task.log_size = datetime.fromtimestamp(stat.st_mtime), stat.st_size
        tail = tail_log(path, max_lines=HINT_LINES, max_bytes=HINT_BYTES)
        task.log_hint = log_hint(tail.text) if tail.available else ""
    return tasks


def log_path(logs_dir: str, name: str) -> Optional[Path]:
    task = next((t for t in TASKS if t.name == name), None)
    if task is None or not ntpath.isabs(logs_dir):
        return None
    return Path(logs_dir) / task.log_file


def tail_log(path: Optional[Path], max_lines: int = 300, max_bytes: int = 200_000) -> LogTail:
    """Last lines of a log file (reads at most ``max_bytes`` from the end; never modifies the file)."""
    if path is None:
        return LogTail(False, note="Kein gültiger Log-Pfad")
    try:
        stat = path.stat()
        with open(path, "rb") as handle:
            start = max(0, stat.st_size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except FileNotFoundError:
        return LogTail(False, note=f"Log-Datei nicht vorhanden: {path.name}")
    except OSError as exc:
        return LogTail(False, note=f"Log nicht lesbar ({type(exc).__name__})")
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    cut = start > 0
    if cut and lines:
        lines = lines[1:]  # the first line is probably cut in the middle
    if len(lines) > max_lines:
        lines, cut = lines[-max_lines:], True
    return LogTail(True, "\n".join(lines), cut, stat.st_size, datetime.fromtimestamp(stat.st_mtime))


class TaskBackend:
    """The blocking operations the GUI runs in worker threads."""

    def __init__(self, logs_dir: str, runner: Optional[CommandRunner] = None) -> None:
        self.logs_dir = logs_dir
        self.runner = runner or PowerShellRunner()

    def query(self) -> list[TaskInfo]:
        return enrich_with_logs(query_tasks(self.runner), self.logs_dir)

    def start(self, name: str) -> tuple[bool, str]:
        return start_task(self.runner, name)

    def tail(self, name: str) -> LogTail:
        return tail_log(log_path(self.logs_dir, name))
