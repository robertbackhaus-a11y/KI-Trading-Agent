"""Cell editors of the Watchlist table: a combo box for the strategy, a number field for the priority, a combo box for the sector group.

An editor only reports the chosen value to the page (``on_commit``); it never writes anything.  The page keeps the value as a pending change until the
user presses "Änderung übernehmen".  A cell without ``EDIT_ROLE`` data (or marked not editable) gets no editor.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QModelIndex, Qt, QTimer
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QComboBox, QLineEdit, QStyledItemDelegate, QWidget

from ..viewmodels.watchlist import SPIN_MAX, SPIN_MIN
from ..viewmodels.watchlist_edit import STRATEGY_OPTIONS, option_index

ROW_ID = Qt.ItemDataRole.UserRole + 1
EDIT_ROLE = Qt.ItemDataRole.UserRole + 2  # {"editable": bool, "current": the shown (pending or backend) value}


class StrategyDelegate(QStyledItemDelegate):
    def __init__(self, on_commit: Callable[[int, Any], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.on_commit = on_commit

    def createEditor(self, parent: QWidget, option: Any, index: QModelIndex) -> Optional[QWidget]:  # noqa: N802 - Qt API
        info = index.data(EDIT_ROLE)
        if not isinstance(info, dict) or not info.get("editable"):
            return None
        combo = QComboBox(parent)
        for text, value in STRATEGY_OPTIONS:
            combo.addItem(text, value)
        combo.activated.connect(lambda _=0, editor=combo: self._finish(editor))
        QTimer.singleShot(0, combo.showPopup)
        return combo

    def _finish(self, editor: QWidget) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:  # noqa: N802
        info = index.data(EDIT_ROLE) or {}
        if isinstance(editor, QComboBox):
            editor.setCurrentIndex(option_index(info.get("current")))

    def setModelData(self, editor: QWidget, model: Any, index: QModelIndex) -> None:  # noqa: N802
        if isinstance(editor, QComboBox) and isinstance(index.data(ROW_ID), int):
            self.on_commit(int(index.data(ROW_ID)), editor.currentData())


class PriorityDelegate(QStyledItemDelegate):
    def __init__(self, on_commit: Callable[[int, Optional[int]], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.on_commit = on_commit

    def createEditor(self, parent: QWidget, option: Any, index: QModelIndex) -> Optional[QWidget]:  # noqa: N802
        info = index.data(EDIT_ROLE)
        if not isinstance(info, dict) or not info.get("editable"):
            return None
        edit = QLineEdit(parent)
        edit.setValidator(QIntValidator(SPIN_MIN, SPIN_MAX, edit))
        edit.setPlaceholderText("leer = keine Priorität")
        return edit

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:  # noqa: N802
        info = index.data(EDIT_ROLE) or {}
        if isinstance(editor, QLineEdit):
            current = info.get("current")
            editor.setText("" if current is None else str(current))
            editor.selectAll()

    def setModelData(self, editor: QWidget, model: Any, index: QModelIndex) -> None:  # noqa: N802
        if not isinstance(editor, QLineEdit) or not isinstance(index.data(ROW_ID), int):
            return
        text = editor.text().strip()
        if text == "":
            self.on_commit(int(index.data(ROW_ID)), None)
            return
        try:
            self.on_commit(int(index.data(ROW_ID)), int(text))
        except ValueError:  # a lone "-" is not a number: nothing changes
            return


class SectorDelegate(QStyledItemDelegate):
    """The sector group: the EXISTING groups of the sector map (nothing can be typed); ``None`` = no local entry (the row's own label says what that means)."""

    def __init__(self, on_commit: Callable[[int, Any], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.on_commit = on_commit

    def createEditor(self, parent: QWidget, option: Any, index: QModelIndex) -> Optional[QWidget]:  # noqa: N802
        info = index.data(EDIT_ROLE)
        if not isinstance(info, dict) or not info.get("editable"):
            return None
        combo = QComboBox(parent)
        for text, value in info.get("options") or []:
            combo.addItem(text, value)
        combo.activated.connect(lambda _=0, editor=combo: self._finish(editor))
        QTimer.singleShot(0, combo.showPopup)
        return combo

    def _finish(self, editor: QWidget) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:  # noqa: N802
        info = index.data(EDIT_ROLE) or {}
        if isinstance(editor, QComboBox):
            wanted = info.get("current")
            position = next((i for i in range(editor.count()) if editor.itemData(i) == wanted), 0)
            editor.setCurrentIndex(position)

    def setModelData(self, editor: QWidget, model: Any, index: QModelIndex) -> None:  # noqa: N802
        if isinstance(editor, QComboBox) and isinstance(index.data(ROW_ID), int):
            self.on_commit(int(index.data(ROW_ID)), editor.currentData())
