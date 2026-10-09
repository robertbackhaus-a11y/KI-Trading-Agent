"""Structured, versioned report of a candidate-discovery run (file I/O only: no network, no database).

``Discover-TradingCandidates.py`` writes the report after a *completely successful* run;
the MCP reader ``get_candidate_discovery`` only reads the last finished report.  Neither
this module nor the reader starts a discovery run, calls a data provider, or touches the
database.

Files in the report directory::

    latest.json                                  last valid report (replaced atomically)
    candidate-discovery-YYYYMMDD-HHMMSS.json     optional history copy (UTC)

``latest.json`` is replaced only after the new report was written to a temporary file in
the same directory, re-read and validated (schema + digest); an aborted or invalid run
leaves the previous report untouched.  The report holds no raw price series.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from strategy_config import DataQualityConfig

SCHEMA_VERSION = 1
LATEST_NAME = "latest.json"
HISTORY_PREFIX = "candidate-discovery-"
STATUSES = ("DISCOVERY_READY", "DISCOVERY_WATCH", "DISCOVERY_DATA_INSUFFICIENT", "DISCOVERY_REJECTED")
STATUS_ALIASES = {"READY": "DISCOVERY_READY", "WATCH": "DISCOVERY_WATCH", "DATA_INSUFFICIENT": "DISCOVERY_DATA_INSUFFICIENT", "INSUFFICIENT": "DISCOVERY_DATA_INSUFFICIENT", "REJECTED": "DISCOVERY_REJECTED"}
FILTER_VALUES = ("ALL", "EXCLUDED", *STATUSES)

RESULT_FIELDS = (
    "rank", "symbol", "name", "market", "currency", "current_price", "current_price_eur", "momentum_score", "discovery_score", "confidence",
    "technical_quality", "fundamental_quality", "valuation_quality", "event_risk_quality", "status", "reason_codes",
    "sma50", "sma200", "perf_1m_pct", "perf_3m_pct", "perf_6m_pct", "median_daily_value_eur", "fundamentals_evaluated", "existing_security_id",
)
COMPACT_FIELDS = ("rank", "symbol", "name", "discovery_score", "status", "reason_codes")
EXCLUDED_FIELDS = ("symbol", "name", "market", "currency", "reason_codes", "existing_security_id")
METADATA_KEYS = (
    "schema_version", "generated_at", "evaluation_as_of", "universe_name", "universe_size", "analyzed_count", "excluded_count",
    "discovery_ready_count", "discovery_watch_count", "discovery_data_insufficient_count", "discovery_rejected_count",
    "source", "deterministic", "results_sha256",
)
COUNT_BY_STATUS = {
    "DISCOVERY_READY": "discovery_ready_count",
    "DISCOVERY_WATCH": "discovery_watch_count",
    "DISCOVERY_DATA_INSUFFICIENT": "discovery_data_insufficient_count",
    "DISCOVERY_REJECTED": "discovery_rejected_count",
}
# size guards of the reader (the MCP adapter refuses results above 40,000 characters)
DEFAULT_LIMIT = 20
MAX_LIMIT_COMPACT = 150
MAX_LIMIT_DETAIL = 40


class ReportError(ValueError):
    """The report is malformed or could not be written safely."""


def _digest(results: Sequence[Any], excluded: Sequence[Any]) -> str:
    payload = json.dumps({"results": results, "excluded": excluded}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_report(
    result: Mapping[str, Any],
    *,
    generated_at: datetime,
    fetch_stats: Optional[Mapping[str, Any]] = None,
    cache_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Project a discovery result onto the report schema (no raw price data)."""
    results = []
    for candidate in result["candidates"]:
        row = {key: candidate.get(key) for key in RESULT_FIELDS if key != "existing_security_id"}
        row["existing_security_id"] = candidate.get("security_id")
        results.append(row)
    excluded = []
    for item in result["excluded_candidates"]:
        row = {key: item.get(key) for key in EXCLUDED_FIELDS if key != "existing_security_id"}
        row["existing_security_id"] = item.get("security_id")
        excluded.append(row)
    universe = result.get("universe") or {}
    stats = dict(fetch_stats or {})
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "evaluation_as_of": result["evaluation_as_of"],
        "universe_name": universe.get("universe_id"),
        "universe_version": universe.get("version"),
        "universe_size": result["universe_size"],
        "analyzed_count": result["analyzed_count"],
        "excluded_count": len(excluded),
        "discovery_ready_count": result["discovery_ready_count"],
        "discovery_watch_count": result["discovery_watch_count"],
        "discovery_data_insufficient_count": result["insufficient_count"],
        "discovery_rejected_count": result["rejected_count"],
        "source": {
            "market_data_provider": "Yahoo Finance chart API (existing market-data helpers)",
            "fetched_count": stats.get("fetched"),
            "cache_hit_count": stats.get("cache_hits"),
            "failed_count": stats.get("failed"),
            "cache_used": bool(cache_dir),
        },
        "deterministic": True,
        "results_sha256": _digest(results, excluded),
        "pre_filter": (result.get("methodology") or {}).get("pre_filter"),
        "methodology": {k: v for k, v in (result.get("methodology") or {}).items() if k != "pre_filter"},
        "fundamentals_evaluated": False,
        "read_only": True,
        "watchlist_written": False,
        "orders_created": False,
        "warnings": list(result.get("warnings") or []),
    }
    return {"schema_version": SCHEMA_VERSION, "metadata": metadata, "results": results, "excluded": excluded}


def validate_report(report: Any) -> list[str]:
    """Return a list of problems; an empty list means the report is valid."""
    if not isinstance(report, dict):
        return ["report is not an object"]
    problems: list[str] = []
    if report.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {report.get('schema_version')!r}")
    metadata, results, excluded = report.get("metadata"), report.get("results"), report.get("excluded")
    if not isinstance(metadata, dict):
        return [*problems, "metadata missing"]
    if not isinstance(results, list) or not isinstance(excluded, list):
        return [*problems, "results/excluded must be lists"]
    problems += [f"metadata.{key} missing" for key in METADATA_KEYS if key not in metadata]
    if problems:
        return problems
    if metadata["deterministic"] is not True:
        problems.append("metadata.deterministic must be true")
    for index, row in enumerate(results):
        if not isinstance(row, dict) or any(key not in row for key in RESULT_FIELDS):
            problems.append(f"results[{index}] misses required fields")
            continue
        if row["status"] not in STATUSES:
            problems.append(f"results[{index}] has an invalid status {row['status']!r}")
        if row["rank"] != index + 1:
            problems.append(f"results[{index}] has rank {row['rank']!r}, expected {index + 1}")
        if "rows" in row or "prices" in row:
            problems.append(f"results[{index}] contains raw price data")
    for index, row in enumerate(excluded):
        if not isinstance(row, dict) or "symbol" not in row or not isinstance(row.get("reason_codes"), list) or not row["reason_codes"]:
            problems.append(f"excluded[{index}] needs a symbol and reason_codes")
    if problems:
        return problems
    if metadata["analyzed_count"] != len(results):
        problems.append("analyzed_count does not match results")
    if metadata["excluded_count"] != len(excluded):
        problems.append("excluded_count does not match excluded")
    for status, key in COUNT_BY_STATUS.items():
        if metadata[key] != sum(1 for row in results if row["status"] == status):
            problems.append(f"{key} does not match results")
    if metadata["results_sha256"] != _digest(results, excluded):
        problems.append("results_sha256 does not match the content")
    return problems


def _atomic_write(path: Path, payload: str, validate: Any = None) -> None:
    """temporary file in the same directory -> re-read + validate -> replace; the temporary file never survives.

    ``validate`` (default: this module's ``validate_report``) lets another report type reuse the same writer."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        problems = (validate or validate_report)(json.loads(temporary.read_text(encoding="utf-8")))
        if problems:
            raise ReportError("written report failed validation: " + "; ".join(problems[:5]))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def write_report(report: Mapping[str, Any], directory: Path | str, *, history: bool = True) -> dict[str, Optional[str]]:
    """Validate and write ``latest.json`` (and optionally a history copy) atomically.

    The history copy is written first, so a failure there leaves ``latest.json`` untouched.
    """
    problems = validate_report(dict(report))
    if problems:
        raise ReportError("report failed validation: " + "; ".join(problems[:5]))
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    history_path: Optional[Path] = None
    if history:
        stamp = datetime.fromisoformat(report["metadata"]["generated_at"]).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
        history_path = target / f"{HISTORY_PREFIX}{stamp}.json"
        _atomic_write(history_path, payload)
    latest = target / LATEST_NAME
    _atomic_write(latest, payload)
    return {"latest": str(latest), "history": str(history_path) if history_path else None}


def read_latest_report(directory: Path | str) -> tuple[Optional[dict[str, Any]], Optional[str], list[str]]:
    """Read the last finished report. Returns ``(report, reason, problems)``; ``reason`` is set when unavailable."""
    path = Path(directory) / LATEST_NAME
    if not path.is_file():
        return None, "NO_DISCOVERY_REPORT", []
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "REPORT_INVALID", [f"{type(exc).__name__}: {exc}"]
    problems = validate_report(report)
    if problems:
        return None, "REPORT_INVALID", problems
    return report, None, []


def _freshness(metadata: Mapping[str, Any], now: datetime, max_age_days: int) -> dict[str, Any]:
    generated = datetime.fromisoformat(metadata["generated_at"])
    evaluation_age = (now.astimezone(timezone.utc).date() - date.fromisoformat(metadata["evaluation_as_of"])).days
    stale = evaluation_age > max_age_days
    return {
        "generated_at": metadata["generated_at"],
        "age_hours": round((now - generated).total_seconds() / 3600.0, 2),
        "evaluation_as_of": metadata["evaluation_as_of"],
        "evaluation_age_days": evaluation_age,
        "max_age_days": max_age_days,
        "max_age_rule": "DataQualityConfig.market_data_max_age_days (the report is market-data based)",
        "status": "REPORT_STALE" if stale else "FRESH",
    }


def query_report(
    report: Mapping[str, Any],
    *,
    status: str = "ALL",
    limit: int = DEFAULT_LIMIT,
    detail: bool = False,
    symbol: Optional[str] = None,
    now: Optional[datetime] = None,
    max_age_days: Optional[int] = None,
) -> dict[str, Any]:
    """Filter an already loaded report (pure; never starts a discovery run)."""
    now = now or datetime.now(timezone.utc)
    max_age = DataQualityConfig().market_data_max_age_days if max_age_days is None else max_age_days
    wanted = STATUS_ALIASES.get(str(status).strip().upper(), str(status).strip().upper())
    if wanted not in FILTER_VALUES:
        return {"ok": False, "operation": "get_candidate_discovery", "error": "INVALID_STATUS", "message": f"status must be one of {', '.join(FILTER_VALUES)}"}
    metadata = report["metadata"]
    cap = MAX_LIMIT_DETAIL if detail else MAX_LIMIT_COMPACT
    applied = max(1, min(int(limit), cap))
    results, excluded = report["results"], report["excluded"]
    wanted_symbol = symbol.strip().upper() if symbol and symbol.strip() else None
    if wanted == "EXCLUDED":
        source = [dict(row, list="excluded") for row in excluded]
    elif wanted == "ALL":
        source = [dict(row, list="results") for row in results]
    else:
        source = [dict(row, list="results") for row in results if row["status"] == wanted]
    if wanted_symbol:
        source = [row for row in source if str(row["symbol"]).upper() == wanted_symbol]
        if wanted == "ALL":
            source += [dict(row, list="excluded") for row in excluded if str(row["symbol"]).upper() == wanted_symbol]
    returned = source[:applied]
    keep = EXCLUDED_FIELDS if wanted == "EXCLUDED" else (RESULT_FIELDS if detail else COMPACT_FIELDS)
    rows = []
    for row in returned:
        if row["list"] == "excluded" and wanted != "EXCLUDED":
            projected = {key: row.get(key) for key in (EXCLUDED_FIELDS if detail else ("symbol", "name", "reason_codes"))}
        else:
            projected = {key: row.get(key) for key in keep}
        projected["list"] = row["list"]
        rows.append(projected)
    counts = {key: metadata[key] for key in ("universe_size", "analyzed_count", "excluded_count", *COUNT_BY_STATUS.values())}
    freshness = _freshness(metadata, now, max_age)
    response = {
        "ok": True,
        "operation": "get_candidate_discovery",
        "status": "AVAILABLE",
        "report_stale": freshness["status"] == "REPORT_STALE",
        "freshness": freshness,
        "universe": {"name": metadata["universe_name"], "version": metadata.get("universe_version"), "size": metadata["universe_size"]},
        "counts": counts,
        "source": metadata["source"],
        "filter": {"status": wanted, "symbol": wanted_symbol, "limit_requested": limit, "limit_applied": applied, "detail": bool(detail)},
        "matching": len(source),
        "returned": len(rows),
        "truncated": len(source) > len(rows),
        "results": rows,
        "notes": [
            "The MCP reader only reads the last finished report; it never starts a discovery run or calls a data provider.",
            "DISCOVERY is not PROMOTE and not a purchase; nothing was added to the watchlist. Fundamentals, valuation and events were not evaluated for new securities.",
        ],
    }
    if freshness["status"] == "REPORT_STALE":
        response["notes"].insert(0, f"REPORT_STALE: the report's evaluation date is {freshness['evaluation_age_days']} days old (limit {max_age}); run Discover-TradingCandidates.py again.")
    if detail or wanted_symbol:
        response["methodology"] = metadata.get("methodology")
        response["pre_filter"] = metadata.get("pre_filter")
    response["rendered_de"] = render_report_de(response)
    return response


def unavailable(reason: str, directory: Path | str, problems: Optional[Sequence[str]] = None) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": "get_candidate_discovery",
        "status": "UNAVAILABLE",
        "reason": reason,
        "report_path": str(Path(directory) / LATEST_NAME),
        "problems": list(problems or []),
        "hint": "Run tools\\Discover-TradingCandidates.py explicitly (read-only); the MCP reader never starts a discovery run.",
    }


def _num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def render_report_de(response: Mapping[str, Any]) -> str:
    """Deterministic German rendering of an already computed reader response (formatting only)."""
    fresh, counts = response["freshness"], response["counts"]
    lines = [
        f"Watchlist-Kandidaten-Discovery (gelesen aus dem letzten fertigen Report; kein neuer Lauf; Stand {fresh['evaluation_as_of']}, Report {fresh['generated_at']}, Alter {fresh['age_hours']} h)",
        f"Frische: {fresh['status']} | Universum {counts['universe_size']} | vorgefiltert {counts['excluded_count']} | analysiert {counts['analyzed_count']}",
        f"READY {counts['discovery_ready_count']} | WATCH {counts['discovery_watch_count']} | DATA_INSUFFICIENT {counts['discovery_data_insufficient_count']} | REJECTED {counts['discovery_rejected_count']}",
        "",
        f"### {response['returned']} von {response['matching']} ({response['filter']['status']})",
    ]
    if response["filter"]["status"] == "EXCLUDED":
        lines += ["| Symbol | Name | Grund |", "|---|---|---|"]
        lines += [f"| {r['symbol']} | {r.get('name')} | {', '.join(r['reason_codes'])} |" for r in response["results"]]
    else:
        lines += ["| Rank | Symbol | Name | Discovery-Score | Status | Grund |", "|---|---|---|---|---|---|"]
        for r in response["results"]:
            if r.get("list") == "excluded":
                lines.append(f"| - | {r['symbol']} | {r.get('name')} | - | VORGEFILTERT | {', '.join(r['reason_codes'])} |")
            else:
                lines.append(f"| {r['rank']} | {r['symbol']} | {r.get('name')} | {_num(r.get('discovery_score'))} | {r['status']} | {', '.join(r['reason_codes'])} |")
    if not response["results"]:
        lines.append("keine Treffer")
    lines.append("")
    lines.append("DISCOVERY ist weder PROMOTE noch Kauf; keine automatische Watchlist-Aufnahme. Fundamentals, Bewertung und Events wurden für neue Titel nicht ausgewertet.")
    return "\n".join(lines)
