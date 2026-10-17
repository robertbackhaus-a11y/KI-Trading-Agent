# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Chat page: transcript with separate styles per message type, input, Send / Stop / New chat, model and thinking switch (plain Qt text, no WebEngine)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QKeyEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget

from ..services.tool_loop import LoopEvent
from ..viewmodels.chat import format_done, format_error, format_tool_call, format_tool_result

COLORS = {"user": "#2e86c1", "assistant": "", "thinking": "#7f8c8d", "tool": "#5d6d7e", "ok": "#1e8449", "warning": "#b9770e", "error": "#c0392b", "info": "#5d6d7e"}
LABELS = {"user": "Du", "assistant": "Assistent", "thinking": "Denken", "tool": "Tool", "error": "Fehler", "warning": "Hinweis", "info": ""}


class _Input(QPlainTextEdit):
    submit = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.submit.emit()
            return
        super().keyPressEvent(event)


class ChatView(QWidget):
    send_requested = Signal(str)
    stop_requested = Signal()
    new_chat_requested = Signal()
    think_toggled = Signal(bool)

    def __init__(self, model: str = "", think: bool = False) -> None:
        super().__init__()
        self._open_kind: Optional[str] = None
        self._think = think
        self.busy = False
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<h2>Chat</h2>"))
        top.addStretch(1)
        self.model_label = QLabel()
        self.ollama_label = QLabel("Ollama: nicht geprüft")
        self.think_box = QCheckBox("Thinking anzeigen")
        self.think_box.setChecked(think)
        self.think_box.toggled.connect(self._on_think)
        self.new_button = QPushButton("Neuer Chat")
        self.new_button.setToolTip("Setzt den Verlauf zurück (er wird nie gespeichert).")
        self.new_button.clicked.connect(self.new_chat_requested.emit)
        for widget in (self.model_label, self.ollama_label, self.think_box, self.new_button):
            top.addWidget(widget)
        outer.addLayout(top)
        note = QLabel("Nur lesend: Dem Modell werden ausschließlich READ-Tools angeboten. Der Verlauf bleibt im Arbeitsspeicher. Keine Orders, keine Änderungen.")
        note.setWordWrap(True)
        outer.addWidget(note)
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        outer.addWidget(self.transcript, 1)
        self.status = QLabel("Bereit")
        outer.addWidget(self.status)
        row = QHBoxLayout()
        self.input = _Input()
        self.input.setPlaceholderText("Frage eingeben … (Strg+Enter sendet)")
        self.input.setFixedHeight(72)
        self.input.submit.connect(self._send)
        self.send_button = QPushButton("Senden")
        self.send_button.clicked.connect(self._send)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Bricht nur diese Anfrage ab; Ollama und MCP laufen weiter.")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        row.addWidget(self.input, 1)
        buttons = QVBoxLayout()
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.stop_button)
        row.addLayout(buttons)
        outer.addLayout(row)
        self.set_model(model)

    # -- controls -----------------------------------------------------------------------------
    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if text and not self.busy:
            self.input.clear()
            self.send_requested.emit(text)

    def _on_think(self, checked: bool) -> None:
        self._think = checked
        self.think_toggled.emit(checked)

    def set_think(self, value: bool) -> None:
        self._think = value
        self.think_box.blockSignals(True)
        self.think_box.setChecked(value)
        self.think_box.blockSignals(False)

    def set_model(self, model: str) -> None:
        self.model_label.setText(f"Modell: {model or '—'}  (Wechsel in den Einstellungen)")

    def set_ollama(self, text: str, ok: Optional[bool]) -> None:
        color = COLORS["ok"] if ok else COLORS["warning"] if ok is False else COLORS["info"]
        self.ollama_label.setText(f"<span style='color:{color}'>Ollama: {text}</span>")

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.send_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.new_button.setEnabled(not busy)
        if not busy:
            self.status.setText("Bereit")

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    # -- transcript -------------------------------------------------------------------------------
    def clear(self) -> None:
        self.transcript.clear()
        self._open_kind = None
        self.status.setText("Bereit")

    def _fmt(self, kind: str, italic: bool = False, mono: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        if COLORS.get(kind):
            fmt.setForeground(QColor(COLORS[kind]))
        fmt.setFontItalic(italic)
        if mono:
            fmt.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        return fmt

    def _begin(self, kind: str) -> QTextCursor:
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self._open_kind != kind:
            if not self.transcript.document().isEmpty():
                cursor.insertBlock()
            label = LABELS.get(kind, "")
            if label:
                head = self._fmt(kind)
                head.setFontWeight(QFont.Weight.Bold)
                cursor.insertText(label + "\n", head)
            self._open_kind = kind
        return cursor

    def append(self, kind: str, text: str, *, italic: bool = False, mono: bool = False, close: bool = False) -> None:
        cursor = self._begin(kind)
        cursor.insertText(text, self._fmt(kind, italic, mono))
        if close:
            self._open_kind = None
        self.transcript.setTextCursor(cursor)
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def add_user(self, text: str) -> None:
        self.append("user", text, close=True)

    def handle_event(self, ev: LoopEvent) -> None:
        kind = ev.kind
        if kind == "content":
            self.append("assistant", ev.text)
        elif kind == "thinking":
            if self._think:
                self.append("thinking", ev.text, italic=True)
        elif kind == "tool_call":
            self.append("tool", format_tool_call(ev.text, ev.data.get("arguments") or {}, bool(ev.data.get("allowed", True))) + "\n", mono=True, close=True)
        elif kind == "tool_result":
            text, level = format_tool_result(ev.text, ev.data)
            self.append(level if level != "ok" else "ok", text + "\n", mono=True, close=True)
        elif kind == "warning":
            self.append("warning", ev.text + "\n", close=True)
        elif kind == "error":
            self.append("error", format_error(str(ev.data.get("kind") or ""), ev.text) + "\n", close=True)
        elif kind == "status":
            self.status.setText(ev.text)
        elif kind == "done":
            self.append("info", format_done(ev.data) + "\n", italic=True, close=True)
            self.status.setText("Bereit")
