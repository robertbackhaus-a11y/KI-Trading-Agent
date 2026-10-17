# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Tasks page: the operations overview of the seven scheduled Trading tasks (rhythm, state, data stand, last success, next run, duration), "Run now" (an ACTION, with
confirmation and a warning when a prerequisite is stale or failed), the progress and result of a manual run, and a log tail."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor, QFontDatabase
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from ..services.task_service import LogTail
from ..viewmodels.portfolio import DASH
from ..viewmodels.tasks import ETF_HEADERS, TaskRowVM
from ._common import LEVEL_COLORS, fill_table, make_table

HEADERS = ["Task", "Rhythmus", "Status", "Datenstand", "Letzter Erfolg", "Nächster Lauf", "Dauer", "Aktion", "Log"]
ACTION_COLUMN, LOG_COLUMN, DATA_COLUMN = 7, 8, 3


class TasksView(QWidget):
    refresh_requested = Signal()
    run_requested = Signal(str)
    log_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.confirm: Callable[[str], bool] = self._ask  # replaceable (tests)
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<h2>Tasks</h2>"))
        top.addStretch(1)
        self.refreshed = QLabel("Letzte Aktualisierung: " + DASH)
        self.button = QPushButton("Aktualisieren")
        self.button.setToolTip("Lesende Abfrage des Windows Task Schedulers")
        self.button.clicked.connect(self.refresh_requested.emit)
        top.addWidget(self.refreshed)
        top.addWidget(self.button)
        outer.addLayout(top)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.run_status = QLabel("")  # the manual run in progress / its result (state, exit code); the log below
        self.run_status.setWordWrap(True)
        outer.addWidget(self.run_status)
        self.table = make_table(HEADERS)
        self.table.setMinimumHeight(260)
        outer.addWidget(self.table)
        self.etf_title = QLabel("ETF-Daten (Holdings und Sektor-Look-through; Stand je Datensatz)")
        self.etf_table = make_table(ETF_HEADERS)
        self.etf_table.setMaximumHeight(190)
        outer.addWidget(self.etf_title)
        outer.addWidget(self.etf_table)
        self.log_title = QLabel("Log: (Task wählen)")
        outer.addWidget(self.log_title)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        outer.addWidget(self.log, 1)
        self.run_buttons: dict[str, QPushButton] = {}
        self.rows: dict[str, TaskRowVM] = {}
        self.last_confirm_text = ""  # what the last confirmation said (tests)

    @staticmethod
    def _ask_text(name: str, warning: str = "") -> str:
        text = f"Den bestehenden Scheduled Task\n\n{name}\n\njetzt starten?\n\nDas ist eine Aktion: Der Task läuft wie geplant (Netzzugriff und Datenbank-Schreibzugriffe des Tasks sind möglich)."
        if warning:
            text += f"\n\nAchtung – nötige Daten sind nicht in Ordnung:\n{warning}\n\nDas Ergebnis des Tasks kann dadurch unvollständig oder veraltet sein. Es wird kein anderer Task gestartet."
        return text

    def _ask(self, name: str) -> bool:
        row = self.rows.get(name)
        self.last_confirm_text = self._ask_text(name, row.run_warning if row else "")
        box = QMessageBox(QMessageBox.Icon.Warning, "Task starten", self.last_confirm_text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def show_etf(self, rows: list[list[str]], levels: list[str]) -> None:
        fill_table(self.etf_table, ETF_HEADERS, rows, levels)
        self.etf_title.setVisible(bool(rows))
        self.etf_table.setVisible(bool(rows))

    def show_rows(self, rows: list[TaskRowVM], refreshed: str = DASH, summary: str = "", etf: Optional[tuple[list[list[str]], list[str]]] = None) -> None:
        self.button.setEnabled(True)
        if etf is not None:
            self.show_etf(*etf)
        self.refreshed.setText("Letzte Aktualisierung: " + refreshed)
        self.message.setText(summary)
        self.rows = {r.name: r for r in rows}
        fill_table(self.table, HEADERS, [[r.title or r.name, r.rhythm, r.status_line or r.status, r.data_stand, r.last_success, r.next_run, r.duration, "", ""] for r in rows], [r.level for r in rows])
        self.run_buttons.clear()
        for index, row in enumerate(rows):
            for column in range(len(HEADERS)):
                item = self.table.item(index, column)
                if item is not None:
                    item.setToolTip(row.details or item.text())
            data = self.table.item(index, DATA_COLUMN)
            if data is not None and row.data_level:
                data.setForeground(QBrush(QColor(LEVEL_COLORS["blocker" if row.data_level == "blocker" else "warning"])))
            run = QPushButton("▶ Jetzt ausführen …")
            run.setEnabled(row.can_run)
            run.setToolTip(row.run_hint)
            run.clicked.connect(lambda _=False, n=row.name: self._on_run(n))
            self.run_buttons[row.name] = run
            self.table.setCellWidget(index, ACTION_COLUMN, run)
            log = QPushButton("Log anzeigen")
            log.clicked.connect(lambda _=False, n=row.name: self.log_requested.emit(n))
            self.table.setCellWidget(index, LOG_COLUMN, log)
        self.table.resizeColumnsToContents()

    def _on_run(self, name: str) -> None:
        if self.confirm(name):
            self.run_requested.emit(name)

    def show_error(self, text: str) -> None:
        self.button.setEnabled(True)
        self.message.setText(f"<span style='color:{LEVEL_COLORS['blocker']}'>{text}</span>")

    def show_message(self, text: str) -> None:
        self.message.setText(text)

    def show_run_status(self, text: str, level: str = "info") -> None:
        """The progress or the result of a manual run (own line: the summary above is rewritten by every refresh)."""
        color = LEVEL_COLORS.get(level, LEVEL_COLORS["info"])
        self.run_status.setText(f"<span style='color:{color}'>{text}</span>" if text else "")

    def show_log(self, name: str, tail: LogTail) -> None:
        if not tail.available:
            self.log_title.setText(f"Log: {name}")
            self.log.setPlainText(tail.note)
            return
        size = f"{tail.size / 1024:,.0f} KB".replace(",", ".")
        when = tail.modified.strftime("%d.%m.%Y %H:%M") if tail.modified else DASH
        self.log_title.setText(f"Log: {name} – {size}, zuletzt geändert {when}" + (" – nur das Ende wird gezeigt" if tail.truncated else ""))
        self.log.setPlainText(tail.text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
