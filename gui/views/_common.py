"""Small shared helpers of the views (tables and form rows)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QFormLayout, QHeaderView, QLabel, QTableWidget, QTableWidgetItem

LEVEL_COLORS = {"blocker": "#c0392b", "warning": "#b9770e", "info": "#5d6d7e"}


def make_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def fill_table(table: QTableWidget, headers: list[str], rows: list[list[str]], levels: Optional[list[str]] = None) -> None:
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        level = levels[r] if levels and r < len(levels) else ""
        for c, text in enumerate(row):
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if level in LEVEL_COLORS:
                item.setForeground(QBrush(QColor(LEVEL_COLORS[level])))
            table.setItem(r, c, item)
    table.resizeColumnsToContents()


def set_form_rows(form: QFormLayout, rows: list[tuple[str, str]]) -> None:
    while form.rowCount():
        form.removeRow(0)
    for label, value in rows:
        text = QLabel(value)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(label + ":", text)
