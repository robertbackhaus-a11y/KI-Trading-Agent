"""Dashboard page: portfolio status, planner status and the planned / deferred entries (read-only)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget

from ..viewmodels.portfolio import DASH, DashboardVM
from ._common import LEVEL_COLORS, fill_table, make_table, set_form_rows


class DashboardView(QWidget):
    refresh_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<h2>Dashboard</h2>"))
        top.addStretch(1)
        self.refreshed = QLabel("Letzte Aktualisierung: " + DASH)
        self.button = QPushButton("Aktualisieren")
        self.button.setToolTip("Lesende Abfrage: run_trading_orchestrator (detail) und database_status")
        self.button.clicked.connect(self.refresh_requested.emit)
        top.addWidget(self.refreshed)
        top.addWidget(self.button)
        outer.addLayout(top)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        body = QWidget()
        layout = QVBoxLayout(body)
        row = QHBoxLayout()
        self.portfolio_form, self.planner_form = QFormLayout(), QFormLayout()
        for caption, form in (("Portfolio", self.portfolio_form), ("Planner (Simulation)", self.planner_form)):
            box = QGroupBox(caption)
            box.setLayout(form)
            row.addWidget(box, 1)
        layout.addLayout(row)
        self.planned = make_table(["Rang"])
        self.deferred = make_table(["Rang"])
        for caption, table in (("Geplante Entries", self.planned), ("Zurückgestellte Entries", self.deferred)):
            box = QGroupBox(caption)
            inner = QVBoxLayout(box)
            inner.addWidget(table)
            table.setMinimumHeight(120)
            layout.addWidget(box)
        box = QGroupBox("Blocker und Hinweise")
        inner = QVBoxLayout(box)
        self.issues = QListWidget()
        self.issues.setMinimumHeight(120)
        inner.addWidget(self.issues)
        layout.addWidget(box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    def show_loading(self) -> None:
        self.button.setEnabled(False)
        self.message.setText("Lade Daten …")

    def show_vm(self, vm: DashboardVM) -> None:
        self.button.setEnabled(True)
        self.refreshed.setText("Letzte Aktualisierung: " + vm.refreshed)
        if not vm.available:
            self.message.setText(f"<span style='color:{LEVEL_COLORS['blocker']}'>Keine Daten: {vm.error}</span>")
            return
        self.message.setText("")
        set_form_rows(self.portfolio_form, vm.portfolio)
        set_form_rows(self.planner_form, vm.planner)
        fill_table(self.planned, vm.planned.headers, vm.planned.rows, vm.planned.levels)
        fill_table(self.deferred, vm.deferred.headers, vm.deferred.rows, vm.deferred.levels)
        self.issues.clear()
        for level, text in vm.issues:
            item = QListWidgetItem(text)
            if level in LEVEL_COLORS:
                item.setForeground(QBrush(QColor(LEVEL_COLORS[level])))
            self.issues.addItem(item)
        if not vm.issues:
            self.issues.addItem("keine")
