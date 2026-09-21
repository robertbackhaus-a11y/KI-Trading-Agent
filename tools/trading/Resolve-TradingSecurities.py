from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")

OPENFIGI_BASE = "https://api.openfigi.com/v3"

# Optional
OPENFIGI_API_KEY = ""

TIMEOUT_SECONDS = 10
MAPPING_BATCH_SIZE = 5
MAX_RETRIES = 3


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
        timeout=5.0,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")

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
        raise RuntimeError(
            "metadata.schema_version not found."
        )

    if row["value"] != "2.0":
        raise RuntimeError(
            f"Expected schema 2.0, got {row['value']}"
        )


# ============================================================
# HTTP
# ============================================================

def openfigi_headers() -> dict:
    result = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "KI-Stack-Trading-Resolver/3.0",
    }

    if OPENFIGI_API_KEY:
        result["X-OPENFIGI-APIKEY"] = OPENFIGI_API_KEY

    return result


def post_json(
    endpoint: str,
    payload,
) -> dict:

    url = (
        f"{OPENFIGI_BASE}/"
        f"{endpoint.lstrip('/')}"
    )

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        request = urllib.request.Request(
            url=url,
            data=body,
            headers=openfigi_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=TIMEOUT_SECONDS,
            ) as response:

                raw = response.read().decode(
                    "utf-8",
                    errors="replace",
                )

                return {
                    "ok": True,
                    "status": response.status,
                    "headers": dict(
                        response.headers
                    ),
                    "data": json.loads(raw),
                }

        except urllib.error.HTTPError as exc:

            raw = exc.read().decode(
                "utf-8",
                errors="replace",
            )

            if exc.code == 429:

                reset = exc.headers.get(
                    "ratelimit-reset"
                )

                try:
                    wait_seconds = float(
                        reset
                    )
                except Exception:
                    wait_seconds = 5.0

                wait_seconds = max(
                    1.0,
                    min(
                        wait_seconds + 0.5,
                        65.0,
                    ),
                )

                print(
                    f"    rate limited - "
                    f"waiting {wait_seconds:.1f}s"
                )

                if attempt < MAX_RETRIES:
                    time.sleep(
                        wait_seconds
                    )
                    continue

            return {
                "ok": False,
                "status": exc.code,
                "message": raw[:500],
            }

        except Exception as exc:

            if attempt < MAX_RETRIES:
                time.sleep(
                    attempt * 2
                )
                continue

            return {
                "ok": False,
                "status": None,
                "message": str(exc),
            }

    return {
        "ok": False,
        "status": None,
        "message": "Retries exhausted.",
    }


# ============================================================
# HELPERS
# ============================================================

def normalize(
    value: Optional[str],
) -> str:

    if value is None:
        return ""

    return str(value).strip().upper()


def asset_type_from_candidate(
    candidate: dict,
) -> str:

    text = " ".join(
        [
            candidate.get(
                "securityType"
            ) or "",
            candidate.get(
                "securityType2"
            ) or "",
        ]
    ).lower()

    if "etf" in text:
        return "etf"

    if "fund" in text:
        return "etf"

    if "depositary" in text:
        return "stock"

    if "adr" in text:
        return "stock"

    if "common stock" in text:
        return "stock"

    if "equity" in text:
        return "stock"

    return "security"


# ============================================================
# CLEANUP WATCHLIST DUPLICATES
# ============================================================

def merge_watchlist_duplicate(
    conn: sqlite3.Connection,
    duplicate_name: str,
    target_name: str,
) -> bool:

    duplicate = conn.execute(
        """
        SELECT id
        FROM security
        WHERE LOWER(name) = LOWER(?)
        LIMIT 1
        """,
        (duplicate_name,),
    ).fetchone()

    target = conn.execute(
        """
        SELECT id
        FROM security
        WHERE LOWER(name) = LOWER(?)
        LIMIT 1
        """,
        (target_name,),
    ).fetchone()

    if (
        duplicate is None
        or target is None
        or duplicate["id"] == target["id"]
    ):
        return False

    duplicate_id = duplicate["id"]
    target_id = target["id"]

    watch = conn.execute(
        """
        SELECT *
        FROM watchlist
        WHERE security_id = ?
        """,
        (duplicate_id,),
    ).fetchone()

    if watch is not None:

        existing_target = conn.execute(
            """
            SELECT security_id
            FROM watchlist
            WHERE security_id = ?
            """,
            (target_id,),
        ).fetchone()

        if existing_target is None:

            conn.execute(
                """
                UPDATE watchlist
                SET security_id = ?
                WHERE security_id = ?
                """,
                (
                    target_id,
                    duplicate_id,
                ),
            )

        else:

            conn.execute(
                """
                DELETE FROM watchlist
                WHERE security_id = ?
                """,
                (duplicate_id,),
            )

    # Stub security only delete if nothing else references it.
    references = 0

    for table in [
        "transactions",
        "positions",
        "market_snapshot",
        "market_data",
        "fundamentals",
        "estimates",
        "ratings",
        "price_targets",
        "events",
        "news",
        "decisions",
        "analysis_history",
    ]:

        count = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {table}
            WHERE security_id = ?
            """,
            (duplicate_id,),
        ).fetchone()[0]

        references += count

    if references == 0:

        conn.execute(
            """
            DELETE FROM security
            WHERE id = ?
            """,
            (duplicate_id,),
        )

    return True


# ============================================================
# LOAD ISIN SECURITIES
# ============================================================

def get_isin_securities(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT
            id,
            symbol,
            isin,
            wkn,
            name,
            exchange,
            currency,
            country,
            asset_type
        FROM security
        WHERE active = 1
          AND isin IS NOT NULL
          AND TRIM(isin) <> ''
        ORDER BY name
        """
    ).fetchall()


# ============================================================
# OPENFIGI MAPPING
# ============================================================

def build_mapping_job(
    row: sqlite3.Row,
) -> dict:

    job = {
        "idType": "ID_ISIN",
        "idValue": row["isin"],
        "marketSecDes": "Equity",
    }

    currency = normalize(
        row["currency"]
    )

    if currency:
        job["currency"] = currency

    return job


def map_isins(
    rows: list[sqlite3.Row],
) -> dict[int, list[dict]]:

    results: dict[
        int,
        list[dict]
    ] = {}

    for start in range(
        0,
        len(rows),
        MAPPING_BATCH_SIZE,
    ):

        batch = rows[
            start:
            start + MAPPING_BATCH_SIZE
        ]

        jobs = [
            build_mapping_job(row)
            for row in batch
        ]

        print(
            f"Mapping "
            f"{start + 1}-"
            f"{start + len(batch)} "
            f"of {len(rows)}"
        )

        response = post_json(
            "mapping",
            jobs,
        )

        if not response.get("ok"):

            print(
                f"  request failed: "
                f"{response.get('status')} "
                f"{response.get('message')}"
            )

            for row in batch:
                results[
                    row["id"]
                ] = []

            continue

        data = response.get(
            "data"
        )

        if not isinstance(
            data,
            list,
        ):

            for row in batch:
                results[
                    row["id"]
                ] = []

            continue

        for index, row in enumerate(
            batch
        ):

            if index >= len(data):
                results[
                    row["id"]
                ] = []
                continue

            item = data[index]

            if not isinstance(
                item,
                dict,
            ):
                results[
                    row["id"]
                ] = []
                continue

            candidates = item.get(
                "data"
            )

            if not isinstance(
                candidates,
                list,
            ):
                candidates = []

            results[
                row["id"]
            ] = candidates

        if not OPENFIGI_API_KEY:
            time.sleep(0.5)

    return results


# ============================================================
# CANDIDATE SELECTION
# ============================================================

def choose_candidate(
    row: sqlite3.Row,
    candidates: list[dict],
) -> tuple[
    Optional[dict],
    str,
]:

    if not candidates:
        return None, "no_match"

    # Equities only
    equities = [
        c
        for c in candidates
        if normalize(
            c.get("marketSector")
        ) == "EQUITY"
    ]

    if equities:
        candidates = equities

    # --------------------------------------------------------
    # Existing symbol wins if OpenFIGI confirms it.
    # --------------------------------------------------------

    current_symbol = normalize(
        row["symbol"]
    )

    if current_symbol:

        symbol_matches = [
            c
            for c in candidates
            if normalize(
                c.get("ticker")
            ) == current_symbol
        ]

        if len(symbol_matches) == 1:
            return (
                symbol_matches[0],
                "existing_symbol",
            )

    # --------------------------------------------------------
    # Same ticker on all results -> listing differences only.
    # In that case symbol is safe.
    # --------------------------------------------------------

    tickers = {
        normalize(
            c.get("ticker")
        )
        for c in candidates
        if c.get("ticker")
    }

    if len(tickers) == 1:

        # Prefer composite entry if available.
        composites = [
            c
            for c in candidates
            if (
                c.get("figi")
                and c.get("compositeFIGI")
                and
                c.get("figi")
                == c.get("compositeFIGI")
            )
        ]

        if len(composites) == 1:
            return (
                composites[0],
                "single_ticker_composite",
            )

        return (
            candidates[0],
            "single_ticker",
        )

    # --------------------------------------------------------
    # US securities:
    # OpenFIGI US composite is reliable for the trading symbol.
    # --------------------------------------------------------

    isin = normalize(
        row["isin"]
    )

    if isin.startswith("US"):

        us = [
            c
            for c in candidates
            if normalize(
                c.get("exchCode")
            ) == "US"
        ]

        if len(us) == 1:
            return (
                us[0],
                "us_composite",
            )

    # --------------------------------------------------------
    # Multiple materially different tickers:
    # do NOT guess.
    # --------------------------------------------------------

    return None, "ambiguous"


# ============================================================
# UPDATE
# ============================================================

def update_security(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    candidate: dict,
) -> None:

    symbol = candidate.get(
        "ticker"
    )

    exchange = candidate.get(
        "exchCode"
    )

    asset_type = (
        asset_type_from_candidate(
            candidate
        )
    )

    conn.execute(
        """
        UPDATE security
        SET
            symbol = ?,
            exchange = ?,
            asset_type = CASE
                WHEN asset_type IS NULL
                  OR asset_type = ''
                  OR asset_type = 'security'
                THEN ?
                ELSE asset_type
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            symbol,
            exchange,
            asset_type,
            row["id"],
        ),
    )


# ============================================================
# REPORT
# ============================================================

def print_candidates(
    candidates: list[dict],
) -> None:

    for candidate in candidates[:10]:

        print(
            "       "
            f"{candidate.get('ticker') or '-':<12} "
            f"{candidate.get('exchCode') or '-':<6} "
            f"{candidate.get('securityType2') or '-':<20} "
            f"{candidate.get('figi') or '-'}"
        )


def print_pending_name_only(
    conn: sqlite3.Connection,
) -> None:

    rows = conn.execute(
        """
        SELECT
            id,
            name
        FROM security
        WHERE active = 1
          AND (
                isin IS NULL
             OR TRIM(isin) = ''
          )
          AND (
                symbol IS NULL
             OR exchange IS NULL
          )
        ORDER BY name
        """
    ).fetchall()

    print()
    print(
        f"Name-only pending: {len(rows)}"
    )

    for row in rows:
        print(
            f"  {row['id']:>3} | "
            f"{row['name']}"
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

        print()
        print(
            "==============================================="
        )
        print(
            " Trading Security Resolver v3"
        )
        print(
            "==============================================="
        )

        # ----------------------------------------------------
        # Cleanup duplicate created by initial watchlist seed
        # ----------------------------------------------------

        conn.execute("BEGIN")

        merged_ing = (
            merge_watchlist_duplicate(
                conn,
                "ING",
                "ING Group",
            )
        )

        conn.commit()

        if merged_ing:
            print(
                "Merged watchlist duplicate: "
                "ING -> ING Group"
            )

        rows = get_isin_securities(
            conn
        )

        print(
            f"ISIN securities: {len(rows)}"
        )
        print()

        mapping = map_isins(
            rows
        )

        resolved = 0
        ambiguous = 0
        no_match = 0

        conn.execute("BEGIN")

        print()
        print(
            "Resolution results:"
        )
        print()

        for row in rows:

            candidates = mapping.get(
                row["id"],
                [],
            )

            candidate, reason = (
                choose_candidate(
                    row,
                    candidates,
                )
            )

            print(
                f"id={row['id']:>3} | "
                f"{row['name']:<45} "
                f"{row['currency'] or '-':>4}"
            )

            if candidate is None:

                if reason == "ambiguous":

                    ambiguous += 1

                    print(
                        "    -> AMBIGUOUS"
                    )

                    print_candidates(
                        candidates
                    )

                else:

                    no_match += 1

                    print(
                        "    -> NO MATCH"
                    )

                continue

            update_security(
                conn,
                row,
                candidate,
            )

            resolved += 1

            print(
                f"    -> {candidate.get('ticker')} "
                f"/ {candidate.get('exchCode')} "
                f"[{reason}]"
            )

        conn.commit()

        print()
        print(
            "==============================================="
        )
        print(
            " Resolver Summary"
        )
        print(
            "==============================================="
        )
        print(
            f"Resolved  : {resolved}"
        )
        print(
            f"Ambiguous : {ambiguous}"
        )
        print(
            f"No match  : {no_match}"
        )

        print_pending_name_only(
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