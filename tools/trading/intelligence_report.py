"""Structured, versioned report of a market-intelligence run (file I/O only: no network, no database).

``Collect-TradingMarketIntelligence.py`` writes the report after a *completely successful* run;
the MCP reader ``get_market_intelligence`` only reads the last finished report.  Neither this
module nor the reader starts a news scan, fetches anything from the web, or touches the database.

    latest.json                                     last valid report (replaced atomically)
    market-intelligence-YYYYMMDD-HHMMSS.json        optional history copy (UTC)

The atomic writer is the one of the discovery report (temporary file -> re-read + validate ->
replace; the history copy is written first so a failure there leaves ``latest.json`` untouched).
The report holds headlines and short summaries only, never article texts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from discovery_report import ReportError, _atomic_write  # noqa: F401  (ReportError is re-exported)
from market_intelligence import CATEGORIES, IMPACTS, IMPORTANCES, MACRO_CATEGORIES, SOURCE_TYPES
from strategy_config import MarketIntelligenceConfig

SCHEMA_VERSION = 1
LATEST_NAME = "latest.json"
HISTORY_PREFIX = "market-intelligence-"
SCOPES = ("ALL", "PORTFOLIO", "WATCHLIST", "DISCOVERY", "MACRO")
EVENT_FIELDS = (
    "event_id", "published_at", "event_date", "source", "source_type", "source_url", "category", "headline", "summary",
    "affected_symbols", "affected_portfolio_symbols", "affected_watchlist_symbols", "affected_discovery_symbols",
    "affected_sectors", "affected_regions", "portfolio_region_exposure", "importance", "impact", "impact_basis", "confidence",
    "reason_codes", "fetched_at",
)
COMPACT_FIELDS = ("published_at", "importance", "category", "headline", "source", "impact", "affected_symbols", "affected_sectors", "affected_regions")
METADATA_KEYS = (
    "schema_version", "generated_at", "evaluation_as_of", "source_counts", "event_count", "high_count", "medium_count", "low_count",
    "portfolio_relevant_count", "watchlist_relevant_count", "discovery_relevant_count", "macro_event_count", "deterministic", "events_sha256",
)
DEFAULT_LIMIT = 20
MAX_LIMIT_COMPACT = 50
RESPONSE_CHAR_BUDGET = 34_000  # the MCP server refuses results above 40,000 characters (RESULT_TOO_LARGE)
MAX_LIMIT_DETAIL = 25
MAX_SYMBOLS_COMPACT = 8


def _digest(events: Sequence[Any]) -> str:
    payload = json.dumps(events, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_report(result: Mapping[str, Any], *, generated_at: datetime, collection: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Project a market-intelligence result onto the report schema."""
    events = [{key: event.get(key) for key in EVENT_FIELDS} for event in result["events"]]
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "evaluation_as_of": result["evaluation_as_of"],
        "lookback_days": result["lookback_days"],
        "lookback_since": result["lookback_since"],
        "source_counts": dict(result["source_counts"]),
        "event_count": len(events),
        "high_count": result["high_count"],
        "medium_count": result["medium_count"],
        "low_count": result["low_count"],
        "portfolio_relevant_count": result["portfolio_relevant_count"],
        "watchlist_relevant_count": result["watchlist_relevant_count"],
        "discovery_relevant_count": result["discovery_relevant_count"],
        "macro_event_count": result["macro_event_count"],
        "deterministic": True,
        "events_sha256": _digest(events),
        "collection": dict(collection or {}),
        "methodology": result["methodology"],
        "read_only": True,
        "orders_created": False,
        "signals_changed": False,
        "warnings": list(result.get("warnings") or []),
    }
    return {"schema_version": SCHEMA_VERSION, "metadata": metadata, "events": events}


def validate_report(report: Any) -> list[str]:
    if not isinstance(report, dict):
        return ["report is not an object"]
    problems: list[str] = []
    if report.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {report.get('schema_version')!r}")
    metadata, events = report.get("metadata"), report.get("events")
    if not isinstance(metadata, dict):
        return [*problems, "metadata missing"]
    if not isinstance(events, list):
        return [*problems, "events must be a list"]
    problems += [f"metadata.{key} missing" for key in METADATA_KEYS if key not in metadata]
    if problems:
        return problems
    if metadata["deterministic"] is not True:
        problems.append("metadata.deterministic must be true")
    for index, event in enumerate(events):
        if not isinstance(event, dict) or any(key not in event for key in EVENT_FIELDS):
            problems.append(f"events[{index}] misses required fields")
            continue
        if event["category"] not in CATEGORIES:
            problems.append(f"events[{index}] has an invalid category {event['category']!r}")
        if event["importance"] not in IMPORTANCES:
            problems.append(f"events[{index}] has an invalid importance {event['importance']!r}")
        if event["impact"] not in IMPACTS:
            problems.append(f"events[{index}] has an invalid impact {event['impact']!r}")
        if event["source_type"] not in SOURCE_TYPES:
            problems.append(f"events[{index}] has an invalid source_type {event['source_type']!r}")
        if len(str(event.get("summary") or "")) > 240 or "full_text" in event or "body" in event:
            problems.append(f"events[{index}] contains an article text")
    if problems:
        return problems
    if metadata["event_count"] != len(events):
        problems.append("event_count does not match events")
    for importance, key in (("HIGH", "high_count"), ("MEDIUM", "medium_count"), ("LOW", "low_count")):
        if metadata[key] != sum(1 for e in events if e["importance"] == importance):
            problems.append(f"{key} does not match events")
    for field, key in (("affected_portfolio_symbols", "portfolio_relevant_count"), ("affected_watchlist_symbols", "watchlist_relevant_count"), ("affected_discovery_symbols", "discovery_relevant_count")):
        if metadata[key] != sum(1 for e in events if e[field]):
            problems.append(f"{key} does not match events")
    if metadata["macro_event_count"] != sum(1 for e in events if e["category"] in MACRO_CATEGORIES):
        problems.append("macro_event_count does not match events")
    if sum(metadata["source_counts"].values()) != len(events):
        problems.append("source_counts do not add up to event_count")
    if metadata["events_sha256"] != _digest(events):
        problems.append("events_sha256 does not match the content")
    return problems


def write_report(report: Mapping[str, Any], directory: Path | str, *, history: bool = True) -> dict[str, Optional[str]]:
    problems = validate_report(dict(report))
    if problems:
        raise ReportError("report failed validation: " + "; ".join(problems[:5]))
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n"  # compact: ~2,000 events
    history_path: Optional[Path] = None
    if history:
        stamp = datetime.fromisoformat(report["metadata"]["generated_at"]).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
        history_path = target / f"{HISTORY_PREFIX}{stamp}.json"
        _atomic_write(history_path, payload, validate_report)
    latest = target / LATEST_NAME
    _atomic_write(latest, payload, validate_report)
    return {"latest": str(latest), "history": str(history_path) if history_path else None}


def read_latest_report(directory: Path | str) -> tuple[Optional[dict[str, Any]], Optional[str], list[str]]:
    path = Path(directory) / LATEST_NAME
    if not path.is_file():
        return None, "NO_MARKET_INTELLIGENCE_REPORT", []
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "REPORT_INVALID", [f"{type(exc).__name__}: {exc}"]
    problems = validate_report(report)
    if problems:
        return None, "REPORT_INVALID", problems
    return report, None, []


def _freshness(metadata: Mapping[str, Any], now: datetime, max_age_hours: int) -> dict[str, Any]:
    generated = datetime.fromisoformat(metadata["generated_at"])
    age = (now - generated).total_seconds() / 3600.0
    return {
        "generated_at": metadata["generated_at"], "age_hours": round(age, 2), "evaluation_as_of": metadata["evaluation_as_of"],
        "max_age_hours": max_age_hours, "max_age_rule": "MarketIntelligenceConfig.report_max_age_hours (news are time critical)",
        "status": "REPORT_STALE" if age > max_age_hours else "FRESH",
    }


def _symbol_matches(event: Mapping[str, Any], wanted: str) -> bool:
    symbols = [s.upper() for s in event["affected_symbols"]]
    return wanted in symbols or any(s.split(".")[0] == wanted for s in symbols)


def query_report(
    report: Mapping[str, Any],
    *,
    scope: str = "ALL",
    category: Optional[str] = None,
    importance: Optional[str] = None,
    symbol: Optional[str] = None,
    sector: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    detail: bool = False,
    now: Optional[datetime] = None,
    max_age_hours: Optional[int] = None,
    slim: bool = True,
) -> dict[str, Any]:
    """Filter an already loaded report (pure; never starts a scan and never fetches anything).

    ``detail=False`` returns the slim answer meant for the MCP reader (events and a short header only); ``slim=False``
    keeps the broad compact answer with ``rendered_de`` that the collector CLI prints.  ``detail=True`` is always complete."""
    now = now or datetime.now(timezone.utc)
    max_age = MarketIntelligenceConfig().report_max_age_hours if max_age_hours is None else max_age_hours
    fail = lambda error, message: {"ok": False, "operation": "get_market_intelligence", "error": error, "message": message}
    wanted_scope = str(scope or "ALL").strip().upper()
    if wanted_scope not in SCOPES:
        return fail("INVALID_SCOPE", f"scope must be one of {', '.join(SCOPES)}")
    wanted_category = str(category).strip().upper() if category and str(category).strip() else None
    if wanted_category and wanted_category not in CATEGORIES:
        return fail("INVALID_CATEGORY", f"category must be one of {', '.join(CATEGORIES)}")
    wanted_importance = str(importance).strip().upper() if importance and str(importance).strip() else None
    if wanted_importance and wanted_importance not in IMPORTANCES:
        return fail("INVALID_IMPORTANCE", f"importance must be one of {', '.join(IMPORTANCES)}")
    wanted_symbol = symbol.strip().upper() if symbol and symbol.strip() else None
    wanted_sector = sector.strip().upper() if sector and sector.strip() else None
    metadata = report["metadata"]
    matching = []
    for event in report["events"]:
        if wanted_scope == "PORTFOLIO" and not event["affected_portfolio_symbols"]:
            continue
        if wanted_scope == "WATCHLIST" and not event["affected_watchlist_symbols"]:
            continue
        if wanted_scope == "DISCOVERY" and not event["affected_discovery_symbols"]:
            continue
        if wanted_scope == "MACRO" and event["category"] not in MACRO_CATEGORIES:
            continue
        if wanted_category and event["category"] != wanted_category:
            continue
        if wanted_importance and event["importance"] != wanted_importance:
            continue
        if wanted_symbol and not _symbol_matches(event, wanted_symbol):
            continue
        if wanted_sector and wanted_sector not in event["affected_sectors"]:
            continue
        matching.append(event)
    cap = MAX_LIMIT_DETAIL if detail else MAX_LIMIT_COMPACT
    applied = max(1, min(int(limit), cap))
    freshness = _freshness(metadata, now, max_age)
    if not detail and slim:
        active = {k: v for k, v in (("scope", wanted_scope if wanted_scope != "ALL" else None), ("category", wanted_category), ("importance", wanted_importance),
                                    ("symbol", wanted_symbol), ("sector", wanted_sector)) if v}
        return _slim_response(matching, applied, freshness, active, wanted_symbol is not None)
    rows = []
    for event in matching[:applied]:
        if detail:
            rows.append({key: event[key] for key in EVENT_FIELDS})
        else:
            row = {key: event[key] for key in COMPACT_FIELDS}
            if len(row["affected_symbols"]) > MAX_SYMBOLS_COMPACT:
                row["affected_symbols_total"] = len(row["affected_symbols"])
                row["affected_symbols"] = row["affected_symbols"][:MAX_SYMBOLS_COMPACT]
            for key in ("affected_portfolio_symbols", "affected_watchlist_symbols", "affected_discovery_symbols", "portfolio_region_exposure"):
                if event[key]:
                    row[key] = event[key][:MAX_SYMBOLS_COMPACT]
            rows.append(row)
    counts = {key: metadata[key] for key in ("event_count", "high_count", "medium_count", "low_count", "portfolio_relevant_count", "watchlist_relevant_count", "discovery_relevant_count", "macro_event_count")}
    response = {
        "ok": True, "operation": "get_market_intelligence", "status": "AVAILABLE",
        "report_stale": freshness["status"] == "REPORT_STALE", "freshness": freshness,
        "counts": counts, "source_counts": metadata["source_counts"], "collection": metadata.get("collection"),
        "filter": {"scope": wanted_scope, "category": wanted_category, "importance": wanted_importance, "symbol": wanted_symbol, "sector": wanted_sector, "limit_requested": limit, "limit_applied": applied, "detail": bool(detail)},
        "matching": len(matching), "returned": len(rows), "truncated": len(matching) > len(rows), "events": rows,
        "notes": [
            "The MCP reader only reads the last finished report; it never starts a news scan, fetches from the web or touches the database.",
            "NEWS is not a signal, not a BUY and not a SELL: events are context and warnings only and change no decision, ranking or sizing. Impact is UNKNOWN unless a primary filing states an adverse fact.",
        ],
    }
    if freshness["status"] == "REPORT_STALE":
        response["notes"].insert(0, f"REPORT_STALE: the report is {freshness['age_hours']} hours old (limit {max_age}); run Collect-TradingMarketIntelligence.py again.")
    if detail:
        response["methodology"] = metadata.get("methodology")
    response["rendered_de"] = render_report_de(response)
    # size guard: the events are sorted by importance, so the least important ones are dropped first
    while len(rows) > 1 and len(json.dumps(response, ensure_ascii=False)) > RESPONSE_CHAR_BUDGET:
        size = len(json.dumps(response, ensure_ascii=False))
        drop = max(1, int(len(rows) * (size - RESPONSE_CHAR_BUDGET) / size) + 1)
        del rows[max(1, len(rows) - drop):]
        response.update(returned=len(rows), truncated=True)
        response["rendered_de"] = render_report_de(response)
        if "RESPONSE_TRIMMED_TO_SIZE_BUDGET" not in response["notes"][-1]:
            response["notes"].append("RESPONSE_TRIMMED_TO_SIZE_BUDGET: fewer events returned than requested to stay below the MCP result limit; narrow the filter (symbol, category, importance, scope) to see the rest.")
    return response


SLIM_SYMBOLS = 6
_GENERIC_REASON_CODES = {"CLASSIFIED_BY_HEADLINE_KEYWORD"}
_LINK_REASON_CODES = {"HEADLINE_NAMES_COMPANY", "SECTOR_SCOPED_EVENT"}


def event_link(event: Mapping[str, Any]) -> str:
    """How an event relates to the symbol it was found for (derived from the stored classification, nothing new is decided).

    DIRECT: a SEC filing, or a headline that names the company and the event concerns that one symbol.  SHARED: the headline names
    a company but the event is attached to several symbols.  SECTOR: mapped to the symbol only through its sector.  LOOSE: attached
    by the provider without the headline naming a company."""
    reasons = event["reason_codes"]
    if event["source_type"] == "SEC_FILING":
        return "DIRECT"
    if "SECTOR_SCOPED_EVENT" in reasons:
        return "SECTOR"
    if "HEADLINE_NAMES_COMPANY" in reasons:
        return "DIRECT" if len(event["affected_symbols"]) == 1 else "SHARED"
    return "LOOSE"


def _slim_event(event: Mapping[str, Any], symbol_query: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"published_at": str(event["published_at"])[:16], "importance": event["importance"], "category": event["category"],
                           "headline": event["headline"], "source": event["source"], "impact": event["impact"]}
    skip = _GENERIC_REASON_CODES | (_LINK_REASON_CODES if symbol_query else set())
    reasons = [c for c in event["reason_codes"] if c not in skip][:3]
    if symbol_query:
        row["link"] = event_link(event)
    else:
        symbols = event["affected_symbols"]
        if symbols:
            row["affected_symbols"] = symbols[:SLIM_SYMBOLS]
            if len(symbols) > SLIM_SYMBOLS:
                row["affected_symbols_total"] = len(symbols)
        if event["affected_sectors"]:
            row["affected_sectors"] = event["affected_sectors"]
    if reasons:
        row["reason_codes"] = reasons
    if event["affected_regions"]:
        row["affected_regions"] = event["affected_regions"]
    if event["portfolio_region_exposure"]:
        row["portfolio_region_exposure"] = event["portfolio_region_exposure"]
    return row


def _slim_response(matching: Sequence[Mapping[str, Any]], applied: int, freshness: Mapping[str, Any], active: Mapping[str, Any], symbol_query: bool) -> dict[str, Any]:
    """The slim answer: short freshness, the filter, importance counts of the matching events and the events - no rendered text,
    no source statistics, no collection details, no methodology, no notes."""
    rows = [_slim_event(e, symbol_query) for e in matching[:applied]]
    stale = freshness["status"] == "REPORT_STALE"
    response: dict[str, Any] = {
        "ok": True, "operation": "get_market_intelligence", "status": "AVAILABLE", "report_stale": stale,
        "freshness": {"status": freshness["status"], "generated_at": str(freshness["generated_at"])[:16], "age_hours": freshness["age_hours"], "evaluation_as_of": freshness["evaluation_as_of"]},
    }
    if active:
        response["filter"] = dict(active)
    counts = {level: sum(e["importance"] == level for e in matching) for level in IMPORTANCES}
    response.update(matching=len(matching), importance=counts, returned=len(rows), truncated=len(matching) > len(rows), events=rows)
    hints = []
    if stale:
        hints.append(f"REPORT_STALE: the report is {freshness['age_hours']} hours old (limit {freshness['max_age_hours']}); run Collect-TradingMarketIntelligence.py again")
    if response["truncated"]:
        hints.append(f"{len(matching) - len(rows)} more events match; raise limit (max {MAX_LIMIT_COMPACT}) or narrow importance/category/symbol/scope")
    if hints:
        response["hint"] = "; ".join(hints)
    while len(rows) > 1 and len(json.dumps(response, ensure_ascii=False)) > RESPONSE_CHAR_BUDGET:
        current = len(json.dumps(response, ensure_ascii=False))
        del rows[max(1, len(rows) - max(1, int(len(rows) * (current - RESPONSE_CHAR_BUDGET) / current) + 1)):]
        response.update(returned=len(rows), truncated=True)
        response["hint"] = "RESPONSE_TRIMMED_TO_SIZE_BUDGET: fewer events returned to stay below the MCP result limit; narrow importance/category/symbol/scope"
    return response


def unavailable(reason: str, directory: Path | str, problems: Optional[Sequence[str]] = None) -> dict[str, Any]:
    return {
        "ok": True, "operation": "get_market_intelligence", "status": "UNAVAILABLE", "reason": reason,
        "report_path": str(Path(directory) / LATEST_NAME), "problems": list(problems or []),
        "hint": "Run tools\\Collect-TradingMarketIntelligence.py explicitly (read-only); the MCP reader never starts a news scan.",
    }


def render_report_de(response: Mapping[str, Any], *, fresh_run: bool = False) -> str:
    fresh, counts = response["freshness"], response["counts"]
    origin = "soeben neu erzeugter Report" if fresh_run else "gelesen aus dem letzten fertigen Report; kein neuer Lauf"
    lines = [
        f"Market Intelligence ({origin}; Stand {fresh['evaluation_as_of']}, Report {fresh['generated_at']}, Alter {fresh['age_hours']} h, {fresh['status']})",
        f"Events {counts['event_count']} | HIGH {counts['high_count']} | MEDIUM {counts['medium_count']} | LOW {counts['low_count']} | Depot {counts['portfolio_relevant_count']} | Watchlist {counts['watchlist_relevant_count']} | Discovery {counts['discovery_relevant_count']} | Makro {counts['macro_event_count']}",
        "",
        f"### {response['returned']} von {response['matching']} ({response['filter']['scope']})",
        "| Zeit (UTC) | Wichtigkeit | Kategorie | Schlagzeile | Quelle | Impact | Betrifft |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in response["events"]:
        who = ", ".join(e.get("affected_portfolio_symbols") or e.get("affected_symbols") or e.get("affected_regions") or []) or "-"
        lines.append(f"| {e['published_at'][:16].replace('T', ' ')} | {e['importance']} | {e['category']} | {str(e['headline'])[:110]} | {e['source']} | {e['impact']} | {who} |")
    if not response["events"]:
        lines.append("| - | - | - | keine Treffer | - | - | - |")
    lines += ["", "News sind kein Signal, kein Kauf und kein Verkauf; sie ändern keine Entscheidung. Impact ist UNKNOWN, sofern keine Primärquelle eine belastende Tatsache nennt."]
    return "\n".join(lines)
