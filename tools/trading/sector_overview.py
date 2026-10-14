"""Read-only sector overview of the current portfolio (the ``get_sector_overview`` MCP tool, the "Sektoren" page of the desktop GUI).

``open positions (EUR market value of the portfolio context) + effective sector (public + local sector map) + ETF look-through + last daily close change
-> exposure grouped by sector``.

Nothing is invented here:

* the market value of a position is the one of ``analysis_engine.build_portfolio_context`` (``shares x EUR-normalised price``), the figure every other
  portfolio report uses; a position without a valuation is listed separately (``unvalued``) and never counted as 0;
* the sector of a single stock is the effective sector of ``sector_assignment`` (local entry over public entry; the key is the listing key the risk guards use);
  a position without an entry is "not assigned" and stays visible as its own group -- it is never dropped and never put into another sector;
* **ETF look-through** (``lookthrough=True``, the default; ``etf_lookthrough``): an ETF with an official provider sector allocation is split into its
  categories (``market value x weight``); a category that maps to exactly one sector group (``etf_sector_mapping_v1.json``) adds to that group as an
  ``etf_lookthrough`` contribution, a category that does not (spans several groups, no group, not a sector) forms its own *unmapped* category group and is
  never put into a group by name or size.  An ETF without a usable allocation stays one unresolved position (its own sector entry or "not assigned"),
  visible with its status.  The split replaces the ETF's single position, so nothing is counted twice (``checks`` compares the sum with the portfolio context).
  Every contribution says whether it is ``direct`` or ``etf_lookthrough``;
* the weight is the share of the invested value (the sum of the valued positions), the basis of the sector guard; ``limit_pct`` is the guard limit
  (``RiskGuardConfig.max_sector_weight``) and ``guard_status`` says ``OVER_LIMIT`` / ``OK`` / ``NOT_ASSESSED`` for the *effective* exposure (direct + look-through).
  The risk guard itself is NOT changed: it still reads direct sectors only; this view reads no guard result;
* the daily change of a position is ``close / previous close - 1`` of the two latest daily bars of the Yahoo source (native currency, so the currency effect
  is not included); a missing or stale bar means ``None``.  A contribution of an ETF carries the change of the ETF.  The change of a group is the exact
  value-weighted change of its entries that have one (``sum(v - v/(1+p)) / sum(v/(1+p))``) and says how much of the group it covers (``day_coverage_pct``).

Only SELECTs; no write, no network, no order.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

import etf_lookthrough
import sector_assignment
from strategy_config import RiskGuardConfig

OPERATION = "get_sector_overview"
GUARD_OK = "OK"
GUARD_OVER = "OVER_LIMIT"
GUARD_NOT_ASSESSED = "NOT_ASSESSED"
KIND_SECTOR, KIND_ETF_CATEGORY, KIND_UNASSIGNED = "sector", "etf_category", "unassigned"
ORIGIN_DIRECT, ORIGIN_ETF = "direct", "etf_lookthrough"
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
    """``(value-weighted daily change in %, share of the group value that has a change in %)`` -- ``None`` when no entry has one."""
    total = sum(p["market_value_eur"] for p in positions)
    covered = [p for p in positions if p.get("day_change_pct") is not None]
    if not covered:
        return None, None
    now = sum(p["market_value_eur"] for p in covered)
    before = sum(p["market_value_eur"] / (1.0 + p["day_change_pct"] / 100.0) for p in covered)
    change = (now / before - 1.0) * 100.0 if before > _EPS else None
    return change, (now / total * 100.0 if total > _EPS else None)


def _bucket(entry: Mapping[str, Any]) -> tuple[str, Optional[str]]:
    if entry["sector"] is not None:
        return KIND_SECTOR, entry["sector"]
    if entry["origin"] == ORIGIN_ETF:
        return KIND_ETF_CATEGORY, entry["category"]
    return KIND_UNASSIGNED, None


def group_positions(entries: list[dict[str, Any]], total: float, limit_pct: float) -> list[dict[str, Any]]:
    """The groups of valued entries: sector groups by value (largest first), then the unmapped ETF categories, "not assigned" last."""
    buckets: dict[tuple[str, Optional[str]], list[dict[str, Any]]] = {}
    for entry in entries:
        buckets.setdefault(_bucket(entry), []).append(entry)
    groups = []
    for (kind, name), members in buckets.items():
        members.sort(key=lambda p: (-p["market_value_eur"], p["symbol"], p.get("category") or ""))
        value = sum(p["market_value_eur"] for p in members)
        direct = sum(p["market_value_eur"] for p in members if p["origin"] == ORIGIN_DIRECT)
        weight = value / total * 100.0 if total > _EPS else 0.0
        change, coverage = group_change_pct(members)
        assessed = kind == KIND_SECTOR
        status = GUARD_NOT_ASSESSED if not assessed else GUARD_OVER if weight > limit_pct + _EPS else GUARD_OK
        securities = {p["security_id"] for p in members}
        groups.append({
            "sector": name if kind == KIND_SECTOR else None, "kind": kind, "assigned": assessed, "label": name or "",
            "market_value_eur": value, "weight_pct": weight, "direct_value_eur": direct, "etf_value_eur": value - direct,
            "direct_weight_pct": direct / total * 100.0 if total > _EPS else 0.0, "etf_weight_pct": (value - direct) / total * 100.0 if total > _EPS else 0.0,
            "position_count": len(securities), "direct_count": len({p["security_id"] for p in members if p["origin"] == ORIGIN_DIRECT}),
            "etf_count": len({p["security_id"] for p in members if p["is_etf"]}),
            "day_change_pct": change, "day_coverage_pct": coverage, "guard_status": status,
            "limit_pct": limit_pct if assessed else None, "headroom_pct": limit_pct - weight if assessed else None, "positions": members,
        })
    order = {KIND_SECTOR: 0, KIND_ETF_CATEGORY: 1, KIND_UNASSIGNED: 2}
    groups.sort(key=lambda g: (order[g["kind"]], -g["market_value_eur"], g["label"]))
    return groups


def _security_info(conn: sqlite3.Connection, ids: list[int]) -> dict[int, tuple[Any, Any, Any]]:
    """``{id: (name, asset_type, isin)}``; a database without an ``isin`` column (an old schema) just has none."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    try:
        return {int(r[0]): (r[1], r[2], r[3]) for r in conn.execute(f"SELECT id, name, asset_type, isin FROM security WHERE id IN ({marks})", ids)}
    except sqlite3.OperationalError:
        return {int(r[0]): (r[1], r[2], None) for r in conn.execute(f"SELECT id, name, asset_type FROM security WHERE id IN ({marks})", ids)}


def _lookthrough_layers(universe_dir: Optional[Path], valid_groups: Optional[set[str]], warnings: list[str]):
    try:
        return etf_lookthrough.read_layers(universe_dir, valid_groups)
    except (OSError, ValueError) as exc:  # LookthroughError is a ValueError: no mapping file means no look-through, never a failed tool
        warnings.append(f"LOOKTHROUGH_UNAVAILABLE: {exc}; ETFs stay single positions")
        return None


def build_sector_overview(
    conn: sqlite3.Connection,
    *,
    as_of: Optional[str] = None,
    universe_dir: Optional[Path] = None,
    max_sector_weight: Optional[float] = None,
    lookthrough: bool = True,
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
    lt = _lookthrough_layers(universe_dir, set(layers.groups) if layers is not None else None, warnings) if lookthrough else None
    if lt is not None:
        warnings.extend(lt.warnings)
    exposures = [e for e in context.exposures if e.quantity is not None and float(e.quantity) > 0]
    ids = [int(e.security_id) for e in exposures]
    info = _security_info(conn, ids)
    changes = day_changes(conn, ids, evaluation)
    positions: list[dict[str, Any]] = []
    unvalued: list[dict[str, Any]] = []
    for exposure in exposures:
        sid = int(exposure.security_id)
        name, asset_type, isin = info.get(sid, (None, None, None))
        try:
            key = sector_assignment.listing_key(conn, sid)
        except sqlite3.Error:  # no alias tables: the plain symbol is the key
            key = exposure.symbol
        resolved = sector_assignment.resolve(layers, key) if layers is not None else {"sector": None, "source": sector_assignment.SOURCE_NONE}
        row = {
            "security_id": sid, "symbol": exposure.symbol, "name": name, "asset_type": asset_type, "is_etf": str(asset_type or "").lower() == "etf", "isin": isin,
            "strategy": exposure.strategy, "quantity": _num(exposure.quantity), "sector": resolved["sector"], "sector_source": resolved["source"],
        }
        value = _num(exposure.market_value_eur)
        if value is None or value <= 0:
            row["reasons"] = list(exposure.valuation_quality.details)
            unvalued.append(row)
            continue
        change = changes.get(sid, {})
        row.update(market_value_eur=value, day_change_pct=change.get("pct"), day_change_date=change.get("date"))
        positions.append(row)
    total = sum(p["market_value_eur"] for p in positions)

    entries: list[dict[str, Any]] = []
    etfs: list[dict[str, Any]] = []
    for position in positions:
        position["origin"], position["category"], position["etf_weight_pct"] = ORIGIN_DIRECT, None, None
        if not (lookthrough and position["is_etf"]):
            entries.append(position)
            continue
        if lt is None:
            status, allocation, detail = etf_lookthrough.NO_DATA, None, "look-through data unavailable"
        else:
            status, allocation, detail = etf_lookthrough.status_of(lt, position["isin"], evaluation)
        item = {"security_id": position["security_id"], "symbol": position["symbol"], "name": position["name"], "isin": position["isin"], "market_value_eur": position["market_value_eur"],
                "status": status, "detail": detail, "as_of": allocation.as_of if allocation else None, "provider": allocation.provider if allocation else None,
                "source": allocation.source if allocation else None, "mapped_value_eur": 0.0, "mapped_pct": None, "categories": 0}
        etfs.append(item)
        if allocation is None:
            entries.append(position)  # stays one unresolved position (its own sector entry, or not assigned)
            continue
        parts = etf_lookthrough.contributions(allocation, position["market_value_eur"], lt.mapping)
        item["categories"] = len(parts)
        for part in parts:
            entries.append({
                "security_id": position["security_id"], "symbol": position["symbol"], "name": position["name"], "asset_type": position["asset_type"], "is_etf": True, "isin": position["isin"],
                "strategy": position["strategy"], "quantity": position["quantity"], "sector": part["group"], "sector_source": "etf_lookthrough",
                "market_value_eur": part["value_eur"], "day_change_pct": position["day_change_pct"], "day_change_date": position["day_change_date"],
                "origin": ORIGIN_ETF, "category": part["category"], "category_reason": part["reason"], "etf_weight_pct": part["provider_weight_pct"],
                "etf_value_eur": position["market_value_eur"], "etf_as_of": allocation.as_of,
            })
            if part["group"] is not None:
                item["mapped_value_eur"] += part["value_eur"]
        item["mapped_pct"] = item["mapped_value_eur"] / position["market_value_eur"] * 100.0
    groups = group_positions(entries, total, limit_pct)
    for group in groups:
        for entry in group["positions"]:
            entry["weight_pct"] = entry["market_value_eur"] / total * 100.0 if total > _EPS else 0.0

    etf_total = sum(e["market_value_eur"] for e in etfs)
    covered = sum(e["market_value_eur"] for e in etfs if e["status"] in (etf_lookthrough.APPLIED, etf_lookthrough.STALE))
    for item in etfs:
        if item["status"] == etf_lookthrough.STALE:
            warnings.append(f"LOOKTHROUGH_STALE: allocation of {item['symbol']} is from {item['as_of']}")
        elif item["status"] != etf_lookthrough.APPLIED:
            warnings.append(f"LOOKTHROUGH_{item['status']}: {item['symbol']} stays one unresolved position ({item['detail']})")
    context_total = _num(context.total_market_value)
    if unvalued:
        warnings.append(f"UNVALUED_POSITIONS: {len(unvalued)} open position(s) without a EUR valuation are not part of the values")
    if context_total is not None and abs(context_total - total) > 0.01:
        warnings.append("TOTAL_MISMATCH: the sum of the valued positions differs from the portfolio context")
    change, coverage = group_change_pct(positions)
    sector_sum = sum(g["market_value_eur"] for g in groups)
    assigned = sum(g["market_value_eur"] for g in groups if g["kind"] == KIND_SECTOR)
    return {
        "ok": True, "operation": OPERATION, "as_of": evaluation, "base_currency": "EUR",
        "total_market_value_eur": total, "position_count": len(positions), "limit_pct": limit_pct,
        "day_change_pct": change, "day_coverage_pct": coverage,
        "lookthrough_enabled": bool(lookthrough),
        "assigned_value_eur": assigned,
        "direct_value_eur": sum(g["direct_value_eur"] for g in groups), "etf_value_eur": sum(g["etf_value_eur"] for g in groups),
        "etf_category_value_eur": sum(g["market_value_eur"] for g in groups if g["kind"] == KIND_ETF_CATEGORY),
        "unassigned_value_eur": sum(g["market_value_eur"] for g in groups if g["kind"] == KIND_UNASSIGNED),
        "lookthrough": {"etf_count": len(etfs), "etf_value_eur": etf_total, "covered_value_eur": covered, "coverage_pct": covered / etf_total * 100.0 if etf_total > _EPS else None,
                        "mapped_value_eur": sum(e["mapped_value_eur"] for e in etfs), "mapped_pct": sum(e["mapped_value_eur"] for e in etfs) / etf_total * 100.0 if etf_total > _EPS else None},
        "etfs": etfs, "sectors": groups, "unvalued": unvalued,
        "checks": {"sector_sum_eur": sector_sum, "context_total_eur": context_total,
                   "reconciles": None if context_total is None else abs(context_total - total) <= 0.01 and abs(sector_sum - total) <= 0.01,  # None while the context has no total (an unvalued position)
                   "groups_sum_matches_total": abs(sector_sum - total) <= 0.01},
        "etf_note": "ETFs with an official sector allocation are split into their categories (etf_lookthrough); categories that fit no single sector group stay unmapped.",
        "warnings": warnings,
    }
