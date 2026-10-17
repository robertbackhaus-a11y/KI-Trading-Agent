# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""A scripted fake Ollama HTTP server for the GUI tests (standard library only; synthetic data only).

``/api/version`` and ``/api/tags`` answer from attributes; every ``/api/chat`` request consumes the next :class:`Script`.  All requests are recorded
(``requests``) so tests can check what the GUI sent (offered tools, messages, the absence of ``num_ctx``).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional


@dataclass
class Script:
    lines: list[dict[str, Any]] = field(default_factory=list)
    delay: float = 0.0  # seconds between lines
    status: int = 200
    error: Optional[str] = None  # body {"error": ...} for status != 200
    drop_after: Optional[int] = None  # close the connection after this many lines (no "done")
    hang: float = 0.0  # sleep before the first byte


def say(*chunks: str, thinking: tuple[str, ...] = ()) -> Script:
    lines = [{"message": {"role": "assistant", "content": "", "thinking": t}, "done": False} for t in thinking]
    lines += [{"message": {"role": "assistant", "content": c}, "done": False} for c in chunks]
    lines.append(done())
    return Script(lines)


def call(name: str, arguments: Any, content: str = "") -> Script:
    return Script([{"message": {"role": "assistant", "content": content, "tool_calls": [{"id": "x1", "function": {"index": 0, "name": name, "arguments": arguments}}]}, "done": False}, done()])


def done(reason: str = "stop") -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": reason, "prompt_eval_count": 10, "eval_count": 5}


class FakeOllama:
    def __init__(self) -> None:
        self.version = "0.0.0-test"
        self.models = ["fake-model:latest", "other:1b"]
        self.scripts: list[Script] = []
        self.requests: list[dict[str, Any]] = []
        self.stopping = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _json(self, status: int, obj: Any) -> None:
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append({"method": "GET", "path": self.path})
                if self.path == "/api/version":
                    self._json(200, {"version": outer.version})
                elif self.path == "/api/tags":
                    self._json(200, {"models": [{"name": m} for m in outer.models]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"method": "POST", "path": self.path, "body": body})
                if self.path != "/api/chat":
                    self._json(404, {"error": "not found"})
                    return
                script = outer.scripts.pop(0) if outer.scripts else Script(status=500, error="no script left")
                if script.hang:
                    time.sleep(script.hang)
                if script.status != 200:
                    self._json(script.status, {"error": script.error or "error"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for index, line in enumerate(script.lines):
                        if script.drop_after is not None and index >= script.drop_after:
                            return
                        if outer.stopping.is_set():
                            return
                        self.wfile.write(json.dumps(line).encode() + b"\n")
                        self.wfile.flush()
                        if script.delay:
                            time.sleep(script.delay)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "FakeOllama":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()

    @property
    def chat_requests(self) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["method"] == "POST" and r["path"] == "/api/chat"]
