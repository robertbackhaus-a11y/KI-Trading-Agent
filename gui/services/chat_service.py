# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Qt side of the chat: one active request at a time, run in a worker thread, events delivered to the GUI thread.

Threading: the Ollama stream and the tool loop run in a ``QThreadPool`` worker.  Tool calls go to the MCP service through ``call_sync``, which
submits them to the MCP asyncio thread and waits, so the ``ClientSession`` is only ever used by its own thread.  The history lives in RAM only.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from .mcp_client import CONNECTED, McpService
from .ollama_client import CancelToken, OllamaClient
from .settings import Settings, get_logger
from .tool_loop import TOOL_TIMEOUT_S, LoopEvent, LoopResult, ToolLoop


class _Bridge(QObject):
    event = Signal(object)
    finished = Signal(object)


class _Job(QRunnable):
    def __init__(self, loop: ToolLoop, history: list[dict[str, Any]], text: str, token: CancelToken, bridge: _Bridge) -> None:
        super().__init__()
        self._loop, self._history, self._text, self._token, self._bridge = loop, history, text, token, bridge

    def run(self) -> None:
        try:
            result = self._loop.run(self._history, self._text, self._bridge.event.emit, self._token)
        except Exception as exc:  # noqa: BLE001 - the GUI must survive anything the loop does
            self._bridge.event.emit(LoopEvent("error", f"Interner Fehler: {type(exc).__name__}", {"kind": "INTERNAL"}))
            self._bridge.event.emit(LoopEvent("done", data={"outcome": "error"}))
            result = LoopResult("error", self._history, error=f"INTERNAL: {type(exc).__name__}")
        self._bridge.finished.emit(result)


class ChatService(QObject):
    event = Signal(object)  # LoopEvent, in the GUI thread
    busy_changed = Signal(bool)

    def __init__(self, get_settings: Callable[[], Settings], mcp: McpService, client_factory: Callable[[str], OllamaClient] = OllamaClient, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._get_settings, self._mcp, self._client_factory = get_settings, mcp, client_factory
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(1)
        self._bridge = _Bridge()
        self._bridge.event.connect(self._on_event)
        self._bridge.finished.connect(self._on_finished)
        self._token: Optional[CancelToken] = None
        self._started = 0.0
        self.history: list[dict[str, Any]] = []  # RAM only, never written anywhere
        self.last_error = ""
        self.busy = False
        self.log = get_logger()

    # -- requests --------------------------------------------------------------------------------
    def send(self, text: str) -> bool:
        """Start a request.  Returns ``False`` (and says why through an event) if one is already running or the chat cannot start."""
        text = text.strip()
        if not text:
            return False
        if self.busy:
            self.event.emit(LoopEvent("warning", "Es läuft bereits eine Anfrage. Warte auf das Ende oder stoppe sie."))
            return False
        settings = self._get_settings()
        try:
            client = self._client_factory(settings.ollama_base_url)
        except ValueError as exc:
            return self._fail(str(exc), "SETTINGS")
        catalog = self._mcp.tools
        if self._mcp.state != CONNECTED or not catalog:
            return self._fail("MCP ist nicht verbunden: ohne Tools kann der Chat keine Daten abfragen.", "MCP_NOT_CONNECTED")
        token = CancelToken()
        loop = ToolLoop(client, catalog, lambda name, args, tok: self._mcp.call_sync(name, args, TOOL_TIMEOUT_S, cancelled=lambda: tok.cancelled), settings.ollama_model, settings.think_enabled)
        self._token, self._started = token, time.perf_counter()
        self._set_busy(True)
        self.log.info("Ollama-Anfrage gestartet: Modell %s, Thinking %s, %d Tools angeboten", settings.ollama_model, "an" if settings.think_enabled else "aus", len(loop.tools))
        self._pool.start(_Job(loop, list(self.history), text, token, self._bridge))
        return True

    def cancel(self) -> None:
        """Stop only the current request (closes its HTTP connection).  Ollama and MCP keep running."""
        if self.busy and self._token is not None:
            self.log.info("Ollama-Anfrage: Abbruch angefordert")
            self._token.cancel()

    def reset(self) -> bool:
        """New chat: clears the history.  Not possible while a request runs."""
        if self.busy:
            return False
        self.history = []
        self.last_error = ""
        return True

    def shutdown(self, timeout_ms: int = 10_000) -> None:
        self.cancel()
        self._pool.waitForDone(timeout_ms)
        self.busy, self._token = False, None  # the worker has ended; its queued "finished" signal no longer matters on the way out

    # -- internals ----------------------------------------------------------------------------------
    def _fail(self, message: str, kind: str) -> bool:
        self.last_error = f"{kind}: {message}"
        self.log.warning("Chat nicht gestartet: %s", kind)
        self.event.emit(LoopEvent("error", message, {"kind": kind}))
        self.event.emit(LoopEvent("done", data={"outcome": "error"}))
        return False

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.busy_changed.emit(busy)

    def _on_event(self, ev: LoopEvent) -> None:
        if ev.kind == "error":
            self.last_error = f"{ev.data.get('kind', 'ERROR')}: {ev.text}"[:300]
        self.event.emit(ev)

    def _on_finished(self, result: LoopResult) -> None:
        if result.outcome == "done":
            self.history = result.history
        elif result.outcome == "error":
            self.last_error = self.last_error or result.error
        self.log.info("Ollama-Anfrage beendet: %s, %d Tool-Runden, %d Tool-Aufrufe, %.1f s%s", result.outcome, result.rounds, result.tool_calls,
                      time.perf_counter() - self._started, f", {result.error}" if result.error else "")
        self._token = None
        self._set_busy(False)
