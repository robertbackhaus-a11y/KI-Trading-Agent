# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""View model of the "Sektoren" page: ``get_sector_overview`` payload -> treemap data, the sector table, the position table and the ETF table.

Pure Python (no Qt, no database).  Nothing is computed here beyond formatting and the colour of a daily change; every figure (value, weight, count, daily
change, guard status, look-through split) comes from the ``get_sector_overview`` result.  A missing or malformed value shows as "—", a missing daily change
is drawn in the neutral colour.

Every entry says where it comes from: ``direct`` (a stock or an ETF held as one position), ``etf_company`` (companies inside an ETF whose own project sector is known),
``etf_lookthrough`` (the part of an ETF that its official provider allocation assigns to this sector category).  Positions without a sector form "Nicht zugeordnet"; ETF categories that fit no single sector group form their own
groups "<category> (ETF, nicht zuordenbar)" -- they are never put into a sector group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .portfolio import DASH, fmt_eur, fmt_pct, fmt_time, _de, _num
from .treemap import performance_color

UNASSIGNED = "Nicht zugeordnet"
SECTOR_HEADERS = ["Sektor", "Exposure €", "Exposure %", "Direkt €", "ETF-Look-through €", "Positionen / ETF-Beiträge", "Tagesperformance", "Guard / Limit"]
POSITION_HEADERS = ["Position", "Sektor", "Herkunft", "Marktwert €", "Anteil %", "Tag", "Art", "Strategie"]
ETF_HEADERS = ["ETF", "Wert €", "Look-through", "Stand", "Anbieter", "Sektor-Gruppen zugeordnet"]
GUARD_TEXT = {"OK": "OK", "OVER_LIMIT": "Über Limit", "NOT_ASSESSED": "nicht bewertet"}
GUARD_TIP = {
    "OK": "Der Sektor liegt unter dem Limit des Sector Guards (gerechnet auf die effektive Exposure: direkt + ETF-Look-through; der Risk Guard selbst wertet nur die direkte Zuordnung aus).",
    "OVER_LIMIT": "Die effektive Exposure (direkt + ETF-Look-through) liegt über dem Limit des Sector Guards. Der Risk Guard selbst wertet nur die direkte Zuordnung aus und blockiert deshalb nicht automatisch.",
    "NOT_ASSESSED": "Ohne eindeutige Sektor-Gruppe kann der Sector Guard nichts bewerten (er blockiert dann nicht). Direkte Zuordnung: Seite Watchlist, Spalte Sektor.",
}
LOOKTHROUGH_TEXT = {"APPLIED": "angewendet", "STALE": "angewendet (veraltet)", "NO_DATA": "keine Daten", "ALLOCATION_INVALID": "Daten ungültig", "NO_ISIN": "keine ISIN"}
STRATEGY_TEXT = {"swing": "Swing", "long_term": "Long-Term", "tactical": "Taktisch"}
KIND_SECTOR, KIND_ETF_CATEGORY, KIND_UNASSIGNED = "sector", "etf_category", "unassigned"


@dataclass
class PositionVM:
    security_id: int
    symbol: str
    name: str
    sector: Optional[str]  # the sector group, None = not in a group
    value: float
    weight_pct: float
    change_pct: Optional[float]
    is_etf: bool
    strategy: str
    color: str
    origin: str = "direct"  # "direct", "etf_company" or "etf_lookthrough"
    category: str = ""  # the provider category of an etf_lookthrough entry
    etf_weight_pct: Optional[float] = None  # share of the ETF in that category
    etf_value: Optional[float] = None
    etf_as_of: str = ""
    group: str = ""
    companies: str = ""  # the largest companies of an etf_company entry

    @property
    def lookthrough(self) -> bool:
        return self.origin in ("etf_lookthrough", "etf_company")

    @property
    def table_label(self) -> str:
        if self.origin == "etf_company":
            return f"{self.symbol} ETF · Einzelfirmen"
        return f"{self.symbol} ETF · {self.category}" if self.lookthrough else self.label

    @property
    def label(self) -> str:
        return f"{self.symbol} ETF" if self.is_etf else self.symbol

    @property
    def origin_text(self) -> str:
        if self.origin == "etf_company":
            return f"ETF-Einzelfirmen ({self.companies}; {fmt_pct(self.etf_weight_pct)} des ETF)"
        if self.lookthrough:
            return f"ETF-Look-through ({self.category}, {fmt_pct(self.etf_weight_pct)} des ETF)"
        return "direkt (ETF als eine Position)" if self.is_etf else "direkt"

    @property
    def tip(self) -> str:
        change = DASH if self.change_pct is None else fmt_signed_pct(self.change_pct, 2)
        if self.origin == "etf_company":
            origin = (f"Beitrag aus ETF-Einzelfirmen (Sektor der Firma bekannt): {self.companies} = {fmt_pct(self.etf_weight_pct)} des ETF "
                      f"({fmt_eur(self.etf_value)} gesamt), Stand {self.etf_as_of or DASH}")
        elif self.lookthrough:
            origin = (f"Beitrag aus ETF-Look-through: Anbieter-Kategorie „{self.category}“ = {fmt_pct(self.etf_weight_pct)} des ETF "
                      f"({fmt_eur(self.etf_value)} gesamt), Stand {self.etf_as_of or DASH}")
        elif self.is_etf:
            origin = "Direkte Position (ETF ohne Look-through-Daten: als eine Position gezählt)"
        else:
            origin = "Direkte Position (Einzelaktie)"
        return (f"{self.name or self.symbol}\n{origin}\nSektor-Gruppe: {self.group}\nMarktwert: {fmt_eur(self.value)} ({fmt_pct(self.weight_pct)})\n"
                f"Tagesperformance: {change}{' (des ETF)' if self.lookthrough else ''}\nStrategie: {self.strategy}")


@dataclass
class SectorVM:
    sector: Optional[str]
    kind: str
    label: str
    value: float
    weight_pct: float
    direct_value: float
    etf_value: float
    count: int
    direct_count: int
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
        if self.kind == KIND_SECTOR:
            return self.label or self.sector or UNASSIGNED
        if self.kind == KIND_ETF_CATEGORY:
            return f"{self.label} (ETF, nicht zuordenbar)"
        return UNASSIGNED


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
    etf_rows: list[list[str]] = field(default_factory=list)
    etf_levels: list[str] = field(default_factory=list)
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


def _day(value: Any) -> str:
    text = str(value or "")
    return f"{text[8:10]}.{text[5:7]}.{text[:4]}" if len(text) >= 10 and text[4] == "-" else (text or DASH)


def _companies_text(p: dict) -> str:
    names = [str(_d(c).get("name") or "") for c in _l(p.get("companies")) if _d(c).get("name")]
    more = int(p.get("company_count") or 0) - len(names)
    return ", ".join(names) + (f" +{more}" if names and more > 0 else "")


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
        kind = group.get("kind") if group.get("kind") in (KIND_SECTOR, KIND_ETF_CATEGORY, KIND_UNASSIGNED) else (KIND_SECTOR if sector else KIND_UNASSIGNED)
        label = str(group.get("label") or sector or "")
        title = label if kind == KIND_SECTOR else f"{label} (ETF, nicht zuordenbar)" if kind == KIND_ETF_CATEGORY else UNASSIGNED
        positions = []
        for p in _l(group.get("positions")):
            p = _d(p)
            value = _num(p.get("market_value_eur"))
            if value is None:
                continue
            change = _num(p.get("day_change_pct"))
            positions.append(PositionVM(int(p.get("security_id") or 0), str(p.get("symbol") or DASH), str(p.get("name") or ""), sector, value, _num(p.get("weight_pct")) or 0.0, change,
                                        bool(p.get("is_etf")), STRATEGY_TEXT.get(str(p.get("strategy")), str(p.get("strategy") or DASH)), performance_color(change),
                                        p.get("origin") if p.get("origin") in ("etf_lookthrough", "etf_company") else "direct", str(p.get("category") or ""), _num(p.get("etf_weight_pct")),
                                        _num(p.get("etf_value_eur")), _day(p.get("etf_as_of")) if p.get("etf_as_of") else "", title, _companies_text(p)))
        change = _num(group.get("day_change_pct"))
        vm.sectors.append(SectorVM(sector, kind, label, _num(group.get("market_value_eur")) or 0.0, _num(group.get("weight_pct")) or 0.0,
                                   _num(group.get("direct_value_eur")) or 0.0, _num(group.get("etf_value_eur")) or 0.0, int(group.get("position_count") or len(positions)),
                                   int(group.get("direct_count") or 0), int(group.get("etf_count") or 0), change, _num(group.get("day_coverage_pct")),
                                   str(group.get("guard_status") or "NOT_ASSESSED"), _num(group.get("limit_pct")), _num(group.get("headroom_pct")), positions, performance_color(change)))
    for group in vm.sectors:
        count = f"{group.direct_count} direkt" + (f", {group.etf_count} ETF" if group.etf_count else "")
        change = fmt_signed_pct(group.change_pct)
        if group.change_pct is not None and group.coverage_pct is not None and group.coverage_pct < 99.5:
            change += f" ({fmt_pct(group.coverage_pct, 0)} des Werts)"
        vm.sector_rows.append([group.title, fmt_eur(group.value), fmt_pct(group.weight_pct), fmt_eur(group.direct_value) if group.direct_value else DASH,
                               fmt_eur(group.etf_value) if group.etf_value else DASH, count, change, guard_text(group)])
        vm.sector_levels.append("blocker" if group.guard == "OVER_LIMIT" else "info" if group.kind != KIND_SECTOR else "")
        for p in group.positions:
            vm.position_rows.append([p.table_label, group.title, p.origin_text, fmt_eur(p.value), fmt_pct(p.weight_pct),
                                     fmt_signed_pct(p.change_pct), "ETF" if p.is_etf else "Aktie", p.strategy])
    total = _num(env.get("total_market_value_eur")) or 0.0
    share = lambda v: fmt_pct(v / total * 100.0 if total > 0 else None)  # noqa: E731 - a display helper of this function
    change = fmt_signed_pct(env.get("day_change_pct"))
    coverage = _num(env.get("day_coverage_pct"))
    if _num(env.get("day_change_pct")) is not None and coverage is not None and coverage < 99.5:
        change += f" ({fmt_pct(coverage, 0)} des Werts)"
    lt = _d(env.get("lookthrough"))
    direct, etf = _num(env.get("direct_value_eur")) or 0.0, _num(env.get("etf_value_eur")) or 0.0
    assigned = _num(env.get("assigned_value_eur")) or 0.0
    summary = [
        ("Bewertungsdatum", str(env.get("as_of") or DASH)),
        ("Investierter Wert", fmt_eur(total)),
        ("Direkt", f"{fmt_eur(direct)} ({share(direct)})"),
        ("Über ETF-Look-through", f"{fmt_eur(etf)} ({share(etf)})"),
        ("Sektor-Gruppen zugeordnet", f"{fmt_eur(assigned)} ({share(assigned)})"),
        ("Davon ETF-Einzelfirmen (Sektor der Firma bekannt)", f"{fmt_eur(env.get('etf_company_value_eur'))} ({share(_num(env.get('etf_company_value_eur')) or 0.0)})"),
        ("ETF-Kategorien nicht zuordenbar", fmt_eur(env.get("etf_category_value_eur")) + f" ({share(_num(env.get('etf_category_value_eur')) or 0.0)})"),
        (UNASSIGNED, f"{fmt_eur(env.get('unassigned_value_eur'))} ({share(_num(env.get('unassigned_value_eur')) or 0.0)})"),
        ("Look-through-Abdeckung", (f"{fmt_pct(lt.get('coverage_pct'), 0)} des ETF-Werts ({int(lt.get('etf_count') or 0)} ETFs), davon {fmt_pct(lt.get('mapped_pct'), 0)} einer Sektor-Gruppe zugeordnet"
                                    if lt.get("etf_count") else "keine ETFs") if env.get("lookthrough_enabled", True) else "aus"),
        ("Tagesperformance", change),
        ("Sektor-Limit (Guard)", fmt_pct(env.get("limit_pct"), 0)),
    ]
    vm.summary = summary
    for item in _l(env.get("etfs")):
        item = _d(item)
        status = str(item.get("status") or "")
        vm.etf_rows.append([str(item.get("symbol") or DASH), fmt_eur(item.get("market_value_eur")), LOOKTHROUGH_TEXT.get(status, status or DASH), _day(item.get("as_of")) if item.get("as_of") else DASH,
                            str(item.get("provider") or DASH), fmt_pct(item.get("mapped_pct"), 0) if item.get("mapped_pct") is not None else DASH])
        vm.etf_levels.append("" if status in ("APPLIED",) else "warning")
    for item in _l(env.get("unvalued")):
        vm.notes.append(f"{_d(item).get('symbol', DASH)}: ohne EUR-Bewertung, nicht in den Werten enthalten")
    for warning in _l(env.get("warnings")):
        vm.notes.append(str(warning))
    vm.notes.append("Firmen in ETFs, deren Sektor bekannt ist, zählen in diesem Sektor (Herkunft „ETF-Einzelfirmen“, gleiche Zuordnung wie eine direkte Position). "
                    "Der Rest der ETFs mit offizieller Anbieter-Sektorverteilung wird in die Kategorien aufgeteilt (Herkunft „ETF-Look-through“); Kategorien, die zu keiner einzelnen Sektor-Gruppe passen, "
                    "bleiben eigene Gruppen. Der Risk Guard wertet weiter nur die direkte Zuordnung aus. Die Tagesperformance ist die Kursänderung in Handelswährung (ohne Währungseffekt).")
    return vm
