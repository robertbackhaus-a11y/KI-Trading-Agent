# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""MCP client of the GUI: the official ``mcp`` SDK against the Trading MCP server as a stdio child process.

Two layers:

* :class:`McpClientCore` -- pure asyncio, no Qt.  One supervisor task owns the child process and the ``ClientSession`` (the SDK's
  cancel scopes must be entered and left by the same task); calls run as separate tasks on the same loop.
* :class:`McpService` -- the Qt side.  A ``QThread`` runs the asyncio loop; the GUI thread never blocks.  Results are delivered to
  a callback *in the GUI thread* through a queued signal.

The server is never imported; it stays a separate runtime boundary (it may run next to another client at the same time).  All
paths are absolute (a relative ``TRADING_DB_PATH`` breaks the server's read-only tools).  Nothing here depends on
``capabilities.experimental`` (2.3.0 omits it).  ``call`` / ``call_sync`` accept READ tools only; ``call_action`` accepts exactly the watchlist ACTIONs of ``tool_policy.GUI_ACTIONS`` (see ``tool_policy``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import ntpath
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import anyio
import mcp_types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from PySide6.QtCore import QObject, QThread, Signal

from . import tool_policy
from .settings import Settings, get_logger

DISCONNECTED, CONNECTING, CONNECTED, ERROR = "DISCONNECTED", "CONNECTING", "CONNECTED", "ERROR"
STDERR_LOG_LIMIT = 2_000_000  # bytes; the log is started fresh above this size
RECONNECT_MIN_INTERVAL_S = 3.0


@dataclass
class McpResult:
    """Outcome of one tool call.  ``ok`` is the technical outcome; ``tool_ok`` is the tool's own ``ok`` flag (if it has one)."""

    name: str
    ok: bool
    data: Any = None
    error: Optional[str] = None
    kind: str = "OK"  # OK | TIMEOUT | DENIED | NOT_CONNECTED | TOOL_ERROR | TRANSPORT | INVALID_RESULT
    seconds: float = 0.0
    chars: int = 0

    @property
    def tool_ok(self) -> bool:
        return self.ok and not (isinstance(self.data, dict) and self.data.get("ok") is False)

    @property
    def message(self) -> str:
        if self.error:
            return self.error
        if isinstance(self.data, dict) and self.data.get("ok") is False:
            return str(self.data.get("message") or self.data.get("error") or "Tool meldet einen Fehler")
        return ""


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def describe(exc: BaseException) -> str:
    leaf = _leaf(exc)
    text = str(leaf).strip().splitlines()[0] if str(leaf).strip() else ""
    return f"{type(leaf).__name__}: {text[:200]}" if text else type(leaf).__name__


class McpClientCore:
    def __init__(
        self,
        python_path: str,
        server_path: str,
        db_path: str,
        stderr_path: Path,
        *,
        user_id: str = "qt-gui",
        connect_timeout: float = 30.0,
        ping_interval: float = 5.0,
        extra_env: Optional[dict[str, str]] = None,
        on_state: Optional[Callable[[str, str], None]] = None,
        on_tools: Optional[Callable[[int], None]] = None,
    ):
        self.configure(python_path, server_path, db_path)
        self.stderr_path = Path(stderr_path)
        self.user_id = user_id
        self.connect_timeout = connect_timeout
        self.ping_interval = ping_interval
        self.extra_env = dict(extra_env or {})
        self._on_state, self._on_tools = on_state, on_tools
        self.state, self.detail = DISCONNECTED, ""
        self.tools: dict[str, dict[str, Any]] = {}
        self._session: Optional[ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._lock: Optional[asyncio.Lock] = None
        self._last_attempt = 0.0
        self.log = get_logger()

    def configure(self, python_path: str, server_path: str, db_path: str) -> None:
        self.python_path, self.server_path, self.db_path = python_path, server_path, db_path

    # -- state ---------------------------------------------------------------
    def _set(self, state: str, detail: str = "") -> None:
        changed = (state, detail) != (self.state, self.detail)
        self.state, self.detail = state, detail
        if changed:
            self.log.info("MCP %s%s", state, f" – {detail}" if detail else "")
            if self._on_state:
                self._on_state(state, detail)

    def _check_paths(self) -> Optional[str]:
        for label, value in (("MCP-Python", self.python_path), ("MCP-Server", self.server_path), ("Datenbank", self.db_path)):
            if not ntpath.isabs(value):
                return f"{label}-Pfad ist nicht absolut: {value}"
        for label, value in (("MCP-Python", self.python_path), ("MCP-Server", self.server_path)):
            if not Path(value).is_file():
                return f"{label} nicht gefunden: {value}"
        return None

    def _open_stderr(self):
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if self.stderr_path.exists() and self.stderr_path.stat().st_size > STDERR_LOG_LIMIT else "a"
        handle = open(self.stderr_path, mode, encoding="utf-8", errors="replace")
        handle.write(f"\n--- MCP start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        handle.flush()
        return handle

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> bool:
        """Connect (idempotent).  Never raises; the outcome is in ``state``/``detail``."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._task is not None and not self._task.done() and self.state == CONNECTED:
                return True
            await self._reap()
            self._last_attempt = time.monotonic()
            problem = self._check_paths()
            if problem:
                self._set(ERROR, problem)
                return False
            self._stop = asyncio.Event()
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(self._supervise(ready))
            try:
                return await asyncio.wait_for(asyncio.shield(ready), self.connect_timeout + 10)
            except asyncio.TimeoutError:
                self._set(ERROR, "Zeitüberschreitung beim Verbinden")
                await self._reap(force=True)
                return False

    async def stop(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._reap()
        if self.state != ERROR:
            self._set(DISCONNECTED, "")

    async def restart(self) -> bool:
        await self.stop()
        return await self.start()

    async def _reap(self, force: bool = False) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        if self._stop is not None:
            self._stop.set()
        if not force:
            try:
                await asyncio.wait_for(asyncio.shield(task), 15)
                return
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _supervise(self, ready: asyncio.Future) -> None:
        errlog = None
        error: Optional[str] = None
        try:
            self._set(CONNECTING, "")
            errlog = self._open_stderr()
            env = {**os.environ, "TRADING_DB_PATH": self.db_path, "TRADING_USER_ID": self.user_id, "PYTHONDONTWRITEBYTECODE": "1", **self.extra_env}
            params = StdioServerParameters(command=self.python_path, args=[self.server_path], env=env)
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), self.connect_timeout)
                    tools: dict[str, dict[str, Any]] = {}
                    cursor = None
                    for _ in range(20):  # the server lists everything in one page; the cap only guards a paginator that never ends
                        page = await session.list_tools(params=mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None)
                        for tool in page.tools:
                            tools[tool.name] = {"description": tool.description or "", "input_schema": tool.input_schema}
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    self.tools, self._session = tools, session
                    self._set(CONNECTED, f"{len(tools)} Tools")
                    if self._on_tools:
                        self._on_tools(len(tools))
                    if not ready.done():
                        ready.set_result(True)
                    while not self._stop.is_set():
                        try:
                            await asyncio.wait_for(self._stop.wait(), self.ping_interval)
                            break
                        except asyncio.TimeoutError:
                            pass
                        try:  # cheap local liveness check: a dead child shows up here within one interval
                            await asyncio.wait_for(session.send_ping(), 10)
                        except Exception as exc:  # noqa: BLE001
                            raise ConnectionError(f"MCP-Server antwortet nicht ({describe(exc)})") from exc
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - includes ExceptionGroup from the SDK's task groups
            error = describe(exc)
        finally:
            self._session = None
            if errlog is not None:
                try:
                    errlog.close()
                except OSError:
                    pass
            if error:
                self._set(ERROR, error)
            elif self.state != ERROR:
                self._set(DISCONNECTED, "")
            if not ready.done():
                ready.set_result(False)

    def _mark_lost(self, why: str) -> None:
        """The transport is gone: let the supervisor leave its contexts (the next call reconnects)."""
        self._session = None  # new calls must not reuse the dead session while the supervisor is still leaving its contexts
        self._set(ERROR, why)
        if self._stop is not None:
            self._stop.set()

    # -- calls -------------------------------------------------------------------
    async def call(self, name: str, arguments: Optional[dict[str, Any]] = None, timeout: float = 60.0) -> McpResult:
        """Call a tool.  Never raises.  Reconnects once if the connection was lost (at most every few seconds)."""
        started = time.perf_counter()

        def result(ok: bool, **kw: Any) -> McpResult:
            return McpResult(name, ok, seconds=round(time.perf_counter() - started, 3), **kw)

        if self._session is None and time.monotonic() - self._last_attempt > RECONNECT_MIN_INTERVAL_S:
            await self.start()
        session = self._session
        if session is None:
            return result(False, error=f"MCP nicht verbunden{': ' + self.detail if self.detail else ''}", kind="NOT_CONNECTED")
        try:
            response = await asyncio.wait_for(session.call_tool(name, arguments or {}, read_timeout_seconds=timeout), timeout + 5)
        except (asyncio.TimeoutError, TimeoutError):
            return result(False, error=f"Zeitüberschreitung nach {timeout:g} s", kind="TIMEOUT")
        except MCPError as exc:
            if exc.code == getattr(mcp_types, "REQUEST_TIMEOUT", object()):
                return result(False, error=f"Zeitüberschreitung nach {timeout:g} s", kind="TIMEOUT")
            if exc.code == getattr(mcp_types, "CONNECTION_CLOSED", object()):
                self._mark_lost("Verbindung zum MCP-Server verloren")
                return result(False, error="Verbindung zum MCP-Server verloren", kind="TRANSPORT")
            return result(False, error=describe(exc), kind="TOOL_ERROR")
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, ConnectionError, OSError) as exc:
            self._mark_lost("Verbindung zum MCP-Server verloren")
            return result(False, error=describe(exc), kind="TRANSPORT")
        except Exception as exc:  # noqa: BLE001
            return result(False, error=describe(exc), kind="TRANSPORT")
        text = "\n".join(getattr(part, "text", "") for part in response.content)
        if response.is_error:
            return result(False, error=text[:300] or "Tool-Fehler", kind="TOOL_ERROR", chars=len(text))
        try:
            data = json.loads(text)
        except ValueError:
            return result(False, error="Ungültiges Tool-Ergebnis (kein JSON)", kind="INVALID_RESULT", chars=len(text))
        return result(True, data=data, chars=len(text))


class _LoopThread(QThread):
    """A thread that only runs one asyncio loop (the loop and everything it owns belong to this thread)."""

    def __init__(self) -> None:
        super().__init__()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)


class McpService(QObject):
    """Qt facade: ``call`` returns at once, the result arrives via ``on_done`` in the GUI thread."""

    state_changed = Signal(str, str)
    tools_changed = Signal(int)
    _deliver = Signal(object, object)

    def __init__(self, settings: Settings, stderr_path: Path, parent: Optional[QObject] = None, *, connect_timeout: float = 30.0, ping_interval: float = 5.0,
                 extra_env: Optional[dict[str, str]] = None, allow_actions: bool = False):
        super().__init__(parent)
        self.stderr_path = Path(stderr_path)
        self.allow_actions = allow_actions  # Phase 1 never sets this
        self._thread: Optional[_LoopThread] = None
        self._core = McpClientCore(
            settings.mcp_python_path, settings.mcp_server_path, settings.trading_db_path, self.stderr_path,
            connect_timeout=connect_timeout, ping_interval=ping_interval, extra_env=extra_env,
            on_state=lambda s, d: self.state_changed.emit(s, d), on_tools=lambda n: self.tools_changed.emit(n),
        )
        self._deliver.connect(self._run_callback)
        self._next_id = 0
        self.log = get_logger()

    # -- properties (read from the GUI thread; plain attribute reads) ---------------
    @property
    def state(self) -> str:
        return self._core.state

    @property
    def detail(self) -> str:
        return self._core.detail

    @property
    def tools(self) -> dict[str, dict[str, Any]]:
        return dict(self._core.tools)

    @property
    def tool_count(self) -> int:
        return len(self._core.tools)

    # -- lifecycle -------------------------------------------------------------------
    def _submit(self, coro) -> "asyncio.Future":
        assert self._thread is not None and self._thread.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._thread.loop)

    def start(self) -> None:
        if self._thread is None:
            self._thread = _LoopThread()
            self._thread.start()
            if not self._thread.wait_ready():
                raise RuntimeError("asyncio-Thread startet nicht")
            self.log.info("MCP-Dienst gestartet")
        self._submit(self._core.start())

    def restart(self, settings: Optional[Settings] = None) -> None:
        """Restart only the MCP child process (no trading data is touched)."""
        if self._thread is None:
            self.start()
            return
        if settings is not None:
            self._core.configure(settings.mcp_python_path, settings.mcp_server_path, settings.trading_db_path)
        self.log.info("MCP-Neustart angefordert")
        self._submit(self._core.restart())

    def shutdown(self, timeout: float = 20.0) -> None:
        """Stop the child process and the loop thread.  Idempotent."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._core.stop(), thread.loop).result(timeout)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("MCP-Stopp nicht sauber: %s", describe(exc))
        thread.loop.call_soon_threadsafe(thread.loop.stop)
        thread.wait(int(timeout * 1000))
        self.log.info("MCP-Dienst beendet")

    # -- calls ---------------------------------------------------------------------------
    def call(self, name: str, arguments: Optional[dict[str, Any]] = None, timeout: float = 60.0, on_done: Optional[Callable[[McpResult], None]] = None) -> int:
        self._next_id += 1
        request_id = self._next_id
        if not self.allow_actions and not tool_policy.is_read(name):
            self.log.warning("Tool %s abgelehnt (%s)", name, tool_policy.classify(name))
            denied = McpResult(name, False, error=f"{name} ist in dieser GUI-Version nicht erlaubt", kind="DENIED")
            self._deliver.emit(on_done, denied)
            return request_id
        if self._thread is None:
            self._deliver.emit(on_done, McpResult(name, False, error="MCP-Dienst ist nicht gestartet", kind="NOT_CONNECTED"))
            return request_id
        future = self._submit(self._core.call(name, arguments, timeout))

        def finished(f) -> None:
            try:
                res = f.result()
            except BaseException as exc:  # noqa: BLE001
                res = McpResult(name, False, error=describe(exc), kind="TRANSPORT")
            self._deliver.emit(on_done, res)

        future.add_done_callback(finished)
        return request_id

    def call_action(self, name: str, arguments: Optional[dict[str, Any]] = None, timeout: float = 30.0, on_done: Optional[Callable[[McpResult], None]] = None) -> int:
        """Call one of the narrow watchlist ACTIONs (``tool_policy.GUI_ACTIONS``) -- only for an explicit user action (a button), never for the chat.

        Anything else (``sql_execute``, ``approve_swing_promotion``, an unknown tool, a READ tool) is refused here exactly as ``call`` refuses
        the ACTIONs.  The result arrives like that of :meth:`call`, in the GUI thread.
        """
        self._next_id += 1
        request_id = self._next_id
        if not tool_policy.is_gui_action(name):
            self.log.warning("Aktion %s abgelehnt (%s)", name, tool_policy.classify(name))
            self._deliver.emit(on_done, McpResult(name, False, error=f"{name} ist keine erlaubte GUI-Aktion", kind="DENIED"))
            return request_id
        if self._thread is None:
            self._deliver.emit(on_done, McpResult(name, False, error="MCP-Dienst ist nicht gestartet", kind="NOT_CONNECTED"))
            return request_id
        future = self._submit(self._core.call(name, arguments, timeout))

        def finished(f) -> None:
            try:
                res = f.result()
            except BaseException as exc:  # noqa: BLE001
                res = McpResult(name, False, error=describe(exc), kind="TRANSPORT")
            self._deliver.emit(on_done, res)

        future.add_done_callback(finished)
        return request_id

    def call_sync(self, name: str, arguments: Optional[dict[str, Any]] = None, timeout: float = 60.0, cancelled: Optional[Callable[[], bool]] = None) -> McpResult:
        """Blocking variant for worker threads (never call it from the GUI thread).  Same read-only policy as :meth:`call`.

        The ``ClientSession`` is still used only inside the asyncio thread: the call is submitted there and this thread waits for the result.
        ``cancelled`` is polled; when it turns true the pending call is cancelled and a ``CANCELLED`` result is returned.
        """
        if not self.allow_actions and not tool_policy.is_read(name):
            self.log.warning("Tool %s abgelehnt (%s)", name, tool_policy.classify(name))
            return McpResult(name, False, error=f"{name} ist in dieser GUI-Version nicht erlaubt", kind="DENIED")
        if self._thread is None:
            return McpResult(name, False, error="MCP-Dienst ist nicht gestartet", kind="NOT_CONNECTED")
        started = time.monotonic()
        future = self._submit(self._core.call(name, arguments, timeout))
        res: Optional[McpResult] = None
        while res is None:
            done, _ = concurrent.futures.wait([future], timeout=0.1)
            if done:
                try:
                    res = future.result()
                except BaseException as exc:  # noqa: BLE001
                    res = McpResult(name, False, error=describe(exc), kind="TRANSPORT")
            elif cancelled is not None and cancelled():
                future.cancel()
                res = McpResult(name, False, error="abgebrochen", kind="CANCELLED", seconds=round(time.monotonic() - started, 3))
            elif time.monotonic() - started > timeout + 15:
                future.cancel()
                res = McpResult(name, False, error=f"Zeitüberschreitung nach {timeout:g} s", kind="TIMEOUT", seconds=round(time.monotonic() - started, 3))
        self._log_result(res)
        return res

    def _log_result(self, res: McpResult) -> None:
        # Name, duration and size only -- never the content.
        self.log.info("Tool %s: %s in %.2f s, %d Zeichen%s", res.name, "ok" if res.ok else res.kind, res.seconds, res.chars, f" – {res.error}" if res.error else "")

    def _run_callback(self, callback: Optional[Callable[[McpResult], None]], res: McpResult) -> None:
        # GUI thread.
        self._log_result(res)
        if callback is not None:
            try:
                callback(res)
            except Exception:  # noqa: BLE001 - a faulty view callback must not kill the app
                self.log.exception("Fehler im Callback von %s", res.name)
