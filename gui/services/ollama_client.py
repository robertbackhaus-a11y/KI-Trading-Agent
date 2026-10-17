# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Ollama client of the GUI (standard library only: ``http.client`` and ``json``; no Qt, no extra package).

What it does: reachability, model list, and ``/api/chat`` as a *stream* of typed chunks (thinking text, answer text, tool calls, final
statistics).  A :class:`CancelToken` closes the HTTP connection from another thread, which stops exactly this request.

What it never does: start, stop, load or unload Ollama or a model (the GUI shares one Ollama instance with other clients), and it never
sets a context size or any other model option (the instance's own settings stay in effect).
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

DEFAULT_TIMEOUT_S = 300.0  # the first byte of a cold model can take a minute; every later chunk is fast
UNREACHABLE, MODEL_MISSING, HTTP_ERROR, TIMEOUT, STREAM_BROKEN, CANCELLED, PROTOCOL = (
    "UNREACHABLE", "MODEL_MISSING", "HTTP_ERROR", "TIMEOUT", "STREAM_BROKEN", "CANCELLED", "PROTOCOL",
)


class OllamaError(Exception):
    """``kind`` is one of the constants above; ``status`` is the HTTP status when there was one."""

    def __init__(self, kind: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind, self.status = kind, status


@dataclass
class OllamaStatus:
    reachable: bool
    version: str = ""
    models: list[str] = field(default_factory=list)
    error: str = ""

    def has_model(self, name: str) -> bool:
        return name in self.models or (":" not in name and f"{name}:latest" in self.models)


@dataclass
class Chunk:
    """``kind``: ``thinking`` | ``content`` | ``tool_calls`` | ``done``."""

    kind: str
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


class CancelToken:
    """Thread-safe cancel switch of one request.  ``cancel()`` may be called from any thread; it closes the connection."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            conn = self._conn
        if conn is not None:
            _force_close(conn)

    def attach(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._conn = conn
        if self._event.is_set():
            _force_close(conn)

    def detach(self) -> None:
        with self._lock:
            self._conn = None


def _force_close(conn: http.client.HTTPConnection) -> None:
    """Closing the file object alone does not wake a blocked read in another thread; shutting the socket down does."""
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        parts = urlsplit(base_url.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError("Ollama-URL muss mit http:// oder https:// beginnen")
        self.https = parts.scheme.lower() == "https"
        self.host, self.port = parts.hostname, parts.port or (443 if self.https else 80)
        self.prefix = parts.path.rstrip("/")
        self.timeout = timeout

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=timeout)

    # -- short GET requests -----------------------------------------------------------------
    def _get_json(self, path: str, timeout: float) -> Any:
        conn = self._connect(timeout)
        try:
            conn.request("GET", self.prefix + path)
            response = conn.getresponse()
            body = response.read(2_000_000)
            if response.status != 200:
                raise OllamaError(HTTP_ERROR, f"HTTP {response.status}", response.status)
            return json.loads(body)
        except OllamaError:
            raise
        except socket.timeout as exc:
            raise OllamaError(UNREACHABLE, "nicht erreichbar (Zeitüberschreitung)") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise OllamaError(UNREACHABLE, f"nicht erreichbar ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise OllamaError(PROTOCOL, "Antwort ist kein JSON") from exc
        finally:
            conn.close()

    def version(self, timeout: float = 2.0) -> str:
        return str(self._get_json("/api/version", timeout).get("version", ""))

    def models(self, timeout: float = 5.0) -> list[str]:
        data = self._get_json("/api/tags", timeout)
        return sorted(str(m.get("name", "")) for m in data.get("models", []) if isinstance(m, dict))

    def status(self, timeout: float = 2.0) -> OllamaStatus:
        """Reachability, version and model names.  These two GET requests never load a model."""
        try:
            version = self.version(timeout)
        except OllamaError as exc:
            return OllamaStatus(False, error=str(exc))
        try:
            models = self.models(max(timeout, 5.0))
        except OllamaError:
            models = []
        return OllamaStatus(True, version, models)

    # -- chat -------------------------------------------------------------------------------------
    def chat_stream(self, model: str, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, think: Optional[bool] = None,
                    token: Optional[CancelToken] = None) -> Iterator[Chunk]:
        """Stream one ``/api/chat`` answer.  Raises :class:`OllamaError`; a cancelled request raises ``OllamaError(CANCELLED)``.

        ``think`` is sent as given (``False`` switches thinking off).  A model that rejects the parameter (HTTP 400 mentioning it) is retried
        once without it.  No other option is sent.
        """
        token = token or CancelToken()
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        if think is not None:
            body["think"] = bool(think)
        try:
            yield from self._stream(body, token)
        except OllamaError as exc:
            if exc.kind == HTTP_ERROR and exc.status == 400 and "think" in body and "think" in str(exc).lower():
                body.pop("think")
                yield from self._stream(body, token)
            else:
                raise

    def _stream(self, body: dict[str, Any], token: CancelToken) -> Iterator[Chunk]:
        conn = self._connect(self.timeout)
        token.attach(conn)
        got_done = False
        try:
            try:
                try:
                    conn.connect()  # connection problems (refused, no route, connect timeout) mean "unreachable"
                except OSError as exc:
                    if token.cancelled:
                        raise OllamaError(CANCELLED, "abgebrochen") from exc
                    raise OllamaError(UNREACHABLE, f"Ollama nicht erreichbar ({type(exc).__name__})") from exc
                token.attach(conn)  # the socket exists now: a cancel can shut it down
                conn.request("POST", self.prefix + "/api/chat", json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
                response = conn.getresponse()
            except OllamaError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                if token.cancelled:
                    raise OllamaError(CANCELLED, "abgebrochen") from exc
                if isinstance(exc, socket.timeout):
                    raise OllamaError(TIMEOUT, "Zeitüberschreitung beim Warten auf Ollama") from exc
                raise OllamaError(UNREACHABLE, f"Ollama nicht erreichbar ({type(exc).__name__})") from exc
            if response.status != 200:
                raw = response.read(4000).decode("utf-8", "replace")
                message = raw
                try:
                    message = str(json.loads(raw).get("error", raw))
                except ValueError:
                    pass
                lowered = message.lower()
                if "not found" in lowered and "model" in lowered:
                    raise OllamaError(MODEL_MISSING, f"Modell nicht vorhanden: {body.get('model')}", response.status)
                raise OllamaError(HTTP_ERROR, f"HTTP {response.status}: {message[:300]}", response.status)
            while True:
                try:
                    line = response.readline()
                except socket.timeout as exc:
                    raise OllamaError(TIMEOUT, "Zeitüberschreitung im Stream") from exc
                except (OSError, http.client.HTTPException) as exc:
                    if token.cancelled:
                        raise OllamaError(CANCELLED, "abgebrochen") from exc
                    raise OllamaError(STREAM_BROKEN, f"Stream abgebrochen ({type(exc).__name__})") from exc
                if token.cancelled:
                    raise OllamaError(CANCELLED, "abgebrochen")
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError as exc:
                    raise OllamaError(PROTOCOL, "Ungültige Zeile im Stream") from exc
                if not isinstance(obj, dict):
                    raise OllamaError(PROTOCOL, "Unerwartete Zeile im Stream")
                if obj.get("error"):
                    message = str(obj["error"])
                    kind = MODEL_MISSING if "not found" in message.lower() else HTTP_ERROR
                    raise OllamaError(kind, message[:300])
                message = obj.get("message") or {}
                if message.get("thinking"):
                    yield Chunk("thinking", str(message["thinking"]))
                if message.get("content"):
                    yield Chunk("content", str(message["content"]))
                if message.get("tool_calls"):
                    yield Chunk("tool_calls", tool_calls=[_normalize_call(c) for c in message["tool_calls"] if isinstance(c, dict)])
                if obj.get("done"):
                    got_done = True
                    stats = {k: obj.get(k) for k in ("done_reason", "prompt_eval_count", "eval_count", "total_duration") if obj.get(k) is not None}
                    yield Chunk("done", stats=stats)
                    break
            if not got_done:
                if token.cancelled:
                    raise OllamaError(CANCELLED, "abgebrochen")
                raise OllamaError(STREAM_BROKEN, "Stream endete ohne Abschluss")
        finally:
            token.detach()
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _normalize_call(call: dict[str, Any]) -> dict[str, Any]:
    """``{"name": ..., "arguments": {...}}``; arguments that arrive as a JSON string are parsed, anything else becomes ``{}``."""
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            arguments = {"__invalid_json__": arguments[:200]}
    if not isinstance(arguments, dict):
        arguments = {}
    return {"name": str(function.get("name") or ""), "arguments": arguments}


def check_ollama(base_url: str, timeout: float = 2.0) -> OllamaStatus:
    """Status for diagnostics; never raises."""
    try:
        return OllamaClient(base_url).status(timeout)
    except ValueError as exc:
        return OllamaStatus(False, error=str(exc))
