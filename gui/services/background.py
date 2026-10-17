# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Run blocking work (PowerShell, file reads, HTTP probes) off the GUI thread and deliver the outcome back in the GUI thread."""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from .settings import get_logger


class _Done(QObject):
    finished = Signal(object, bool, object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any], callback: Callable[[bool, Any], None], done: _Done) -> None:
        super().__init__()
        self._fn, self._callback, self._done = fn, callback, done

    def run(self) -> None:
        try:
            outcome = (True, self._fn())
        except Exception as exc:  # noqa: BLE001 - the callback decides what to show
            outcome = (False, exc)
        self._done.finished.emit(self._callback, *outcome)


class AsyncRunner(QObject):
    """``submit(fn, callback)`` runs ``fn`` in the thread pool; ``callback(ok, value_or_exception)`` runs in the GUI thread."""

    def __init__(self, parent: Optional[QObject] = None, max_threads: int = 4) -> None:
        super().__init__(parent)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max_threads)
        self._done = _Done()
        self._done.finished.connect(self._deliver)
        self.log = get_logger()

    def submit(self, fn: Callable[[], Any], callback: Callable[[bool, Any], None]) -> None:
        self._pool.start(_Job(fn, callback, self._done))

    def _deliver(self, callback: Callable[[bool, Any], None], ok: bool, value: Any) -> None:
        try:
            callback(ok, value)
        except Exception:  # noqa: BLE001
            self.log.exception("Fehler im Callback eines Hintergrundjobs")

    def wait(self, timeout_ms: int = 10_000) -> bool:
        return self._pool.waitForDone(timeout_ms)
