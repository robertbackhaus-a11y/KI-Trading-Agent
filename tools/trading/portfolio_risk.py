# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Sector and correlation guards for the Portfolio Action Planner (pure functions plus one read-only loader).

Two guards, one limit (``RiskGuardConfig.max_sector_weight``, share of the invested market value):

* **Sector guard**: the weight of the candidate's sector after the purchase may not exceed the limit.
* **Correlation guard**: the *cluster* made of the candidate and every holding whose daily returns correlate with it at
  ``correlation_threshold`` or more may not exceed the same limit after the purchase.  Without a correlated holding the cluster
  guard does not apply (a single security is covered by the per-security limits of ``entry_sizing``).

Both are evaluated sequentially against the state *after* every earlier planned purchase of the same run (:class:`RiskBook`), for
new entries (BUY) and for additions (ADD, the existing position counts).  They never re-rank and never read the network: sectors come
from the versioned sector map (plus the optional local one), prices from ``market_data``.

Missing data never invents a value and never blocks: an unknown sector means "not assessable" for the sector guard; a pair of
securities with too few common trading days (or stale or missing prices) is left out of the correlation guard and counted.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

from strategy_config import RiskGuardConfig

SECTOR_BLOCK = "BLOCKED_BY_SECTOR_CONCENTRATION"
CORRELATION_BLOCK = "BLOCKED_BY_CORRELATION"
DEFAULT_SECTOR_MAP = Path(__file__).resolve().parent / "universe" / "sector_map_v1.json"
HISTORY_CALENDAR_DAYS = 420  # about 250 trading days plus calendar slack
_EPS = 1e-9


@dataclass(frozen=True)
class RiskInputs:
    """Read-only inputs of the guards (no database handle)."""

    sector_of: Mapping[int, Optional[str]]
    closes: Mapping[int, Mapping[str, float]]
    symbols: Mapping[int, str] = field(default_factory=dict)
    as_of: Optional[str] = None
    sector_map_available: bool = True


# --------------------------------------------------------------------------- loading
def read_sector_map(path: Path = DEFAULT_SECTOR_MAP) -> dict[str, str]:
    """``{YAHOO_SYMBOL: SECTOR}`` from the versioned map plus the optional, not versioned ``sector_map_local.json`` next to it (a local entry overrides the public one)."""
    import sector_assignment

    return sector_assignment.effective(sector_assignment.read_layers(path))


def load_risk_inputs(
    conn: sqlite3.Connection,
    security_ids,
    *,
    as_of: Optional[str],
    sector_map_path: Optional[Path] = None,
) -> RiskInputs:
    """Read-only SELECTs.  A missing sector map or price table yields unknown sectors / missing history, never an error."""
    ids = sorted({int(i) for i in security_ids if i is not None})
    try:
        mapping = read_sector_map(sector_map_path or DEFAULT_SECTOR_MAP)
    except (OSError, ValueError):
        mapping = {}
    keys: dict[int, str] = {}
    try:
        from security_aliases import AliasIndex

        index = AliasIndex.from_connection(conn)
        keys = {sid: index.key_for(sid) for sid in ids if index.key_for(sid)}
    except Exception:  # noqa: BLE001 - sectors then stay unknown
        keys = {}
    symbols: dict[int, str] = {}
    try:
        for sid, symbol in conn.execute(f"SELECT id, symbol FROM security WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else ():
            symbols[int(sid)] = symbol
            keys.setdefault(int(sid), symbol)
    except sqlite3.Error:
        pass
    sector_of = {sid: mapping.get(keys.get(sid, "")) for sid in ids}
    closes: dict[int, dict[str, float]] = {sid: {} for sid in ids}
    try:
        row = conn.execute("SELECT id FROM data_sources WHERE name = 'Yahoo Finance'").fetchone()
        source_id = int(row[0]) if row else None
        floor = (date.fromisoformat(as_of[:10]) - timedelta(days=HISTORY_CALENDAR_DAYS)).isoformat() if as_of else "0000-00-00"
        ceiling = as_of[:10] if as_of else "9999-12-31"
        source_clause = " AND source_id = ?" if source_id is not None else ""
        for sid in ids:
            params: list[Any] = [sid, floor, ceiling] + ([source_id] if source_id is not None else [])
            for trade_date, close, adjusted in conn.execute(
                f"SELECT trade_date, close, adjusted_close FROM market_data WHERE security_id = ? AND trade_date >= ? AND trade_date <= ?{source_clause} ORDER BY trade_date",
                params,
            ):
                price = adjusted if adjusted is not None else close
                if price is not None and float(price) > 0:
                    closes[sid].setdefault(str(trade_date), float(price))
    except (sqlite3.Error, ValueError):
        pass
    return RiskInputs(sector_of, closes, symbols, as_of[:10] if as_of else None, sector_map_available=bool(mapping))


# --------------------------------------------------------------------------- correlation
def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def return_correlation(
    a: Mapping[str, float],
    b: Mapping[str, float],
    *,
    window: int,
    minimum: int,
    as_of: Optional[str] = None,
    max_age_days: Optional[int] = None,
) -> tuple[Optional[float], int, Optional[str]]:
    """``(correlation, common_return_days, reason)``; ``correlation`` is ``None`` (never 0.0) when it cannot be established.

    Returns are measured between *consecutive common* trading days of both series (so exchanges with different holidays compare
    the same intervals), over the last ``window`` such returns.
    """
    if not a or not b:
        return None, 0, "NO_PRICE_HISTORY"
    common = sorted(set(a) & set(b))
    if as_of and max_age_days is not None and common:
        try:
            if (date.fromisoformat(as_of) - date.fromisoformat(common[-1])).days > max_age_days:
                return None, max(len(common) - 1, 0), "STALE_PRICES"
        except ValueError:
            pass
    common = common[-(window + 1):]
    days = max(len(common) - 1, 0)
    if days < minimum:
        return None, days, "INSUFFICIENT_COMMON_DAYS"
    ra = [a[common[i]] / a[common[i - 1]] - 1.0 for i in range(1, len(common))]
    rb = [b[common[i]] / b[common[i - 1]] - 1.0 for i in range(1, len(common))]
    rho = pearson(ra, rb)
    if rho is None:
        return None, days, "ZERO_VARIANCE"
    return rho, days, None


# --------------------------------------------------------------------------- book
class RiskBook:
    """Mutable exposure book of one planner run (security value in EUR); the planner updates it after every accepted purchase."""

    def __init__(self, inputs: RiskInputs, config: RiskGuardConfig, *, exposures: Mapping[int, float], max_age_days: Optional[int] = 5):
        self.inputs = inputs
        self.config = config
        self.max_age_days = max_age_days
        self.positions: dict[int, float] = {int(k): float(v) for k, v in exposures.items() if v is not None and float(v) > 0}
        self._pairs: dict[tuple[int, int], tuple[Optional[float], int, Optional[str]]] = {}
        self.sector_unknown: set[int] = set()
        self.correlation_unavailable: set[int] = set()

    # -- state updates -------------------------------------------------------
    def buy(self, security_id: int, value: float) -> None:
        self.positions[int(security_id)] = self.positions.get(int(security_id), 0.0) + float(value)

    def sell(self, security_id: int, value: float) -> None:
        sid = int(security_id)
        if sid in self.positions:
            self.positions[sid] = max(0.0, self.positions[sid] - float(value))

    # -- helpers -------------------------------------------------------------
    def sector(self, security_id: int) -> Optional[str]:
        return self.inputs.sector_of.get(int(security_id))

    def symbol(self, security_id: int) -> str:
        return str(self.inputs.symbols.get(int(security_id), security_id))

    def unknown_held(self) -> int:
        return sum(1 for sid in self.positions if self.sector(sid) is None)

    def _pair(self, a: int, b: int) -> tuple[Optional[float], int, Optional[str]]:
        key = (a, b) if a < b else (b, a)
        if key not in self._pairs:
            cfg = self.config
            self._pairs[key] = return_correlation(
                self.inputs.closes.get(a) or {}, self.inputs.closes.get(b) or {},
                window=int(cfg.correlation_window_days), minimum=int(cfg.correlation_min_common_days),
                as_of=self.inputs.as_of, max_age_days=self.max_age_days,
            )
        return self._pairs[key]

    # -- assessment ----------------------------------------------------------
    def assess(self, security_id: int, purchase: float, total: float) -> dict[str, Any]:
        """Guards for buying ``purchase`` EUR of ``security_id`` when the invested value is ``total`` EUR; does not change the book."""
        sid, cfg = int(security_id), self.config
        limit = cfg.max_sector_weight * 100.0
        result: dict[str, Any] = {"blocked_by": None, "limit_pct": round(limit, 2)}
        if total <= 0 or purchase <= 0:
            result["sector"] = {"status": "UNAVAILABLE"}
            result["correlation"] = {"status": "UNAVAILABLE"}
            return result
        after = total + purchase
        # sector guard
        sector = self.sector(sid)
        if sector is None:
            self.sector_unknown.add(sid)
            result["sector"] = {"status": "SECTOR_UNKNOWN"}
        else:
            held = sum(value for other, value in self.positions.items() if self.sector(other) == sector)
            projected = (held + purchase) / after * 100.0
            blocked = projected > limit + _EPS
            result["sector"] = {
                "status": "BLOCKED" if blocked else "OK", "sector": sector,
                "current_weight_pct": round(held / total * 100.0, 2), "projected_weight_pct": round(projected, 2), "limit_pct": round(limit, 2),
            }
            if blocked:
                result["blocked_by"] = SECTOR_BLOCK
        # correlation (cluster) guard
        members: list[int] = []
        unavailable: dict[str, int] = {}
        strongest: Optional[tuple[float, int, int]] = None
        checked = 0
        for other, value in self.positions.items():
            if other == sid or value <= 0:
                continue
            checked += 1
            rho, days, why = self._pair(sid, other)
            if rho is None:
                unavailable[why or "UNAVAILABLE"] = unavailable.get(why or "UNAVAILABLE", 0) + 1
                continue
            if strongest is None or rho > strongest[0]:
                strongest = (rho, days, other)
            if rho >= cfg.correlation_threshold - _EPS:
                members.append(other)
        corr: dict[str, Any] = {"threshold": cfg.correlation_threshold, "min_common_days": int(cfg.correlation_min_common_days), "limit_pct": round(limit, 2)}
        if checked == 0:
            corr["status"] = "NO_OTHER_POSITION"
        else:
            usable = checked - sum(unavailable.values())
            corr["status"] = "UNAVAILABLE" if usable == 0 else ("PARTIAL" if unavailable else "OK")
            if unavailable:
                corr["unavailable_pairs"] = sum(unavailable.values())
                corr["unavailable_reasons"] = dict(sorted(unavailable.items()))
                self.correlation_unavailable.add(sid)
            if strongest is not None:
                corr["strongest"] = {"symbol": self.symbol(strongest[2]), "correlation": round(strongest[0], 3), "common_days": strongest[1]}
            cluster = self.positions.get(sid, 0.0) + sum(self.positions[m] for m in members)
            projected = (cluster + purchase) / after * 100.0
            corr["cluster_members"] = sorted(self.symbol(m) for m in members)
            corr["cluster_current_weight_pct"] = round(cluster / total * 100.0, 2)
            corr["cluster_projected_weight_pct"] = round(projected, 2)
            if members and projected > limit + _EPS:
                corr["status"] = "BLOCKED"
                if result["blocked_by"] is None:
                    result["blocked_by"] = CORRELATION_BLOCK
        result["correlation"] = corr
        return result
