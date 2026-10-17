# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Runs one Trading script for the Task Scheduler without a console window.

The scheduled tasks used to start ``cmd.exe /c python script args >> log 2>&1``; cmd.exe and python.exe are console programs, so every run opened a window on the desktop (and
took the focus from a game).  The tasks now start ``pythonw.exe Run-TradingTask.py <log> <script> [args]`` instead: pythonw has no console, and this launcher does what the
redirection did -- stdout and stderr are appended to the log, with the same encoding and buffering as a redirected Python stream -- and the exit code of the process is the exit code of
the script (or 1 for an uncaught exception, whose traceback is in the log), so the scheduler's last result and the logs mean what they always meant.

The script runs as ``__main__`` with its own folder first on ``sys.path`` and its arguments in ``sys.argv``, as with ``python script.py``.  Nothing else is added: no retry, no
second process, no follow-up job.  (None of the Trading scripts starts a console child process, so no window can appear from below either.)
"""

from __future__ import annotations

import os
import runpy
import sys


def run(log: str, script: str, args: list[str]) -> None:
    out = open(log, "a")  # what ">>" gave stdout: the default encoding, block buffered
    err = open(log, "a", errors="backslashreplace", buffering=1)  # what Python's stderr is: backslashreplace, line buffered; the same order of writes as one shared handle
    sys.stdout, sys.stderr = out, err
    sys.argv = [script, *args]
    sys.path[0] = os.path.dirname(os.path.abspath(script))  # replaces the launcher's folder, like "python script.py"
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        for stream in (out, err):
            try:
                stream.flush()
            except OSError:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(2)  # usage: Run-TradingTask.py <log> <script> [args...] -- there is nowhere to print it without a console
    run(sys.argv[1], sys.argv[2], sys.argv[3:])
