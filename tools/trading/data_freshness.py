# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Read-only data freshness of what the scheduled tasks produce (the ``get_data_freshness`` MCP tool, the "Tasks" page of the desktop GUI).

The Windows Task Scheduler only knows that a task *ran* and with which exit code; it says nothing about the age of the data the task is meant to produce
(a task can end with exit code 0 and still have fetched nothing).  This module reports the facts the data themselves carry::

    market_data           market_data / market_snapshot  latest daily bar, oldest latest bar of a held position, last fetch
    fx                    fx_rates                       latest rate date, oldest latest date of a currency, last fetch
    events_news           news / events                  last fetch, latest article, last stored SEC event
    fundamentals          fundamentals                   last fetch, latest period end, latest filing date
    discovery             candidate-discovery/latest.json            generated_at and evaluation date of the last finished report
    market_intelligence   market-intelligence/latest.json           generated_at and evaluation date of the last finished report
    etf_data              etf_holdings_local.json / etf_lookthrough_local.json   per ETF the stand (``as_of``) of the holdings and of the sector allocation

The two reports are only written after a complete, valid run, so their ``generated_at`` is the last *successful* run of their task.  Only facts are returned
(no verdict); the age rules that already exist in the project are returned next to them (``rules``) so that a client does not copy the numbers.
Only SELECTs and file reads; no write, no network, no task is started.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import runtime_paths

OPERATION = "get_data_freshness"
SOURCE_NAME = "Yahoo Finance"


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def market_data(conn: sqlite3.Connection) -> dict[str, Any]:
    source = conn.execute("SELECT id FROM data_sources WHERE name = ?", (SOURCE_NAME,)).fetchone()
    clause, params = (" AND source_id = ?", (int(source[0]),)) if source is not None else ("", ())
    held = [int(r[0]) for r in conn.execute("SELECT security_id FROM positions WHERE shares > 0")]
    latest_by_held = []
    for sid in held:
        latest = _one(conn, f"SELECT MAX(trade_date) FROM market_data WHERE security_id = ?{clause}", (sid, *params))
        latest_by_held.append(latest)
    with_bars = [d for d in latest_by_held if d]
    return {
        "latest_bar_date": _one(conn, f"SELECT MAX(trade_date) FROM market_data WHERE 1 = 1{clause}", params),
        "bars_fetched_at": _one(conn, f"SELECT MAX(fetched_at) FROM market_data WHERE 1 = 1{clause}", params),
        "snapshot_fetched_at": _one(conn, "SELECT MAX(fetched_at) FROM market_snapshot"),
        "held_positions": len(held), "held_without_bars": len(held) - len(with_bars),
        "oldest_held_bar_date": min(with_bars) if with_bars else None,
    }


def fx(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT quote_currency, MAX(rate_date) FROM fx_rates WHERE base_currency = 'EUR' GROUP BY quote_currency").fetchall()
    oldest = min(rows, key=lambda r: r[1]) if rows else None
    return {
        "latest_rate_date": max((r[1] for r in rows), default=None), "oldest_currency_date": oldest[1] if oldest else None, "oldest_currency": oldest[0] if oldest else None,
        "currencies": len(rows), "fetched_at": _one(conn, "SELECT MAX(fetched_at) FROM fx_rates"),
    }


def events_news(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "news_fetched_at": _one(conn, "SELECT MAX(fetched_at) FROM news"), "latest_news_published_at": _one(conn, "SELECT MAX(published_at) FROM news"),
        "events_created_at": _one(conn, "SELECT MAX(created_at) FROM events"), "latest_event_date": _one(conn, "SELECT MAX(event_date) FROM events"),
    }


def fundamentals(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "fetched_at": _one(conn, "SELECT MAX(fetched_at) FROM fundamentals"), "latest_period_end": _one(conn, "SELECT MAX(period_end) FROM fundamentals"),
        "latest_filing_date": _one(conn, "SELECT MAX(filing_date) FROM fundamentals"),
    }


def report(module: Any, directory: Path) -> dict[str, Any]:
    """The last finished report of a report module (``read_latest_report``): ``generated_at`` is the last successful run of the task that writes it."""
    document, reason, problems = module.read_latest_report(directory)
    if document is None:
        return {"status": "UNAVAILABLE", "reason": reason, "problems": list(problems)[:3]}
    meta = document.get("metadata") or {}
    return {"status": "AVAILABLE", "generated_at": meta.get("generated_at"), "evaluation_as_of": meta.get("evaluation_as_of")}


def etf_data(universe_dir: Optional[Path] = None, today: Optional[date] = None) -> dict[str, Any]:
    """The stand of the private ETF files per ETF and dataset (holdings, sector allocation) as the loaders judge it: ``APPLIED`` / ``STALE`` (older than the loaders' own limit,
    ``stale_after_days``, which is not defined here) / ``MISSING`` / ``INVALID``.  ``manual`` marks a dataset that is not refreshed automatically (the SPXS sector allocation exists
    only as a factsheet PDF)."""
    import json

    import etf_holdings as eh
    import etf_lookthrough as lt
    from etf_refresh import SPECS

    folder = Path(universe_dir) if universe_dir is not None else eh.DEFAULT_UNIVERSE
    today = today or date.today()
    problems: list[str] = []

    def entries(file_name: str) -> dict[str, Any]:
        path = runtime_paths.private_file(folder, file_name)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload.get("etfs") or {}
        except (OSError, ValueError, AttributeError) as exc:
            problems.append(f"{file_name}: {type(exc).__name__}")
            return {}

    def row(spec: Any, kind: str, entry: Any, check: Callable[[Any], None], stale_after: int) -> dict[str, Any]:
        out: dict[str, Any] = {"symbol": spec.symbol, "isin": spec.isin, "dataset": kind, "provider": spec.provider, "manual": kind == "lookthrough" and not spec.sector_automated,
                               "as_of": None, "age_days": None, "status": "MISSING", "detail": ""}
        if not isinstance(entry, dict):
            return out
        try:
            check(entry)
        except (eh.HoldingsError, lt.LookthroughError) as exc:
            out.update(status="INVALID", detail=str(exc)[:160], as_of=str(entry.get("as_of") or "") or None)
            return out
        out["as_of"] = str(entry["as_of"])
        out["age_days"] = (today - date.fromisoformat(out["as_of"])).days
        out["status"] = "STALE" if out["age_days"] > stale_after else "APPLIED"
        if kind == "holdings":
            out["rows"] = len(entry.get("holdings") or [])
        return out

    holdings, sectors = entries(eh.FILE_NAME), entries(lt.ALLOCATION_NAME)
    return {
        "stale_after_days": eh.STALE_AFTER_DAYS, "problems": problems,
        "holdings": [row(s, "holdings", holdings.get(s.isin), lambda e, s=s: eh._etf(s.isin, e), eh.STALE_AFTER_DAYS) for s in SPECS],
        "lookthrough": [row(s, "lookthrough", sectors.get(s.isin), lambda e, s=s: lt._allocation(s.isin, e), lt.STALE_AFTER_DAYS) for s in SPECS],
    }


def rules() -> dict[str, Any]:
    """The age rules that already exist in the project (``strategy_config``); nothing new is defined here."""
    from strategy_config import DataQualityConfig, FXConfig, MarketIntelligenceConfig

    return {
        "market_data_max_age_days": DataQualityConfig().market_data_max_age_days, "fx_max_age_days": FXConfig().fx_max_age_days,
        "discovery_report_max_age_days": DataQualityConfig().market_data_max_age_days,  # discovery_report: "the report is market-data based"
        "intelligence_report_max_age_hours": MarketIntelligenceConfig().report_max_age_hours,
    }


def build_data_freshness(conn: sqlite3.Connection, *, discovery_module: Any, intelligence_module: Any, discovery_dir: Path, intelligence_dir: Path,
                         now: Optional[datetime] = None, universe_dir: Optional[Path] = None) -> dict[str, Any]:
    """One entry per source; a source that cannot be read reports ``available: false`` with the error, never a failed tool."""
    now = now or datetime.now(timezone.utc)
    sources: dict[str, Any] = {}
    jobs: dict[str, Callable[[], dict[str, Any]]] = {
        "market_data": lambda: market_data(conn), "fx": lambda: fx(conn), "events_news": lambda: events_news(conn), "fundamentals": lambda: fundamentals(conn),
        "discovery": lambda: report(discovery_module, Path(discovery_dir)), "market_intelligence": lambda: report(intelligence_module, Path(intelligence_dir)),
        "etf_data": lambda: etf_data(universe_dir, now.astimezone().date() if now.tzinfo else now.date()),
    }
    for key, job in jobs.items():
        try:
            sources[key] = {"available": True, **job()}
        except Exception as exc:  # noqa: BLE001 - one unreadable source must not hide the others
            sources[key] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        rule_values = rules()
    except Exception as exc:  # noqa: BLE001
        rule_values = {"error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "operation": OPERATION, "now": now.astimezone(timezone.utc).isoformat(), "sources": sources, "rules": rule_values}
