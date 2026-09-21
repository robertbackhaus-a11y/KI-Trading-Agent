from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ============================================================
# CONFIG
# ============================================================

DB_PATH = Path(
    r"C:\KI-Stack\data\trading\trading.db"
)

YAHOO_BASE = (
    "https://query1.finance.yahoo.com"
)

HISTORY_RANGE = "2y"
HISTORY_INTERVAL = "1d"

# Incremental reload window: when a security already has market_data,
# only the last HISTORY_OVERLAP_DAYS calendar days are re-requested
# (plus whatever is newer), instead of the full HISTORY_RANGE. The
# overlap absorbs Yahoo's occasional after-the-fact corrections to
# the most recent sessions and weekends/holidays.
HISTORY_OVERLAP_DAYS = 5

TIMEOUT_SECONDS = 10
MAX_RETRIES = 2

REQUEST_DELAY_SECONDS = 0.75


# ============================================================
# DATABASE
# ============================================================

def connect() -> sqlite3.Connection:

    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Trading DB not found: {DB_PATH}"
        )

    # IMPORTANT:
    # isolation_level=None enables SQLite autocommit.
    # Explicit BEGIN/COMMIT is then fully under our control.
    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=5.0,
        isolation_level=None,
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


def ensure_source_symbol_table(
    conn: sqlite3.Connection,
) -> None:

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_symbols (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            security_id INTEGER NOT NULL,

            source_id INTEGER NOT NULL,

            symbol TEXT NOT NULL,

            exchange TEXT,
            currency TEXT,

            verified_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (security_id)
                REFERENCES security(id)
                ON DELETE CASCADE,

            FOREIGN KEY (source_id)
                REFERENCES data_sources(id)
                ON DELETE CASCADE,

            UNIQUE (
                security_id,
                source_id
            )
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_source_symbols_symbol
        ON source_symbols(
            source_id,
            symbol
        )
        """
    )


def get_yahoo_source_id(
    conn: sqlite3.Connection,
) -> int:

    row = conn.execute(
        """
        SELECT id
        FROM data_sources
        WHERE name = 'Yahoo Finance'
        LIMIT 1
        """
    ).fetchone()

    if row is not None:
        return row["id"]

    cursor = conn.execute(
        """
        INSERT INTO data_sources (
            name,
            source_type,
            url,
            active
        )
        VALUES (
            'Yahoo Finance',
            'web',
            'https://finance.yahoo.com',
            1
        )
        """
    )

    return cursor.lastrowid


# ============================================================
# HTTP
# ============================================================

def yahoo_headers() -> dict:

    return {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        ),
    }


def get_json(
    url: str,
) -> Optional[dict]:

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        request = urllib.request.Request(
            url=url,
            headers=yahoo_headers(),
            method="GET",
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

                return json.loads(
                    raw
                )

        except urllib.error.HTTPError as exc:

            if (
                exc.code == 429
                and attempt < MAX_RETRIES
            ):

                print(
                    "      HTTP 429 - retry"
                )

                time.sleep(3)

                continue

            print(
                f"      HTTP {exc.code}"
            )

            return None

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as exc:

            if attempt < MAX_RETRIES:

                time.sleep(1)

                continue

            print(
                f"      Network error: {exc}"
            )

            return None

        except Exception as exc:

            print(
                f"      Error: {exc}"
            )

            return None

    return None


# ============================================================
# HELPERS
# ============================================================

def normalize(
    value: Optional[str],
) -> str:

    if value is None:
        return ""

    return str(
        value
    ).strip().upper()


def normalize_name(
    value: Optional[str],
) -> str:

    if value is None:
        return ""

    result = (
        str(value)
        .upper()
        .replace("&", "AND")
        .replace(".", "")
        .replace(",", "")
        .replace("-", " ")
        .replace("'", "")
        .replace('"', "")
    )

    return " ".join(
        result.split()
    )


def utc_now() -> str:

    return datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# RELEVANT UNIVERSE
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
                OR
                w.security_id IS NOT NULL
          )

        ORDER BY
            in_portfolio DESC,
            s.name
        """
    ).fetchall()


# ============================================================
# YAHOO SYMBOL CANDIDATES
# ============================================================

def direct_candidates(
    row: sqlite3.Row,
) -> list[str]:

    symbol = (
        row["symbol"]
        or ""
    ).strip()

    exchange = normalize(
        row["exchange"]
    )

    if not symbol:
        return []

    candidates = []

    # US
    if exchange in {
        "US",
        "NASDAQ",
        "NYSE",
        "NYSEARCA",
    }:

        candidates.append(
            symbol
        )

    # Germany / Xetra
    elif exchange in {
        "XETRA",
        "GR",
    }:

        candidates.append(
            f"{symbol}.DE"
        )

        candidates.append(
            symbol
        )

    # Frankfurt
    elif exchange in {
        "FRANKFURT",
        "GF",
    }:

        candidates.append(
            f"{symbol}.F"
        )

        candidates.append(
            f"{symbol}.DE"
        )

    # London
    elif exchange == "LSE":

        base = symbol.rstrip(".")

        candidates.append(
            f"{base}.L"
        )

    # Amsterdam
    elif exchange in {
        "EURONEXT_AMSTERDAM",
        "NA",
    }:

        candidates.append(
            f"{symbol}.AS"
        )

    # Paris
    elif exchange in {
        "EURONEXT_PARIS",
        "FP",
    }:

        candidates.append(
            f"{symbol}.PA"
        )

    # Vienna
    elif exchange == "VIENNA":

        candidates.append(
            f"{symbol}.VI"
        )

    # SIX
    elif exchange in {
        "SW",
        "SIX",
    }:

        candidates.append(
            f"{symbol}.SW"
        )

    # Korea
    elif exchange in {
        "KSE",
        "KRX",
        "KOREA",
    }:

        candidates.append(
            f"{symbol}.KS"
        )

    # Tokyo
    elif exchange in {
        "JP",
        "TOKYO",
    }:

        candidates.append(
            f"{symbol}.T"
        )

    # Canada Venture
    elif exchange in {
        "CN",
        "CV",
    }:

        candidates.append(
            f"{symbol}.V"
        )

    # Fallback
    else:

        candidates.append(
            symbol
        )

    result = []

    for candidate in candidates:

        if candidate not in result:
            result.append(
                candidate
            )

    return result


# ============================================================
# YAHOO CHART
# ============================================================

def yahoo_chart(
    symbol: str,
    range_value: str = "5d",
    period1: Optional[int] = None,
) -> Optional[dict]:
    """If period1 (unix timestamp) is given, an explicit period1..now
    window is requested instead of a relative range -- this is what
    makes the incremental (few-days) reload possible without asking
    Yahoo for the full HISTORY_RANGE every time."""

    encoded_symbol = urllib.parse.quote(
        symbol,
        safe=".-^=",
    )

    params = {
        "interval": HISTORY_INTERVAL,
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }

    if period1 is not None:
        params["period1"] = int(period1)
        params["period2"] = int(time.time())
    else:
        params["range"] = range_value

    query = urllib.parse.urlencode(
        params
    )

    url = (
        f"{YAHOO_BASE}/v8/finance/chart/"
        f"{encoded_symbol}?{query}"
    )

    data = get_json(
        url
    )

    if data is None:
        return None

    chart = data.get(
        "chart"
    )

    if not isinstance(
        chart,
        dict,
    ):
        return None

    if chart.get(
        "error"
    ):
        return None

    results = chart.get(
        "result"
    )

    if not results:
        return None

    return results[0]


# ============================================================
# YAHOO SEARCH
# ============================================================

def yahoo_search(
    query: str,
) -> list[dict]:

    params = urllib.parse.urlencode(
        {
            "q": query,
            "quotesCount": 20,
            "newsCount": 0,
            "enableFuzzyQuery": "false",
        }
    )

    url = (
        f"{YAHOO_BASE}/"
        f"v1/finance/search?"
        f"{params}"
    )

    data = get_json(
        url
    )

    if not data:
        return []

    quotes = data.get(
        "quotes"
    )

    if not isinstance(
        quotes,
        list,
    ):
        return []

    return quotes


# ============================================================
# SEARCH SCORING
# ============================================================

def score_search_result(
    row: sqlite3.Row,
    candidate: dict,
) -> int:

    score = 0

    expected_name = normalize_name(
        row["name"]
    )

    expected_symbol = normalize(
        row["symbol"]
    )

    expected_currency = normalize(
        row["currency"]
    )

    yahoo_symbol = normalize(
        candidate.get("symbol")
    )

    yahoo_name = normalize_name(
        candidate.get("longname")
        or
        candidate.get("shortname")
    )

    quote_type = normalize(
        candidate.get("quoteType")
    )

    exchange = normalize(
        candidate.get("exchange")
    )

    if (
        expected_name
        and yahoo_name
    ):

        if (
            expected_name
            == yahoo_name
        ):

            score += 100

        elif (
            expected_name
            in yahoo_name
            or
            yahoo_name in expected_name
        ):

            score += 70

    if expected_symbol:

        if yahoo_symbol == expected_symbol:

            score += 80

        elif yahoo_symbol.startswith(
            expected_symbol + "."
        ):

            score += 60

    asset_type = normalize(
        row["asset_type"]
    )

    if (
        asset_type == "ETF"
        and quote_type == "ETF"
    ):

        score += 30

    if (
        asset_type == "STOCK"
        and quote_type in {
            "EQUITY",
            "STOCK",
        }
    ):

        score += 30

    candidate_currency = normalize(
        candidate.get("currency")
    )

    if (
        expected_currency
        and candidate_currency
        and expected_currency
        == candidate_currency
    ):

        score += 15

    canonical_exchange = normalize(
        row["exchange"]
    )

    if canonical_exchange in {
        "NASDAQ",
        "NYSE",
        "US",
    } and exchange in {
        "NMS",
        "NYQ",
        "NGM",
        "NCM",
        "PCX",
    }:

        score += 15

    return score


def choose_search_result(
    row: sqlite3.Row,
    candidates: list[dict],
) -> Optional[dict]:

    if not candidates:
        return None

    scored = []

    for candidate in candidates:

        symbol = candidate.get(
            "symbol"
        )

        if not symbol:
            continue

        score = score_search_result(
            row,
            candidate,
        )

        scored.append(
            (
                score,
                candidate,
            )
        )

    if not scored:
        return None

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    best_score = scored[0][0]

    if best_score < 50:
        return None

    if len(scored) > 1:

        second_score = scored[1][0]

        if (
            second_score
            >= best_score - 5
        ):

            return None

    return scored[0][1]


# ============================================================
# SOURCE SYMBOLS
# ============================================================

def get_existing_source_symbol(
    conn: sqlite3.Connection,
    security_id: int,
    source_id: int,
) -> Optional[str]:

    row = conn.execute(
        """
        SELECT symbol
        FROM source_symbols
        WHERE security_id = ?
          AND source_id = ?
        """,
        (
            security_id,
            source_id,
        ),
    ).fetchone()

    if row is None:
        return None

    return row["symbol"]


def save_source_symbol(
    conn: sqlite3.Connection,
    security_id: int,
    source_id: int,
    symbol: str,
    exchange: Optional[str],
    currency: Optional[str],
) -> None:

    conn.execute(
        """
        INSERT INTO source_symbols (
            security_id,
            source_id,
            symbol,
            exchange,
            currency,
            verified_at
        )

        VALUES (?, ?, ?, ?, ?, ?)

        ON CONFLICT (
            security_id,
            source_id
        )

        DO UPDATE SET
            symbol = excluded.symbol,
            exchange = excluded.exchange,
            currency = excluded.currency,
            verified_at = excluded.verified_at
        """,
        (
            security_id,
            source_id,
            symbol,
            exchange,
            currency,
            utc_now(),
        ),
    )


# ============================================================
# RESOLVE YAHOO SYMBOL
# ============================================================

def resolve_yahoo_symbol(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    source_id: int,
    timing: dict,
) -> tuple[Optional[str], bool]:
    """Returns (symbol, reused). reused is True when an already
    verified Yahoo symbol from source_symbols was used as-is, with
    no HTTP request at all -- candidate/search discovery only runs
    when no symbol is on file yet."""

    existing = get_existing_source_symbol(
        conn,
        row["id"],
        source_id,
    )

    if existing:

        print(
            f"      existing {existing}"
        )

        return existing, True

    # --------------------------------------------------------
    # Direct candidates
    # --------------------------------------------------------

    for candidate_symbol in direct_candidates(
        row
    ):

        print(
            f"      test {candidate_symbol}"
        )

        t0 = time.perf_counter()

        chart = yahoo_chart(
            candidate_symbol,
            "5d",
        )

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

        timing["http"] += (
            time.perf_counter() - t0
        )

        if chart is None:
            continue

        meta = (
            chart.get("meta")
            or {}
        )

        resolved_symbol = (
            meta.get("symbol")
            or candidate_symbol
        )

        save_source_symbol(
            conn,
            row["id"],
            source_id,
            resolved_symbol,
            meta.get(
                "exchangeName"
            ),
            meta.get(
                "currency"
            ),
        )

        return resolved_symbol, False

    # --------------------------------------------------------
    # Search fallback
    # --------------------------------------------------------

    search_queries = []

    if row["isin"]:
        search_queries.append(
            row["isin"]
        )

    if row["name"]:
        search_queries.append(
            row["name"]
        )

    for query in search_queries:

        print(
            f"      search {query}"
        )

        t0 = time.perf_counter()

        results = yahoo_search(
            query
        )

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

        timing["http"] += (
            time.perf_counter() - t0
        )

        candidate = choose_search_result(
            row,
            results,
        )

        if candidate is None:
            continue

        candidate_symbol = (
            candidate.get("symbol")
        )

        if not candidate_symbol:
            continue

        t0 = time.perf_counter()

        chart = yahoo_chart(
            candidate_symbol,
            "5d",
        )

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

        timing["http"] += (
            time.perf_counter() - t0
        )

        if chart is None:
            continue

        meta = (
            chart.get("meta")
            or {}
        )

        save_source_symbol(
            conn,
            row["id"],
            source_id,
            candidate_symbol,
            meta.get(
                "exchangeName"
            ),
            meta.get(
                "currency"
            ),
        )

        return candidate_symbol, False

    return None, False


# ============================================================
# HISTORY PARSING
# ============================================================

def safe_get(
    values: list,
    index: int,
):

    if index >= len(values):
        return None

    return values[index]


def parse_history(
    chart: dict,
) -> list[dict]:

    timestamps = (
        chart.get("timestamp")
        or []
    )

    indicators = (
        chart.get("indicators")
        or {}
    )

    quote_list = (
        indicators.get("quote")
        or []
    )

    if not quote_list:
        return []

    quote = quote_list[0]

    opens = (
        quote.get("open")
        or []
    )

    highs = (
        quote.get("high")
        or []
    )

    lows = (
        quote.get("low")
        or []
    )

    closes = (
        quote.get("close")
        or []
    )

    volumes = (
        quote.get("volume")
        or []
    )

    adjclose = []

    adjclose_list = (
        indicators.get("adjclose")
        or []
    )

    if adjclose_list:

        adjclose = (
            adjclose_list[0].get(
                "adjclose"
            )
            or []
        )

    rows = []

    for index, timestamp in enumerate(
        timestamps
    ):

        close = safe_get(
            closes,
            index,
        )

        if close is None:
            continue

        trade_date = datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        ).date().isoformat()

        rows.append(
            {
                "trade_date": trade_date,

                "open": safe_get(
                    opens,
                    index,
                ),

                "high": safe_get(
                    highs,
                    index,
                ),

                "low": safe_get(
                    lows,
                    index,
                ),

                "close": close,

                "adjusted_close": safe_get(
                    adjclose,
                    index,
                ),

                "volume": safe_get(
                    volumes,
                    index,
                ),
            }
        )

    return rows


# ============================================================
# STORE MARKET HISTORY
# ============================================================

def store_market_history(
    conn: sqlite3.Connection,
    security_id: int,
    source_id: int,
    currency: Optional[str],
    rows: list[dict],
) -> int:

    fetched_at = utc_now()

    conn.executemany(
        """
            INSERT INTO market_data (

                security_id,
                trade_date,

                open,
                high,
                low,
                close,
                adjusted_close,

                volume,

                currency,
                source_id,
                fetched_at
            )

            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT (
                security_id,
                trade_date,
                source_id
            )

            DO UPDATE SET

                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,

                adjusted_close =
                    excluded.adjusted_close,

                volume = excluded.volume,
                currency = excluded.currency,
                fetched_at = excluded.fetched_at
            """,
        [
            (
                security_id,
                row["trade_date"],

                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["adjusted_close"],

                row["volume"],

                currency,
                source_id,
                fetched_at,
            )
            for row in rows
        ],
    )

    return len(rows)


# ============================================================
# SNAPSHOT
# ============================================================

def store_snapshot(
    conn: sqlite3.Connection,
    security_id: int,
    source_id: int,
    chart: dict,
    history: list[dict],
) -> None:

    meta = (
        chart.get("meta")
        or {}
    )

    if history:
        latest = history[-1]
    else:
        latest = {}

    price = meta.get(
        "regularMarketPrice"
    )

    if price is None:
        price = latest.get(
            "close"
        )

    # previous_close must represent the immediately preceding
    # trading day from the same imported Daily history.
    #
    # Yahoo's chartPreviousClose can refer to the beginning of
    # the requested chart range and is therefore not reliable
    # for a 2-year history request.
    if len(history) >= 2:
        previous_close = history[-2].get(
            "close"
        )
    else:
        previous_close = meta.get(
            "previousClose"
        )

        if previous_close is None:
            previous_close = meta.get(
                "chartPreviousClose"
            )

    market_time = meta.get(
        "regularMarketTime"
    )

    if market_time:

        as_of = datetime.fromtimestamp(
            market_time,
            tz=timezone.utc,
        ).isoformat()

    elif latest.get(
        "trade_date"
    ):

        as_of = (
            latest["trade_date"]
            + "T00:00:00+00:00"
        )

    else:
        as_of = utc_now()

    conn.execute(
        """
        INSERT INTO market_snapshot (

            security_id,
            as_of_at,

            price,
            previous_close,

            open,
            high,
            low,

            volume,

            market_cap,
            shares_outstanding,

            currency,

            source_id,
            fetched_at
        )

        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?
        )

        ON CONFLICT (
            security_id
        )

        DO UPDATE SET

            as_of_at = excluded.as_of_at,

            price = excluded.price,

            previous_close =
                excluded.previous_close,

            open = excluded.open,
            high = excluded.high,
            low = excluded.low,

            volume = excluded.volume,

            currency = excluded.currency,

            source_id = excluded.source_id,
            fetched_at = excluded.fetched_at
        """,
        (
            security_id,
            as_of,

            price,
            previous_close,

            latest.get(
                "open"
            ),

            latest.get(
                "high"
            ),

            latest.get(
                "low"
            ),

            latest.get(
                "volume"
            ),

            meta.get(
                "currency"
            ),

            source_id,
            utc_now(),
        ),
    )


# ============================================================
# INCREMENTAL STATE
# ============================================================

def get_last_trade_date(
    conn: sqlite3.Connection,
    security_id: int,
    source_id: int,
) -> Optional[str]:

    row = conn.execute(
        """
        SELECT MAX(trade_date) AS latest
        FROM market_data
        WHERE security_id = ?
          AND source_id = ?
        """,
        (
            security_id,
            source_id,
        ),
    ).fetchone()

    if row is None:
        return None

    return row["latest"]


def today_utc_iso() -> str:

    return datetime.now(
        timezone.utc
    ).date().isoformat()


# ============================================================
# SINGLE SECURITY IMPORT
# ============================================================

def import_security(
    conn: sqlite3.Connection,
    security: sqlite3.Row,
    source_id: int,
    timing: dict,
) -> tuple[
    str,
    int,
]:
    """Returns (status, rows_written). status is one of
    "OK", "SKIP", "FAILED"."""

    symbol, symbol_reused = resolve_yahoo_symbol(
        conn,
        security,
        source_id,
        timing,
    )

    if symbol is None:

        print(
            "    -> SYMBOL UNRESOLVED"
        )

        return "FAILED", 0

    print(
        f"    -> Yahoo symbol: {symbol}"
    )

    last_date = get_last_trade_date(
        conn,
        security["id"],
        source_id,
    )

    # Already have today's (UTC) row -- nothing new can exist yet.
    if (
        last_date is not None
        and last_date >= today_utc_iso()
    ):

        print(
            "    -> SKIP - already current"
        )

        return "SKIP", 0

    # Incremental reload only when the resolved symbol is unchanged
    # and we already have history: fetch a small overlap window
    # instead of the full HISTORY_RANGE. A brand-new security, or
    # one whose provider symbol just changed, still gets a full
    # fetch (and the old rows for that security+source are purged
    # below, so a listing/symbol change can't leave stale rows).
    incremental = (
        symbol_reused
        and last_date is not None
    )

    t_http0 = time.perf_counter()

    if incremental:

        since = (
            datetime.fromisoformat(last_date)
            - timedelta(days=HISTORY_OVERLAP_DAYS)
        )

        period1 = int(
            since.replace(
                tzinfo=timezone.utc
            ).timestamp()
        )

        chart = yahoo_chart(
            symbol,
            period1=period1,
        )

    else:

        chart = yahoo_chart(
            symbol,
            HISTORY_RANGE,
        )

    time.sleep(
        REQUEST_DELAY_SECONDS
    )

    timing["http"] += (
        time.perf_counter() - t_http0
    )

    if chart is None:

        print(
            "    -> HISTORY FAILED"
        )

        return "FAILED", 0

    t_parse0 = time.perf_counter()

    history = parse_history(
        chart
    )

    timing["parse"] += (
        time.perf_counter() - t_parse0
    )

    if not history:

        print(
            "    -> HISTORY EMPTY"
        )

        return "FAILED", 0

    meta = (
        chart.get("meta")
        or {}
    )

    currency = meta.get(
        "currency"
    )

    t_db0 = time.perf_counter()

    # --------------------------------------------------------
    # One explicit transaction per security.
    # source_symbols has already been stored in autocommit mode.
    # --------------------------------------------------------

    conn.execute(
        "BEGIN IMMEDIATE"
    )

    try:

        if not incremental:

            # Full (re)fetch: keep Yahoo market history for this
            # security in sync with the currently resolved provider
            # instrument. This prevents stale rows from surviving
            # after a listing/provider-symbol change, e.g.
            # INGA.SW -> INGA.AS. Not needed on the incremental path,
            # where existing rows are simply upserted in place.
            conn.execute(
                """
                DELETE FROM market_data
                WHERE security_id = ?
                  AND source_id = ?
                """,
                (
                    security["id"],
                    source_id,
                ),
            )

        written = store_market_history(
            conn,
            security["id"],
            source_id,
            currency,
            history,
        )

        store_snapshot(
            conn,
            security["id"],
            source_id,
            chart,
            history,
        )

        conn.execute(
            "COMMIT"
        )

    except Exception:

        conn.execute(
            "ROLLBACK"
        )

        raise

    timing["db"] += (
        time.perf_counter() - t_db0
    )

    print(
        f"    -> rows: {written} "
        f"({'incremental' if incremental else 'full'})"
    )

    print(
        f"    -> "
        f"{history[0]['trade_date']} "
        f"to "
        f"{history[-1]['trade_date']}"
    )

    print(
        f"    -> currency: "
        f"{currency or '-'}"
    )

    return "OK", written


# ============================================================
# REPORT
# ============================================================

def report(
    conn: sqlite3.Connection,
    source_id: int,
) -> None:

    print()
    print(
        "==============================================="
    )
    print(
        " Market Data Status"
    )
    print(
        "==============================================="
    )

    row_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM market_data
        WHERE source_id = ?
        """,
        (source_id,),
    ).fetchone()[0]

    securities = conn.execute(
        """
        SELECT COUNT(
            DISTINCT security_id
        )
        FROM market_data
        WHERE source_id = ?
        """,
        (source_id,),
    ).fetchone()[0]

    snapshots = conn.execute(
        """
        SELECT COUNT(*)
        FROM market_snapshot
        WHERE source_id = ?
        """,
        (source_id,),
    ).fetchone()[0]

    mappings = conn.execute(
        """
        SELECT COUNT(*)
        FROM source_symbols
        WHERE source_id = ?
        """,
        (source_id,),
    ).fetchone()[0]

    print(
        f"Market rows        : {row_count}"
    )

    print(
        f"Market securities  : {securities}"
    )

    print(
        f"Snapshots          : {snapshots}"
    )

    print(
        f"Provider mappings  : {mappings}"
    )

    print()
    print(
        "Current snapshots:"
    )
    print()

    rows = conn.execute(
        """
        SELECT

            s.name,

            ss.symbol AS yahoo_symbol,

            ms.price,
            ms.previous_close,
            ms.currency,
            ms.as_of_at

        FROM market_snapshot ms

        JOIN security s
            ON s.id = ms.security_id

        LEFT JOIN source_symbols ss
            ON ss.security_id = s.id
           AND ss.source_id = ?

        WHERE ms.source_id = ?

        ORDER BY s.name
        """,
        (
            source_id,
            source_id,
        ),
    ).fetchall()

    for row in rows:

        price = (
            f"{row['price']:.4f}"
            if row["price"] is not None
            else "-"
        )

        previous_close = (
            f"{row['previous_close']:.4f}"
            if row["previous_close"] is not None
            else "-"
        )

        print(
            f"{row['name'][:34]:<34} | "
            f"{(row['yahoo_symbol'] or '-'):>12} | "
            f"{price:>12} | "
            f"prev={previous_close:>12} | "
            f"{row['currency'] or '-':>3}"
        )

    print()
    print(
        "==============================================="
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    t_total0 = time.perf_counter()

    timing = {
        "http": 0.0,
        "parse": 0.0,
        "db": 0.0,
    }

    conn = connect()

    try:

        validate_schema(
            conn
        )

        ensure_source_symbol_table(
            conn
        )

        source_id = get_yahoo_source_id(
            conn
        )

        securities = get_relevant_securities(
            conn
        )

        print()
        print(
            "==============================================="
        )
        print(
            " Trading Market Data Backfill v2"
        )
        print(
            "==============================================="
        )

        print(
            f"Relevant securities : {len(securities)}"
        )

        print(
            f"History              : {HISTORY_RANGE}"
        )

        print(
            f"Interval             : {HISTORY_INTERVAL}"
        )

        print()

        resolved = 0
        skipped = 0
        unresolved = 0
        total_rows = 0

        failures = []

        for index, security in enumerate(
            securities,
            start=1,
        ):

            print(
                f"[{index}/{len(securities)}] "
                f"{security['name']}"
            )

            try:

                status, written = import_security(
                    conn,
                    security,
                    source_id,
                    timing,
                )

                if status == "OK":

                    resolved += 1
                    total_rows += written

                elif status == "SKIP":

                    skipped += 1

                else:

                    unresolved += 1

                    failures.append(
                        {
                            "id": security["id"],
                            "name": security["name"],
                        }
                    )

            except Exception as exc:

                unresolved += 1

                failures.append(
                    {
                        "id": security["id"],
                        "name": security["name"],
                        "error": str(exc),
                    }
                )

                print(
                    f"    -> ERROR: {exc}"
                )

            print()

        print(
            "==============================================="
        )
        print(
            " Backfill Summary"
        )
        print(
            "==============================================="
        )

        print(
            f"Resolved securities   : {resolved}"
        )

        print(
            f"Skipped securities    : {skipped}"
        )

        print(
            f"Unresolved securities : {unresolved}"
        )

        print(
            f"Rows written           : {total_rows}"
        )

        if failures:

            print()
            print(
                "Unresolved / failed:"
            )

            print()

            for item in failures:

                error = item.get(
                    "error"
                )

                if error:

                    print(
                        f"  {item['id']:>3} | "
                        f"{item['name']} | "
                        f"{error}"
                    )

                else:

                    print(
                        f"  {item['id']:>3} | "
                        f"{item['name']}"
                    )

        report(
            conn,
            source_id,
        )

        total_elapsed = (
            time.perf_counter() - t_total0
        )

        print()
        print(
            f"Total: {total_elapsed:.2f}s | "
            f"HTTP: {timing['http']:.2f}s | "
            f"Parse: {timing['parse']:.2f}s | "
            f"DB: {timing['db']:.2f}s | "
            f"Skipped: {skipped}"
        )

    finally:

        conn.close()


if __name__ == "__main__":
    main()