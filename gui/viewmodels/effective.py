# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""View model of the "Einzelwertrisiko / Effective Exposure" section: ``get_effective_exposure`` payload -> the company table, the ETF coverage table and the notes.

Pure Python (no Qt, no database).  Nothing is computed here beyond formatting; every figure (direct, ETF and effective value and weight, contributors, coverage, checks)
comes from the ``get_effective_exposure`` result.  The section only informs: no limit is applied and no risk guard is read or changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from .portfolio import DASH, fmt_eur, fmt_pct, fmt_time, _num
from .treemap import NEUTRAL, performance_color

COMPANY_HEADERS = ["Unternehmen / Symbol", "Direkt €", "Direkt %", "ETF €", "ETF %", "Effektiv €", "Effektiv %", "Beitragende ETFs", "Datenstand"]
EFFECTIVE_COLUMNS = (5, 6)  # the columns the page highlights: the economic total per company
NUMERIC_COLUMNS = (1, 2, 3, 4, 5, 6)
DEFAULT_SORT_COLUMN = 6  # Effektiv %, descending
ETF_HEADERS = ["ETF", "Wert €", "Daten", "Stand", "Art / Quelle", "Holdings", "Aktien-Gewicht", "Direkt gehaltene Firmen", "Nur über ETFs", "Unklar", "Sonstiges / Rundung"]
STATUS_TEXT = {"APPLIED": "angewendet", "STALE": "angewendet (veraltet)", "NO_DATA": "keine Daten", "ALLOCATION_INVALID": "Daten ungültig", "NO_ISIN": "keine ISIN"}
KIND_TEXT = {"physical": "physisch (Fondsvermögen)", "synthetic_index": "synthetisch (Index-Zusammensetzung)"}
MATCH_TEXT = {"isin": "ISIN", "alias": "geprüfte Zuordnung", "ticker_exchange": "Ticker + Börse", "standalone": "ohne Portfolio-Wertpapier", "ambiguous": "mehrdeutig", "name_only": "nur Name"}
CATEGORY_TEXT = {"direct_matched": "direkt gehalten", "etf_only": "nur über ETFs (eindeutig identifiziert)", "ambiguous_unmatched": "unklar / nicht zuordenbar"}


@dataclass
class CompanyVM:
    name: str
    ticker: str
    direct_value: float
    direct_weight: float
    etf_value: float
    etf_weight: float
    effective_value: float
    effective_weight: float
    contributors: list[dict] = field(default_factory=list)
    matched: bool = False
    isin: str = ""
    category: str = "etf_only"
    etf_info: dict = field(default_factory=dict)  # ETF symbol -> {"as_of", "source", "status", "provider"} of the holdings it was computed from
    security_id: Optional[int] = None
    key: str = ""  # the company key of the backend (one per company, share classes of one issuer share one)
    change_pct: Optional[float] = None  # the daily change of the company's OWN share (direct position); never the change of an ETF

    @property
    def hints(self) -> list[str]:
        """Matching hints that matter: a reviewed alias, an issuer group (share classes), an exact ticker + exchange match."""
        bases = {str(c.get("match")) for c in self.contributors}
        out = []
        if "alias" in bases:
            out.append("Zuordnungshinweis: Die Holding wurde über eine geprüfte Zuordnung (zum Beispiel ADR ↔ Stammaktie desselben Emittenten) mit dem Wertpapier des Portfolios zusammengeführt.")
        if "ticker_exchange" in bases:
            out.append("Zuordnungshinweis: Zuordnung über denselben Ticker an derselben Börse.")
        if self.key.startswith("grp:"):
            out.append("Zuordnungshinweis: Gattungen eines Emittenten (zum Beispiel Alphabet A und C) sind als eine Firma zusammengeführt.")
        return out

    @property
    def tile_tip(self) -> str:
        """The tooltip of the treemap tile: everything of the table tooltip plus the daily change of the company's own share and the matching hints."""
        lines = [self.tip]
        lines.append("Tagesperformance der Aktie: " + (f"{'+' if self.change_pct > 0 else ''}{fmt_pct(self.change_pct, 2)}" if self.change_pct is not None
                                                      else "nicht verfügbar (die Performance eines ETF wird nie als Unternehmensperformance verwendet)"))
        lines.extend(self.hints)
        return "\n".join(lines)

    @property
    def label(self) -> str:
        """The company and, if there is one, its symbol: ``TSMC (ADR) · TSM``."""
        return f"{self.name} · {self.ticker}" if self.ticker and self.ticker.strip().lower() != self.name.strip().lower() else self.name

    @property
    def stand_text(self) -> str:
        """The stand of the holdings the ETF share is based on, per contributing ETF (``IS3N 12.10.``); a stale one is marked.  A company without an ETF share has none."""
        parts = []
        for c in self.contributors:
            info = self.etf_info.get(str(c.get("etf")), {})
            day = _day(c.get("etf_as_of") or info.get("as_of"))
            parts.append(f"{c.get('etf')} {day[:5] + '.' if len(day) >= 10 and day[2] == '.' else day}" + (" ⚠ veraltet" if info.get("status") == "STALE" else ""))
        return "; ".join(parts) or DASH

    @property
    def overlap(self) -> bool:
        return self.direct_value > 0 and self.etf_value > 0

    @property
    def contributors_text(self) -> str:
        return "; ".join(f"{c.get('etf')} {fmt_eur(c.get('value_eur'))} ({fmt_pct(c.get('weight_in_etf_pct'), 2)} im ETF)" for c in self.contributors) or DASH

    @property
    def tip(self) -> str:
        lines = [f"{self.name}" + (f" ({self.ticker})" if self.ticker else ""), f"ISIN: {self.isin or DASH}",
                 f"Direkt: {fmt_eur(self.direct_value)} ({fmt_pct(self.direct_weight, 2)})", f"Über ETFs: {fmt_eur(self.etf_value)} ({fmt_pct(self.etf_weight, 2)})",
                 f"Effektiv: {fmt_eur(self.effective_value)} ({fmt_pct(self.effective_weight, 2)})"]
        for c in self.contributors:
            info = self.etf_info.get(str(c.get("etf")), {})
            lines.append(f"• {c.get('etf')}: {fmt_eur(c.get('value_eur'))}, {fmt_pct(c.get('weight_in_etf_pct'), 3)} des ETF "
                         f"(Holding „{c.get('holding_name')}“, Zuordnung: {MATCH_TEXT.get(str(c.get('match')), c.get('match'))})")
            lines.append(f"   Datenstand {_day(c.get('etf_as_of') or info.get('as_of')) if (c.get('etf_as_of') or info.get('as_of')) else DASH}"
                         + (f" ({info.get('provider')})" if info.get("provider") else "") + (" – veraltet" if info.get("status") == "STALE" else "")
                         + (f"\n   Quelle: {info.get('source')}" if info.get("source") else ""))
        lines.append("Einordnung: " + CATEGORY_TEXT.get(self.category, self.category))
        lines.append("Als Wertpapier des Portfolios erkannt: " + ("ja" if self.matched else "nein (eigene Firma aus den ETF-Holdings, eindeutig identifiziert)" if self.category == "etf_only" else "nein"))
        return "\n".join(lines)


@dataclass
class TileVM:
    """One tile of the exposure treemap: a company, or one of the separate blocks (the rest of the companies, cash and derivatives, ETFs without holdings)."""

    kind: str  # "company", "rest", "other", "unresolved"
    label: str
    value: float
    weight_pct: float
    color: str
    tip: str


@dataclass
class EffectiveVM:
    available: bool = False
    error: str = ""
    refreshed: str = DASH
    summary: list[tuple[str, str]] = field(default_factory=list)
    companies: list[CompanyVM] = field(default_factory=list)
    company_rows: list[list[str]] = field(default_factory=list)
    company_levels: list[str] = field(default_factory=list)
    etf_rows: list[list[str]] = field(default_factory=list)
    etf_levels: list[str] = field(default_factory=list)
    etf_tips: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    company_keys: list[list[Any]] = field(default_factory=list)  # per row and column the value the column sorts by (None = no value, sorted last)
    headline: str = ""  # the biggest effective single position, as one line
    headline_detail: str = ""
    chips: list[tuple[str, str, str]] = field(default_factory=list)  # (label, value, level) -- the compact figures at the top
    tiles: list[TileVM] = field(default_factory=list)  # the treemap: one tile per company (largest first) plus the separate blocks; never the rounding drift


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _day(value: Any) -> str:
    text = str(value or "")
    return f"{text[8:10]}.{text[5:7]}.{text[:4]}" if len(text) >= 10 and text[4] == "-" else (text or DASH)


def fmt_signed(value: Any) -> str:
    n = _num(value)
    return DASH if n is None else f"{'+' if n > 0 else ''}{fmt_pct(n, 2)}"


def _bucket_lines(b: dict) -> list[tuple[str, str]]:
    """The four buckets (plus the rounding drift of a published index list) that add up to the invested value."""
    def line(label: str, key: str, extra: str = "") -> tuple[str, str]:
        x = _d(b.get(key))
        return label, f"{fmt_eur(x.get('value_eur'))} ({fmt_pct(x.get('pct_of_total'), 2)}){extra}"

    lines = [line("Direkt gehalten (inkl. ETF-Anteil)", "direct_matched"),
             line("Nur über ETFs (eindeutig identifiziert)", "etf_only"),
             line("Unklar / nicht zuordenbar", "ambiguous_unmatched"),
             line("Sonstiges (Cash, Derivate, Rest)", "other")]
    if _d(b.get("rounding_drift")).get("value_eur"):
        lines.append(line("Rundungsabweichung (Index-Liste)", "rounding_drift"))
    return lines if b else [("Aufteilung", DASH)]


def holdings_freshness(env: dict) -> tuple[str, str]:
    """``(text, level)`` of the holdings the exposure is based on: current / stale / incomplete, with the oldest stand."""
    etfs = [e for e in _l(env.get("etfs")) if isinstance(e, dict)]
    if not etfs:
        return "keine ETFs", ""
    stale = [e for e in etfs if e.get("status") == "STALE"]
    missing = [e for e in etfs if e.get("status") not in ("APPLIED", "STALE")]
    used = [e for e in etfs if e.get("status") in ("APPLIED", "STALE") and e.get("as_of")]
    oldest = min(used, key=lambda e: str(e["as_of"])) if used else None
    age = ""
    if oldest is not None:
        try:
            from datetime import date as _date

            days = (_date.fromisoformat(str(env.get("as_of"))[:10]) - _date.fromisoformat(str(oldest["as_of"])[:10])).days
            age = f", {days} Tage"
        except ValueError:
            pass
    if missing:
        return "unvollständig: " + ", ".join(f"{e.get('symbol')} ({STATUS_TEXT.get(str(e.get('status')), e.get('status'))})" for e in missing), "blocker"
    if stale:
        return "veraltet: " + ", ".join(f"{e.get('symbol')} (Stand {_day(e.get('as_of'))})" for e in stale), "warning"
    return f"aktuell – ältester Stand {_day(oldest['as_of'])}{age}" if oldest else "aktuell", ""


def _headline(vm: "EffectiveVM", env: dict) -> None:
    """The compact figures on top: the biggest effective single position, its direct / ETF split, the number of companies over 5 % and 10 %, the holdings freshness."""
    if vm.companies:
        top = max(vm.companies, key=lambda c: c.effective_weight)
        share = (lambda part: fmt_pct(part / top.effective_value * 100.0, 0)) if top.effective_value > 0 else (lambda part: DASH)
        vm.headline = f"{top.label} – {fmt_pct(top.effective_weight, 2)} effektiv"
        vm.headline_detail = (f"Direkt {fmt_pct(top.direct_weight, 2) if top.direct_value else DASH} · ETF {fmt_pct(top.etf_weight, 2) if top.etf_value else DASH}"
                              f" (Anteil direkt / ETF: {share(top.direct_value)} / {share(top.etf_value)})")
    else:
        vm.headline, vm.headline_detail = DASH, ""
    over5 = sum(1 for c in vm.companies if c.effective_weight > 5.0)
    over10 = sum(1 for c in vm.companies if c.effective_weight > 10.0)
    text, level = holdings_freshness(env)
    vm.chips = [("Firmen über 5 %", str(over5), ""), ("Firmen über 10 %", str(over10), "warning" if over10 else ""), ("Holdings-Daten", text, level)]


def direct_performance(sector_envelope: Any) -> dict[int, float]:
    """``{security_id: daily change in %}`` of the DIRECT, non-ETF positions of a ``get_sector_overview`` answer -- the company's own share price.  The change of an ETF
    (or of an ETF look-through entry) is never included, so it can never be mistaken for the performance of a company inside it."""
    out: dict[int, float] = {}
    for group in _l(_d(sector_envelope).get("sectors")):
        for p in _l(_d(group).get("positions")):
            p = _d(p)
            change = _num(p.get("day_change_pct"))
            if p.get("origin", "direct") == "direct" and not p.get("is_etf") and change is not None and isinstance(p.get("security_id"), int):
                out[p["security_id"]] = change
    return out


def _merge_by_key(companies: list[CompanyVM]) -> list[CompanyVM]:
    """One entry per company key (defensive: the backend already aggregates; a repeated key is added up, never counted twice as two companies)."""
    merged: dict[str, CompanyVM] = {}
    for c in companies:
        key = c.key or f"name:{c.name}"
        if key not in merged:
            merged[key] = c
            continue
        m = merged[key]
        m.direct_value += c.direct_value
        m.direct_weight += c.direct_weight
        m.etf_value += c.etf_value
        m.etf_weight += c.etf_weight
        m.effective_value += c.effective_value
        m.effective_weight += c.effective_weight
        m.contributors = m.contributors + c.contributors
    return list(merged.values())


def _tiles(vm: "EffectiveVM", env: dict, companies: list[CompanyVM]) -> list[TileVM]:
    total = _num(env.get("total_market_value_eur")) or 0.0
    weight = lambda value: value / total * 100.0 if total > 0 else 0.0  # noqa: E731 - a display helper
    tiles = [TileVM("company", c.label, c.effective_value, c.effective_weight, performance_color(c.change_pct) if c.change_pct is not None else NEUTRAL, c.tile_tip)
             for c in sorted(companies, key=lambda c: (-c.effective_value, c.name)) if c.effective_value > 0]
    chk, buckets = _d(env.get("checks")), _d(env.get("buckets"))
    listed = sum(t.value for t in tiles)
    rest = (_num(chk.get("companies_eur")) or 0.0) - listed
    remaining = int(env.get("company_count") or 0) - len(companies)
    if rest > 0.5 and remaining > 0:
        tiles.append(TileVM("rest", f"Weitere {remaining} Firmen", rest, weight(rest), "#c9d1dc",
                            f"Weitere {remaining} Firmen\nSumme: {fmt_eur(rest)} ({fmt_pct(weight(rest), 2)})\nJede dieser Firmen liegt unter der kleinsten gezeigten Kachel."))
    other = _num(_d(buckets.get("other")).get("value_eur")) or 0.0
    if other > 0.5:
        tiles.append(TileVM("other", "Sonstiges", other, weight(other), "#d9dde3", f"Sonstiges (Cash, Derivate, nicht erklärter Rest eines ETF)\n{fmt_eur(other)} ({fmt_pct(weight(other), 2)}); keine Firma."))
    unresolved = _num(_d(buckets.get("ambiguous_unmatched")).get("unresolved_etf_value_eur")) or 0.0
    if unresolved > 0.5:
        tiles.append(TileVM("unresolved", "ETFs ohne Holdings", unresolved, weight(unresolved), "#e8d5d0",
                            f"ETFs ohne Holdings-Daten\n{fmt_eur(unresolved)} ({fmt_pct(weight(unresolved), 2)}); nicht auf Firmen aufgelöst."))
    return tiles  # the rounding drift is a reconciliation bucket (negative); it is never a tile


def build_effective(envelope: Any, refreshed: Optional[datetime] = None, performance: Optional[Mapping[int, float]] = None) -> EffectiveVM:
    vm = EffectiveVM(refreshed=fmt_time(refreshed))
    env = _d(envelope)
    if not env:
        vm.error = "Keine Daten"
        return vm
    if env.get("ok") is False or not isinstance(env.get("top"), list):
        vm.error = str(env.get("message") or env.get("error") or "Einzelwert-Auswertung nicht verfügbar")
        return vm
    vm.available = True
    etf_info = {str(_d(e).get("symbol")): {"as_of": _d(e).get("as_of"), "source": _d(e).get("source"), "status": _d(e).get("status"), "provider": _d(e).get("provider")}
                for e in _l(env.get("etfs")) if isinstance(e, dict)}
    for raw in _l(env["top"]):
        c = _d(raw)
        if _num(c.get("effective_value_eur")) is None:  # a row without its main figure is not a company row
            continue
        vm.companies.append(CompanyVM(str(c.get("name") or DASH), str(c.get("ticker") or ""), _num(c.get("direct_value_eur")) or 0.0, _num(c.get("direct_weight_pct")) or 0.0,
                                      _num(c.get("etf_value_eur")) or 0.0, _num(c.get("etf_weight_pct")) or 0.0, _num(c.get("effective_value_eur")) or 0.0,
                                      _num(c.get("effective_weight_pct")) or 0.0, [_d(x) for x in _l(c.get("contributors"))], bool(c.get("matched")), str(c.get("isin") or ""), str(c.get("category") or "etf_only"), etf_info,
                                      c.get("security_id") if isinstance(c.get("security_id"), int) else None, str(c.get("key") or "")))
        company = vm.companies[-1]
        if performance and company.security_id is not None and company.direct_value > 0:
            company.change_pct = performance.get(company.security_id)  # only a company with a direct position has a price of its own here
    for company in vm.companies:
        vm.company_rows.append([company.label, fmt_eur(company.direct_value) if company.direct_value else DASH, fmt_pct(company.direct_weight, 2) if company.direct_value else DASH,
                                fmt_eur(company.etf_value) if company.etf_value else DASH, fmt_pct(company.etf_weight, 2) if company.etf_value else DASH,
                                fmt_eur(company.effective_value), fmt_pct(company.effective_weight, 2), company.contributors_text, company.stand_text])
        vm.company_keys.append([company.name.lower(), company.direct_value or None, company.direct_weight if company.direct_value else None, company.etf_value or None,
                                company.etf_weight if company.etf_value else None, company.effective_value, company.effective_weight, company.contributors_text.lower(), company.stand_text.lower()])
        vm.company_levels.append("warning" if company.category == "ambiguous_unmatched" else "info" if company.overlap else "")
    for raw in _l(env.get("etfs")):
        if not isinstance(raw, dict):
            continue
        e = raw
        status = str(e.get("status") or "")
        applied = status in ("APPLIED", "STALE")
        vm.etf_rows.append([str(e.get("symbol") or DASH), fmt_eur(e.get("value_eur")), STATUS_TEXT.get(status, status or DASH), _day(e.get("as_of")) if e.get("as_of") else DASH,
                            (KIND_TEXT.get(str(e.get("kind")), str(e.get("kind") or DASH)) + (f" · {e.get('provider')}" if e.get("provider") else "")) if applied else DASH,
                            f"{int(e.get('holdings_count') or 0)} ({int(e.get('equity_count') or 0)} Aktien)" if applied else DASH,
                            fmt_pct(e.get("equity_weight_pct"), 1) if applied else DASH]
                           + ([f"{fmt_pct(e.get(f'{k}_weight_pct'), 1)} ({int(e.get(f'{k}_count') or 0)})" for k in ("direct_matched", "etf_only", "ambiguous_unmatched")] if applied else [DASH] * 3)
                           + [(fmt_pct(e.get("other_weight_pct"), 1) + (f" / {fmt_signed(e.get('weights_drift_pct'))}" if e.get("weights_drift_pct") is not None else "")) if applied else DASH])
        vm.etf_levels.append("" if status == "APPLIED" else "warning")
        tip = [f"{e.get('symbol')}: {e.get('name') or ''}", f"Quelle: {e.get('source') or DASH}", f"Stand: {_day(e.get('as_of')) if e.get('as_of') else DASH}"]
        if applied:
            tip.append(f"Summe der veröffentlichten Gewichte: {fmt_pct(e.get('weights_total_pct'), 2)}" + (f" (Rundungsabweichung {fmt_pct(e.get('weights_drift_pct'), 2)} über 100 %: die Gewichte werden nicht skaliert, die Abweichung steht als negativer Block {fmt_eur(e.get('rounding_drift_eur'))})"
                                                                                                          if e.get("weights_drift_pct") is not None else ""))
            tip.append(f"Direkt gehaltene Firmen: {int(e.get('direct_matched_count') or 0)} ({fmt_pct(e.get('direct_matched_weight_pct'), 2)}); nur über ETFs, eindeutig identifiziert: "
                       f"{int(e.get('etf_only_count') or 0)} ({fmt_pct(e.get('etf_only_weight_pct'), 2)}); unklar (mehrdeutig oder ohne Kennung): {int(e.get('ambiguous_unmatched_count') or 0)} "
                       f"({fmt_pct(e.get('ambiguous_unmatched_weight_pct'), 3)})")
            tip.append(f"Gewicht mit ISIN: {fmt_pct(e.get('isin_weight_pct'), 1)}")
            if e.get("note"):
                tip.append(str(e["note"]))
        else:
            tip.append(f"Nicht aufgelöst: {e.get('detail') or status}")
        vm.etf_tips.append("\n".join(tip))
    _headline(vm, env)
    vm.tiles = _tiles(vm, env, _merge_by_key([CompanyVM(**{**c.__dict__, "contributors": list(c.contributors)}) for c in vm.companies]))
    cov, chk = _d(env.get("coverage")), _d(env.get("checks"))
    total = _num(env.get("total_market_value_eur")) or 0.0
    vm.summary = [
        ("Bewertungsdatum", str(env.get("as_of") or DASH)),
        ("Investierter Wert", fmt_eur(total)),
        ("Firmen insgesamt", f"{int(env.get('company_count') or 0)} (direkt {int(env.get('direct_company_count') or 0)}, über ETFs {int(env.get('etf_company_count') or 0)}, beides {int(env.get('overlap_company_count') or 0)})"),
        (f"Top {int(env.get('top_count') or 0)}", fmt_pct(env.get("top_share_pct"), 1) + " des Werts"),
        ("ETF-Holdings-Abdeckung", fmt_pct(cov.get("resolved_pct"), 0) + " des ETF-Werts"),
        *_bucket_lines(_d(env.get("buckets"))),
        ("Summen stimmen", "ja" if chk.get("reconciles") else "NEIN" if chk.get("reconciles") is False else DASH),
    ]
    for item in _l(env.get("unvalued")):
        vm.notes.append(f"{_d(item).get('symbol', DASH)}: ohne EUR-Bewertung, nicht in den Werten enthalten")
    for warning in _l(env.get("warnings")):
        vm.notes.append(str(warning))
    vm.notes.append("Effektive Exposure = direkte Position + Anteil derselben Firma in allen gehaltenen ETFs (Marktwert × Holding-Gewicht laut Anbieter); eine Firma zählt nur einmal. "
                    "Die Zuordnung läuft über ISIN, eine geprüfte Zuordnung oder exakt Ticker + Börse, nie unscharf. Bei synthetischen ETFs zählt die Index-Zusammensetzung, nicht der Collateral-Korb; ihre veröffentlichten Gewichte werden nicht auf 100 % skaliert, eine Abweichung steht als Rundungsabweichung. "
                    "Es wird kein Limit angewendet; der Risk Guard ist unverändert.")
    return vm
