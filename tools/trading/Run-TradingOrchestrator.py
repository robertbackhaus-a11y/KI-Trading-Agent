"""Run the read-only portfolio-level trading orchestrator."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from trading_orchestrator import run_trading_orchestrator


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only trading orchestration")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--as-of")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:///{args.db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        result = run_trading_orchestrator(conn, as_of=args.as_of).primitive()
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        print("PORTFOLIO STATUS")
        print(f"Evaluation: {result['evaluation_as_of']} | status: {result['global_status']}")
        print("Actions:", result["action_summary"])
        print("Next review:")
        for item in result["next_review_items"]:
            print(f"- {item['priority']} {item.get('symbol') or item.get('code')}: {item.get('kind') or item.get('code')}")


if __name__ == "__main__":
    main()
