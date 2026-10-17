# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Explicit, read-only market-intelligence collection (never part of the orchestrator run, never started by MCP).

Fetches the official macro feeds (ECB, Federal Reserve, BLS, BEA, Destatis, EIA) and the configured company-wire RSS feeds
(``universe/news_feeds_v1.json``), reads the cached company news and SEC events from the production database (read only),
fetches Yahoo headlines for the discovery candidates, resolves symbols/ISINs/names to securities, deduplicates across sources,
classifies and maps everything with fixed rules and writes a structured
report (``latest.json`` plus an optional history copy) that the MCP reader ``get_market_intelligence``
serves to the MCP client.

It writes **nothing** to the database and creates no order; news never change a signal.  ``latest.json``
is replaced atomically and only after a valid run in which at least one official feed answered; a failed
run leaves the previous report untouched.

    python Collect-TradingMarketIntelligence.py [--output-dir DIR] [--no-history] [--discovery-news-limit N] [--news-feeds FILE | --no-news-feeds] [--json]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import ssl
import sys
import time
import urllib.error
import xml.etree.ElementTree as ET
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import discovery_report  # noqa: E402
import runtime_paths  # noqa: E402
import sector_assignment  # noqa: E402
from intelligence_report import build_report, query_report, render_report_de, write_report  # noqa: E402
from market_intelligence import FEEDS, build_market_intelligence, load_db_context, load_news_feeds, load_sector_map, parse_feed, parse_feed_detailed  # noqa: E402
from strategy_config import MarketIntelligenceConfig  # noqa: E402

DB_PATH = runtime_paths.default_db_path()
DEFAULT_REPORT_DIR = runtime_paths.data_dir() / "market-intelligence"
DEFAULT_DISCOVERY_DIR = runtime_paths.data_dir() / "candidate-discovery"
DEFAULT_SECTOR_MAP = HERE / "universe" / "sector_map_v1.json"
DEFAULT_NEWS_FEEDS = HERE / "universe" / "news_feeds_v1.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) trading-agent market-intelligence (read-only)"
TIMEOUT_SECONDS = 15
RSS_ATTEMPTS = 2
RSS_RETRY_SECONDS = 1.5


def read_sector_map(path: Path) -> dict[str, str]:
    """Sector map file plus the optional, not versioned ``sector_map_local.json`` next to it (own portfolio/watchlist symbols)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    local = runtime_paths.private_file(path.parent, "sector_map_local.json")
    local_payload = json.loads(local.read_text(encoding="utf-8")) if local.is_file() else None
    return load_sector_map(sector_assignment.merged_payload(payload, local_payload))  # a local entry overrides the public one


def fetch_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()) as response:
        return response.read()


def _load_events_news_module():
    spec = importlib.util.spec_from_file_location("events_news_backfill", HERE / "Backfill-TradingEventsNews.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main(
    argv: Optional[list[str]] = None,
    *,
    fetch: Optional[Callable[[str], bytes]] = None,
    fetch_news: Optional[Callable[[str, Optional[str]], list[dict[str, Any]]]] = None,
    now: Optional[datetime] = None,
) -> int:
    parser = argparse.ArgumentParser(description="Read-only market intelligence collection (no database writes, no orders)")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--discovery-dir", type=Path, default=DEFAULT_DISCOVERY_DIR)
    parser.add_argument("--sector-map", type=Path, default=DEFAULT_SECTOR_MAP)
    parser.add_argument("--as-of", default=None, help="evaluation date YYYY-MM-DD (default: today)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--news-feeds", type=Path, default=DEFAULT_NEWS_FEEDS, help="company-wire RSS feed configuration (JSON)")
    parser.add_argument("--no-news-feeds", action="store_true", help="do not read the company-wire RSS feeds")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--discovery-news-limit", type=int, default=40, help="Yahoo headline requests for the best discovery candidates (0 = none)")
    parser.add_argument("--delay", type=float, default=0.2, help="seconds between Yahoo requests")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON instead of the German table")
    args = parser.parse_args(argv)
    if not args.db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {args.db_path}")
    now = now or datetime.now(timezone.utc)
    as_of = args.as_of or now.date().isoformat()
    date.fromisoformat(as_of)
    cfg = MarketIntelligenceConfig()
    since = (date.fromisoformat(as_of) - timedelta(days=cfg.lookback_days)).isoformat()
    warnings: list[str] = []
    sector_map = read_sector_map(args.sector_map)

    # ---- official feeds (one failing feed never stops the run)
    getter = fetch or fetch_url
    feed_items: dict[str, list[dict[str, Any]]] = {}
    feed_status: dict[str, dict[str, Any]] = {}
    for key, spec in FEEDS.items():
        try:
            items = parse_feed(getter(spec["url"]))
            feed_items[key] = items
            feed_status[key] = {"status": "OK", "items": len(items)}
        except Exception as exc:
            feed_status[key] = {"status": "FAILED", "items": 0, "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            warnings.append(f"FEED_FAILED:{key}")
    if not feed_items:
        print("FAILED: no official feed answered (network problem); the previous report was left untouched.", file=sys.stderr)
        return 2

    # ---- company-wire RSS feeds: configurable, every feed isolated (a broken feed is a warning, never a failed run)
    rss_items: dict[str, list[dict[str, Any]]] = {}
    rss_status: dict[str, dict[str, Any]] = {}
    news_feeds: dict[str, dict[str, Any]] = {}
    if not args.no_news_feeds:
        try:
            news_feeds = load_news_feeds(json.loads(args.news_feeds.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            warnings.append(f"NEWS_FEED_CONFIG_UNAVAILABLE:{type(exc).__name__}")
    for key, spec in news_feeds.items():
        try:
            payload = None
            for attempt in range(RSS_ATTEMPTS if fetch is None else 1):  # public CDNs answer an occasional 404/5xx: one retry for the real network only
                try:
                    payload = getter(spec["url"])
                    break
                except Exception:
                    if attempt + 1 >= RSS_ATTEMPTS or fetch is not None:
                        raise
                    time.sleep(RSS_RETRY_SECONDS)
            items, skipped = parse_feed_detailed(payload)
            rss_items[key] = items
            rss_status[key] = {"status": "OK" if items else "EMPTY", "parsed": len(items), "parse_errors": 0, "skipped_items": skipped}
        except ET.ParseError as exc:
            rss_status[key] = {"status": "FAILED", "parsed": 0, "parse_errors": 1, "skipped_items": 0, "error": f"ParseError: {str(exc)[:80]}"}
            warnings.append(f"RSS_FEED_FAILED:{key}")
        except Exception as exc:
            rss_status[key] = {"status": "FAILED", "parsed": 0, "parse_errors": 0, "skipped_items": 0, "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            warnings.append(f"RSS_FEED_FAILED:{key}")
        if fetch is None:
            time.sleep(args.delay)

    # ---- production database (read only): membership, cached company news and SEC events
    conn = sqlite3.connect(f"file:///{args.db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        context = load_db_context(conn, since=since, until=as_of)
    finally:
        conn.close()

    # ---- discovery candidates (from the last finished discovery report) and their Yahoo headlines
    discovery: dict[str, Any] = {}
    discovery_meta: dict[str, Any] = {"report_found": False, "symbols_requested": 0, "symbols_ok": 0, "symbols_failed": 0}
    report, reason, _problems = discovery_report.read_latest_report(args.discovery_dir)
    if report is None:
        warnings.append(f"DISCOVERY_REPORT_UNAVAILABLE:{reason}")
    else:
        discovery_meta["report_found"] = True
        discovery_meta["report_generated_at"] = report["metadata"]["generated_at"]
        for row in report["results"]:
            if row["status"] in ("DISCOVERY_READY", "DISCOVERY_WATCH") and row["symbol"] not in context["portfolio"] and row["symbol"] not in context["watchlist"]:
                discovery[row["symbol"]] = {"name": row["name"], "status": row["status"], "rank": row["rank"]}
    news_items = list(context["news"])
    requested = [symbol for symbol, info in sorted(discovery.items(), key=lambda kv: kv[1]["rank"]) if info["status"] == "DISCOVERY_READY"][: max(0, args.discovery_news_limit)]
    if requested:
        fetcher = fetch_news
        if fetcher is None:
            module = _load_events_news_module()
            fetcher = lambda symbol, name: module.fetch_yahoo_news(symbol, name=name)
        for symbol in requested:
            discovery_meta["symbols_requested"] += 1
            try:
                items = fetcher(symbol, discovery[symbol]["name"])
                news_items += [{**item, "symbol": symbol} for item in items]
                discovery_meta["symbols_ok"] += 1
            except Exception as exc:
                discovery_meta["symbols_failed"] += 1
                warnings.append(f"DISCOVERY_NEWS_FAILED:{symbol}:{type(exc).__name__}")
            if fetch_news is None:
                time.sleep(args.delay)

    result = build_market_intelligence(
        evaluation_as_of=as_of, fetched_at=now.astimezone(timezone.utc).isoformat(), feed_items=feed_items, news_items=news_items,
        sec_rows=context["sec_events"], portfolio=context["portfolio"], watchlist=context["watchlist"], discovery=discovery,
        sector_map=sector_map, config=cfg, rss_items=rss_items, news_feeds=news_feeds, alias_index=context["alias_index"],
    )
    result["warnings"] = warnings
    collection = {
        "feeds": feed_status,
        "news_feeds": {key: {**status, **{k: v for k, v in result["news_feed_stats"].get(key, {}).items()}} for key, status in rss_status.items()},
        "db_news_rows": len(context["news"]),
        "db_sec_event_rows": len(context["sec_events"]),
        "discovery": discovery_meta,
        "portfolio_symbols": len(context["portfolio"]),
        "watchlist_symbols": len(context["watchlist"]),
        "discovery_symbols": len(discovery),
        "sector_map": args.sector_map.name,
    }
    built = build_report(result, generated_at=now, collection=collection)
    paths = write_report(built, args.output_dir, history=not args.no_history)
    if args.json:
        print(json.dumps(built, indent=2, ensure_ascii=False))
    else:
        print(render_report_de(query_report(built, scope="ALL", limit=args.top, now=now, slim=False), fresh_run=True))
        print(f"\nReport: {paths['latest']}" + (f" | History: {paths['history']}" if paths["history"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
