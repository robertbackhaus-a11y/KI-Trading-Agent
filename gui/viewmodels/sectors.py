"""View model of the "Sektoren" page: ``get_sector_overview`` payload -> treemap data, the sector table and the position table.

Pure Python (no Qt, no database).  Nothing is computed here beyond formatting and the colour of a daily change; every figure (value, weight, count, daily
change, guard status) comes from the ``get_sector_overview`` result.  A missing or malformed value shows as "—", a missing daily change is drawn in the
neutral colour.  Positions without a sector form their own group ("Nicht zugeordnet"); ETFs are normal positions, marked "ETF".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .portfolio import DASH, fmt_eur, fmt_pct, fmt_time, _de, _num
from .treemap import performance_color

UNASSIGNED = "Nicht zugeordnet"
SECTOR_HEADERS = ["Sektor", "Marktwert €", "Anteil %", "Positionen", "Tagesperformance", "Guard / Limit"]
POSITION_HEADERS = ["Position", "Sektor", "Marktwert €", "Anteil %", "Tag", "Art", "Strategie"]
GUARD_TEXT = {"OK": "OK", "OVER_LIMIT": "Über Limit", "NOT_ASSESSED": "nicht bewertet"}
GUARD_TIP = {
    "OK": "Der Sektor liegt unter dem Limit des Sector Guards.",
    "OVER_LIMIT": "Der Sektor liegt über dem Limit des Sector Guards; Zukäufe in diesen Sektor werden blockiert (BLOCKED_BY_SECTOR_CONCENTRATION).",
    "NOT_ASSESSED": "Ohne Sektor-Zuordnung kann der Sector Guard nichts bewerten (er blockiert dann nicht). Zuordnung: Seite Watchlist, Spalte Sektor.",
}
STRATEGY_TEXT = {"swing": "Swing", "long_term": "Long-Term", "tactical": "Taktisch"}


@dataclass
class PositionVM:
    security_id: int
    symbol: str
    name: str
    sector: Optional[str]  # None = not assigned
    value: float
    weight_pct: float
    change_pct: Optional[float]
    is_etf: bool
    strategy: str
    color: str

    @property
    def label(self) -> str:
        return f"{self.symbol} ETF" if self.is_etf else self.symbol

    @property
    def tip(self) -> str:
        change = DASH if self.change_pct is None else fmt_signed_pct(self.change_pct, 2)
        kind = "ETF (als eine Position, ohne Zerlegung)" if self.is_etf else "Aktie"
        return f"{self.name or self.symbol}\n{kind}\nSektor: {self.sector or UNASSIGNED}\nMarktwert: {fmt_eur(self.value)} ({fmt_pct(self.weight_pct)})\nTagesperformance: {change}\nStrategie: {self.strategy}"


@dataclass
class SectorVM:
    sector: Optional[str]
    value: float
    weight_pct: float
    count: int
    etf_count: int
    change_pct: Optional[float]
    coverage_pct: Optional[float]
    guard: str
    limit_pct: Optional[float]
    headroom_pct: Optional[float]
    positions: list[PositionVM] = field(default_factory=list)
    color: str = ""

    @property
    def title(self) -> str:
        return self.sector or UNASSIGNED


@dataclass
class SectorsVM:
    available: bool = False
    error: str = ""
    refreshed: str = DASH
    summary: list[tuple[str, str]] = field(default_factory=list)
    sectors: list[SectorVM] = field(default_factory=list)
    sector_rows: list[list[str]] = field(default_factory=list)
    sector_levels: list[str] = field(default_factory=list)
    position_rows: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def treemap_groups(self) -> list[tuple[str, float, list[tuple[str, float]]]]:
        """The input of ``treemap.layout``: the group key is the index into ``sectors``, the position key the index into its ``positions`` (as text)."""
        return [(str(i), s.value, [(str(j), p.value) for j, p in enumerate(s.positions)]) for i, s in enumerate(self.sectors)]


def fmt_signed_pct(value: Any, digits: int = 2) -> str:
    n = _num(value)
    return DASH if n is None else f"{'+' if n > 0 else ''}{_de(n, digits)} %"


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def guard_text(group: SectorVM) -> str:
    if group.guard == "NOT_ASSESSED" or group.limit_pct is None:
        return GUARD_TEXT["NOT_ASSESSED"]
    text = GUARD_TEXT.get(group.guard, group.guard)
    return f"{text} (Limit {fmt_pct(group.limit_pct, 0)}, " + (f"frei {fmt_pct(group.headroom_pct)})" if (group.headroom_pct or 0) >= 0 else f"über um {fmt_pct(-(group.headroom_pct or 0))})")


def build_sectors(envelope: Any, refreshed: Optional[datetime] = None) -> SectorsVM:
    vm = SectorsVM(refreshed=fmt_time(refreshed))
    env = _d(envelope)
    if not env:
        vm.error = "Keine Daten"
        return vm
    if env.get("ok") is False or not isinstance(env.get("sectors"), list):
        vm.error = str(env.get("message") or env.get("error") or "Sektor-Übersicht nicht verfügbar")
        return vm
    vm.available = True
    for raw in _l(env["sectors"]):
        group = _d(raw)
        sector = group.get("sector") if isinstance(group.get("sector"), str) and group.get("sector") else None
        positions = []
        for p in _l(group.get("positions")):
            p = _d(p)
            value = _num(p.get("market_value_eur"))
            if value is None:
                continue
            change = _num(p.get("day_change_pct"))
            positions.append(PositionVM(int(p.get("security_id") or 0), str(p.get("symbol") or DASH), str(p.get("name") or ""), sector, value,
                                        _num(p.get("weight_pct")) or 0.0, change, bool(p.get("is_etf")),
                                        STRATEGY_TEXT.get(str(p.get("strategy")), str(p.get("strategy") or DASH)), performance_color(change)))
        change = _num(group.get("day_change_pct"))
        vm.sectors.append(SectorVM(sector, _num(group.get("market_value_eur")) or 0.0, _num(group.get("weight_pct")) or 0.0, int(group.get("position_count") or len(positions)),
                                   int(group.get("etf_count") or 0), change, _num(group.get("day_coverage_pct")), str(group.get("guard_status") or "NOT_ASSESSED"),
                                   _num(group.get("limit_pct")), _num(group.get("headroom_pct")), positions, performance_color(change)))
    for group in vm.sectors:
        count = f"{group.count}" + (f" (davon {group.etf_count} ETF)" if group.etf_count else "")
        change = fmt_signed_pct(group.change_pct)
        if group.change_pct is not None and group.coverage_pct is not None and group.coverage_pct < 99.5:
            change += f" ({fmt_pct(group.coverage_pct, 0)} des Werts)"
        vm.sector_rows.append([group.title, fmt_eur(group.value), fmt_pct(group.weight_pct), count, change, guard_text(group)])
        vm.sector_levels.append("blocker" if group.guard == "OVER_LIMIT" else "info" if group.sector is None else "")
        for p in group.positions:
            vm.position_rows.append([p.label, group.title, fmt_eur(p.value), fmt_pct(p.weight_pct), fmt_signed_pct(p.change_pct), "ETF" if p.is_etf else "Aktie", p.strategy])
    assigned = sum(g.value for g in vm.sectors if g.sector is not None)
    unassigned = sum(g.value for g in vm.sectors if g.sector is None)
    total = _num(env.get("total_market_value_eur")) or 0.0
    change = fmt_signed_pct(env.get("day_change_pct"))
    coverage = _num(env.get("day_coverage_pct"))
    if _num(env.get("day_change_pct")) is not None and coverage is not None and coverage < 99.5:
        change += f" ({fmt_pct(coverage, 0)} des Werts)"
    vm.summary = [
        ("Bewertungsdatum", str(env.get("as_of") or DASH)),
        ("Investierter Wert", fmt_eur(total)),
        ("Zugeordnet", f"{fmt_eur(assigned)} ({fmt_pct(assigned / total * 100.0 if total > 0 else None)})"),
        (UNASSIGNED, f"{fmt_eur(unassigned)} ({fmt_pct(unassigned / total * 100.0 if total > 0 else None)})"),
        ("Tagesperformance", change),
        ("Sektor-Limit (Guard)", fmt_pct(env.get("limit_pct"), 0)),
    ]
    for item in _l(env.get("unvalued")):
        vm.notes.append(f"{_d(item).get('symbol', DASH)}: ohne EUR-Bewertung, nicht in den Werten enthalten")
    for warning in _l(env.get("warnings")):
        vm.notes.append(str(warning))
    vm.notes.append("ETFs sind je eine Position (keine Zerlegung). Die Tagesperformance ist die Kursänderung in Handelswährung (ohne Währungseffekt).")
    return vm
