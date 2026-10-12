"""Shared helpers of the GUI tests: import paths, Qt availability, event pumping, process checks."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
for path in (ROOT, ROOT / "tools" / "trading", ROOT / "tests" / "trading", HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

HAVE_QT = importlib.util.find_spec("PySide6") is not None
FAKE_SERVER = HERE / "fake_mcp_server.py"


def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def pump(seconds: float, until=None) -> bool:
    """Process Qt events until ``until()`` is true or ``seconds`` have passed."""
    app = qapp()
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return bool(until()) if until is not None else True


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True).stdout.decode("utf-8", "replace")
    return f'"{pid}"' in out


def wait_gone(pid: int, seconds: float = 10.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return False
