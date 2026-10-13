"""Planner / Actions page: engine actions, planned and deferred entries, sector and correlation risk (read-only, no order functions)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QTabWidget, QVBoxLayout, QWidget

from ..viewmodels.portfolio import DASH, PlannerVM
from ._common import LEVEL_COLORS, fill_table, make_table, set_form_rows, tooltip_of


class PlannerView(QWidget):
    refresh_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<h2>Planner / Actions</h2>"))
        top.addStretch(1)
        self.button = QPushButton("Aktualisieren")
        self.button.clicked.connect(self.refresh_requested.emit)
        top.addWidget(self.button)
        outer.addLayout(top)
        note = QLabel("Nur Anzeige: Simulation, keine Orders. Die Orderausführung erfolgt außerhalb des Trading Agents.")
        note.setWordWrap(True)
        outer.addWidget(note)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.header = QFormLayout()
        outer.addLayout(self.header)
        self.counts = QLabel("Aktionen der Engine: " + DASH)
        outer.addWidget(self.counts)
        self.tabs = QTabWidget()
        self.tables = {key: make_table(["x"]) for key in ("positions", "planned", "deferred", "candidates")}
        for key, caption in (("positions", "Positionen / Aktionen"), ("planned", "Geplante Entries"), ("deferred", "Zurückgestellt"), ("candidates", "Kandidaten && Risiko")):
            self.tabs.addTab(self.tables[key], caption)
        self.notes = QListWidget()
        self.tabs.addTab(self.notes, "Hinweise")
        outer.addWidget(self.tabs, 1)

    def show_loading(self) -> None:
        self.button.setEnabled(False)
        self.message.setText("Lade Daten …")

    def show_vm(self, vm: PlannerVM) -> None:
        self.button.setEnabled(True)
        if not vm.available:
            self.message.setText(f"<span style='color:{LEVEL_COLORS['blocker']}'>Keine Daten: {vm.error}</span>")
            return
        self.message.setText("")
        set_form_rows(self.header, vm.header)
        self.counts.setText("Aktionen der Engine: " + vm.counts)
        for key, widget in self.tables.items():
            table = getattr(vm, key)
            fill_table(widget, table.headers, table.rows, table.levels)
        self.tabs.setTabText(1, f"Geplante Entries ({len(vm.planned.rows)})")
        self.tabs.setTabText(2, f"Zurückgestellt ({len(vm.deferred.rows)})")
        self.notes.clear()
        for note in vm.notes or ["keine"]:
            item = QListWidgetItem(note)
            item.setToolTip(tooltip_of(note))
            self.notes.addItem(item)
