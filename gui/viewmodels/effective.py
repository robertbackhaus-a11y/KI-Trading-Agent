"""View model of the "Einzelwertrisiko / Effective Exposure" section: ``get_effective_exposure`` payload -> the company table, the ETF coverage table and the notes.

Pure Python (no Qt, no database).  Nothing is computed here beyond formatting; every figure (direct, ETF and effective value and weight, contributors, coverage, checks)
comes from the ``get_effective_exposure`` result.  The section only informs: no limit is applied and no risk guard is read or changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .portfolio import DASH, fmt_eur, fmt_pct, fmt_time, _num

COMPANY_HEADERS = ["Firma", "Direkt €", "Direkt %", "ETF €", "ETF %", "Effektiv €", "Effektiv %", "ETF-Beiträge"]
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
            lines.append(f"• {c.get('etf')}: {fmt_eur(c.get('value_eur'))}, {fmt_pct(c.get('weight_in_etf_pct'), 3)} des ETF "
                         f"(Holding „{c.get('holding_name')}“, Zuordnung: {MATCH_TEXT.get(str(c.get('match')), c.get('match'))}, Stand {_day(c.get('etf_as_of')) if c.get('etf_as_of') else DASH})")
        lines.append("Einordnung: " + CATEGORY_TEXT.get(self.category, self.category))
        lines.append("Als Wertpapier des Portfolios erkannt: " + ("ja" if self.matched else "nein (eigene Firma aus den ETF-Holdings, eindeutig identifiziert)" if self.category == "etf_only" else "nein"))
        return "\n".join(lines)


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


def build_effective(envelope: Any, refreshed: Optional[datetime] = None) -> EffectiveVM:
    vm = EffectiveVM(refreshed=fmt_time(refreshed))
    env = _d(envelope)
    if not env:
        vm.error = "Keine Daten"
        return vm
    if env.get("ok") is False or not isinstance(env.get("top"), list):
        vm.error = str(env.get("message") or env.get("error") or "Einzelwert-Auswertung nicht verfügbar")
        return vm
    vm.available = True
    for raw in _l(env["top"]):
        c = _d(raw)
        if _num(c.get("effective_value_eur")) is None:  # a row without its main figure is not a company row
            continue
        vm.companies.append(CompanyVM(str(c.get("name") or DASH), str(c.get("ticker") or ""), _num(c.get("direct_value_eur")) or 0.0, _num(c.get("direct_weight_pct")) or 0.0,
                                      _num(c.get("etf_value_eur")) or 0.0, _num(c.get("etf_weight_pct")) or 0.0, _num(c.get("effective_value_eur")) or 0.0,
                                      _num(c.get("effective_weight_pct")) or 0.0, [_d(x) for x in _l(c.get("contributors"))], bool(c.get("matched")), str(c.get("isin") or ""), str(c.get("category") or "etf_only")))
    for company in vm.companies:
        vm.company_rows.append([company.name, fmt_eur(company.direct_value) if company.direct_value else DASH, fmt_pct(company.direct_weight, 2) if company.direct_value else DASH,
                                fmt_eur(company.etf_value) if company.etf_value else DASH, fmt_pct(company.etf_weight, 2) if company.etf_value else DASH,
                                fmt_eur(company.effective_value), fmt_pct(company.effective_weight, 2), company.contributors_text])
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
