"""Deterministically restore missing ``market_snapshot.currency`` values.

The script never guesses from ticker/name/price.  It only persists a currency
when the existing provider observation, same-provider source symbol, or
security listing metadata provides one unambiguous ISO-style value.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import shutil
import sqlite3
from pathlib import Path
from typing import Optional


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")


def _currency(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text if len(text) == 3 and text.isalpha() and text == text.upper() else None


@dataclass(frozen=True)
class CurrencyResolution:
    security_id: int
    currency: Optional[str]
    status: str
    provenance: Optional[str]


def _distinct(values) -> list[str]:
    return sorted({code for value in values if (code := _currency(value)) is not None})


def resolve_snapshot_currency(conn: sqlite3.Connection, row: sqlite3.Row) -> CurrencyResolution:
    """Resolve one snapshot with strict ordered authoritative fallbacks."""
    security_id = int(row["security_id"])
    snapshot_currency = _currency(row["currency"])
    if snapshot_currency is not None:
        return CurrencyResolution(security_id, snapshot_currency, "CURRENCY_ALREADY_PRESENT", "market_snapshot.currency")
    if str(row["currency"] or "").strip():
        return CurrencyResolution(security_id, None, "CURRENCY_UNRESOLVED", "market_snapshot.currency unsupported quote unit")
    as_of = str(row["as_of_at"] or "")[:10]
    source_id = row["source_id"]
    if source_id is not None and as_of:
        observed = _distinct(item["currency"] for item in conn.execute(
            """SELECT currency FROM market_data
               WHERE security_id=? AND source_id=? AND trade_date<=? AND currency IS NOT NULL
               ORDER BY trade_date DESC, id DESC""", (security_id, source_id, as_of)
        ))
        if len(observed) == 1:
            return CurrencyResolution(security_id, observed[0], "CURRENCY_RESOLVED", "market_data.currency")
        if len(observed) > 1:
            return CurrencyResolution(security_id, None, "CURRENCY_AMBIGUOUS", "market_data.currency conflict")
        source_symbol = _distinct(item["currency"] for item in conn.execute(
            "SELECT currency FROM source_symbols WHERE security_id=? AND source_id=? AND currency IS NOT NULL",
            (security_id, source_id),
        ))
        if len(source_symbol) == 1:
            return CurrencyResolution(security_id, source_symbol[0], "CURRENCY_RESOLVED", "source_symbols.currency")
        if len(source_symbol) > 1:
            return CurrencyResolution(security_id, None, "CURRENCY_AMBIGUOUS", "source_symbols.currency conflict")
    security = conn.execute("SELECT currency FROM security WHERE id=?", (security_id,)).fetchone()
    security_currency = _currency(security["currency"] if security else None)
    if security_currency is not None:
        return CurrencyResolution(security_id, security_currency, "CURRENCY_RESOLVED", "security.currency")
    return CurrencyResolution(security_id, None, "CURRENCY_UNRESOLVED", None)


def build_plan(conn: sqlite3.Connection, *, watchlist_only: bool = False) -> list[CurrencyResolution]:
    query = """SELECT ms.security_id, ms.as_of_at, ms.currency, ms.source_id
               FROM market_snapshot ms WHERE ms.price IS NOT NULL"""
    if watchlist_only:
        query += " AND EXISTS (SELECT 1 FROM watchlist w WHERE w.security_id=ms.security_id AND w.status='WATCH')"
    query += " ORDER BY ms.security_id"
    return [resolve_snapshot_currency(conn, row) for row in conn.execute(query)]


def apply_plan(conn: sqlite3.Connection, plan: list[CurrencyResolution]) -> int:
    rows = [(item.currency, item.security_id) for item in plan if item.status == "CURRENCY_RESOLVED" and item.currency]
    if not rows:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("UPDATE market_snapshot SET currency=? WHERE security_id=? AND currency IS NULL", rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        conn = sqlite3.connect(f"file:///{path.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill deterministic market snapshot currencies (dry-run by default)")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--watchlist-only", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if not args.db.is_file():
        raise FileNotFoundError(f"Trading DB not found: {args.db}")
    conn = _connect(args.db, write=args.write)
    try:
        plan = build_plan(conn, watchlist_only=args.watchlist_only)
        summary = {status: sum(item.status == status for item in plan) for status in ("CURRENCY_RESOLVED", "CURRENCY_ALREADY_PRESENT", "CURRENCY_AMBIGUOUS", "CURRENCY_UNRESOLVED")}
        result = {"dry_run": not args.write, "summary": summary, "rows": [item.__dict__ for item in plan]}
        if args.write:
            backup = args.db.with_name(args.db.name + ".snapshot-currency-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".bak")
            shutil.copy2(args.db, backup)
            result["backup"] = str(backup)
            result["updated"] = apply_plan(conn, plan)
        print(result)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
