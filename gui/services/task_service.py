"""Windows Task Scheduler access of the GUI (PowerShell with JSON output; no MCP, no Qt).

Only the six known Trading tasks are ever queried or started.  "Run now" starts the *existing* scheduled task (``Start-ScheduledTask``);
the underlying script is never launched directly and no task definition is changed.  The scheduler's own ``MultipleInstances = IgnoreNew``
stays in effect; the GUI additionally refuses to start a task that is already running or disabled.  Output is JSON, never localized text.
"""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
from dataclasses import dataclass
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
)
TASK_NAMES = tuple(t.name for t in TASKS)
RESULT_TEXT = {0: "OK", 267009: "läuft", 267010: "nicht geplant", 267011: "noch nie gelaufen", 267014: "beendet (Zeitlimit/Abbruch)", 1: "Fehler (1)"}
CREATE_NO_WINDOW = 0x08000000


class CommandRunner(Protocol):
    def run(self, args: list[str], timeout: float) -> tuple[int, str, str]: ...


class PowerShellRunner:
    """Runs ``args`` as a subprocess without a console window and decodes the output as UTF-8."""

    def run(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        done = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)  # noqa: S603 - fixed argument list
        return done.returncode, done.stdout.decode("utf-8-sig", "replace"), done.stderr.decode("utf-8", "replace")


@dataclass
class TaskInfo:
    name: str
    found: bool = False
    state: str = ""
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    last_result: Optional[int] = None
    log_file: str = ""

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
        "last_result = [int64]$i.LastTaskResult } } }; "
        "ConvertTo-Json -InputObject @($rows) -Compress"
    )


def run_now_command(name: str) -> list[str]:
    """The exact command for "Run now"; ``name`` must be one of the six known tasks."""
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
        out.append(TaskInfo(
            task.name, True, str(item.get("state") or ""), _when(item.get("last_run")), _when(item.get("next_run")),
            int(result) if isinstance(result, (int, float)) else None, task.log_file,
        ))
    return out


def result_text(code: Optional[int]) -> str:
    if code is None:
        return "—"
    if code in RESULT_TEXT:
        return RESULT_TEXT[code]
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
        return query_tasks(self.runner)

    def start(self, name: str) -> tuple[bool, str]:
        return start_task(self.runner, name)

    def tail(self, name: str) -> LogTail:
        return tail_log(log_path(self.logs_dir, name))
