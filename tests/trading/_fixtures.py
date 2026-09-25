from __future__ import annotations

import sqlite3
from datetime import date, timedelta


MARKET_START = date.today() - timedelta(days=259)
MARKET_END = date.today()


def make_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT NOT NULL, asset_type TEXT NOT NULL);
        CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE market_data (id INTEGER PRIMARY KEY, security_id INTEGER NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, adjusted_close REAL, volume REAL, currency TEXT, source_id INTEGER, fetched_at TEXT);
        CREATE TABLE watchlist (security_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE positions (security_id INTEGER PRIMARY KEY, shares REAL NOT NULL, avg_cost REAL, remaining_cost_basis REAL, currency TEXT, invested_amount REAL, realized_gain REAL, transaction_count INTEGER);
        CREATE TABLE market_snapshot (security_id INTEGER PRIMARY KEY, as_of_at TEXT, price REAL, currency TEXT);
        CREATE TABLE fx_rates (id INTEGER PRIMARY KEY, rate_date TEXT NOT NULL, base_currency TEXT NOT NULL, quote_currency TEXT NOT NULL, rate REAL NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL, UNIQUE(rate_date, base_currency, quote_currency, source));
        CREATE TABLE fundamentals (id INTEGER PRIMARY KEY, security_id INTEGER NOT NULL, period_end TEXT NOT NULL, period_type TEXT NOT NULL, fiscal_year INTEGER, fiscal_quarter INTEGER, filing_date TEXT, currency TEXT, revenue REAL, operating_income REAL, net_income REAL, operating_cash_flow REAL, free_cash_flow REAL, cash REAL, total_debt REAL, source_id INTEGER);
        CREATE TABLE estimates (security_id INTEGER, as_of_date TEXT);
        CREATE TABLE ratings (security_id INTEGER, as_of_date TEXT);
        CREATE TABLE price_targets (security_id INTEGER, as_of_date TEXT);
        CREATE TABLE events (security_id INTEGER, event_date TEXT);
        CREATE TABLE news (security_id INTEGER, published_at TEXT);
        CREATE TABLE strategy_assignment (
            id INTEGER PRIMARY KEY, security_id INTEGER NOT NULL,
            strategy_type TEXT NOT NULL, effective_from TEXT NOT NULL,
            effective_to TEXT, source TEXT, rationale TEXT,
            created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO security(id, symbol, name, asset_type) VALUES (?, ?, ?, ?)",
        [(1, "POS", "Position Corp", "stock"), (2, "WATCH", "Watch Corp", "stock"), (3, "ETF", "ETF Corp", "etf")],
    )
    conn.execute("INSERT INTO data_sources(id, name) VALUES (1, 'Yahoo Finance')")
    conn.executemany("INSERT INTO watchlist(security_id, status) VALUES (?, 'WATCH')", [(1,), (2,)])
    rows = []
    for index in range(260):
        trade_date = (MARKET_START + timedelta(days=index)).isoformat()
        for security_id, close in ((1, 100.0 + index), (2, 400.0 - index)):
            rows.append((security_id, trade_date, close, close, 1))
    conn.executemany("INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id) VALUES (?, ?, ?, ?, ?)", rows)
    conn.execute("INSERT INTO positions(security_id, shares, avg_cost, remaining_cost_basis, currency, invested_amount, realized_gain, transaction_count) VALUES (1, 10, 100, 1000, 'USD', 1000, 50, 3)")
    conn.execute(
        "INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (1, ?, 130, 'USD')",
        (MARKET_END.isoformat() + "T00:00:00+00:00",),
    )
    conn.executemany(
        "INSERT INTO fundamentals(security_id, period_end, period_type, fiscal_year, fiscal_quarter, currency, revenue, operating_income, net_income, operating_cash_flow, free_cash_flow, cash, total_debt, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2024-12-31", "annual", 2024, None, "USD", 100, 10, 10, 20, 8, 40, 15, 1),
            (1, "2025-09-30", "quarterly", 2025, 3, "USD", 999, 99, 99, 99, 99, 99, 99, 1),
            (1, "2025-12-31", "annual", 2025, None, "USD", 120, 12, 20, 30, 10, 50, 20, 1),
        ],
    )
    return conn
