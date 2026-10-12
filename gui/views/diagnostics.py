"""Diagnostics / Settings page: MCP and DB status, paths, tool classification, Ollama reachability, settings form, MCP restart."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from ..services.settings import Settings, validate_edit
from ._common import LEVEL_COLORS, fill_table, make_table, set_form_rows

TEXT_FIELDS = (
    ("mcp_python_path", "MCP-Python"), ("mcp_server_path", "MCP-Server"), ("trading_db_path", "Datenbank (absolut)"), ("trading_logs_dir", "Log-Ordner der Tasks"),
    ("ollama_base_url", "Ollama-URL (spätere Einstellung)"), ("ollama_model", "Ollama-Modell (spätere Einstellung)"),
)


class DiagnosticsView(QWidget):
    refresh_requested = Signal()
    restart_requested = Signal()
    ollama_check_requested = Signal()
    save_requested = Signal(object)  # a new Settings object

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<h2>Diagnostics / Settings</h2>"))
        top.addStretch(1)
        refresh = QPushButton("Aktualisieren")
        refresh.clicked.connect(self.refresh_requested.emit)
        self.restart = QPushButton("MCP neu starten")
        self.restart.setToolTip("Startet nur den MCP-Kindprozess neu; es werden keine Trading-Daten verändert.")
        self.restart.clicked.connect(self.restart_requested.emit)
        top.addWidget(refresh)
        top.addWidget(self.restart)
        outer.addLayout(top)
        body = QWidget()
        layout = QVBoxLayout(body)
        self.info = QFormLayout()
        box = QGroupBox("Status und Pfade")
        box.setLayout(self.info)
        layout.addWidget(box)
        self.warnings = QLabel("")
        self.warnings.setWordWrap(True)
        layout.addWidget(self.warnings)
        tools_box = QGroupBox("MCP-Tools und Klassifizierung (READ darf die GUI aufrufen, ACTION nicht)")
        tools_layout = QVBoxLayout(tools_box)
        self.tools = make_table(["Tool", "Klasse"])
        self.tools.setMinimumHeight(180)
        tools_layout.addWidget(self.tools)
        layout.addWidget(tools_box)
        ollama_box = QGroupBox("Ollama (nur Erreichbarkeit)")
        ollama_layout = QHBoxLayout(ollama_box)
        self.ollama = QLabel("nicht geprüft")
        self.ollama.setWordWrap(True)
        check = QPushButton("Prüfen")
        check.clicked.connect(self.ollama_check_requested.emit)
        ollama_layout.addWidget(self.ollama, 1)
        ollama_layout.addWidget(check)
        layout.addWidget(ollama_box)
        form_box = QGroupBox("Einstellungen")
        self.form = QFormLayout(form_box)
        self.edits: dict[str, QLineEdit] = {}
        for key, caption in TEXT_FIELDS:
            edit = QLineEdit(getattr(settings, key))
            self.edits[key] = edit
            self.form.addRow(caption + ":", edit)
        self.interval = QSpinBox()
        self.interval.setRange(30, 3600)
        self.interval.setSuffix(" s")
        self.interval.setValue(settings.refresh_interval_s)
        self.form.addRow("Aktualisierungsintervall:", self.interval)
        self.detail = QCheckBox("Detailmodus als Standard (für spätere Seiten)")
        self.detail.setChecked(settings.default_detail_mode)
        self.think = QCheckBox("Thinking aktivieren (für den späteren Chat)")
        self.think.setChecked(settings.think_enabled)
        self.form.addRow("", self.detail)
        self.form.addRow("", self.think)
        save = QPushButton("Einstellungen speichern")
        save.clicked.connect(self._save)
        self.form.addRow("", save)
        self.form_message = QLabel("Pfadänderungen wirken nach „MCP neu starten“.")
        self.form_message.setWordWrap(True)
        self.form.addRow("", self.form_message)
        layout.addWidget(form_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    def _save(self) -> None:
        values = {key: edit.text().strip() for key, edit in self.edits.items()}
        for key, value in values.items():
            problem = validate_edit(key, value)
            if problem:
                self.form_message.setText(f"<span style='color:{LEVEL_COLORS['blocker']}'>{key}: {problem}</span>")
                return
        new = Settings(**{**self._settings.__dict__, **values, "refresh_interval_s": self.interval.value(), "default_detail_mode": self.detail.isChecked(), "think_enabled": self.think.isChecked()})
        self.form_message.setText("Gespeichert. Pfadänderungen wirken nach „MCP neu starten“.")
        self.save_requested.emit(new)

    def set_settings(self, settings: Settings) -> None:
        self._settings = settings

    def show_info(self, rows: list[tuple[str, str]], warnings: list[str]) -> None:
        set_form_rows(self.info, rows)
        self.warnings.setText("".join(f"<div style='color:{LEVEL_COLORS['warning']}'>⚠ {w}</div>" for w in warnings))

    def show_tools(self, rows: list[list[str]]) -> None:
        fill_table(self.tools, ["Tool", "Klasse"], rows, ["" if r[1] == "READ" else "warning" for r in rows])

    def show_ollama(self, text: str, ok: Optional[bool]) -> None:
        color = LEVEL_COLORS["info"] if ok else LEVEL_COLORS["warning"]
        self.ollama.setText(f"<span style='color:{color}'>{text}</span>")
