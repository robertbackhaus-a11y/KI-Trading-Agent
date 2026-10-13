"""Dialog "Neues Wertpapier aufnehmen": a new security is created only from what the user types, after a check (dry run) without conflicts.

Nothing is looked up or completed.  "Prüfen" asks the server for a dry run; "Anlegen" stays disabled until a check came back without conflicts, and
it is disabled again as soon as a field is edited (the check no longer matches the form).  The answers arrive later through :meth:`show_outcome`
(non-blocking, like the other dialogs of the Watchlist page).
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget

from ..viewmodels.onboarding import ASSET_TYPES, Outcome, form_arguments, missing_fields
from ..viewmodels.watchlist import SPIN_MAX, SPIN_MIN
from ._common import LEVEL_COLORS


def _color(level: str) -> str:
    return LEVEL_COLORS.get(level, LEVEL_COLORS["info"])


class OnboardSecurityDialog(QDialog):
    check_requested = Signal(dict)  # the arguments of a dry run
    create_requested = Signal(dict)  # the same arguments, checked just before
    watchlist_requested = Signal(int)  # "Zur Watchlist hinzufügen" for a security that already exists

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Neues Wertpapier aufnehmen")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._checked: Optional[dict[str, Any]] = None  # the arguments the last successful check was made for
        self._existing_id: Optional[int] = None
        outer = QVBoxLayout(self)
        hint = QLabel("Das Wertpapier wird nur mit den Angaben angelegt, die hier stehen. Es wird nichts recherchiert, ergänzt oder geraten. "
                      "Marktdaten, Fundamentaldaten und News kommen später über die regulären Läufe.")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        form = QFormLayout()
        self.symbol = self._line(form, "Symbol *:", "genau so, wie es gespeichert werden soll (zum Beispiel ABC oder ABC.DE)", 20)
        self.name = self._line(form, "Name *:", "Name des Wertpapiers", 200)
        self.asset = QComboBox()
        self.asset.addItems(list(ASSET_TYPES))
        self.asset.setToolTip("Aktie oder ETF; wird nicht aus dem Namen abgeleitet")
        form.addRow("Art *:", self.asset)
        self.isin = self._line(form, "ISIN:", "optional, zwölf Zeichen mit Prüfziffer", 12)
        self.wkn = self._line(form, "WKN:", "optional, sechs Zeichen", 6)
        self.exchange = self._line(form, "Börse:", "optional, Freitext", 40)
        self.currency = self._line(form, "Währung:", "optional, dreistelliger Code, zum Beispiel EUR", 3)
        self.country = self._line(form, "Land:", "optional, zweistelliger Code, zum Beispiel DE", 2)
        self.yahoo = self._line(form, "Yahoo-Symbol:", "optional; ohne Angabe sucht die Marktdaten-Beschaffung selbst", 24)
        self.fmp = self._line(form, "FMP-Symbol:", "optional", 24)
        row = QHBoxLayout()
        self.watch = QCheckBox("Nach Anlage zur Watchlist hinzufügen")
        self.use_priority = QCheckBox("Priorität")
        self.priority = QSpinBox()
        self.priority.setRange(SPIN_MIN, SPIN_MAX)
        self.priority.setValue(0)
        self.use_priority.setEnabled(False)
        self.priority.setEnabled(False)
        self.watch.toggled.connect(self._on_watch_toggled)
        self.use_priority.toggled.connect(self.priority.setEnabled)
        row.addWidget(self.watch)
        row.addWidget(self.use_priority)
        row.addWidget(self.priority)
        row.addStretch(1)
        form.addRow("Optional:", row)
        outer.addLayout(form)
        sector = QLabel("Der Sector bleibt „nicht zugeordnet“; er wird in einem späteren Schritt gesetzt.")
        sector.setWordWrap(True)
        outer.addWidget(sector)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setOpenExternalLinks(False)
        outer.addWidget(self.result)
        buttons = QHBoxLayout()
        self.check = QPushButton("Prüfen")
        self.check.setToolTip("Prüft die Eingaben und den Security Master – es wird nichts geschrieben")
        self.create = QPushButton("Anlegen")
        self.create.setEnabled(False)
        self.create.setToolTip("Erst nach einer Prüfung ohne Konflikte möglich")
        self.to_watchlist = QPushButton("Zur Watchlist hinzufügen")
        self.to_watchlist.setVisible(False)
        self.cancel = QPushButton("Abbrechen")
        self.cancel.setDefault(True)
        buttons.addWidget(self.check)
        buttons.addWidget(self.to_watchlist)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.create)
        outer.addLayout(buttons)
        self.cancel.clicked.connect(self.reject)
        self.check.clicked.connect(self._on_check)
        self.create.clicked.connect(self._on_create)
        self.to_watchlist.clicked.connect(self._on_to_watchlist)
        for edit in (self.symbol, self.name, self.isin, self.wkn, self.exchange, self.currency, self.country, self.yahoo, self.fmp):
            edit.textEdited.connect(self._invalidate)
        self.asset.currentIndexChanged.connect(self._invalidate)
        self.watch.toggled.connect(self._invalidate)
        self.use_priority.toggled.connect(self._invalidate)
        self.priority.valueChanged.connect(self._invalidate)

    @staticmethod
    def _line(form: QFormLayout, label: str, placeholder: str, limit: int) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMaxLength(limit)
        form.addRow(label, edit)
        return edit

    # -- form ----------------------------------------------------------------------------------------------
    def arguments(self) -> dict[str, Any]:
        return form_arguments(
            self.symbol.text(), self.name.text(), self.asset.currentText(), self.isin.text(), self.wkn.text(), self.exchange.text(), self.currency.text(), self.country.text(),
            self.yahoo.text(), self.fmp.text(), self.watch.isChecked(), self.priority.value() if self.watch.isChecked() and self.use_priority.isChecked() else None,
        )

    def _on_watch_toggled(self, on: bool) -> None:
        self.use_priority.setEnabled(on)
        self.priority.setEnabled(on and self.use_priority.isChecked())
        if not on:
            self.use_priority.setChecked(False)

    def _invalidate(self, *_: object) -> None:
        """An edited form no longer matches the last check: "Anlegen" must be earned again."""
        self._checked = None
        self._existing_id = None
        self.create.setEnabled(False)
        self.to_watchlist.setVisible(False)

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.check, self.symbol, self.name, self.asset, self.isin, self.wkn, self.exchange, self.currency, self.country, self.yahoo, self.fmp, self.watch):
            widget.setEnabled(not busy)
        self.use_priority.setEnabled(not busy and self.watch.isChecked())
        self.priority.setEnabled(not busy and self.watch.isChecked() and self.use_priority.isChecked())
        self.create.setEnabled(False if busy else self._checked is not None)
        self.to_watchlist.setEnabled(not busy)

    # -- actions -------------------------------------------------------------------------------------------
    def _on_check(self) -> None:
        arguments = self.arguments()
        missing = missing_fields(arguments)
        if missing:
            self.show_text("Bitte ausfüllen: " + ", ".join(missing) + ".", "warning")
            return
        self._invalidate()
        self._set_busy(True)
        self.show_text("Prüfe …", "info")
        self.check_requested.emit(arguments)

    def _on_create(self) -> None:
        if self._checked is None or self._checked != self.arguments():  # defence in depth: the button is disabled in that case
            self.show_text("Bitte zuerst prüfen.", "warning")
            return
        arguments = dict(self._checked)
        self._set_busy(True)
        self.create.setEnabled(False)
        self.show_text("Lege an …", "info")
        self.create_requested.emit(arguments)

    def _on_to_watchlist(self) -> None:
        if self._existing_id is None:
            return
        self.to_watchlist.setEnabled(False)
        self.show_text("Füge zur Watchlist hinzu …", "info")
        self.watchlist_requested.emit(int(self._existing_id))

    # -- answers -------------------------------------------------------------------------------------------
    def show_text(self, text: str, level: str, tip: str = "") -> None:
        self.result.setText(f"<span style='color:{_color(level)}'>{text.replace(chr(10), '<br>')}</span>")
        self.result.setToolTip(tip)

    def show_outcome(self, outcome: Outcome, checked_with: Optional[dict[str, Any]] = None) -> None:
        """The answer of a check or of the creation.  ``checked_with`` are the arguments the check was made for."""
        self._set_busy(False)
        lines = "".join(f"<br>{line}" for line in outcome.lines)
        self.show_text(outcome.text + lines, outcome.level, outcome.tooltip)
        if outcome.created:
            self.accept()
            return
        self._existing_id = None
        if outcome.can_create and checked_with is not None and checked_with == self.arguments():
            self._checked = dict(checked_with)
            self.create.setEnabled(True)
        else:
            self._checked = None
            self.create.setEnabled(False)
        if outcome.existing and not outcome.existing.get("on_watchlist") and outcome.existing.get("active", True) and outcome.existing.get("security_id") is not None:
            self._existing_id = int(outcome.existing["security_id"])
            self.to_watchlist.setVisible(True)
            self.to_watchlist.setEnabled(True)
        else:
            self.to_watchlist.setVisible(False)

    def show_watchlist_result(self, text: str, level: str, done: bool) -> None:
        """The answer of "Zur Watchlist hinzufügen" for an already existing security."""
        self.show_text(text, level)
        if done:
            self.accept()
        else:
            self.to_watchlist.setEnabled(True)
