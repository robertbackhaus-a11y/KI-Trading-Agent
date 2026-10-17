# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""View model of the Tasks page: the operations overview of the seven scheduled tasks (pure Python).

One row per task: rhythm (from its triggers), state with the last run and its result, the stand of the data it produces (from ``get_data_freshness``, not from
the exit code), the last success (a time at which the data prove that the task produced its output, or its last run if that ended with exit code 0), the next
run and the duration.  Dependencies are the technical ones of ``services.task_model``; a prerequisite that is stale or failed only produces a *warning* before a
manual start -- no other task is started and no scheduler definition is changed.
"""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from ..services import task_model as tm
from ..services.task_service import TaskInfo, result_text, rhythm_text, trigger_line
from .portfolio import DASH, fmt_time

STATE_TEXT = {"ready": "bereit", "running": "läuft", "disabled": "deaktiviert", "queued": "wartet", "unknown": "unbekannt"}
MAX_DURATION_HOURS = 6.0  # a log written more than this long after the start belongs to something else
SUCCESS_GAP_MINUTES = 5.0  # the last success is "the last run" when they are this close


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
    title: str = ""
    rhythm: str = DASH
    data_stand: str = DASH
    data_level: str = ""  # "", "warning" (older), "blocker" (stale)
    last_success: str = DASH
    duration: str = DASH
    details: str = ""
    run_warning: str = ""  # shown in the confirmation before a manual start
    dependency_lines: list[str] = field(default_factory=list)
    last_error: str = ""
    status_line: str = ""  # the table cell: state, last run and its result


def _short(value: Optional[datetime]) -> str:
    """``14.10. 10:55`` -- the table cells of this page are compact; the tooltip has the full time with year and seconds."""
    return value.strftime("%d.%m. %H:%M") if value else DASH


def _long(value: Optional[datetime]) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else DASH


def _iso_duration(text: str) -> str:
    """``PT2H`` -> ``2 Stunden``; the scheduler's own default of three days (``PT72H``) shows as 72 Stunden."""
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not match or not any(match.groups()):
        return text or DASH
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    total_h = days * 24 + hours
    def unit(n: int, one: str, many: str) -> str:
        return f"{n} {one if n == 1 else many}"

    parts = ([unit(total_h, "Stunde", "Stunden")] if total_h else []) + ([unit(minutes, "Minute", "Minuten")] if minutes else []) + ([unit(seconds, "Sekunde", "Sekunden")] if seconds else [])
    return " ".join(parts) or DASH


def _span(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 90:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 10:
        return f"{minutes} min {rest:02d} s"
    if minutes < 90:
        return f"{minutes} min"
    return f"{seconds / 3600:.1f} h".replace(".", ",")


def duration_text(task: TaskInfo, now: datetime) -> str:
    """The run time: so far for a running task, else about the time from the start to the last write of the task's log (the scheduler stores no duration)."""
    if task.last_run is None:
        return DASH
    if task.running:
        return "läuft seit " + _span(max(0.0, (now - task.last_run).total_seconds()))
    if task.log_modified is None or task.log_modified < task.last_run:
        return DASH
    seconds = (task.log_modified - task.last_run).total_seconds()
    return DASH if seconds > MAX_DURATION_HOURS * 3600 else "≈ " + _span(seconds)


def last_success(task: TaskInfo, fresh: Optional[tm.Freshness]) -> Optional[datetime]:
    """A time at which the task provably succeeded: what its data say (reports are written only after a complete run) and, if its last run ended with exit
    code 0, the start of that run.  The latest of them."""
    known = [c for c in (fresh.last_success if fresh is not None else None, task.last_run if task.last_result == 0 else None) if c is not None]
    return max(known) if known else None


def _error_text(task: TaskInfo) -> str:
    if task.last_result not in (None, *tm.NO_ERROR_RESULTS):
        return f"Windows-Ergebnis {result_text(task.last_result)} beim Lauf vom {fmt_time(task.last_run)}"
    return ""


def build_task_rows(tasks: list[TaskInfo], freshness: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None, logs_dir: str = "") -> list[TaskRowVM]:
    """The rows of the page.  ``freshness`` is the ``get_data_freshness`` answer (or ``None``); without it the data columns show a dash and the dependencies are unknown."""
    now = now or datetime.now()
    payload = freshness if isinstance(freshness, Mapping) and freshness.get("ok") is not False else None
    by_name = {t.name: t for t in tasks}
    stands = {spec.produces: tm.evaluate(spec.produces, payload, now, tm.RunFacts(by_name[spec.name].last_run, by_name[spec.name].last_result, by_name[spec.name].missed_runs)
                                         if spec.name in by_name and by_name[spec.name].found else None) for spec in tm.SPECS.values()}
    results = {t.name: t.last_result for t in tasks}
    rows: list[TaskRowVM] = []
    for task in tasks:
        spec = tm.SPECS.get(task.name)
        title = spec.title if spec else task.name
        if not task.found:
            rows.append(TaskRowVM(task.name, "nicht registriert", DASH, DASH, DASH, False, "Task ist nicht registriert", "blocker", title,
                                  details=f"{task.name}: nicht im Windows Task Scheduler registriert.", status_line="nicht registriert"))
            continue
        fresh = stands.get(spec.produces) if spec else None
        state = STATE_TEXT.get(task.state.lower(), task.state or "unbekannt")
        level = "warning" if task.last_result not in (None, *tm.NO_ERROR_RESULTS) else ""
        run_hint = "Task läuft bereits" if task.running else "Task ist deaktiviert" if task.disabled else "Aktion: startet den bestehenden Scheduled Task"
        dep_lines: list[str] = []
        warnings: list[str] = []
        for need in spec.needs if spec else ():
            dep_level, text = tm.dependency_state(need, stands, results)
            marker = {"ok": "✓", "warning": "⚠", "blocker": "✗", "info": "ℹ"}[dep_level]
            dep_lines.append(f"{marker} {'benötigt' if need.kind == tm.HARD else 'optional'}: {tm.SOURCE_TEXT.get(need.source, need.source)} "
                             f"(Task {need.task.replace('Trading-', '')}) – {text}")
            if dep_level != "ok":
                warnings.append(f"{'Vorläufer' if need.kind == tm.HARD else 'Optionaler Vorläufer'}: {text}")
        if task.silent is False:  # the task still starts a console program: a window opens on every run
            level = level or "warning"
            warnings_exec = ("Der Task öffnet bei jedem Lauf ein Konsolenfenster (Aktion: " + ntpath.basename(task.action_execute) + "); "
                             "einmalig Manage-TradingTasks.ps1 -Action UpdateAction ausführen.")
        else:
            warnings_exec = ""
        data_level = "" if fresh is None or fresh.level in (tm.OK, tm.UNKNOWN) else "warning" if fresh.level == tm.AGING else "blocker"
        if data_level or any(w.startswith("Vorläufer") for w in warnings) or task.log_hint:
            level = level or "warning"
        success = last_success(task, fresh)
        success_text = _short(success)
        if task.last_run and task.last_result not in (None, 267009, 267011) and (success is None or (task.last_run - success).total_seconds() > SUCCESS_GAP_MINUTES * 60):
            success_text += f" (Lauf {_short(task.last_run)}: {result_text(task.last_result)})"
        stand_text = fresh.as_of if fresh is not None else DASH
        if fresh is not None and fresh.level in (tm.AGING, tm.STALE):
            stand_text += f" – {tm.LEVEL_TEXT[fresh.level]}"
        elif fresh is not None and fresh.level == tm.UNKNOWN and stand_text == DASH:
            stand_text = "unbekannt"
        status_line = (f"{state} · Lauf {_short(task.last_run) if task.last_run else 'nie'}" + (f" · {result_text(task.last_result)}" if task.last_result is not None else "")
                  + (" · Log-Hinweis" if task.log_hint else ""))
        log_path = ntpath.join(logs_dir, task.log_file) if logs_dir and task.log_file else task.log_file
        error = _error_text(task) or (f"Hinweis aus dem Log-Ende: {task.log_hint}" if task.log_hint else "")
        kb = f"{task.log_size / 1024:,.0f}".replace(",", ".") if task.log_size is not None else ""
        log_size = f" ({kb} KB, zuletzt geschrieben {fmt_time(task.log_modified)})" if task.log_modified and task.log_size is not None else ""
        details = "\n".join(filter(None, [
            f"{task.name} ({title})",
            f"Rhythmus: {rhythm_text(task.triggers)}" if task.triggers else "Rhythmus: unbekannt (Auslöser nicht gelesen)",
            "Auslöser: " + "; ".join(trigger_line(t) for t in task.triggers) if task.triggers else "",
            "Nachholen verpasster Läufe (StartWhenAvailable): " + ("Ja" if task.start_when_available else "Nein" if task.start_when_available is False else "unbekannt"),
            "Keine zweite Instanz (IgnoreNew): " + ("Ja – ein neuer Start wird ignoriert, solange der Task läuft" if task.multiple_instances == "IgnoreNew"
                                                    else f"Nein (Mehrfachinstanzen: {task.multiple_instances})" if task.multiple_instances else "unbekannt"),
            ("Ausführung: unsichtbar (pythonw.exe, ohne Konsolenfenster)" if task.silent else f"Ausführung: {warnings_exec}" if task.silent is False else ""),
            f"Zeitlimit: {_iso_duration(task.time_limit)}" if task.time_limit else "",
            f"Verpasste Läufe: {task.missed_runs}" if task.missed_runs is not None else "",
            "Aktiviert: " + ("Nein" if task.disabled else "Ja"),
            "Abhängigkeiten (technisch, aus dem Code der Skripte; kein Task startet einen anderen):" if spec else "",
            *(["  " + line for line in dep_lines] if dep_lines else (["  keine – eigenständig"] if spec else [])),
            f"Genutzt von: {spec.reads_by}" if spec and spec.reads_by else "",
            f"Hinweis: {spec.note}" if spec and spec.note else "",
            f"Datenstand: {fresh.detail}" if fresh is not None and fresh.detail else "",
            f"Letzter Lauf: {fmt_time(task.last_run)} – Ergebnis {result_text(task.last_result)}",
            f"Letzter Erfolg: {_long(success)}{success_text[len(_short(success)):]}" if success else "Letzter Erfolg: nicht bekannt",
            f"Dauer: {duration_text(task, now)} (aus Start und letztem Schreiben des Logs; der Scheduler speichert keine Laufzeit)",
            f"Letzter Fehler: {error}" if error else "Letzter Fehler: keiner erkannt",
            f"Log: {log_path}{log_size}",
        ]))
        rows.append(TaskRowVM(task.name, state, fmt_time(task.last_run), _short(task.next_run), result_text(task.last_result), not task.running and not task.disabled, run_hint, level,
                              title, rhythm_text(task.triggers) if task.triggers else DASH, stand_text, data_level, success_text, duration_text(task, now), details,
                              "\n".join(f"• {w}" for w in warnings), dep_lines, error, status_line))
    return rows


START_GRACE_S = 120.0  # the scheduler normally shows the new run within seconds; without a new run after this long the start is not confirmed
WATCH_LIMIT_H = 3.0  # a manual run is followed for at most this long
RUN_CLOCK_SLACK_S = 5.0  # LastRunTime is the scheduler's clock


@dataclass
class RunWatch:
    """A task that was started from the page: followed until the scheduler reports its end (or the start was never confirmed)."""

    name: str
    started: datetime
    seen_running: bool = False


def run_progress(watch: RunWatch, info: Optional[TaskInfo], now: datetime) -> tuple[str, str, bool]:
    """``(text, level, finished)`` for a manual run, from the same scheduler answer the table uses (state, last run, last result).

    Nothing is inferred beyond that: the task is running (state ``Running``), or the scheduler reports a run that started after the click and its result - the exit code of the
    script, which the launcher hands on - or, after the grace period, that no new run was seen.
    """
    spec = tm.SPECS.get(watch.name)
    title = spec.title if spec else watch.name
    if info is None or not info.found:
        return f"{title}: nicht im Scheduler registriert.", "blocker", True
    if info.running:
        watch.seen_running = True
        return f"{title}: läuft seit {_span((now - watch.started).total_seconds())} (gestartet {watch.started:%H:%M:%S}) – Status wird alle paar Sekunden aktualisiert.", "info", False
    new_run = info.last_run is not None and (info.last_run - watch.started).total_seconds() >= -RUN_CLOCK_SLACK_S
    if new_run:
        code = info.last_result
        ok = code == 0
        text = (f"{title}: beendet ({_short(info.last_run)}) – Ergebnis {result_text(code)}" + (" (Exitcode 0)" if ok else "")
                + (f"; Dauer {duration_text(info, now)}" if duration_text(info, now) != DASH else "") + ". Das Log steht unten.")
        if info.log_hint and not ok:
            text += f" Hinweis aus dem Log: {info.log_hint}"
        return text, "info" if ok else "blocker", True
    waited = (now - watch.started).total_seconds()
    if waited > WATCH_LIMIT_H * 3600 or (waited > START_GRACE_S and not watch.seen_running):
        return f"{title}: der Scheduler meldet keinen neuen Lauf – der Start ist nicht bestätigt.", "warning", True
    return f"{title}: wird gestartet …", "info", False


ETF_HEADERS = ["Datensatz", "Stand", "Alter", "Status", "Hinweis"]
ETF_STATUS_TEXT = {"MISSING": "fehlt", "INVALID": "ungültig"}


def build_etf_rows(freshness: Optional[Mapping[str, Any]], now: Optional[datetime] = None) -> tuple[list[list[str]], list[str]]:
    """``(rows, levels)`` of the small ETF table under the tasks: the six datasets, each with its stand, age and verdict.  The SPXS sector allocation says "manuell" and shows
    its date and age; it never turns the task red."""
    now = now or datetime.now()
    payload = freshness if isinstance(freshness, Mapping) and freshness.get("ok") is not False else None
    facts = ((payload or {}).get("sources") or {}).get(tm.ETF_DATA) if isinstance(payload, Mapping) else None
    rows: list[list[str]] = []
    levels: list[str] = []
    for d in tm.etf_datasets(facts, now):
        if d.manual:
            status = "manuell gepflegt" + (" – älter als die Grenze der Loader" if d.level != tm.UNKNOWN and d.age_days is not None and d.age_days > int(facts.get("stale_after_days") or 120) else "")
            note = "Invesco veröffentlicht die Sektorverteilung nur als Factsheet-PDF; wird nicht automatisch aktualisiert"
            level = "warning" if d.age_days is not None and d.age_days > tm.MANUAL_FRESH_D else ""
        else:
            status = ETF_STATUS_TEXT.get(d.status) or tm.LEVEL_TEXT[d.level]
            note = f"{d.rows} Zeilen" if d.dataset == "holdings" and d.rows else ""
            level = "" if d.level in (tm.OK, tm.UNKNOWN) else "warning" if d.level == tm.AGING else "blocker"
        rows.append([tm.etf_label(d), tm.fmt_day(d.as_of), f"{d.age_days} Tage" if d.age_days is not None else DASH, status, note or d.detail])
        levels.append(level)
    return rows, levels


def summarize(rows: list[TaskRowVM], freshness_available: bool) -> str:
    """One line above the table: how many tasks need a look."""
    issues = [r for r in rows if r.level]
    stale = [r.title or r.name for r in rows if r.data_level == "blocker"]
    parts = [f"{len(rows)} Tasks"]
    if stale:
        parts.append("Daten veraltet: " + ", ".join(stale))
    if len(issues) > len(stale):
        parts.append(f"{len(issues)} mit Hinweis")
    if not freshness_available:
        parts.append("Datenstand nicht verfügbar (MCP nicht verbunden?)")
    if len(parts) == 1 and rows:
        parts.append("alles im Plan")
    return " · ".join(parts)
