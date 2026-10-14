"""Read-only sector overview of the current portfolio (the ``get_sector_overview`` MCP tool, the "Sektoren" page of the desktop GUI).

``open positions (EUR market value of the portfolio context) + effective sector (public + local sector map) + last daily close change
-> positions grouped by sector``.

Nothing is invented here:

* the market value of a position is the one of ``analysis_engine.build_portfolio_context`` (``shares x EUR-normalised price``), the figure every other
  portfolio report uses; a position without a valuation is listed separately (``unvalued``) and never counted as 0;
* the sector is the effective sector of ``sector_assignment`` (local entry over public entry; the key is the listing key the risk guards use);
  a position without an entry is "not assigned" and stays visible as its own group -- it is never dropped and never put into another sector;
* ETFs are NOT decomposed: an ETF is one normal position (``is_etf``) in the group its map entry names, or in "not assigned";
* the weight is the share of the invested value (the sum of the valued positions), the basis of the sector guard; ``limit_pct`` is the guard limit
  (``RiskGuardConfig.max_sector_weight``) and ``guard_status`` says ``OVER_LIMIT`` / ``OK`` / ``NOT_ASSESSED`` (not assigned).  The guard only blocks
  purchases that would lift a sector above the limit; this view reads no guard result and changes none;
* the daily change of a position is ``close / previous close - 1`` of the two latest daily bars of the Yahoo source (native currency, so the currency effect
  is not included); a missing or stale bar means ``None``.  The change of a group is the exact value-weighted change of its positions that have one
  (``sum(v - v/(1+p)) / sum(v/(1+p))``) and says how much of the group it covers (``day_coverage_pct``).

Only SELECTs; no write, no network, no order.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

import sector_assignment
from strategy_config import RiskGuardConfig

OPERATION = "get_sector_overview"
GUARD_OK = "OK"
GUARD_OVER = "OVER_LIMIT"
GUARD_NOT_ASSESSED = "NOT_ASSESSED"
STALE_BAR_DAYS = 5  # the latest daily bar may be at most this many calendar days older than the evaluation date
BAR_GAP_DAYS = 7  # the previous bar must lie at most this many days before the latest one
SOURCE_NAME = "Yahoo Finance"
_EPS = 1e-9


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def day_changes(conn: sqlite3.Connection, security_ids, as_of: str) -> dict[int, dict[str, Any]]:
    """``{security_id: {"pct": float|None, "date": str|None}}`` from the two latest daily closes up to ``as_of`` (read-only)."""
    out: dict[int, dict[str, Any]] = {int(i): {"pct": None, "date": None} for i in security_ids}
    try:
        row = conn.execute("SELECT id FROM data_sources WHERE name = ?", (SOURCE_NAME,)).fetchone()
        source_id = int(row[0]) if row else None
        evaluation = date.fromisoformat(as_of[:10])
    except (sqlite3.Error, ValueError):
        return out
    clause = " AND source_id = ?" if source_id is not None else ""
    for sid in out:
        try:
            params: list[Any] = [sid, evaluation.isoformat()] + ([source_id] if source_id is not None else [])
            bars = conn.execute(
                f"SELECT trade_date, close FROM market_data WHERE security_id = ? AND trade_date <= ? AND close IS NOT NULL AND close > 0{clause} "
                "ORDER BY trade_date DESC LIMIT 2", params).fetchall()
            if len(bars) < 2:
                continue
            latest, previous = date.fromisoformat(str(bars[0][0])[:10]), date.fromisoformat(str(bars[1][0])[:10])
            if (evaluation - latest).days > STALE_BAR_DAYS or (latest - previous).days > BAR_GAP_DAYS:
                continue
            out[sid] = {"pct": (float(bars[0][1]) / float(bars[1][1]) - 1.0) * 100.0, "date": latest.isoformat()}
        except (sqlite3.Error, ValueError, TypeError):
            continue
    return out


def group_change_pct(positions: list[Mapping[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    """``(value-weighted daily change in %, share of the group value that has a change in %)`` -- ``None`` when no position has one."""
    total = sum(p["market_value_eur"] for p in positions)
    covered = [p for p in positions if p.get("day_change_pct") is not None]
    if not covered:
        return None, None
    now = sum(p["market_value_eur"] for p in covered)
    before = sum(p["market_value_eur"] / (1.0 + p["day_change_pct"] / 100.0) for p in covered)
    change = (now / before - 1.0) * 100.0 if before > _EPS else None
    return change, (now / total * 100.0 if total > _EPS else None)


def group_positions(positions: list[dict[str, Any]], total: float, limit_pct: float) -> list[dict[str, Any]]:
    """The sector groups of valued positions: assigned groups by value (largest first), "not assigned" last."""
    buckets: dict[Optional[str], list[dict[str, Any]]] = {}
    for position in positions:
        buckets.setdefault(position["sector"], []).append(position)
    groups = []
    for sector, members in buckets.items():
        members.sort(key=lambda p: (-p["market_value_eur"], p["symbol"]))
        value = sum(p["market_value_eur"] for p in members)
        weight = value / total * 100.0 if total > _EPS else 0.0
        change, coverage = group_change_pct(members)
        status = GUARD_NOT_ASSESSED if sector is None else GUARD_OVER if weight > limit_pct + _EPS else GUARD_OK
        groups.append({
            "sector": sector, "assigned": sector is not None, "market_value_eur": value, "weight_pct": weight, "position_count": len(members),
            "day_change_pct": change, "day_coverage_pct": coverage, "guard_status": status,
            "limit_pct": None if sector is None else limit_pct, "headroom_pct": None if sector is None else limit_pct - weight,
            "etf_count": sum(1 for p in members if p["is_etf"]), "positions": members,
        })
    groups.sort(key=lambda g: (not g["assigned"], -g["market_value_eur"], g["sector"] or ""))
    return groups


def build_sector_overview(
    conn: sqlite3.Connection,
    *,
    as_of: Optional[str] = None,
    universe_dir: Optional[Path] = None,
    max_sector_weight: Optional[float] = None,
) -> dict[str, Any]:
    """The sector overview of the open positions (see the module docstring).  Read-only."""
    import analysis_engine

    limit_pct = (RiskGuardConfig().max_sector_weight if max_sector_weight is None else float(max_sector_weight)) * 100.0
    context = analysis_engine.build_portfolio_context(as_of=as_of, connection=conn)
    evaluation = str(context.evaluation_as_of)[:10]
    warnings: list[str] = []
    try:
        layers = sector_assignment.read_layers(None if universe_dir is None else Path(universe_dir) / sector_assignment.PUBLIC_NAME)
    except (OSError, ValueError, sector_assignment.SectorMapError):
        layers = None
        warnings.append("SECTOR_MAP_UNAVAILABLE: no sector could be resolved; every position is shown as not assigned")
    exposures = [e for e in context.exposures if e.quantity is not None and float(e.quantity) > 0]
    ids = [int(e.security_id) for e in exposures]
    info = {int(r[0]): (r[1], r[2]) for r in conn.execute(f"SELECT id, name, asset_type FROM security WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    changes = day_changes(conn, ids, evaluation)
    valued: list[dict[str, Any]] = []
    unvalued: list[dict[str, Any]] = []
    for exposure in exposures:
        sid = int(exposure.security_id)
        name, asset_type = info.get(sid, (None, None))
        try:
            key = sector_assignment.listing_key(conn, sid)
        except sqlite3.Error:  # no alias tables: the plain symbol is the key
            key = exposure.symbol
        resolved = sector_assignment.resolve(layers, key) if layers is not None else {"sector": None, "source": sector_assignment.SOURCE_NONE}
        row = {
            "security_id": sid, "symbol": exposure.symbol, "name": name, "asset_type": asset_type, "is_etf": str(asset_type or "").lower() == "etf",
            "strategy": exposure.strategy, "quantity": _num(exposure.quantity), "sector": resolved["sector"], "sector_source": resolved["source"],
        }
        value = _num(exposure.market_value_eur)
        if value is None or value <= 0:
            row["reasons"] = list(exposure.valuation_quality.details)
            unvalued.append(row)
            continue
        change = changes.get(sid, {})
        row.update(market_value_eur=value, day_change_pct=change.get("pct"), day_change_date=change.get("date"))
        valued.append(row)
    total = sum(p["market_value_eur"] for p in valued)
    groups = group_positions(valued, total, limit_pct)
    for group in groups:
        for position in group["positions"]:
            position["weight_pct"] = position["market_value_eur"] / total * 100.0 if total > _EPS else 0.0
    context_total = _num(context.total_market_value)
    if unvalued:
        warnings.append(f"UNVALUED_POSITIONS: {len(unvalued)} open position(s) without a EUR valuation are not part of the values")
    if context_total is not None and abs(context_total - total) > 0.01:
        warnings.append("TOTAL_MISMATCH: the sum of the valued positions differs from the portfolio context")
    change, coverage = group_change_pct(valued)
    return {
        "ok": True, "operation": OPERATION, "as_of": evaluation, "base_currency": "EUR",
        "total_market_value_eur": total, "position_count": len(valued), "limit_pct": limit_pct,
        "day_change_pct": change, "day_coverage_pct": coverage,
        "assigned_value_eur": sum(g["market_value_eur"] for g in groups if g["assigned"]),
        "unassigned_value_eur": sum(g["market_value_eur"] for g in groups if not g["assigned"]),
        "sectors": groups, "unvalued": unvalued,
        "checks": {"sector_sum_eur": sum(g["market_value_eur"] for g in groups), "context_total_eur": context_total,
                   "reconciles": None if context_total is None else abs(context_total - total) <= 0.01},  # reconciles is None while the context has no total (an unvalued position)
        "etf_note": "ETFs are shown as one position each (no look-through).",
        "warnings": warnings,
    }
