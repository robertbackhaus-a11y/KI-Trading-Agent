"""Read-only watchlist candidate discovery: pre-filter, existing-engine analysis, deterministic ranking, no writes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import candidate_discovery as cd  # noqa: E402
from candidate_discovery import (  # noqa: E402
    DISCOVERY_DATA_INSUFFICIENT,
    DISCOVERY_READY,
    DISCOVERY_REJECTED,
    DISCOVERY_STATUSES,
    DISCOVERY_WATCH,
    UniverseEntry,
    discover_watchlist_candidates,
    load_universe,
    render_discovery_de,
)
from strategy_config import DiscoveryConfig, StrategyConfig  # noqa: E402
from swing_promotion import DATA_INSUFFICIENT, KEEP_WATCHING, PROMOTE, evaluate_swing_candidates  # noqa: E402
from _fixtures import make_connection  # noqa: E402

TODAY = date.today().isoformat()
BIG_VOLUME = 5_000_000.0


def series(*, days=260, start=100.0, daily=0.003, volume=BIG_VOLUME, tail_drop=None):
    """Calendar-day closes ending today; ``tail_drop`` multiplies the last 10 closes (pullback)."""
    rows = []
    for index in range(days):
        close = start * (1.0 + daily) ** index
        if tail_drop is not None and index >= days - 10:
            close *= tail_drop
        day = (date.today() - timedelta(days=days - 1 - index)).isoformat()
        rows.append({"trade_date": day, "open": close, "high": close, "low": close, "close": close, "adjusted_close": close, "volume": volume})
    return rows


def history(rows=None, *, currency="USD", instrument="EQUITY", **kw):
    rows = rows if rows is not None else series(**kw)
    return {"currency": currency, "instrument_type": instrument, "exchange": "TEST", "rows": rows, "price": rows[-1]["close"] if rows else None, "price_time": None}


def entry(symbol, *, name=None, market="US", currency="USD", asset_type="stock"):
    return UniverseEntry(symbol, name or symbol, market, currency, asset_type)


class Fetcher:
    def __init__(self, mapping):
        self.mapping, self.calls = mapping, []

    def __call__(self, symbol):
        self.calls.append(symbol)
        value = self.mapping.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value


def make_prod():
    """Production-shaped test database (fixture schema + the columns the real tables have)."""
    conn = make_connection()
    conn.executescript(
        """
        ALTER TABLE security ADD COLUMN exchange TEXT;
        ALTER TABLE security ADD COLUMN currency TEXT;
        ALTER TABLE security ADD COLUMN active INTEGER DEFAULT 1;
        ALTER TABLE watchlist ADD COLUMN priority INTEGER;
        ALTER TABLE watchlist ADD COLUMN entry_reason TEXT;
        ALTER TABLE watchlist ADD COLUMN updated_at TEXT;
        ALTER TABLE market_snapshot ADD COLUMN previous_close REAL;
        ALTER TABLE market_snapshot ADD COLUMN source_id INTEGER;
        ALTER TABLE market_snapshot ADD COLUMN fetched_at TEXT;
        CREATE TABLE source_symbols (id INTEGER PRIMARY KEY, security_id INTEGER, source_id INTEGER, symbol TEXT, exchange TEXT, currency TEXT, verified_at TEXT);
        CREATE TABLE swing_campaign (id INTEGER PRIMARY KEY, security_id INTEGER, status TEXT);
        """
    )
    for quote, rate in (("USD", 1.2), ("GBP", 0.86)):
        conn.execute("INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', ?, ?, 'ECB', ?)", (TODAY, quote, rate, TODAY + "T10:00:00+00:00"))
    # existing portfolio / watchlist (ids 1, 2 exist in the fixture); give them Yahoo symbols
    conn.execute("INSERT INTO source_symbols(security_id, source_id, symbol) VALUES (1, 1, 'HELD')")
    conn.execute("INSERT INTO source_symbols(security_id, source_id, symbol) VALUES (2, 1, 'WATCHED')")
    conn.execute("UPDATE positions SET shares = 10 WHERE security_id = 1")
    conn.commit()
    return conn


def fingerprint(conn):
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
        digest = hashlib.sha256()
        for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY 1'):
            digest.update(repr(tuple(row)).encode())
        out[name] = digest.hexdigest()
    return out


def run(conn, entries, mapping, **kw):
    fetcher = Fetcher(mapping)
    result = discover_watchlist_candidates(conn, entries, fetcher, as_of=TODAY, **kw)
    return result, fetcher


def by_symbol(result):
    return {c["symbol"]: c for c in result["candidates"]}


class PreFilterTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_prod()

    def tearDown(self):
        self.conn.close()

    def test_existing_position_is_excluded_before_any_request(self):
        result, fetcher = run(self.conn, [entry("HELD")], {"HELD": history()})
        self.assertEqual([(e["symbol"], e["reason_codes"], e["status"]) for e in result["excluded_candidates"]], [("HELD", ["ALREADY_IN_PORTFOLIO"], DISCOVERY_REJECTED)])
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(result["candidates"], [])

    def test_existing_watchlist_entry_is_excluded_before_any_request(self):
        result, fetcher = run(self.conn, [entry("WATCHED")], {"WATCHED": history()})
        self.assertEqual(result["excluded_candidates"][0]["reason_codes"], ["ALREADY_ON_WATCHLIST"])
        self.assertEqual(fetcher.calls, [])

    def test_unsupported_security_type_is_excluded_by_the_universe_flag_and_by_the_provider_type(self):
        result, fetcher = run(self.conn, [entry("FUND", asset_type="etf"), entry("ETFX")], {"ETFX": history(instrument="ETF")})
        self.assertEqual(result["excluded_candidates"][0]["reason_codes"], ["UNSUPPORTED_SECURITY_TYPE"])
        self.assertEqual(fetcher.calls, ["ETFX"])  # the flagged one was never requested
        self.assertEqual((by_symbol(result)["ETFX"]["status"], by_symbol(result)["ETFX"]["reason_codes"]), (DISCOVERY_REJECTED, ["UNSUPPORTED_SECURITY_TYPE"]))

    def test_unsupported_currency_is_filtered_before_the_request_and_after_it(self):
        result, fetcher = run(self.conn, [entry("SWISS", currency="CHF"), entry("YEN", currency="USD")], {"YEN": history(currency="JPY")})
        self.assertEqual(result["excluded_candidates"][0]["reason_codes"], ["UNSUPPORTED_CURRENCY"])
        self.assertEqual(fetcher.calls, ["YEN"])
        self.assertEqual((by_symbol(result)["YEN"]["status"], by_symbol(result)["YEN"]["reason_codes"]), (DISCOVERY_REJECTED, ["UNSUPPORTED_CURRENCY"]))

    def test_pence_quotes_are_supported_through_the_existing_gbp_normalization(self):
        result, _ = run(self.conn, [entry("UKCO.L", market="GB", currency="GBp")], {"UKCO.L": history(currency="GBp", start=2000.0)})
        self.assertEqual(result["excluded_candidates"], [])
        self.assertIsNotNone(by_symbol(result)["UKCO.L"]["current_price_eur"])

    def test_price_and_liquidity_minimums(self):
        cfg = StrategyConfig()
        penny = history(start=1.0, daily=0.0)
        illiquid = history(volume=100.0)
        no_volume = history(volume=None)
        result, _ = run(self.conn, [entry("PENNY"), entry("THIN"), entry("NOVOL")], {"PENNY": penny, "THIN": illiquid, "NOVOL": no_volume}, config=cfg)
        got = by_symbol(result)
        self.assertEqual((got["PENNY"]["status"], got["PENNY"]["reason_codes"]), (DISCOVERY_REJECTED, ["PRICE_BELOW_MINIMUM"]))
        self.assertEqual((got["THIN"]["status"], got["THIN"]["reason_codes"]), (DISCOVERY_REJECTED, ["LOW_LIQUIDITY"]))
        self.assertEqual((got["NOVOL"]["status"], got["NOVOL"]["reason_codes"]), (DISCOVERY_DATA_INSUFFICIENT, ["LIQUIDITY_DATA_UNAVAILABLE"]))
        self.assertEqual(DiscoveryConfig().min_price_eur, 5.0)
        self.assertEqual(DiscoveryConfig().min_median_daily_value_eur, 5_000_000.0)

    def test_limit_and_fetch_errors_do_not_stop_the_run(self):
        entries = [entry("AAA"), entry("BAD"), entry("CCC")]
        result, fetcher = run(self.conn, entries, {"AAA": history(), "BAD": RuntimeError("boom"), "CCC": history()}, limit=2)
        self.assertEqual(fetcher.calls, ["AAA", "BAD"])
        self.assertTrue(any(w.startswith("UNIVERSE_LIMITED_TO_2_OF_3") for w in result["warnings"]))
        self.assertIn("FETCH_ERROR:BAD:RuntimeError", result["warnings"])
        self.assertEqual((by_symbol(result)["BAD"]["status"], by_symbol(result)["BAD"]["reason_codes"]), (DISCOVERY_DATA_INSUFFICIENT, ["NO_MARKET_DATA"]))


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_prod()

    def tearDown(self):
        self.conn.close()

    def test_sufficient_data_uses_the_existing_engine_and_fills_every_field(self):
        result, _ = run(self.conn, [entry("UPTR")], {"UPTR": history()})
        c = by_symbol(result)["UPTR"]
        for key in ("rank", "security_id", "symbol", "name", "market", "currency", "current_price", "current_price_eur", "momentum_score", "discovery_score",
                    "technical_quality", "fundamental_quality", "event_risk_quality", "status", "reason_codes"):
            self.assertIn(key, c)
        self.assertEqual(c["technical_quality"], "available")
        self.assertIsNotNone(c["current_price_eur"])
        self.assertAlmostEqual(c["current_price_eur"], c["current_price"] / 1.2, places=6)
        self.assertEqual(c["fundamental_quality"], "unavailable")  # nothing is fetched for new securities
        self.assertFalse(c["fundamentals_evaluated"])
        self.assertAlmostEqual(c["discovery_score"], round(c["momentum_score"] * c["confidence"], 4), places=9)

    def test_discovery_ready(self):
        c = by_symbol(run(self.conn, [entry("UPTR")], {"UPTR": history()})[0])["UPTR"]
        self.assertEqual(c["status"], DISCOVERY_READY)
        self.assertEqual(c["reason_codes"], ["TREND_INTACT", "MOMENTUM_ABOVE_THRESHOLD"])
        self.assertGreaterEqual(c["momentum_score"], 20.0)

    def test_discovery_watch_for_a_pullback_inside_the_long_term_uptrend(self):
        c = by_symbol(run(self.conn, [entry("PULL")], {"PULL": history(tail_drop=0.85)})[0])["PULL"]
        self.assertEqual(c["status"], DISCOVERY_WATCH)
        self.assertEqual(c["reason_codes"], ["PRICE_BELOW_SMA50"])
        self.assertGreater(c["current_price"], c["sma200"])

    def test_discovery_watch_when_the_trend_is_intact_but_momentum_is_below_the_threshold(self):
        decision = SimpleNamespace(recommendation=PROMOTE, reasons=("PROMOTION_READY",), current_price=120.0, sma200=100.0)
        self.assertEqual(cd._classify(decision, 19.99), (DISCOVERY_WATCH, ["MOMENTUM_BELOW_THRESHOLD"]))
        self.assertEqual(cd._classify(decision, 20.0)[0], DISCOVERY_READY)
        self.assertEqual(cd._classify(decision, None)[0], DISCOVERY_WATCH)

    def test_data_insufficient_for_short_history(self):
        got = by_symbol(run(self.conn, [entry("SHORT"), entry("TINY")], {"SHORT": history(days=100), "TINY": history(days=10)})[0])
        self.assertEqual((got["SHORT"]["status"], got["SHORT"]["reason_codes"]), (DISCOVERY_DATA_INSUFFICIENT, ["INSUFFICIENT_HISTORY"]))
        self.assertEqual((got["TINY"]["status"], got["TINY"]["reason_codes"]), (DISCOVERY_DATA_INSUFFICIENT, ["INSUFFICIENT_HISTORY"]))

    def test_stale_market_data_is_insufficient(self):
        rows = series()
        stale = [dict(r, trade_date=(date.fromisoformat(r["trade_date"]) - timedelta(days=30)).isoformat()) for r in rows]
        c = by_symbol(run(self.conn, [entry("OLD")], {"OLD": history(stale)})[0])["OLD"]
        self.assertEqual(c["status"], DISCOVERY_DATA_INSUFFICIENT)
        self.assertEqual(c["reason_codes"], ["MARKET_DATA_STALE"])

    def test_missing_history_is_reported(self):
        got = by_symbol(run(self.conn, [entry("NONE"), entry("EMPTY")], {"EMPTY": history([])})[0])
        for symbol in ("NONE", "EMPTY"):
            self.assertEqual((got[symbol]["status"], got[symbol]["reason_codes"]), (DISCOVERY_DATA_INSUFFICIENT, ["NO_MARKET_DATA"]))

    def test_reject_for_a_downtrend_below_sma200(self):
        c = by_symbol(run(self.conn, [entry("DOWN")], {"DOWN": history(daily=-0.003, start=300.0)})[0])["DOWN"]
        self.assertEqual((c["status"], c["reason_codes"]), (DISCOVERY_REJECTED, ["PRICE_BELOW_SMA200"]))
        self.assertLessEqual(c["current_price"], c["sma200"])

    def test_classification_maps_every_promotion_outcome(self):
        decision = lambda rec, reasons, price=100.0, sma200=90.0: SimpleNamespace(recommendation=rec, reasons=tuple(reasons), current_price=price, sma200=sma200)
        self.assertEqual(cd._classify(decision(DATA_INSUFFICIENT, ["PROMOTION_FX_RATE_MISSING"]), 30.0), (DISCOVERY_DATA_INSUFFICIENT, ["FX_RATE_UNAVAILABLE"]))
        self.assertEqual(cd._classify(decision(KEEP_WATCHING, ["PROMOTION_SMA50_BELOW_OR_EQUAL_SMA200"]), 10.0), (DISCOVERY_WATCH, ["SMA50_BELOW_SMA200"]))
        self.assertEqual(cd._classify(decision(KEEP_WATCHING, ["PROMOTION_PRICE_BELOW_OR_EQUAL_SMA50"], price=80.0), 10.0), (DISCOVERY_REJECTED, ["PRICE_BELOW_SMA200"]))
        self.assertEqual(cd._classify(decision("REJECT", ["PROMOTION_X"]), 0.0), (DISCOVERY_REJECTED, ["PROMOTION_X"]))


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_prod()

    def tearDown(self):
        self.conn.close()

    def mapping(self):
        return {"UPA": history(daily=0.003), "UPB": history(daily=0.0015), "DOWN": history(daily=-0.003, start=300.0), "SHORT": history(days=60), "PULL": history(tail_drop=0.85)}

    def entries(self):
        return [entry(s) for s in ("DOWN", "SHORT", "UPB", "PULL", "UPA")]

    def test_ranking_is_deterministic_and_ordered_by_status_then_score(self):
        first = run(self.conn, self.entries(), self.mapping())[0]
        second = run(self.conn, list(reversed(self.entries())), self.mapping())[0]
        self.assertEqual(json.dumps(first["candidates"], sort_keys=True), json.dumps(second["candidates"], sort_keys=True))
        statuses = [c["status"] for c in first["candidates"]]
        self.assertEqual(statuses, sorted(statuses, key=DISCOVERY_STATUSES.index))
        self.assertEqual([c["rank"] for c in first["candidates"]], list(range(1, len(first["candidates"]) + 1)))
        ready = [c for c in first["candidates"] if c["status"] == DISCOVERY_READY]
        self.assertEqual([c["discovery_score"] for c in ready], sorted((c["discovery_score"] for c in ready), reverse=True))

    def test_equal_scores_are_ordered_by_symbol(self):
        same = history()
        result = run(self.conn, [entry("ZZZ"), entry("AAA"), entry("MMM")], {"ZZZ": same, "AAA": same, "MMM": same})[0]
        self.assertEqual([c["symbol"] for c in result["candidates"]], ["AAA", "MMM", "ZZZ"])
        self.assertEqual(len({c["discovery_score"] for c in result["candidates"]}), 1)

    def test_counts_add_up(self):
        entries = [*self.entries(), entry("HELD"), entry("SWISS", currency="CHF")]
        result = run(self.conn, entries, self.mapping())[0]
        self.assertEqual(result["universe_size"], 7)
        self.assertEqual(result["prefiltered_count"] + result["analyzed_count"], result["universe_size"])
        self.assertEqual(result["discovery_ready_count"] + result["discovery_watch_count"] + result["insufficient_count"] + result["rejected_count"], result["analyzed_count"])
        self.assertEqual(result["prefiltered_count"], 2)

    def test_rendering_has_the_table_and_the_disclaimers(self):
        text = render_discovery_de(run(self.conn, self.entries(), self.mapping())[0], top=3)
        for fragment in ("| Rank | Symbol | Name | Discovery-Score | Status | Grund |", "keine automatische Watchlist-Aufnahme", "weder PROMOTE noch Kauf", "nicht ausgewertet"):
            self.assertIn(fragment, text)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_prod()
        self.conn.execute("PRAGMA query_only = ON")  # the real CLI opens the database like this

    def tearDown(self):
        self.conn.close()

    def full_run(self):
        entries = [entry("UPTR"), entry("HELD"), entry("WATCHED"), entry("DOWN"), entry("SWISS", currency="CHF")]
        return run(self.conn, entries, {"UPTR": history(), "DOWN": history(daily=-0.003, start=300.0)})[0]

    def test_no_database_writes_at_all(self):
        before = fingerprint(self.conn)
        self.full_run()
        self.assertEqual(before, fingerprint(self.conn))

    def test_no_automatic_watchlist_security_strategy_or_campaign_creation(self):
        counts = lambda: tuple(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("security", "watchlist", "positions", "strategy_assignment", "swing_campaign", "market_data", "market_snapshot"))
        before = counts()
        result = self.full_run()
        self.assertEqual(before, counts())
        self.assertIs(result["watchlist_written"], False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM security WHERE symbol = 'UPTR'").fetchone()[0], 0)

    def test_no_buy_no_entry_plan_no_order(self):
        result = self.full_run()
        self.assertIs(result["orders_created"], False)
        self.assertIs(result["read_only"], True)
        self.assertNotIn("planned_entries", json.dumps(result))
        data = json.dumps([result["candidates"], result["excluded_candidates"]])  # results only, not the methodology prose
        for forbidden in ('"BUY"', "ENTRY_READY", '"PROMOTE"', "order_id"):
            self.assertNotIn(forbidden, data)
        for candidate in result["candidates"]:
            self.assertIn(candidate["status"], DISCOVERY_STATUSES)

    def test_existing_candidate_and_promotion_logic_is_unchanged_by_a_discovery_run(self):
        rows = [(2, (date.today() - timedelta(days=259 - i)).isoformat(), 100.0 + i) for i in range(260)]
        self.conn.execute("PRAGMA query_only = OFF")  # test setup only
        self.conn.execute("DELETE FROM market_data WHERE security_id = 2")
        self.conn.executemany("INSERT INTO market_data(security_id, trade_date, close, adjusted_close, currency, source_id) VALUES (?, ?, ?, ?, 'EUR', 1)", [(a, b, c, c) for a, b, c in rows])
        self.conn.execute("INSERT OR REPLACE INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (2, ?, 359, 'EUR')", (TODAY + "T00:00:00+00:00",))
        self.conn.execute("PRAGMA query_only = ON")
        before = [d.primitive() for d in evaluate_swing_candidates(self.conn, as_of=TODAY)]
        self.full_run()
        after = [d.primitive() for d in evaluate_swing_candidates(self.conn, as_of=TODAY)]
        self.assertEqual(before, after)
        self.assertEqual({d["recommendation"] for d in after}, {PROMOTE})


class UniverseAndCliTests(unittest.TestCase):
    def test_shipped_universe_file_is_valid_and_has_no_duplicates(self):
        meta, entries, warnings = load_universe(ROOT / "tools" / "trading" / "universe" / "swing_large_cap_v1.json")
        self.assertEqual((meta["universe_id"], meta["version"]), ("swing_large_cap_v1", 1))
        self.assertEqual(warnings, [])
        self.assertEqual(len({e.symbol.upper() for e in entries}), len(entries))
        self.assertGreater(len(entries), 100)
        self.assertTrue(all(e.asset_type == "stock" and e.market and e.currency for e in entries))

    def test_loader_rejects_malformed_files_and_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "u.json"
            path.write_text(json.dumps({"entries": [{"symbol": "A", "name": "A", "market": "US"}, {"symbol": "a", "name": "A2", "market": "US"}]}), encoding="utf-8")
            _, entries, warnings = load_universe(path)
            self.assertEqual([e.symbol for e in entries], ["A"])
            self.assertEqual(warnings, ["UNIVERSE_DUPLICATE_SKIPPED:a"])
            for bad in ({"entries": []}, {"entries": [{"symbol": "A"}]}, {}):
                path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_universe(path)

    def test_known_security_id_of_a_previously_traded_us_stock_is_reported_but_not_a_reason_to_skip(self):
        conn = make_prod()
        conn.execute("INSERT INTO security(id, symbol, name, asset_type, exchange) VALUES (50, 'OLDX', 'Old X', 'stock', 'US')")
        result, _ = run(conn, [entry("OLDX")], {"OLDX": history()})
        self.assertEqual(by_symbol(result)["OLDX"]["security_id"], 50)
        self.assertEqual(result["excluded_candidates"], [])
        conn.close()

    def test_cli_fetcher_uses_the_per_day_cache_and_never_caches_failures(self):
        spec = importlib.util.spec_from_file_location("discover_cli", ROOT / "tools" / "trading" / "Discover-TradingCandidates.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        calls = []
        chart = {"meta": {"currency": "USD", "instrumentType": "EQUITY", "regularMarketPrice": 10.0, "regularMarketTime": 1}}

        class FakeMarketData:
            HISTORY_RANGE = "2y"

            @staticmethod
            def yahoo_chart(symbol, range_value):
                calls.append(symbol)
                return None if symbol == "NOPE" else chart

            @staticmethod
            def parse_history(c):
                return [{"trade_date": "2026-01-02", "close": 10.0}]

        with tempfile.TemporaryDirectory() as tmp:
            fetch = cli.make_yahoo_fetcher(FakeMarketData, today="2026-10-07", delay=0, cache_dir=Path(tmp))
            first, second = fetch("GOOD"), fetch("GOOD")
            self.assertEqual(first, second)
            self.assertEqual(calls, ["GOOD"])  # second call came from the cache
            self.assertIsNone(fetch("NOPE"))
            self.assertIsNone(fetch("NOPE"))
            self.assertEqual(calls, ["GOOD", "NOPE", "NOPE"])
            self.assertEqual((first["currency"], first["instrument_type"], first["price"]), ("USD", "EQUITY", 10.0))


if __name__ == "__main__":
    unittest.main()
