"""Page "Import": a transaction export (Parqet or the canonical CSV) is checked, the difference to the database is shown, and after a check that would change something it is imported.

"Prüfen" is a dry run on the server (nothing is written).  "Importieren" stays disabled until such a check came back, it is disabled again as soon as the form is edited (the check
no longer matches), it asks once, and it sends the token of exactly that check.  The answers arrive later through :meth:`show_view` (non-blocking, like the other pages).
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTabWidget, QVBoxLayout,
                               QWidget)

from ..viewmodels import transaction_import as vm
from ._common import LEVEL_COLORS, fill_table, make_table


class TransactionImportView(QWidget):
    check_requested = Signal(dict)  # arguments of check_transaction_import
    import_requested = Signal(dict, str)  # the arguments of the check and its plan token

    def __init__(self) -> None:
        super().__init__()
        self._checked: Optional[dict[str, Any]] = None
        self._token: Optional[str] = None
        outer = QVBoxLayout(self)
        hint = QLabel("Eine Export-Datei wird geprüft, mit der Datenbank verglichen und erst nach Ihrer Bestätigung importiert. Positionen werden aus den Transaktionen neu berechnet; "
                      "Watchlist, Strategien und Kampagnen bleiben unverändert. Kurse und Wechselkurse kommen aus den regulären Läufen.")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        form = QFormLayout()
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("Pfad der CSV-Datei (zum Beispiel ein Parqet-Export)")
        self.browse = QPushButton("Datei wählen …")
        row.addWidget(self.path, 1)
        row.addWidget(self.browse)
        form.addRow("Datei:", row)
        self.format = QComboBox()
        self.format.addItems(list(vm.FORMATS))
        form.addRow("Format:", self.format)
        self.create = QCheckBox("Neue Wertpapiere aus der Datei anlegen")
        self.create.setChecked(True)
        self.create.setToolTip("Angelegt wird nur, was die Datei ausdrücklich enthält (ISIN, WKN, Name); nichts wird recherchiert oder erfunden")
        self.historical = QCheckBox("Auch Zeilen importieren, die älter sind als die letzte gespeicherte Transaktion")
        form.addRow("", self.create)
        form.addRow("", self.historical)
        outer.addLayout(form)
        buttons = QHBoxLayout()
        self.check = QPushButton("Prüfen")
        self.check.setToolTip("Prüft die Datei und zeigt den Unterschied zur Datenbank – es wird nichts geschrieben")
        self.run = QPushButton("Importieren")
        self.run.setEnabled(False)
        self.run.setToolTip("Erst nach einer Prüfung möglich, die etwas ändern würde")
        buttons.addWidget(self.check)
        buttons.addWidget(self.run)
        buttons.addStretch(1)
        outer.addLayout(buttons)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)
        self.tabs = QTabWidget()
        self.tables: dict[str, QTableWidget] = {}
        for key, title in (("securities", "Neue Wertpapiere"), ("masterdata", "Stammdaten"), ("positions", "Positionen"), ("problems", "Problemzeilen"),
                           ("open_items", "Offene Punkte"), ("differences", "Abweichungen Datei ↔ Datenbank")):
            table = make_table(["–"])
            self.tables[key] = table
            self.tabs.addTab(table, title)
        outer.addWidget(self.tabs, 1)
        self.closing = QLabel("")
        self.closing.setWordWrap(True)
        outer.addWidget(self.closing)

        self.browse.clicked.connect(self._on_browse)
        self.check.clicked.connect(self._on_check)
        self.run.clicked.connect(self._on_run)
        self.path.textEdited.connect(self._invalidate)
        self.format.currentIndexChanged.connect(self._invalidate)
        self.create.toggled.connect(self._invalidate)
        self.historical.toggled.connect(self._invalidate)

    # -- form ------------------------------------------------------------------------------------------------
    def arguments(self) -> dict[str, Any]:
        return vm.request_arguments(self.path.text(), self.format.currentText(), self.create.isChecked(), self.historical.isChecked())

    def _invalidate(self, *_: object) -> None:
        self._checked, self._token = None, None
        self.run.setEnabled(False)

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.check, self.browse, self.path, self.format, self.create, self.historical):
            widget.setEnabled(not busy)
        self.run.setEnabled(False if busy else self._checked is not None)

    def _on_browse(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Export-Datei wählen", "", "CSV-Dateien (*.csv);;Alle Dateien (*)")
        if name:
            self.path.setText(name)
            self._invalidate()

    # -- actions ---------------------------------------------------------------------------------------------
    def _on_check(self) -> None:
        arguments = self.arguments()
        if not arguments["file_path"]:
            self.show_text("Bitte eine Datei wählen.", "warning")
            return
        self._invalidate()
        self._set_busy(True)
        self.show_text("Prüfe …", "info")
        self.check_requested.emit(arguments)

    def confirm_import(self, headline: str) -> bool:
        """The one question before anything is written (replaced in tests)."""
        answer = QMessageBox.question(self, "Importieren", headline + "\n\nJetzt importieren? Vorher wird eine Sicherung der Datenbank angelegt.")
        return answer == QMessageBox.StandardButton.Yes

    def _on_run(self) -> None:
        if self._checked is None or self._token is None or self._checked != self.arguments():  # defence in depth: the button is disabled in that case
            self.show_text("Bitte zuerst prüfen.", "warning")
            return
        if not self.confirm_import(self.message.text().replace("<br>", "\n")):
            return
        arguments, token = dict(self._checked), self._token
        self._set_busy(True)
        self.run.setEnabled(False)
        self.show_text("Importiere …", "info")
        self.import_requested.emit(arguments, token)

    # -- answers ---------------------------------------------------------------------------------------------
    def show_text(self, text: str, level: str, tip: str = "") -> None:
        self.message.setText(f"<span style='color:{LEVEL_COLORS.get(level, LEVEL_COLORS['info'])}'>{text}</span>")
        self.message.setToolTip(tip)

    def show_view(self, view: vm.ImportView, checked_with: Optional[dict[str, Any]] = None) -> None:
        """The answer of a check (``checked_with`` = its arguments) or of the import (``None``)."""
        self._set_busy(False)
        self.show_text(view.headline, view.level, view.tooltip)
        self.summary.setText("<br>".join(view.lines))
        headers = {"securities": vm.SECURITY_HEADERS, "masterdata": vm.MASTERDATA_HEADERS, "positions": vm.POSITION_HEADERS, "problems": vm.PROBLEM_HEADERS,
                   "open_items": vm.OPEN_HEADERS, "differences": vm.DIFFERENCE_HEADERS}
        for key, table in self.tables.items():
            fill_table(table, headers[key], getattr(view, key))
        self.closing.setText("<br>".join(view.closing))
        if view.can_import and checked_with is not None and checked_with == self.arguments():
            self._checked, self._token = dict(checked_with), view.plan_token
            self.run.setEnabled(True)
        else:
            self._checked, self._token = None, None
            self.run.setEnabled(False)
