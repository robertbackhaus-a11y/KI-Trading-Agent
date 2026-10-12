"""Tasks page: the six scheduled Trading tasks, "Run now" (an ACTION, with confirmation) and a log tail."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from ..services.task_service import LogTail
from ..viewmodels.portfolio import DASH
from ..viewmodels.tasks import TaskRowVM
from ._common import LEVEL_COLORS, fill_table, make_table

HEADERS = ["Task", "Status", "Letzter Lauf", "Nächster Lauf", "Ergebnis", "Aktion", "Log"]


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
        self.table = make_table(HEADERS)
        self.table.setMinimumHeight(230)
        outer.addWidget(self.table)
        self.log_title = QLabel("Log: (Task wählen)")
        outer.addWidget(self.log_title)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        outer.addWidget(self.log, 1)
        self.run_buttons: dict[str, QPushButton] = {}

    @staticmethod
    def _ask_text(name: str) -> str:
        return f"Den bestehenden Scheduled Task\n\n{name}\n\njetzt starten?\n\nDas ist eine Aktion: Der Task läuft wie geplant (Netzzugriff und Datenbank-Schreibzugriffe des Tasks sind möglich)."

    def _ask(self, name: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, "Task starten", self._ask_text(name), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def show_rows(self, rows: list[TaskRowVM], refreshed: str = DASH) -> None:
        self.button.setEnabled(True)
        self.refreshed.setText("Letzte Aktualisierung: " + refreshed)
        self.message.setText("")
        fill_table(self.table, HEADERS, [[r.name, r.status, r.last_run, r.next_run, r.result, "", ""] for r in rows], [r.level for r in rows])
        self.run_buttons.clear()
        for index, row in enumerate(rows):
            run = QPushButton("▶ Jetzt starten …")
            run.setEnabled(row.can_run)
            run.setToolTip(row.run_hint)
            run.clicked.connect(lambda _=False, n=row.name: self._on_run(n))
            self.run_buttons[row.name] = run
            self.table.setCellWidget(index, 5, run)
            log = QPushButton("Log anzeigen")
            log.clicked.connect(lambda _=False, n=row.name: self.log_requested.emit(n))
            self.table.setCellWidget(index, 6, log)
        self.table.resizeColumnsToContents()

    def _on_run(self, name: str) -> None:
        if self.confirm(name):
            self.run_requested.emit(name)

    def show_error(self, text: str) -> None:
        self.button.setEnabled(True)
        self.message.setText(f"<span style='color:{LEVEL_COLORS['blocker']}'>{text}</span>")

    def show_message(self, text: str) -> None:
        self.message.setText(text)

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
