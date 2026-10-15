"""Page "Import": a transaction export (Parqet or the canonical CSV) is checked, the difference to the database is shown, and after a check that would change something it is imported.

"Prüfen" is a dry run on the server (nothing is written).  "Importieren" stays disabled until such a check came back, it is disabled again as soon as the form is edited (the check
no longer matches), it asks once, and it sends the token of exactly that check.  The answers arrive later through :meth:`show_view` (non-blocking, like the other pages).
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from ..viewmodels import transaction_import as vm
from ._common import LEVEL_COLORS, fill_table, make_table


class TransactionImportView(QWidget):
    check_requested = Signal(dict)  # arguments of check_transaction_import
    import_requested = Signal(dict, str)  # the arguments of the check and its plan token

    def __init__(self) -> None:
        super().__init__()
        self._checked: Optional[dict[str, Any]] = None
        self._token: Optional[str] = None
        self.strategy_choice: dict[str, str] = {}  # strategy key -> swing | long_term | none  (the user's decisions; "none" is a decision, "open" is no entry)
        self.alias_choice: dict[str, str] = {}  # Parqet identifier -> security key
        self._alias_infos: dict[str, dict[str, Any]] = {}
        self._strategy_infos: dict[str, dict[str, Any]] = {}
        self.decision_boxes: dict[tuple[str, str], QComboBox] = {}
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
        self.decisions_label = QLabel("Offene Zuordnungen – hier entscheiden, dann erneut prüfen:")
        self.decisions = make_table(vm.DECISION_HEADERS)
        self.decisions.setMaximumHeight(170)
        self.decisions_label.setVisible(False)
        self.decisions.setVisible(False)
        outer.addWidget(self.decisions_label)
        outer.addWidget(self.decisions)
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
        self.path.textEdited.connect(self._reset)
        self.format.currentIndexChanged.connect(self._reset)
        self.create.toggled.connect(self._reset)
        self.historical.toggled.connect(self._reset)

    # -- form ------------------------------------------------------------------------------------------------
    def arguments(self) -> dict[str, Any]:
        return vm.request_arguments(self.path.text(), self.format.currentText(), self.create.isChecked(), self.historical.isChecked(), self.strategy_choice, self.alias_choice)

    def _invalidate(self, *_: object) -> None:
        self._checked, self._token = None, None
        self.run.setEnabled(False)

    def _reset(self, *_: object) -> None:
        """Another file or other options: the decisions belonged to the old plan and are dropped with it."""
        self._invalidate()
        self.strategy_choice, self.alias_choice, self._alias_infos, self._strategy_infos = {}, {}, {}, {}
        self._fill_decisions()

    def _on_decision(self, kind: str, key: str, box: QComboBox) -> None:
        """A decision changes the plan: the old token is worthless, the page asks for a new check (nothing is sent by itself)."""
        value = vm.STRATEGY_CHOICES.get(box.currentText()) if kind == "strategy" else vm.ALIAS_CHOICES.get(box.currentText())
        if kind == "strategy":
            self.strategy_choice.pop(key, None) if value is None else self.strategy_choice.__setitem__(key, value)
        elif value is None:
            self.alias_choice.pop(key, None)
        else:
            self.alias_choice[key] = self._alias_infos[key]["security"]
        self._invalidate()
        self.show_text("Auswahl geändert – bitte erneut prüfen, bevor importiert wird.", "warning")

    @staticmethod
    def _merged(old: dict[str, dict[str, Any]], decided: dict[str, str], new: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """The decided entries keep their details from the earlier checks; what a check reports now wins field by field (a missing value never erases a known one)."""
        merged = {k: v for k, v in old.items() if k in decided}
        for key, info in new.items():
            merged[key] = {**merged.get(key, {}), **{a: b for a, b in info.items() if b is not None}}
        return merged

    def _fill_decisions(self) -> None:
        """One row per open identifier conflict and per newly held position without a strategy; the choices stay selected across checks."""
        rows = []
        for identifier, info in sorted(self._alias_infos.items()):
            who = " – ".join(str(x) for x in (info.get("security"), info.get("name")) if x)
            detail = f"Parqet-ID {identifier}" + (f" ↔ Datenbank {info['database_isin']}" if info.get("database_isin") else "") + (f"; WKN {info['wkn']}" if info.get("wkn") else "")
            if info.get("same_wkn") is not None:
                detail += "; gleiche WKN" if info["same_wkn"] else "; keine gleiche WKN (wird vom Server abgelehnt)"
            status = {"OFFEN": "offen"}.get(info.get("status", "OFFEN")) or (vm.ALIAS_STATUS.get(info["status"], info["status"]) + (f": {info['reason']}" if info.get("reason") else ""))
            rows.append(("alias", identifier, "Kennung", who, detail, status, list(vm.ALIAS_CHOICES), "Alias übernehmen" if identifier in self.alias_choice else "Offen"))
        for key, info in sorted(self._strategy_infos.items()):
            chosen = self.strategy_choice.get(key)
            status = {"swing": "wird Swing zugeordnet (ohne Campaign)", "long_term": "wird Long-Term zugeordnet", "none": "bewusst ohne Strategie"}.get(chosen or "", "offen")
            shares = f"{info['shares']:g} Stück neu gehalten" if info.get("shares") else "neu gehalten"
            text = {v: k for k, v in vm.STRATEGY_CHOICES.items() if v}.get(chosen, "Offen")
            rows.append(("strategy", key, "Strategie", str(info.get("name") or key), f"{shares}; Kennung {key}", status, list(vm.STRATEGY_CHOICES), text))
        self.decision_boxes = {}
        self.decisions.setRowCount(len(rows))
        self.decisions.setColumnCount(len(vm.DECISION_HEADERS))
        self.decisions.setHorizontalHeaderLabels(vm.DECISION_HEADERS)
        for r, (kind, key, label, who, detail, status, options, current) in enumerate(rows):
            for c, text in enumerate((label, who, detail)):
                self.decisions.setItem(r, c, QTableWidgetItem(text))
            box = QComboBox()
            box.addItems(options)
            box.setCurrentText(current)  # before the signal is connected: filling is not a decision
            box.currentIndexChanged.connect(lambda _i, k=kind, ky=key, b=box: self._on_decision(k, ky, b))
            self.decisions.setCellWidget(r, 3, box)
            self.decisions.setItem(r, 4, QTableWidgetItem(status))
            self.decision_boxes[(kind, key)] = box
        self.decisions.resizeColumnsToContents()
        visible = bool(rows)
        self.decisions_label.setVisible(visible)
        self.decisions.setVisible(visible)

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
        if view.ok:  # the open points of this check; earlier ones stay as long as they are decided (a decided alias / strategy no longer shows up as open)
            self._alias_infos = self._merged(self._alias_infos, self.alias_choice, view.alias_infos)
            self._strategy_infos = self._merged(self._strategy_infos, self.strategy_choice, view.strategy_infos)
            self._fill_decisions()
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
