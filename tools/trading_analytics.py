"""
Read-only technical analysis library for the trading agent.

Pure reads against the existing schema (security, watchlist,
market_data) -- no writes, no new tables, no web access. Every
function takes an already-open sqlite3.Connection; this module never
opens, creates, or migrates the database itself.

Price series use adjusted_close where available (falls back to
close), so splits/dividends don't distort performance, SMA and
volatility figures.
"""

from __future__ import annotations

import sqlite3
import statistics
from typing import Optional


DB_PATH_HINT = r"C:\KI-Stack\data\trading\trading.db"

TRADING_DAYS_PER_YEAR = 252

# Lookback windows expressed in trading days (approximate calendar
# equivalents: ~21 = 1 month, ~63 = 3 months, ~126 = 6 months).
PERF_1M_DAYS = 21
PERF_3M_DAYS = 63
PERF_6M_DAYS = 126

SMA_SHORT_DAYS = 50
SMA_LONG_DAYS = 200
RSI_DAYS = 14
VOLATILITY_DAYS = 60
DRAWDOWN_WINDOW_DAYS = 252  # ~52 trading weeks

# Data-quality thresholds (row count = number of usable price points).
QUALITY_OK_MIN_ROWS = SMA_LONG_DAYS            # enough for every metric
QUALITY_LIMITED_MIN_ROWS = 30                  # enough for most metrics
# below QUALITY_LIMITED_MIN_ROWS: "INSUFFICIENT"


# ============================================================
# DATA LOADING
# ============================================================

def _get_yahoo_source_id(
    connection: sqlite3.Connection,
) -> Optional[int]:

    row = connection.execute(
        "SELECT id FROM data_sources WHERE name = 'Yahoo Finance' LIMIT 1"
    ).fetchone()

    if row is None:
        return None

    return row[0]


def _load_price_series(
    connection: sqlite3.Connection,
    security_id: int,
) -> list[float]:
    """Returns closing prices (adjusted_close preferred, else close),
    oldest first. Restricted to the Yahoo Finance source when that
    source exists, so multiple providers for the same security never
    get mixed into a single series."""

    source_id = _get_yahoo_source_id(connection)

    if source_id is not None:

        rows = connection.execute(
            """
            SELECT close, adjusted_close
            FROM market_data
            WHERE security_id = ?
              AND source_id = ?
            ORDER BY trade_date ASC
            """,
            (security_id, source_id),
        ).fetchall()

    else:

        rows = connection.execute(
            """
            SELECT close, adjusted_close
            FROM market_data
            WHERE security_id = ?
            ORDER BY trade_date ASC
            """,
            (security_id,),
        ).fetchall()

    closes = []

    for close, adjusted_close in rows:

        price = adjusted_close if adjusted_close is not None else close

        if price is None:
            continue

        closes.append(float(price))

    return closes


def _load_last_trade_date(
    connection: sqlite3.Connection,
    security_id: int,
) -> Optional[str]:

    source_id = _get_yahoo_source_id(connection)

    if source_id is not None:

        row = connection.execute(
            """
            SELECT MAX(trade_date)
            FROM market_data
            WHERE security_id = ?
              AND source_id = ?
            """,
            (security_id, source_id),
        ).fetchone()

    else:

        row = connection.execute(
            """
            SELECT MAX(trade_date)
            FROM market_data
            WHERE security_id = ?
            """,
            (security_id,),
        ).fetchone()

    return row[0] if row else None


# ============================================================
# INDICATORS (plain functions over a price list, oldest-first)
# ============================================================

def quality_status(data_points: int) -> str:

    if data_points >= QUALITY_OK_MIN_ROWS:
        return "OK"

    if data_points >= QUALITY_LIMITED_MIN_ROWS:
        return "LIMITED"

    return "INSUFFICIENT"


def performance_pct(
    closes: list[float],
    lookback_days: int,
) -> Optional[float]:
    """% change from lookback_days trading days ago to the latest
    close. None if there isn't enough history yet."""

    if len(closes) <= lookback_days:
        return None

    past = closes[-1 - lookback_days]
    current = closes[-1]

    if past == 0:
        return None

    return (current - past) / past * 100.0


def sma(
    closes: list[float],
    window: int,
) -> Optional[float]:

    if len(closes) < window:
        return None

    return sum(closes[-window:]) / window


def distance_to_sma_pct(
    current: float,
    sma_value: Optional[float],
) -> Optional[float]:

    if not sma_value:
        return None

    return (current - sma_value) / sma_value * 100.0


def rsi14(
    closes: list[float],
    window: int = RSI_DAYS,
) -> Optional[float]:
    """Classic RSI using a simple (non-Wilder-smoothed) average of
    gains/losses over the last `window` daily changes -- kept as the
    plain, traceable textbook formula rather than an exponential
    smoothing variant."""

    if len(closes) < window + 1:
        return None

    changes = [
        closes[i] - closes[i - 1]
        for i in range(len(closes) - window, len(closes))
    ]

    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]

    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window

    if avg_loss == 0:
        return 100.0

    relative_strength = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + relative_strength))


def drawdown_52w_pct(
    closes: list[float],
    window: int = DRAWDOWN_WINDOW_DAYS,
) -> Optional[float]:
    """Current price's % distance below the highest close in the
    last `window` trading days (0 = at the 52-week high, negative =
    below it). Uses whatever history is available if shorter than
    the full window."""

    if len(closes) < QUALITY_LIMITED_MIN_ROWS:
        return None

    recent = closes[-window:] if len(closes) >= window else closes

    high = max(recent)
    current = closes[-1]

    if high == 0:
        return None

    return (current - high) / high * 100.0


def annualized_volatility_pct(
    closes: list[float],
    window: int = VOLATILITY_DAYS,
) -> Optional[float]:
    """Standard deviation of daily returns over the last `window`
    trading days, annualized with sqrt(252)."""

    if len(closes) < window + 1:
        return None

    recent = closes[-(window + 1):]

    returns = [
        (recent[i] - recent[i - 1]) / recent[i - 1]
        for i in range(1, len(recent))
        if recent[i - 1] != 0
    ]

    if len(returns) < 2:
        return None

    daily_std = statistics.pstdev(returns)

    return daily_std * (TRADING_DAYS_PER_YEAR ** 0.5) * 100.0


def _clip(value: float, low: float, high: float) -> float:

    return max(low, min(high, value))


# ============================================================
# MOMENTUM / TREND SCORE
#
# Simple, transparent, additive score. Every contribution is capped
# so no single input can dominate, and every contribution is
# returned alongside the total in `score_components` so the result
# is traceable field by field:
#
#   price  > SMA50           : +10 / -10
#   price  > SMA200          : +10 / -10
#   SMA50  > SMA200          : +10 / -10   (golden/death cross state)
#   1M performance            : clipped to [-10, +10]
#   3M performance            : clipped to [-10, +10]
#   6M performance            : clipped to [-10, +10]
#   RSI14 vs neutral 50       : (RSI - 50) / 50 * 10   (~[-10, +10])
#   52-week drawdown          : the (negative) drawdown %, floored at -20
#
# Any component whose inputs are unavailable (too little history)
# is simply left out of the sum -- it does not count as zero, it is
# absent, and callers can see that from `score_components`.
# ============================================================

def momentum_score(
    current_price: Optional[float],
    sma50_value: Optional[float],
    sma200_value: Optional[float],
    perf_1m: Optional[float],
    perf_3m: Optional[float],
    perf_6m: Optional[float],
    rsi_value: Optional[float],
    drawdown_value: Optional[float],
) -> tuple[Optional[float], dict]:

    components: dict = {}

    if current_price is not None and sma50_value is not None:

        components["price_vs_sma50"] = (
            10.0 if current_price > sma50_value else -10.0
        )

    if current_price is not None and sma200_value is not None:

        components["price_vs_sma200"] = (
            10.0 if current_price > sma200_value else -10.0
        )

    if sma50_value is not None and sma200_value is not None:

        components["sma50_vs_sma200"] = (
            10.0 if sma50_value > sma200_value else -10.0
        )

    if perf_1m is not None:
        components["momentum_1m"] = _clip(perf_1m, -10.0, 10.0)

    if perf_3m is not None:
        components["momentum_3m"] = _clip(perf_3m, -10.0, 10.0)

    if perf_6m is not None:
        components["momentum_6m"] = _clip(perf_6m, -10.0, 10.0)

    if rsi_value is not None:
        components["rsi"] = (rsi_value - 50.0) / 50.0 * 10.0

    if drawdown_value is not None:
        components["drawdown_52w"] = max(drawdown_value, -20.0)

    if not components:
        return None, {}

    components = {
        key: round(value, 2)
        for key, value in components.items()
    }

    total = round(sum(components.values()), 2)

    return total, components


# ============================================================
# PUBLIC API
# ============================================================

def analyze_security(
    connection: sqlite3.Connection,
    security_id: int,
) -> dict:
    """Read-only technical analysis snapshot for one security.

    Raises ValueError if security_id does not exist. Returns a dict
    with every metric set to None (and quality="INSUFFICIENT") when
    there is no usable market_data history at all -- it never
    raises just because history is missing or short.
    """

    security = connection.execute(
        "SELECT id, name, symbol FROM security WHERE id = ?",
        (security_id,),
    ).fetchone()

    if security is None:
        raise ValueError(f"security_id {security_id} not found")

    _, name, symbol = security

    closes = _load_price_series(connection, security_id)
    data_points = len(closes)

    result = {
        "security_id": security_id,
        "name": name,
        "symbol": symbol,
        "as_of": _load_last_trade_date(connection, security_id),
        "current_price": closes[-1] if closes else None,
        "data_points": data_points,
        "quality": quality_status(data_points),
        "perf_1m_pct": None,
        "perf_3m_pct": None,
        "perf_6m_pct": None,
        "sma50": None,
        "sma200": None,
        "dist_sma50_pct": None,
        "dist_sma200_pct": None,
        "rsi14": None,
        "drawdown_52w_pct": None,
        "volatility_60d_annualized_pct": None,
        "score": None,
        "score_components": {},
    }

    if data_points == 0:
        return result

    current_price = closes[-1]

    result["perf_1m_pct"] = performance_pct(closes, PERF_1M_DAYS)
    result["perf_3m_pct"] = performance_pct(closes, PERF_3M_DAYS)
    result["perf_6m_pct"] = performance_pct(closes, PERF_6M_DAYS)

    result["sma50"] = sma(closes, SMA_SHORT_DAYS)
    result["sma200"] = sma(closes, SMA_LONG_DAYS)

    result["dist_sma50_pct"] = distance_to_sma_pct(
        current_price, result["sma50"]
    )

    result["dist_sma200_pct"] = distance_to_sma_pct(
        current_price, result["sma200"]
    )

    result["rsi14"] = rsi14(closes)

    result["drawdown_52w_pct"] = drawdown_52w_pct(closes)

    result["volatility_60d_annualized_pct"] = annualized_volatility_pct(
        closes
    )

    score, components = momentum_score(
        current_price,
        result["sma50"],
        result["sma200"],
        result["perf_1m_pct"],
        result["perf_3m_pct"],
        result["perf_6m_pct"],
        result["rsi14"],
        result["drawdown_52w_pct"],
    )

    result["score"] = score
    result["score_components"] = components

    return result


def rank_watchlist(
    connection: sqlite3.Connection,
) -> list[dict]:
    """analyze_security() for every watchlist entry with
    status='WATCH', sorted by score descending. Entries where a
    score could not be computed (no usable history) are appended at
    the end, in watchlist order, rather than dropped."""

    rows = connection.execute(
        """
        SELECT security_id
        FROM watchlist
        WHERE status = 'WATCH'
        """
    ).fetchall()

    analyzed = []

    for (security_id,) in rows:

        try:

            analyzed.append(
                analyze_security(connection, security_id)
            )

        except ValueError:

            # watchlist row pointing at a security_id that no longer
            # exists -- skip rather than fail the whole ranking.
            continue

    scored = [r for r in analyzed if r["score"] is not None]
    unscored = [r for r in analyzed if r["score"] is None]

    scored.sort(
        key=lambda r: r["score"],
        reverse=True,
    )

    return scored + unscored
