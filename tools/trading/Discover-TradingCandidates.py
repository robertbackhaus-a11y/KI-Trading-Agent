"""Explicit, read-only watchlist candidate discovery (never part of the normal orchestrator run).

Reads the production database read-only, fetches daily history for the universe symbols
from Yahoo Finance through the existing market-data helpers and ranks the symbols.  After a
completely successful run it writes a structured report (``latest.json`` plus an optional
history copy) that the MCP reader ``get_candidate_discovery`` serves to the MCP client.

It writes **nothing** to the database: no security, no watchlist entry, no strategy
assignment, no campaign, no order.  The only files are the report and the optional
per-day request cache (``--cache-dir``).  ``latest.json`` is replaced atomically and only
after a valid, complete run; a failed or aborted run leaves the previous report untouched.

    python Discover-TradingCandidates.py [--limit N] [--top N] [--json] [--output-dir DIR] [--no-history] [--cache-dir DIR]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from candidate_discovery import discover_watchlist_candidates, load_universe, render_discovery_de  # noqa: E402
from discovery_report import build_report, write_report  # noqa: E402

DB_PATH = Path(r"C:\tools\trading\data\trading.db")
DEFAULT_UNIVERSE = HERE / "universe" / "swing_large_cap_v1.json"
DEFAULT_REPORT_DIR = Path(r"C:\tools\trading\data\candidate-discovery")


def _load_market_data_module():
    spec = importlib.util.spec_from_file_location("market_data_backfill", HERE / "Backfill-TradingMarketData.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _cache_path(cache_dir: Path, symbol: str, day: str) -> Path:
    return cache_dir / f"{re.sub(r'[^A-Za-z0-9._-]', '_', symbol)}_{day}.json"


def make_yahoo_fetcher(market_data, *, today: str, delay: float, cache_dir: Optional[Path] = None):
    """History fetcher on top of the existing market-data helpers; a per-day cache avoids repeated requests.

    The returned callable carries ``.stats`` = ``{"fetched", "cache_hits", "failed"}``."""
    stats = {"fetched": 0, "cache_hits": 0, "failed": 0}

    def fetch(symbol: str) -> Optional[dict[str, Any]]:
        if cache_dir is not None:
            path = _cache_path(cache_dir, symbol, today)
            if path.exists():
                stats["cache_hits"] += 1
                return json.loads(path.read_text(encoding="utf-8"))
        chart = market_data.yahoo_chart(symbol, market_data.HISTORY_RANGE)
        time.sleep(delay)
        if chart is None:
            stats["failed"] += 1
            return None
        meta = chart.get("meta") or {}
        result = {
            "currency": meta.get("currency"),
            "instrument_type": meta.get("instrumentType"),
            "exchange": meta.get("exchangeName"),
            "rows": market_data.parse_history(chart),
            "price": meta.get("regularMarketPrice"),
            "price_time": meta.get("regularMarketTime"),
            "previous_close": meta.get("previousClose") if meta.get("previousClose") is not None else meta.get("chartPreviousClose"),
        }
        stats["fetched"] += 1
        if cache_dir is not None and result["rows"]:
            cache_dir.mkdir(parents=True, exist_ok=True)
            _cache_path(cache_dir, symbol, today).write_text(json.dumps(result), encoding="utf-8")
        return result

    fetch.stats = stats  # type: ignore[attr-defined]
    return fetch


def _counting(fetch: Callable[[str], Optional[dict[str, Any]]]):
    """Statistics for an injected fetcher."""
    stats = {"fetched": 0, "cache_hits": 0, "failed": 0}

    def wrapped(symbol: str):
        try:
            value = fetch(symbol)
        except Exception:
            stats["failed"] += 1
            raise
        stats["fetched" if value and value.get("rows") else "failed"] += 1
        return value

    wrapped.stats = stats  # type: ignore[attr-defined]
    return wrapped


def main(argv: Optional[list[str]] = None, *, fetch_history: Optional[Callable[[str], Optional[dict[str, Any]]]] = None, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only watchlist candidate discovery (no database writes)")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--as-of", default=None, help="evaluation date YYYY-MM-DD (default: today)")
    parser.add_argument("--limit", type=int, default=None, help="analyze at most N pre-filtered symbols")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--delay", type=float, default=None, help="seconds between Yahoo requests (default: the backfill's delay)")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR, help="report directory (latest.json + history copies)")
    parser.add_argument("--no-history", action="store_true", help="write only latest.json, no history copy")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON instead of the German table")
    args = parser.parse_args(argv)
    if not args.db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {args.db_path}")
    now = now or datetime.now(timezone.utc)
    as_of = args.as_of or now.date().isoformat()
    date.fromisoformat(as_of)
    meta, entries, warnings = load_universe(args.universe)
    if fetch_history is None:
        market_data = _load_market_data_module()
        delay = market_data.REQUEST_DELAY_SECONDS if args.delay is None else args.delay
        fetch = make_yahoo_fetcher(market_data, today=as_of, delay=delay, cache_dir=args.cache_dir)
    else:
        fetch = _counting(fetch_history)
    conn = sqlite3.connect(f"file:///{args.db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        result = discover_watchlist_candidates(conn, entries, fetch, as_of=as_of, limit=args.limit, universe_meta=meta)
    finally:
        conn.close()
    result["warnings"] = [*warnings, *result["warnings"]]
    # A run in which not a single symbol delivered market data is a failed run: the previous report must survive.
    if not any(c["reason_codes"] != ["NO_MARKET_DATA"] for c in result["candidates"]):
        print("FAILED: no symbol delivered market data (network or provider problem); the previous report was left untouched.", file=sys.stderr)
        return 2
    report = build_report(result, generated_at=now, fetch_stats=getattr(fetch, "stats", None), cache_dir=str(args.cache_dir) if args.cache_dir else None)
    paths = write_report(report, args.output_dir, history=not args.no_history)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_discovery_de(result, top=args.top))
        print(f"\nReport: {paths['latest']}" + (f" | History: {paths['history']}" if paths["history"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
