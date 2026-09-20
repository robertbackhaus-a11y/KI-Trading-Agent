from __future__ import annotations

import sqlite3
from pathlib import Path


DB_PATH = Path(
    r"C:\KI-Stack\data\trading\trading.db"
)


# ============================================================
# Canonical security data
#
# Nur verifizierte Daten.
#
# country:
#   Land des Emittenten / Fondsdomizil,
#   NICHT Handelsplatz.
#
# exchange:
#   bevorzugter Handelsplatz für den Trading-Agenten.
#
# Wir verändern ausschließlich Securities, die aktuell
# Portfolio oder Watchlist sind.
# ============================================================

CANONICAL = {

    # ========================================================
    # AKTIVE POSITIONEN / RELEVANTE BESTÄNDE
    # ========================================================

    "ASML Holding": {
        "symbol": "ASML",
        "isin": "NL0010273215",
        "exchange": "EURONEXT_AMSTERDAM",
        "currency": "EUR",
        "country": "NL",
        "asset_type": "stock",
    },

    "Rheinmetall": {
        "symbol": "RHM",
        "isin": "DE0007030009",
        "wkn": "703000",
        "exchange": "XETRA",
        "currency": "EUR",
        "country": "DE",
        "asset_type": "stock",
    },

    "Invesco S&P 500 UCITS ETF - USD ACC": {
        "symbol": "SPXS",
        "isin": "IE00B3YCGJ38",
        "wkn": "A1CYW7",
        "exchange": "LSE",
        "country": "IE",
        "asset_type": "etf",
    },

    "iShares MSCI World ex-USA UCITS ETF - USD ACC": {
        "symbol": "XUSE",
        "country": "IE",
        "asset_type": "etf",
    },

    "iShares Core MSCI EM IMI UCITS ETF - USD ACC": {
        "symbol": "IS3N",
        "isin": "IE00BKM4GZ66",
        "wkn": "A111X9",
        "exchange": "XETRA",
        "country": "IE",
        "asset_type": "etf",
    },

    # ========================================================
    # WATCHLIST - NAME ONLY
    # ========================================================

    "AT&S": {
        "symbol": "ATS",
        "isin": "AT0000969985",
        "wkn": "969985",
        "exchange": "VIENNA",
        "currency": "EUR",
        "country": "AT",
        "asset_type": "stock",
    },

    "Aixtron": {
        "symbol": "AIXA",
        "isin": "DE000A0WMPJ6",
        "wkn": "A0WMPJ",
        "exchange": "XETRA",
        "currency": "EUR",
        "country": "DE",
        "asset_type": "stock",
    },

    "BAE Systems": {
        "symbol": "BA.",
        "exchange": "LSE",
        "currency": "GBP",
        "country": "GB",
        "asset_type": "stock",
    },

    "Bayer": {
        "symbol": "BAYN",
        "isin": "DE000BAY0017",
        "wkn": "BAY001",
        "exchange": "XETRA",
        "currency": "EUR",
        "country": "DE",
        "asset_type": "stock",
    },

    "BridgeBio Pharma": {
        "symbol": "BBIO",
        "exchange": "NASDAQ",
        "currency": "USD",
        "country": "US",
        "asset_type": "stock",
    },

    "Hensoldt": {
        "symbol": "HAG",
        "isin": "DE000HAG0005",
        "wkn": "HAG000",
        "exchange": "XETRA",
        "currency": "EUR",
        "country": "DE",
        "asset_type": "stock",
    },

    "LyondellBasell": {
        "symbol": "LYB",
        "exchange": "NYSE",
        "currency": "USD",
        "country": "NL",
        "asset_type": "stock",
    },

    "Neurocrine Biosciences": {
        "symbol": "NBIX",
        "exchange": "NASDAQ",
        "currency": "USD",
        "country": "US",
        "asset_type": "stock",
    },

    "OHB": {
        "symbol": "OHB",
        "isin": "DE0005936124",
        "wkn": "593612",
        "exchange": "FRANKFURT",
        "currency": "EUR",
        "country": "DE",
        "asset_type": "stock",
    },

    "Thales": {
        "symbol": "HO",
        "isin": "FR0000121329",
        "exchange": "EURONEXT_PARIS",
        "currency": "EUR",
        "country": "FR",
        "asset_type": "stock",
    },

    "Vertex Pharmaceuticals": {
        "symbol": "VRTX",
        "exchange": "NASDAQ",
        "currency": "USD",
        "country": "US",
        "asset_type": "stock",
    },
}


# ============================================================
# DB
# ============================================================

def connect() -> sqlite3.Connection:

    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Trading DB not found: {DB_PATH}"
        )

    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=5.0,
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA foreign_keys = ON;"
    )

    conn.execute(
        "PRAGMA journal_mode = WAL;"
    )

    conn.execute(
        "PRAGMA busy_timeout = 5000;"
    )

    return conn


def validate_schema(
    conn: sqlite3.Connection,
) -> None:

    row = conn.execute(
        """
        SELECT value
        FROM metadata
        WHERE key = 'schema_version'
        """
    ).fetchone()

    if row is None:
        raise RuntimeError(
            "metadata.schema_version missing."
        )

    if row["value"] != "2.0":
        raise RuntimeError(
            f"Expected schema 2.0, "
            f"got {row['value']}"
        )


# ============================================================
# RELEVANT TRADING UNIVERSE
# ============================================================

def get_relevant_securities(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT DISTINCT
            s.id,
            s.name,
            s.symbol,
            s.isin,
            s.wkn,
            s.exchange,
            s.currency,
            s.country,
            s.asset_type,

            CASE
                WHEN p.shares > 0
                THEN 1
                ELSE 0
            END AS in_portfolio,

            CASE
                WHEN w.security_id IS NOT NULL
                THEN 1
                ELSE 0
            END AS in_watchlist

        FROM security s

        LEFT JOIN positions p
            ON p.security_id = s.id

        LEFT JOIN watchlist w
            ON w.security_id = s.id

        WHERE s.active = 1
          AND (
                p.shares > 0
                OR w.security_id IS NOT NULL
          )

        ORDER BY
            in_portfolio DESC,
            s.name
        """
    ).fetchall()


# ============================================================
# CANONICAL LOOKUP
# ============================================================

def find_canonical(
    name: str,
) -> dict | None:

    # Exact first
    if name in CANONICAL:
        return CANONICAL[name]

    lower = name.lower().strip()

    for canonical_name, data in CANONICAL.items():

        if (
            canonical_name.lower().strip()
            == lower
        ):
            return data

    return None


# ============================================================
# UPDATE
# ============================================================

def apply_canonical(
    conn: sqlite3.Connection,
    security_id: int,
    data: dict,
) -> None:

    conn.execute(
        """
        UPDATE security

        SET
            symbol = COALESCE(?, symbol),
            isin = COALESCE(?, isin),
            wkn = COALESCE(?, wkn),
            exchange = COALESCE(?, exchange),
            currency = COALESCE(?, currency),
            country = COALESCE(?, country),
            asset_type = COALESCE(
                ?,
                asset_type
            ),
            updated_at = CURRENT_TIMESTAMP

        WHERE id = ?
        """,
        (
            data.get("symbol"),
            data.get("isin"),
            data.get("wkn"),
            data.get("exchange"),
            data.get("currency"),
            data.get("country"),
            data.get("asset_type"),
            security_id,
        ),
    )


# ============================================================
# IMPORTANT:
#
# Für kanonische Felder überschreiben wir symbol/exchange etc.
# bewusst, falls ein vorheriger OpenFIGI-Lauf ein alternatives
# Listing eingetragen hat.
# ============================================================

def force_canonical(
    conn: sqlite3.Connection,
    security_id: int,
    data: dict,
) -> None:

    current = conn.execute(
        """
        SELECT *
        FROM security
        WHERE id = ?
        """,
        (security_id,),
    ).fetchone()

    if current is None:
        return

    symbol = (
        data["symbol"]
        if data.get("symbol") is not None
        else current["symbol"]
    )

    isin = (
        data["isin"]
        if data.get("isin") is not None
        else current["isin"]
    )

    wkn = (
        data["wkn"]
        if data.get("wkn") is not None
        else current["wkn"]
    )

    exchange = (
        data["exchange"]
        if data.get("exchange") is not None
        else current["exchange"]
    )

    currency = (
        data["currency"]
        if data.get("currency") is not None
        else current["currency"]
    )

    country = (
        data["country"]
        if data.get("country") is not None
        else current["country"]
    )

    asset_type = (
        data["asset_type"]
        if data.get("asset_type") is not None
        else current["asset_type"]
    )

    conn.execute(
        """
        UPDATE security
        SET
            symbol = ?,
            isin = ?,
            wkn = ?,
            exchange = ?,
            currency = ?,
            country = ?,
            asset_type = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            symbol,
            isin,
            wkn,
            exchange,
            currency,
            country,
            asset_type,
            security_id,
        ),
    )


# ============================================================
# VALIDATION
# ============================================================

def get_incomplete_relevant(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT DISTINCT
            s.id,
            s.name,
            s.symbol,
            s.isin,
            s.wkn,
            s.exchange,
            s.currency,
            s.country,
            s.asset_type,

            CASE
                WHEN p.shares > 0
                THEN 1
                ELSE 0
            END AS in_portfolio,

            CASE
                WHEN w.security_id IS NOT NULL
                THEN 1
                ELSE 0
            END AS in_watchlist

        FROM security s

        LEFT JOIN positions p
            ON p.security_id = s.id

        LEFT JOIN watchlist w
            ON w.security_id = s.id

        WHERE s.active = 1
          AND (
                p.shares > 0
                OR w.security_id IS NOT NULL
          )
          AND (
                s.symbol IS NULL
                OR TRIM(s.symbol) = ''
                OR s.exchange IS NULL
                OR TRIM(s.exchange) = ''
          )

        ORDER BY
            in_portfolio DESC,
            s.name
        """
    ).fetchall()


# ============================================================
# REPORT
# ============================================================

def print_universe(
    conn: sqlite3.Connection,
) -> None:

    rows = get_relevant_securities(
        conn
    )

    print()
    print(
        "==============================================="
    )
    print(
        " Current Trading Universe"
    )
    print(
        "==============================================="
    )

    print(
        f"Relevant securities: {len(rows)}"
    )

    print()

    for row in rows:

        flags = []

        if row["in_portfolio"]:
            flags.append("PORT")

        if row["in_watchlist"]:
            flags.append("WATCH")

        marker = "+".join(flags)

        print(
            f"{row['id']:>3} | "
            f"{marker:<10} | "
            f"{row['name'][:36]:<36} | "
            f"{(row['symbol'] or '-'):>8} | "
            f"{(row['exchange'] or '-'):>18} | "
            f"{(row['currency'] or '-'):>3} | "
            f"{(row['country'] or '-'):>2}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    conn = connect()

    try:

        validate_schema(
            conn
        )

        relevant = get_relevant_securities(
            conn
        )

        print()
        print(
            "==============================================="
        )
        print(
            " Trading Universe Canonicalizer"
        )
        print(
            "==============================================="
        )

        print(
            f"Relevant securities: {len(relevant)}"
        )

        print()

        updated = 0
        untouched = 0

        conn.execute(
            "BEGIN"
        )

        for row in relevant:

            canonical = find_canonical(
                row["name"]
            )

            if canonical is None:

                untouched += 1

                print(
                    f"KEEP    id={row['id']:<3} "
                    f"{row['name']}"
                )

                continue

            force_canonical(
                conn,
                row["id"],
                canonical,
            )

            updated += 1

            print(
                f"UPDATE  id={row['id']:<3} "
                f"{row['name']:<42} "
                f"-> "
                f"{canonical.get('symbol', '-')}"
                f" / "
                f"{canonical.get('exchange', '-')}"
            )

        conn.commit()

        print()
        print(
            "==============================================="
        )
        print(
            " Canonicalization Summary"
        )
        print(
            "==============================================="
        )

        print(
            f"Updated   : {updated}"
        )

        print(
            f"Untouched : {untouched}"
        )

        # ----------------------------------------------------
        # Relevant incomplete securities
        # ----------------------------------------------------

        incomplete = get_incomplete_relevant(
            conn
        )

        print(
            f"Incomplete : {len(incomplete)}"
        )

        if incomplete:

            print()
            print(
                "Still incomplete:"
            )
            print()

            for row in incomplete:

                marker = []

                if row["in_portfolio"]:
                    marker.append("PORT")

                if row["in_watchlist"]:
                    marker.append("WATCH")

                print(
                    f"{row['id']:>3} | "
                    f"{'+'.join(marker):<10} | "
                    f"{row['name']:<42} | "
                    f"symbol={row['symbol'] or '-':<10} | "
                    f"exchange={row['exchange'] or '-'}"
                )

        print_universe(
            conn
        )

        print()
        print(
            "==============================================="
        )

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


if __name__ == "__main__":
    main()