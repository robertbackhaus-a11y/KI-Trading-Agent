"""
Add a security to the trading watchlist.

Pure DB write logic only -- no web research, no market data /
fundamentals backfill. Master data (name, symbol, ISIN, WKN,
exchange, currency, country) must be supplied already verified by
the caller (the trading agent). Optional fields that are not
supplied stay NULL; nothing is guessed.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Optional


DB_PATH = Path(
    r"C:\KI-Stack\data\trading\trading.db"
)


# ============================================================
# DATABASE
# ============================================================

def connect() -> sqlite3.Connection:

    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Trading DB not found: {DB_PATH}"
        )

    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=10.0,
        isolation_level=None,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")

    return conn


def validate_schema(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        """
        SELECT value
        FROM metadata
        WHERE key = 'schema_version'
        """
    ).fetchone()

    if row is None:
        raise RuntimeError("metadata.schema_version missing")

    if row["value"] != "2.0":
        raise RuntimeError(
            f"Expected schema 2.0, got {row['value']}"
        )


# ============================================================
# SECURITY LOOKUP / CREATE
# ============================================================

def find_existing_security(
    conn: sqlite3.Connection,
    isin: Optional[str],
    symbol: Optional[str],
    name: Optional[str],
) -> Optional[sqlite3.Row]:
    """Look up an existing security by whichever identifiers were
    supplied, most reliable first (ISIN, then symbol, then name)."""

    if isin:

        row = conn.execute(
            "SELECT * FROM security WHERE isin = ?",
            (isin,),
        ).fetchone()

        if row is not None:
            return row

    if symbol:

        row = conn.execute(
            "SELECT * FROM security WHERE UPPER(symbol) = UPPER(?)",
            (symbol,),
        ).fetchone()

        if row is not None:
            return row

    if name:

        row = conn.execute(
            "SELECT * FROM security WHERE UPPER(name) = UPPER(?)",
            (name,),
        ).fetchone()

        if row is not None:
            return row

    return None


def create_security(
    conn: sqlite3.Connection,
    name: str,
    symbol: Optional[str],
    isin: Optional[str],
    wkn: Optional[str],
    exchange: Optional[str],
    currency: Optional[str],
    country: Optional[str],
) -> int:

    cursor = conn.execute(
        """
        INSERT INTO security (
            symbol, isin, wkn, name,
            exchange, currency, country,
            asset_type, active
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'stock', 1)
        """,
        (
            symbol,
            isin,
            wkn,
            name,
            exchange,
            currency,
            country,
        ),
    )

    return cursor.lastrowid


# ============================================================
# WATCHLIST
# ============================================================

def ensure_watchlist_entry(
    conn: sqlite3.Connection,
    security_id: int,
) -> tuple[bool, str]:
    """Returns (created, status). created is False if a watchlist
    row already existed for this security -- no duplicate is ever
    inserted (security_id is the watchlist primary key)."""

    existing = conn.execute(
        "SELECT status FROM watchlist WHERE security_id = ?",
        (security_id,),
    ).fetchone()

    if existing is not None:
        return False, existing["status"]

    conn.execute(
        "INSERT INTO watchlist (security_id, status) VALUES (?, 'WATCH')",
        (security_id,),
    )

    return True, "WATCH"


# ============================================================
# STATUS CHECKS (report only, no side effects)
# ============================================================

def has_rows(
    conn: sqlite3.Connection,
    table: str,
    security_id: int,
) -> bool:

    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE security_id = ? LIMIT 1",
        (security_id,),
    ).fetchone()

    return row is not None


# ============================================================
# MAIN OPERATION
# ============================================================

def add_to_watchlist(
    name: Optional[str],
    symbol: Optional[str],
    isin: Optional[str],
    wkn: Optional[str],
    exchange: Optional[str],
    currency: Optional[str],
    country: Optional[str],
) -> dict:

    if not name and not symbol:
        raise ValueError(
            "either --name or --symbol is required"
        )

    conn = connect()

    try:

        validate_schema(conn)

        conn.execute("BEGIN IMMEDIATE")

        try:

            existing = find_existing_security(
                conn,
                isin,
                symbol,
                name,
            )

            if existing is not None:

                security_id = existing["id"]
                security_created = False

            else:

                security_id = create_security(
                    conn,
                    name or symbol,
                    symbol,
                    isin,
                    wkn,
                    exchange,
                    currency,
                    country,
                )

                security_created = True

            watchlist_created, watchlist_status = ensure_watchlist_entry(
                conn,
                security_id,
            )

            conn.execute("COMMIT")

        except Exception:

            conn.execute("ROLLBACK")
            raise

        return {
            "security_id": security_id,
            "security_created": security_created,
            "watchlist_created": watchlist_created,
            "watchlist_status": watchlist_status,
            "has_source_symbols": has_rows(
                conn, "source_symbols", security_id
            ),
            "has_market_data": has_rows(
                conn, "market_data", security_id
            ),
            "has_fundamentals": has_rows(
                conn, "fundamentals", security_id
            ),
        }

    finally:

        conn.close()


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=(
            "Add a security to the trading watchlist "
            "(DB write only -- no research, no backfills)."
        )
    )

    parser.add_argument("--name", default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--isin", default=None)
    parser.add_argument("--wkn", default=None)
    parser.add_argument("--exchange", default=None)
    parser.add_argument("--currency", default=None)
    parser.add_argument("--country", default=None)

    return parser.parse_args()


def main() -> None:

    args = parse_args()

    if not args.name and not args.symbol:
        raise SystemExit(
            "error: either --name or --symbol is required"
        )

    result = add_to_watchlist(
        name=args.name,
        symbol=args.symbol,
        isin=args.isin,
        wkn=args.wkn,
        exchange=args.exchange,
        currency=args.currency,
        country=args.country,
    )

    print()
    print("===============================================")
    print(" Trading Watchlist Add")
    print("===============================================")

    print(f"security_id           : {result['security_id']}")
    print(
        f"security neu angelegt : "
        f"{'ja' if result['security_created'] else 'nein (vorhanden)'}"
    )
    print(
        f"Watchlist angelegt    : "
        f"{'ja' if result['watchlist_created'] else 'nein'}"
    )
    print(f"Watchlist-Status      : {result['watchlist_status']}")
    print(
        f"Source-Identifier     : "
        f"{'ja' if result['has_source_symbols'] else 'nein'}"
    )
    print(
        f"Market Data vorhanden : "
        f"{'ja' if result['has_market_data'] else 'nein'}"
    )
    print(
        f"Fundamentals vorhanden: "
        f"{'ja' if result['has_fundamentals'] else 'nein'}"
    )
    print("===============================================")


if __name__ == "__main__":
    main()
