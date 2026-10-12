"""View model of the Tasks page (pure Python)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..services.task_service import TaskInfo, result_text
from .portfolio import DASH, fmt_time

STATE_TEXT = {"ready": "bereit", "running": "läuft", "disabled": "deaktiviert", "queued": "wartet", "unknown": "unbekannt"}


@dataclass
class TaskRowVM:
    name: str
    status: str
    last_run: str
    next_run: str
    result: str
    can_run: bool
    run_hint: str
    level: str = ""  # "", "warning", "blocker"


def build_task_rows(tasks: list[TaskInfo]) -> list[TaskRowVM]:
    rows: list[TaskRowVM] = []
    for task in tasks:
        if not task.found:
            rows.append(TaskRowVM(task.name, "nicht registriert", DASH, DASH, DASH, False, "Task ist nicht registriert", "blocker"))
            continue
        status = STATE_TEXT.get(task.state.lower(), task.state or "unbekannt")
        level = ""
        if task.last_result not in (None, 0, 267009, 267011):
            level = "warning"
        if task.running:
            run_hint = "Task läuft bereits"
        elif task.disabled:
            run_hint = "Task ist deaktiviert"
        else:
            run_hint = "Aktion: startet den bestehenden Scheduled Task"
        rows.append(TaskRowVM(task.name, status, fmt_time(task.last_run), fmt_time(task.next_run), result_text(task.last_result),
                              not task.running and not task.disabled, run_hint, level))
    return rows
