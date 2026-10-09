"""Read-only watchlist candidate discovery (no writes, no orders, no LLM).

Flow::

    UNIVERSE -> PRE-FILTER -> DATA AVAILABILITY -> ANALYSIS -> DISCOVERY SCORE -> DISCOVERED_CANDIDATE

``DISCOVERY != PROMOTE != ENTRY_READY != ORDER``.  A discovered candidate is only a
suggestion for the user; nothing here creates a security, a watchlist entry, a
strategy assignment, a campaign, a transaction, a BUY or an entry plan.

Nothing is evaluated twice with a second rule set.  New securities are loaded into a
throw-away in-memory SQLite database that has the production schema (the production
database is only read) and are then run through the existing engine unchanged:

* technical quality, SMA50/SMA200, performance, momentum, FX/EUR valuation:
  :func:`analysis_engine.build_analysis_snapshot`
* trend gate / data gate: :func:`swing_promotion.evaluate_swing_promotion`
* confidence: :func:`decision_engine._confidence`

``discovery_score = momentum_score * confidence`` is the same formula the portfolio
planner uses as ``entry_score``.  Ties break on a higher momentum score, then on the
symbol.  Fundamentals, valuation and events are **not fetched** for new securities:
those qualities are reported as the engine sees them (no cached data) and
``fundamentals_evaluated`` is ``False``.

Market data comes from an injected ``fetch_history`` callable, so this module performs
no network access itself.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from analysis_contracts import AvailabilityStatus
from analysis_engine import build_analysis_snapshot
from candidate_decision import BUY_SCORE_THRESHOLD
from decision_engine import _confidence
from fx_resolver import resolve_fx_rate
from strategy_config import DiscoveryConfig, StrategyConfig
from swing_promotion import DATA_INSUFFICIENT, KEEP_WATCHING, PROMOTE, evaluate_swing_promotion

DISCOVERY_READY = "DISCOVERY_READY"
DISCOVERY_WATCH = "DISCOVERY_WATCH"
DISCOVERY_DATA_INSUFFICIENT = "DISCOVERY_DATA_INSUFFICIENT"
DISCOVERY_REJECTED = "DISCOVERY_REJECTED"
DISCOVERY_STATUSES = (DISCOVERY_READY, DISCOVERY_WATCH, DISCOVERY_DATA_INSUFFICIENT, DISCOVERY_REJECTED)

# promotion reason -> discovery reason
_DATA_REASON = {
    "PROMOTION_REQUIRED_DATA_INCOMPLETE": "INSUFFICIENT_HISTORY",
    "PROMOTION_PRICE_UNAVAILABLE": "INSUFFICIENT_HISTORY",
    "PROMOTION_SMA50_UNAVAILABLE": "INSUFFICIENT_HISTORY",
    "PROMOTION_SMA200_UNAVAILABLE": "INSUFFICIENT_HISTORY",
    "PROMOTION_TECHNICAL_DATA_STALE": "MARKET_DATA_STALE",
    "PROMOTION_FX_RATE_MISSING": "FX_RATE_UNAVAILABLE",
    "PROMOTION_MARKET_DATA_MISSING": "MARKET_DATA_MISSING",
}
_WATCH_REASON = {
    "PROMOTION_PRICE_BELOW_OR_EQUAL_SMA50": "PRICE_BELOW_SMA50",
    "PROMOTION_PRICE_BELOW_OR_EQUAL_SMA200": "PRICE_BELOW_SMA200",
    "PROMOTION_SMA50_BELOW_OR_EQUAL_SMA200": "SMA50_BELOW_SMA200",
}

FetchHistory = Callable[[str], Optional[Mapping[str, Any]]]


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str  # Yahoo Finance symbol
    name: str
    market: str
    currency: Optional[str]  # expected quote currency; pre-filter hint only
    asset_type: str = "stock"


def load_universe(path: Path | str) -> tuple[dict[str, Any], list[UniverseEntry], list[str]]:
    """Read a universe file. Returns ``(meta, entries, warnings)``; duplicates keep the first entry."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = payload.get("entries")
    if not isinstance(raw, list) or not raw:
        raise ValueError("universe file needs a non-empty 'entries' list")
    entries: list[UniverseEntry] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for item in raw:
        symbol = str(item.get("symbol") or "").strip()
        name = str(item.get("name") or "").strip()
        market = str(item.get("market") or "").strip()
        if not symbol or not name or not market:
            raise ValueError(f"universe entry needs symbol, name and market: {item!r}")
        if symbol.upper() in seen:
            warnings.append(f"UNIVERSE_DUPLICATE_SKIPPED:{symbol}")
            continue
        seen.add(symbol.upper())
        currency = item.get("currency")
        entries.append(UniverseEntry(symbol, name, market, str(currency).strip() if currency else None, str(item.get("asset_type") or "stock").strip().lower()))
    meta = {"universe_id": payload.get("universe_id"), "version": payload.get("version"), "description": payload.get("description")}
    return meta, entries, warnings


def _fx_supported(conn: sqlite3.Connection, currency: Optional[str], as_of: str) -> bool:
    """A quote currency is supported iff the existing resolver can value it in EUR."""
    if not currency:
        return False
    resolution = resolve_fx_rate(conn, currency, "EUR", as_of)
    return resolution.quality.status in {AvailabilityStatus.AVAILABLE, AvailabilityStatus.NOT_APPLICABLE}


def _existing_securities(conn: sqlite3.Connection) -> dict[str, Any]:
    """Portfolio / watchlist membership by Yahoo symbol, plus known security ids (read-only)."""
    yahoo = conn.execute("SELECT id FROM data_sources WHERE name = 'Yahoo Finance'").fetchone()
    by_symbol: dict[str, int] = {}
    if yahoo is not None:
        for row in conn.execute("SELECT symbol, security_id FROM source_symbols WHERE source_id = ? AND symbol IS NOT NULL", (yahoo[0],)):
            by_symbol[str(row[0]).upper()] = int(row[1])
    portfolio = {int(r[0]) for r in conn.execute("SELECT security_id FROM positions WHERE shares > 0")}
    watchlist = {int(r[0]) for r in conn.execute("SELECT security_id FROM watchlist")}
    by_plain: dict[str, int] = {}
    for row in conn.execute("SELECT id, symbol FROM security WHERE symbol IS NOT NULL AND (exchange IS NULL OR exchange = 'US')"):
        by_plain.setdefault(str(row[1]).upper(), int(row[0]))
    return {"by_symbol": by_symbol, "portfolio": portfolio, "watchlist": watchlist, "by_plain": by_plain}


def _known_security_id(entry: UniverseEntry, known: Mapping[str, Any]) -> Optional[int]:
    found = known["by_symbol"].get(entry.symbol.upper())
    if found is not None:
        return found
    if entry.market == "US" and "." not in entry.symbol:
        return known["by_plain"].get(entry.symbol.upper())
    return None


def _excluded(entry: UniverseEntry, known_id: Optional[int], *reasons: str) -> dict[str, Any]:
    return {
        "security_id": known_id, "symbol": entry.symbol, "name": entry.name, "market": entry.market, "currency": entry.currency,
        "status": DISCOVERY_REJECTED, "reason_codes": list(reasons), "stage": "PRE_FILTER",
    }


def _ephemeral_connection(prod: sqlite3.Connection) -> sqlite3.Connection:
    """In-memory copy of the production *schema* plus the FX/reference rows (production is only read)."""
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    for (sql,) in prod.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND type IN ('table', 'index') "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
    ).fetchall():
        mem.execute(sql)
    present = {row[0] for row in prod.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in ("data_sources", "metadata", "fx_rates"):
        if table not in present:
            continue
        rows = prod.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            marks = ",".join("?" * len(rows[0]))
            mem.executemany(f"INSERT INTO {table} VALUES ({marks})", [tuple(r) for r in rows])
    return mem


def _iso_from_epoch(value: Any, fallback: str) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return fallback


def _load_candidate(mem: sqlite3.Connection, security_id: int, entry: UniverseEntry, history: Mapping[str, Any], currency: str, as_of: str) -> None:
    source = mem.execute("SELECT id FROM data_sources WHERE name = 'Yahoo Finance'").fetchone()
    source_id = source[0] if source is not None else None
    stamp = f"{as_of}T00:00:00+00:00"
    mem.execute(
        "INSERT INTO security(id, symbol, name, exchange, currency, asset_type, active) VALUES (?, ?, ?, ?, ?, 'stock', 1)",
        (security_id, entry.symbol, entry.name, entry.market, currency),
    )
    rows = list(history["rows"])
    mem.executemany(
        "INSERT INTO market_data(security_id, trade_date, open, high, low, close, adjusted_close, volume, currency, source_id, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(security_id, r["trade_date"], r.get("open"), r.get("high"), r.get("low"), r["close"], r.get("adjusted_close"), r.get("volume"), currency, source_id, stamp) for r in rows],
    )
    last = rows[-1]
    price = history.get("price") if history.get("price") is not None else last["close"]
    previous = rows[-2]["close"] if len(rows) >= 2 else history.get("previous_close")
    mem.execute(
        "INSERT INTO market_snapshot(security_id, as_of_at, price, previous_close, currency, source_id, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (security_id, _iso_from_epoch(history.get("price_time"), f"{last['trade_date']}T00:00:00+00:00"), price, previous, currency, source_id, stamp),
    )
    mem.execute("INSERT INTO watchlist(security_id, status) VALUES (?, 'WATCH')", (security_id,))


def _median_daily_value_eur(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], currency: str, as_of: str, window: int) -> Optional[float]:
    values = [float(r["volume"]) * float(r["close"]) for r in rows[-window:] if r.get("volume") is not None and r.get("close") is not None and float(r["volume"]) > 0]
    if not values:
        return None
    converted = resolve_fx_rate(conn, currency, "EUR", as_of).convert(statistics.median(values))
    return None if converted is None else float(converted)


def _candidate_base(entry: UniverseEntry, known_id: Optional[int], currency: Optional[str]) -> dict[str, Any]:
    return {
        "rank": None, "security_id": known_id, "symbol": entry.symbol, "name": entry.name, "market": entry.market, "currency": currency,
        "current_price": None, "current_price_eur": None, "momentum_score": None, "confidence": None, "discovery_score": None,
        "technical_quality": None, "fundamental_quality": None, "valuation_quality": None, "event_risk_quality": None,
        "sma50": None, "sma200": None, "perf_1m_pct": None, "perf_3m_pct": None, "perf_6m_pct": None,
        "median_daily_value_eur": None, "fundamentals_evaluated": False,
        "status": None, "reason_codes": [],
    }


def _classify(decision, momentum: Optional[float]) -> tuple[str, list[str]]:
    """Map the existing promotion result onto discovery statuses (no new trend rule)."""
    if decision.recommendation == DATA_INSUFFICIENT:
        return DISCOVERY_DATA_INSUFFICIENT, [_DATA_REASON.get(code, code) for code in decision.reasons]
    if decision.recommendation == PROMOTE:
        if momentum is not None and momentum >= BUY_SCORE_THRESHOLD:
            return DISCOVERY_READY, ["TREND_INTACT", "MOMENTUM_ABOVE_THRESHOLD"]
        return DISCOVERY_WATCH, ["MOMENTUM_BELOW_THRESHOLD"]
    if decision.recommendation == KEEP_WATCHING:
        if decision.current_price is not None and decision.sma200 is not None and float(decision.current_price) <= float(decision.sma200):
            return DISCOVERY_REJECTED, ["PRICE_BELOW_SMA200"]
        return DISCOVERY_WATCH, [_WATCH_REASON.get(code, code) for code in decision.reasons]
    return DISCOVERY_REJECTED, list(decision.reasons)


def _sort_key(candidate: Mapping[str, Any]):
    score = candidate["discovery_score"]
    momentum = candidate["momentum_score"]
    return (
        DISCOVERY_STATUSES.index(candidate["status"]),
        -(score if score is not None else -1e18),
        -(momentum if momentum is not None else -1e18),
        candidate["symbol"],
    )


def discover_watchlist_candidates(
    conn: sqlite3.Connection,
    universe: Sequence[UniverseEntry],
    fetch_history: FetchHistory,
    *,
    as_of: str,
    config: Optional[StrategyConfig] = None,
    limit: Optional[int] = None,
    universe_meta: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Run the discovery. ``conn`` (production DB) is only read; the result is a plain dict."""
    cfg = (config or StrategyConfig()).discovery
    known = _existing_securities(conn)
    warnings: list[str] = []
    excluded: list[dict[str, Any]] = []
    pending: list[tuple[UniverseEntry, Optional[int]]] = []

    # ---- PRE-FILTER (before any market-data request)
    for entry in universe:
        known_id = _known_security_id(entry, known)
        security_id = known["by_symbol"].get(entry.symbol.upper())
        if entry.asset_type != "stock":
            excluded.append(_excluded(entry, known_id, "UNSUPPORTED_SECURITY_TYPE"))
        elif security_id is not None and security_id in known["portfolio"]:
            excluded.append(_excluded(entry, known_id, "ALREADY_IN_PORTFOLIO"))
        elif security_id is not None and security_id in known["watchlist"]:
            excluded.append(_excluded(entry, known_id, "ALREADY_ON_WATCHLIST"))
        elif not _fx_supported(conn, entry.currency, as_of):
            excluded.append(_excluded(entry, known_id, "UNSUPPORTED_CURRENCY"))
        else:
            pending.append((entry, known_id))
    if limit is not None and len(pending) > limit:
        warnings.append(f"UNIVERSE_LIMITED_TO_{limit}_OF_{len(pending)}_ANALYZABLE")
        pending = pending[:limit]

    # ---- DATA AVAILABILITY
    candidates: list[dict[str, Any]] = []
    loaded: list[tuple[int, dict[str, Any], Mapping[str, Any], str]] = []
    mem = _ephemeral_connection(conn)
    try:
        next_id = 1
        for entry, known_id in pending:
            candidate = _candidate_base(entry, known_id, entry.currency)
            try:
                history = fetch_history(entry.symbol)
            except Exception as exc:  # one failing symbol never stops the run
                history = None
                warnings.append(f"FETCH_ERROR:{entry.symbol}:{type(exc).__name__}")
            rows = list((history or {}).get("rows") or [])
            if not history or not rows:
                candidate.update(status=DISCOVERY_DATA_INSUFFICIENT, reason_codes=["NO_MARKET_DATA"])
                candidates.append(candidate)
                continue
            currency = history.get("currency") or entry.currency
            candidate["currency"] = currency
            instrument = history.get("instrument_type")
            if instrument is not None and str(instrument).upper() != "EQUITY":
                candidate.update(status=DISCOVERY_REJECTED, reason_codes=["UNSUPPORTED_SECURITY_TYPE"])
            elif not currency:
                candidate.update(status=DISCOVERY_DATA_INSUFFICIENT, reason_codes=["CURRENCY_UNAVAILABLE"])
            elif not _fx_supported(conn, currency, as_of):
                candidate.update(status=DISCOVERY_REJECTED, reason_codes=["UNSUPPORTED_CURRENCY"])
            else:
                last_close = float(rows[-1]["close"])
                price_eur = resolve_fx_rate(conn, currency, "EUR", as_of).convert(last_close)
                liquidity = _median_daily_value_eur(conn, rows, currency, as_of, cfg.liquidity_window_days)
                candidate["median_daily_value_eur"] = liquidity
                if price_eur is not None and price_eur < cfg.min_price_eur:
                    candidate.update(status=DISCOVERY_REJECTED, reason_codes=["PRICE_BELOW_MINIMUM"])
                elif liquidity is None:
                    candidate.update(status=DISCOVERY_DATA_INSUFFICIENT, reason_codes=["LIQUIDITY_DATA_UNAVAILABLE"])
                elif liquidity < cfg.min_median_daily_value_eur:
                    candidate.update(status=DISCOVERY_REJECTED, reason_codes=["LOW_LIQUIDITY"])
                else:
                    sid = next_id
                    next_id += 1
                    _load_candidate(mem, sid, entry, history, currency, as_of)
                    loaded.append((sid, candidate, history, currency))
                    continue
            candidates.append(candidate)

        # ---- ANALYSIS (existing engine, unchanged) + DISCOVERY SCORE
        cache: dict[str, bool] = {}
        for sid, candidate, _history, _currency in loaded:
            decision = evaluate_swing_promotion(mem, sid, as_of=as_of, table_exists_cache=cache)
            snapshot = build_analysis_snapshot(sid, as_of=as_of, connection=mem, table_exists_cache=cache)
            confidence = _confidence(snapshot)
            momentum = decision.momentum_score
            status, reasons = _classify(decision, momentum)
            candidate.update(
                current_price=decision.current_price, current_price_eur=decision.current_price_eur, momentum_score=momentum, confidence=confidence,
                discovery_score=round(momentum * confidence, 4) if momentum is not None else None,
                technical_quality=decision.technical_quality, fundamental_quality=decision.fundamental_quality,
                valuation_quality=decision.valuation_quality, event_risk_quality=decision.event_risk_quality,
                sma50=decision.sma50, sma200=decision.sma200, perf_1m_pct=decision.perf_1m_pct, perf_3m_pct=decision.perf_3m_pct, perf_6m_pct=decision.perf_6m_pct,
                status=status, reason_codes=reasons,
            )
            candidates.append(candidate)
    finally:
        mem.close()

    candidates.sort(key=_sort_key)
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    count = lambda status: sum(1 for c in candidates if c["status"] == status)
    result = {
        "status": "AVAILABLE",
        "read_only": True,
        "watchlist_written": False,
        "orders_created": False,
        "evaluation_as_of": as_of,
        "universe": {**(dict(universe_meta) if universe_meta else {}), "size": len(universe)},
        "universe_size": len(universe),
        "prefiltered_count": len(excluded),
        "analyzed_count": len(candidates),
        "discovery_ready_count": count(DISCOVERY_READY),
        "discovery_watch_count": count(DISCOVERY_WATCH),
        "insufficient_count": count(DISCOVERY_DATA_INSUFFICIENT),
        "rejected_count": count(DISCOVERY_REJECTED),
        "candidates": candidates,
        "excluded_candidates": excluded,
        "methodology": {
            "chain": "DISCOVERY != PROMOTE != ENTRY_READY != ORDER",
            "engine": "analysis_engine.build_analysis_snapshot + swing_promotion.evaluate_swing_promotion (unchanged), run on an in-memory copy of the production schema",
            "discovery_score": "momentum_score * decision_engine._confidence (same formula as the planner's entry_score)",
            "ranking": ["status (READY, WATCH, DATA_INSUFFICIENT, REJECTED)", "discovery_score (higher first)", "momentum_score (higher first)", "symbol (ascending)"],
            "ready": f"promotion trend gate passed (price > SMA50 > SMA200) and momentum_score >= {BUY_SCORE_THRESHOLD} (candidate_decision.BUY_SCORE_THRESHOLD)",
            "pre_filter": {
                "min_price_eur": cfg.min_price_eur,
                "min_median_daily_value_eur": cfg.min_median_daily_value_eur,
                "liquidity_window_days": cfg.liquidity_window_days,
                "currency": "supported iff the existing FX resolver can value it in EUR",
                "history": "technical quality AVAILABLE (existing rule: >= 200 price points, data not older than market_data_max_age_days)",
            },
            "fundamentals_evaluated": False,
            "fundamentals_note": "Fundamentals, valuation and events are not fetched for new securities; their quality shows the engine's view without cached data.",
        },
        "warnings": warnings,
    }
    result["rendered_de"] = render_discovery_de(result)
    return result


def render_discovery_de(result: Mapping[str, Any], *, top: int = 20) -> str:
    """Deterministic German rendering of an already computed discovery result (formatting only)."""
    def num(value: Optional[float], digits: int = 2) -> str:
        return "-" if value is None else f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    lines = [
        "Watchlist-Kandidaten-Discovery (read-only; DISCOVERY ist weder PROMOTE noch Kauf; keine automatische Watchlist-Aufnahme)",
        f"Stand: {result['evaluation_as_of']} | Universum {result['universe_size']} | vorgefiltert {result['prefiltered_count']} | analysiert {result['analyzed_count']}",
        f"READY {result['discovery_ready_count']} | WATCH {result['discovery_watch_count']} | DATA_INSUFFICIENT {result['insufficient_count']} | REJECTED {result['rejected_count']}",
        "",
        f"### Top {min(top, len(result['candidates']))} Kandidaten",
        "| Rank | Symbol | Name | Discovery-Score | Status | Grund |",
        "|---|---|---|---|---|---|",
    ]
    for c in result["candidates"][:top]:
        lines.append(f"| {c['rank']} | {c['symbol']} | {c['name']} | {num(c['discovery_score'])} | {c['status']} | {', '.join(c['reason_codes'])} |")
    if not result["candidates"]:
        lines.append("| - | - | - | - | - | keine analysierten Kandidaten |")
    reasons: dict[str, int] = {}
    for item in result["excluded_candidates"]:
        for code in item["reason_codes"]:
            reasons[code] = reasons.get(code, 0) + 1
    lines.append("")
    lines.append("Vorgefiltert (nicht analysiert): " + (", ".join(f"{code} {count}" for code, count in sorted(reasons.items())) if reasons else "keine"))
    lines.append("Fundamentals, Bewertung und Events wurden für neue Titel nicht ausgewertet. Die Aufnahme in die Watchlist entscheidet der Nutzer.")
    return "\n".join(lines)
