"""Watchlist page: the whole watchlist (read from ``get_watchlist``), filters, and the three explicit actions: add, remove, change priority.

Nothing is written while scrolling or filtering: every write needs a button (or the context menu) and, for removing, a confirmation whose
default is "Abbrechen".  A security that is not in the Security Master is never created here; the dialog says so.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..viewmodels.onboarding import Outcome
from ..viewmodels.portfolio import DASH
from ..viewmodels.watchlist import ALL, HEADERS, NUMERIC_COLUMNS, SPIN_MAX, SPIN_MIN, ActionMessage, WatchlistVM, WatchRow, apply_filter, filter_options, strategy_text
from ..viewmodels.watchlist_edit import PENDING_TEXT, PENDING_TIP, display, prune, strategy_editable
from ._common import LEVEL_COLORS, tooltip_of
from .onboarding import OnboardSecurityDialog
from .watchlist_edit import EDIT_ROLE, PriorityDelegate, StrategyDelegate

ROW_ID = Qt.ItemDataRole.UserRole + 1
SORT_KEY = Qt.ItemDataRole.UserRole
EDIT_COLUMN = len(HEADERS)  # the marker column "Änderung" behind the data columns
STRATEGY_COLUMN, PRIORITY_COLUMN = 2, 3


def _color(level: str) -> str:
    return LEVEL_COLORS.get(level, LEVEL_COLORS["info"])


class SortItem(QTableWidgetItem):
    """A table cell that sorts by value (numbers as numbers, an empty value last) instead of by its text."""

    def __lt__(self, other: QTableWidgetItem) -> bool:  # noqa: D105 - Qt API
        a, b = self.data(SORT_KEY), other.data(SORT_KEY)
        if a is None:
            return False
        if b is None:
            return True
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class AddCandidateDialog(QDialog):
    """Identifier (+ optional priority).  Non-blocking: the answer of the MCP action arrives later through :meth:`show_result`."""

    check_requested = Signal(str)
    add_requested = Signal(str, object)
    onboard_requested = Signal()  # "Neues Wertpapier aufnehmen": only offered after "noch nicht im Security Master", and only by a click

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Kandidat hinzufügen")
        self.setModal(True)
        self.setMinimumWidth(460)
        outer = QVBoxLayout(self)
        hint = QLabel("Hier werden nur Wertpapiere aufgenommen, die bereits im Security Master stehen. Ein unbekanntes Wertpapier legt erst „Neues Wertpapier aufnehmen“ an.")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        form = QFormLayout()
        self.identifier = QLineEdit()
        self.identifier.setPlaceholderText("Symbol, ISIN, WKN, security_id oder vollständiger Firmenname")
        self.identifier.setMaxLength(80)
        form.addRow("Identifier:", self.identifier)
        row = QHBoxLayout()
        self.use_priority = QCheckBox("Priorität setzen")
        self.priority = QSpinBox()
        self.priority.setRange(SPIN_MIN, SPIN_MAX)
        self.priority.setValue(0)
        self.priority.setEnabled(False)
        self.use_priority.toggled.connect(self.priority.setEnabled)
        row.addWidget(self.use_priority)
        row.addWidget(self.priority)
        row.addStretch(1)
        form.addRow("Optional:", row)
        outer.addLayout(form)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        outer.addWidget(self.result)
        buttons = QHBoxLayout()
        self.check = QPushButton("Prüfen")
        self.check.setToolTip("Löst den Identifier nur auf und zeigt das gefundene Wertpapier – es wird nichts geschrieben")
        self.cancel = QPushButton("Abbrechen")
        self.add = QPushButton("Hinzufügen")
        self.add.setDefault(True)
        self.onboard = QPushButton("Neues Wertpapier aufnehmen …")
        self.onboard.setToolTip("Öffnet einen eigenen Dialog, in dem das Wertpapier mit eigenen Angaben geprüft und angelegt wird")
        self.onboard.setVisible(False)
        buttons.addWidget(self.check)
        buttons.addWidget(self.onboard)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.add)
        outer.addLayout(buttons)
        self.cancel.clicked.connect(self.reject)
        self.check.clicked.connect(self._on_check)
        self.add.clicked.connect(self._on_add)
        self.onboard.clicked.connect(self.onboard_requested.emit)
        self.identifier.returnPressed.connect(self._on_add)

    def value(self) -> tuple[str, Optional[int]]:
        return self.identifier.text().strip(), (self.priority.value() if self.use_priority.isChecked() else None)

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.check, self.add, self.identifier, self.use_priority):
            widget.setEnabled(not busy)
        self.priority.setEnabled(not busy and self.use_priority.isChecked())

    def _on_check(self) -> None:
        text, _ = self.value()
        if not text:
            self.show_text("Bitte einen Identifier eingeben.", "warning")
            return
        self._set_busy(True)
        self.onboard.setVisible(False)
        self.show_text("Prüfe …", "info")
        self.check_requested.emit(text)

    def _on_add(self) -> None:
        text, priority = self.value()
        if not text:
            self.show_text("Bitte einen Identifier eingeben.", "warning")
            return
        self._set_busy(True)
        self.onboard.setVisible(False)
        self.show_text("Füge hinzu …", "info")
        self.add_requested.emit(text, priority)

    def show_text(self, text: str, level: str) -> None:
        self.result.setText(f"<span style='color:{_color(level)}'>{text.replace(chr(10), '<br>')}</span>")

    def show_result(self, message: ActionMessage) -> None:
        self._set_busy(False)
        self.show_text(message.text, message.level)
        self.onboard.setVisible(message.result == "SECURITY_NOT_FOUND")
        if message.done:
            self.accept()


class WatchlistView(QWidget):
    refresh_requested = Signal()
    add_requested = Signal(str, object)  # identifier, priority or None
    check_requested = Signal(str)
    remove_requested = Signal(int)  # security_id
    edit_apply_requested = Signal(int, dict)  # security_id, the pending changes {"strategy": value, "priority": value} (only the keys that changed)
    onboard_check_requested = Signal(dict)  # arguments of a dry run of onboard_security
    onboard_create_requested = Signal(dict)
    onboard_watchlist_requested = Signal(int)  # an already existing security -> the ordinary add action

    def __init__(self) -> None:
        super().__init__()
        self.confirm_remove: Callable[[WatchRow], bool] = self._ask_remove  # replaceable (tests)
        self.add_dialog: Optional[AddCandidateDialog] = None
        self.onboard_dialog: Optional[OnboardSecurityDialog] = None
        self.confirm_discard: Callable[[], bool] = self._ask_discard  # replaceable (tests)
        self._pending: dict[int, dict] = {}  # unsaved changes per security_id; local until "Änderung übernehmen"
        self._applying: set[int] = set()
        self._vm: Optional[WatchlistVM] = None
        self._by_id: dict[int, WatchRow] = {}
        self._action_html = ""  # the outcome of the last action stays visible while the list reloads
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<h2>Watchlist</h2>"))
        top.addStretch(1)
        self.summary = QLabel(DASH)
        self.refreshed = QLabel("Letzte Aktualisierung: " + DASH)
        self.refresh = QPushButton("Aktualisieren")
        self.refresh.setToolTip("Lesende Abfrage: get_watchlist")
        self.add = QPushButton("Kandidat hinzufügen …")
        self.add.setToolTip("Nimmt ein Wertpapier, das bereits im Security Master steht, auf die Watchlist auf")
        for widget in (self.summary, self.refreshed, self.refresh, self.add):
            top.addWidget(widget)
        outer.addLayout(top)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Suche: Symbol, Name oder ISIN")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.status_filter, self.strategy_filter, self.quality_filter = QComboBox(), QComboBox(), QComboBox()
        filters.addWidget(self.search)
        for label, combo in (("Status", self.status_filter), ("Strategie", self.strategy_filter), ("Daten", self.quality_filter)):
            filters.addWidget(QLabel(label + ":"))
            combo.addItem(ALL)
            filters.addWidget(combo)
        filters.addStretch(1)
        outer.addLayout(filters)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.table = QTableWidget(0, len(HEADERS) + 1)
        self.table.setHorizontalHeaderLabels([*HEADERS, "Änderung"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)  # only the two editable columns have an editor
        self.table.setItemDelegateForColumn(STRATEGY_COLUMN, StrategyDelegate(lambda sid, value: self._on_edited(sid, "strategy", value), self.table))
        self.table.setItemDelegateForColumn(PRIORITY_COLUMN, PriorityDelegate(lambda sid, value: self._on_edited(sid, "priority", value), self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        outer.addWidget(self.table, 1)
        bottom = QHBoxLayout()
        self.remove_button = QPushButton("Von der Watchlist entfernen …")
        self.apply_button = QPushButton("Änderung übernehmen")
        self.apply_button.setToolTip("Speichert die geänderte Strategie und/oder Priorität der markierten Zeile")
        self.discard_button = QPushButton("Änderung verwerfen")
        self.discard_button.setToolTip("Setzt die ungespeicherte Änderung der markierten Zeile zurück")
        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        bottom.addWidget(self.apply_button)
        bottom.addWidget(self.discard_button)
        bottom.addWidget(self.remove_button)
        bottom.addWidget(self.notes, 1)
        outer.addLayout(bottom)
        self.remove_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.discard_button.setEnabled(False)

        self.refresh.clicked.connect(self._on_refresh_clicked)
        self.add.clicked.connect(self.open_add_dialog)
        self.search.textChanged.connect(self._render)
        for combo in (self.status_filter, self.strategy_filter, self.quality_filter):
            combo.currentIndexChanged.connect(self._render)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.remove_button.clicked.connect(self._on_remove)
        self.apply_button.clicked.connect(self._on_apply)
        self.discard_button.clicked.connect(self._on_discard)

    # -- data -----------------------------------------------------------------------------------------------
    def _on_refresh_clicked(self) -> None:
        if self._pending:  # a reload would replace the unsaved changes: ask, the default is "Abbrechen"
            if not self.confirm_discard():
                return
            self._pending.clear()
            self._render()
        self._action_html = ""
        self.refresh_requested.emit()

    def show_loading(self) -> None:
        self.refresh.setEnabled(False)
        if not self._action_html:
            self.message.setText("Lade Daten …")

    def show_vm(self, vm: WatchlistVM) -> None:
        self.refresh.setEnabled(True)
        if not vm.available:
            self.message.setText(f"<span style='color:{_color('blocker')}'>Keine Daten: {vm.error}</span>")
            return
        self.message.setText(self._action_html)
        self._vm = vm
        self._by_id = {r.security_id: r for r in vm.rows}
        self._pending = prune(self._pending, self._by_id)  # a saved value, or a vanished row, ends the pending change; the rest stays
        self.summary.setText(vm.summary)
        self.refreshed.setText("Letzte Aktualisierung: " + vm.refreshed)
        self.notes.setText(" · ".join(vm.notes))
        options = filter_options(vm.rows)
        for combo, key in ((self.status_filter, "status"), (self.strategy_filter, "strategy"), (self.quality_filter, "quality")):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(options[key])
            combo.setCurrentText(current if current in options[key] else ALL)
            combo.blockSignals(False)
        self._render()

    def show_error(self, text: str) -> None:
        self.refresh.setEnabled(True)
        self.message.setText(f"<span style='color:{_color('blocker')}'>{text}</span>")

    def show_message(self, message: ActionMessage, to_dialog: bool = False) -> None:
        """The outcome of an action: in the open dialog (while it runs there) and in the page's message line."""
        if to_dialog and self.add_dialog is not None:
            self.add_dialog.show_result(message)
        if not (to_dialog and self.add_dialog is not None and not message.done):
            self._action_html = f"<span style='color:{_color(message.level)}'>{message.text.replace(chr(10), '<br>')}</span>"
            self.message.setText(self._action_html)

    def shown_rows(self) -> list[WatchRow]:
        if self._vm is None:
            return []
        return apply_filter(self._vm.rows, self.search.text(), self.status_filter.currentText(), self.strategy_filter.currentText(), self.quality_filter.currentText())

    def _render(self, *_: object) -> None:
        rows = self.shown_rows()
        header = self.table.horizontalHeader()
        column, order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        selected = self.selected_id()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            edits = self._pending.get(row.security_id) or {}
            shown_strategy, shown_priority = display(row, edits)
            can_edit_strategy, strategy_hint = strategy_editable(row)
            for c, text in enumerate(row.cells):
                if c == STRATEGY_COLUMN:
                    text = shown_strategy
                elif c == PRIORITY_COLUMN:
                    text = shown_priority
                item = SortItem(str(text))
                item.setData(SORT_KEY, row.sort_keys[c])
                item.setData(ROW_ID, row.security_id)
                item.setToolTip(tooltip_of(text) if c not in (1, 13) else str(text))
                editable = False
                if c == STRATEGY_COLUMN:
                    editable = can_edit_strategy and row.security_id not in self._applying
                    item.setData(EDIT_ROLE, {"editable": editable, "current": edits["strategy"] if "strategy" in edits else row.strategy_value})
                    item.setToolTip("Doppelklick: Strategie wählen (wird erst mit „Änderung übernehmen“ gespeichert)" if can_edit_strategy else strategy_hint)
                elif c == PRIORITY_COLUMN:
                    editable = row.security_id not in self._applying
                    item.setData(EDIT_ROLE, {"editable": editable, "current": edits["priority"] if "priority" in edits else row.priority})
                    item.setToolTip("Doppelklick: Priorität eingeben, leer = keine (wird erst mit „Änderung übernehmen“ gespeichert)")
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if c in NUMERIC_COLUMNS:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if row.level:
                    item.setForeground(QBrush(QColor(_color(row.level))))
                if (c == STRATEGY_COLUMN and "strategy" in edits) or (c == PRIORITY_COLUMN and "priority" in edits):  # a pending change: bold and in the warning colour
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setForeground(QBrush(QColor(_color("warning"))))
                self.table.setItem(r, c, item)
            marker = SortItem(PENDING_TEXT if edits else "")
            marker.setData(SORT_KEY, 1 if edits else 0)
            marker.setData(ROW_ID, row.security_id)
            marker.setToolTip(PENDING_TIP if edits else "")
            marker.setFlags(marker.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if edits:
                marker.setForeground(QBrush(QColor(_color("warning"))))
            self.table.setItem(r, EDIT_COLUMN, marker)
        self.table.setSortingEnabled(True)
        if column >= 0:
            self.table.sortItems(column, order)
        self.table.resizeColumnsToContents()
        if selected is not None:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(ROW_ID) == selected:
                    self.table.selectRow(r)
                    break
        self._on_selection()

    # -- selection and actions -------------------------------------------------------------------------------
    def selected_id(self) -> Optional[int]:
        items = self.table.selectedItems()
        return None if not items else items[0].data(ROW_ID)

    def selected_row(self) -> Optional[WatchRow]:
        sid = self.selected_id()
        return None if sid is None else self._by_id.get(sid)

    def _on_selection(self) -> None:
        has = self.selected_row() is not None
        self.remove_button.setEnabled(has)
        self._update_edit_buttons()

    def _update_edit_buttons(self) -> None:
        sid = self.selected_id()
        usable = sid is not None and sid in self._pending and sid not in self._applying
        self.apply_button.setEnabled(usable)
        self.discard_button.setEnabled(usable)

    # -- inline editing: nothing is written until the user presses "Änderung übernehmen" -------------------
    def pending(self) -> dict[int, dict]:
        return {sid: dict(edits) for sid, edits in self._pending.items()}

    def _on_edited(self, security_id: int, field: str, value) -> None:
        """An editor reported a value.  It becomes a pending change (or ends one when it equals the backend); the table is redrawn after the editor is closed."""
        row = self._by_id.get(security_id)
        if row is None:
            return
        backend = row.strategy_value if field == "strategy" else row.priority
        edits = self._pending.setdefault(security_id, {})
        if value == backend:
            edits.pop(field, None)
        else:
            edits[field] = value
        if not edits:
            self._pending.pop(security_id, None)
        QTimer.singleShot(0, self._rendered_after_edit)

    def _rendered_after_edit(self) -> None:
        self._render()
        self._update_edit_buttons()

    def _on_apply(self) -> None:
        sid = self.selected_id()
        if sid is None or sid not in self._pending or sid in self._applying:
            return
        self._applying.add(sid)
        self._update_edit_buttons()
        self.show_text_message("Speichere …", "info")
        self.edit_apply_requested.emit(sid, dict(self._pending[sid]))

    def _on_discard(self) -> None:
        sid = self.selected_id()
        if sid is not None and self._pending.pop(sid, None) is not None:
            self._render()
            self._update_edit_buttons()

    def edit_finished(self, security_id: int, saved: set, message: ActionMessage) -> None:
        """The outcome of "Änderung übernehmen": what was saved leaves the pending state, what failed stays; the page then reloads (the backend is the truth)."""
        self._applying.discard(security_id)
        edits = self._pending.get(security_id)
        if edits is not None:
            row = self._by_id.get(security_id)
            for field in saved:
                if row is not None and field in edits:
                    self._show_saved(row, field, edits[field])  # the backend confirmed it: show it until the reload arrives (no flicker back to the old value)
                edits.pop(field, None)
            if not edits:
                self._pending.pop(security_id, None)
        self._action_html = f"<span style='color:{_color(message.level)}'>{message.text.replace(chr(10), '<br>')}</span>"
        self.message.setText(self._action_html)
        self.message.setToolTip(message.tip)
        self._render()
        self._update_edit_buttons()

    @staticmethod
    def _show_saved(row: WatchRow, field: str, value) -> None:
        if field == "strategy":
            row.strategy_value, row.strategy = value, strategy_text(value)
            row.cells[STRATEGY_COLUMN], row.sort_keys[STRATEGY_COLUMN] = row.strategy, row.strategy.lower()
        else:
            row.priority = value
            row.cells[PRIORITY_COLUMN], row.sort_keys[PRIORITY_COLUMN] = ("—" if value is None else str(value)), value

    def show_text_message(self, text: str, level: str) -> None:
        self._action_html = f"<span style='color:{_color(level)}'>{text}</span>"
        self.message.setText(self._action_html)
        self.message.setToolTip("")

    def _ask_discard(self) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, "Ungespeicherte Änderungen", "Ungespeicherte Änderungen verwerfen?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _context_menu(self, pos: QPoint) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        self.table.selectRow(item.row())
        menu = QMenu(self)
        menu.addAction("Von der Watchlist entfernen …", self._on_remove)
        if self.selected_id() in self._pending:
            menu.addSeparator()
            menu.addAction("Änderung übernehmen", self._on_apply)
            menu.addAction("Änderung verwerfen", self._on_discard)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_add_dialog(self) -> AddCandidateDialog:
        dialog = AddCandidateDialog(self)
        dialog.check_requested.connect(self.check_requested.emit)
        dialog.add_requested.connect(self.add_requested.emit)
        dialog.onboard_requested.connect(lambda: (dialog.reject(), self.open_onboard_dialog()))
        dialog.finished.connect(lambda _=0: setattr(self, "add_dialog", None))
        self.add_dialog = dialog
        dialog.open()
        return dialog

    def open_onboard_dialog(self) -> OnboardSecurityDialog:
        dialog = OnboardSecurityDialog(self)
        dialog.check_requested.connect(self.onboard_check_requested.emit)
        dialog.create_requested.connect(self.onboard_create_requested.emit)
        dialog.watchlist_requested.connect(self.onboard_watchlist_requested.emit)
        dialog.finished.connect(lambda _=0: setattr(self, "onboard_dialog", None))
        self.onboard_dialog = dialog
        dialog.open()
        return dialog

    def show_onboard_outcome(self, outcome: Outcome, arguments: Optional[dict] = None) -> None:
        """The answer of a check (``arguments`` = what was checked) or of the creation; also copied into the page's message line."""
        if self.onboard_dialog is not None:
            self.onboard_dialog.show_outcome(outcome, arguments)
        if outcome.created or self.onboard_dialog is None:
            self._action_html = f"<span style='color:{_color(outcome.level)}'>{outcome.text}</span>"
            self.message.setText(self._action_html)

    def show_onboard_watchlist_result(self, message: ActionMessage) -> None:
        if self.onboard_dialog is not None:
            self.onboard_dialog.show_watchlist_result(message.text, message.level, message.done)
        if message.done or self.onboard_dialog is None:
            self._action_html = f"<span style='color:{_color(message.level)}'>{message.text.replace(chr(10), '<br>')}</span>"
            self.message.setText(self._action_html)

    def _ask_remove(self, row: WatchRow) -> bool:
        held = "\n\nDas Wertpapier ist im Bestand. Nur der Watchlist-Eintrag wird entfernt, die Position bleibt unverändert." if row.held else ""
        notes = "\n\nZu diesem Eintrag gibt es Einstiegsgrund, These oder Notizen. Sie gehen beim Entfernen verloren und kommen beim erneuten Hinzufügen nicht zurück." if row.annotated else ""
        box = QMessageBox(QMessageBox.Icon.Warning, "Von der Watchlist entfernen", f"{row.symbol} aus der Watchlist entfernen?{held}{notes}",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _on_remove(self) -> None:
        row = self.selected_row()
        if row is not None and self.confirm_remove(row):
            self.remove_requested.emit(row.security_id)
