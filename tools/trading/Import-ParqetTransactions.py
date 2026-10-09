"""Preview or safely import incremental Parqet CSV transactions.

Thin entry point kept for existing workflows: it runs ``Import-TradingTransactions.py`` with ``--format parqet`` as the default. All
options (``--write``, ``--include-historical``, ``--reconcile-campaigns``, ``--strategy``, ``--campaign-opened-at``, ...) are those of
that script; use it with ``--format canonical`` for a non-Parqet source.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_cli():
    path = Path(__file__).resolve().with_name("Import-TradingTransactions.py")
    spec = importlib.util.spec_from_file_location("import_trading_transactions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not any(item == "--format" or item.startswith("--format=") for item in arguments) and "--reconcile-campaigns" not in arguments:
        arguments = ["--format", "parqet", *arguments]
    return _load_cli().main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
