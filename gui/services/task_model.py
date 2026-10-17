# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The operational model of the seven scheduled tasks: what each one produces, what it really needs, and how old its data may be.  Pure Python (no Qt, no I/O).

**Dependencies are technical facts, not a Windows task chain.**  They were derived from what each script reads (checked in the code, not guessed from the
clock times); a task never waits for or starts another one, and no scheduler definition is touched::

    MarketData        needs nothing from another task (Yahoo + the database); writes market_data / market_snapshot
    FXRates           needs nothing (ECB); writes fx_rates
    EventsNews        needs nothing (SEC / Yahoo); writes events and news
    Fundamentals-SEC  needs nothing (SEC); weekly and separate; writes fundamentals
    Candidate-Discovery
                      reads fx_rates of the database (EUR conversion: a currency without a usable rate is rejected, UNSUPPORTED_CURRENCY; the median daily
                      value in EUR) -> HARD on FX.  It fetches its own Yahoo history, so it does NOT read market_data; it reads no news or events either.
    Market-Intelligence
                      reads the cached company news and SEC events of the database -> HARD on EventsNews; reads the last Discovery report for the Yahoo headlines
                      of the candidates -> SOFT on Discovery (without a report it continues with DISCOVERY_REPORT_UNAVAILABLE); it reads no prices and no FX.

    ETF-Refresh       needs nothing from another task (the providers' published files); weekly (Sunday 11:00, after Fundamentals); writes the two private ETF files
                      (holdings and sector allocation); the stand of each dataset is judged from the files, the health of a run from its exit code (0 only if nothing failed)

The consumers of MarketData, FX and Fundamentals are the portfolio analysis behind the MCP tools (valuation, decisions, the planner), not another task.

The data freshness of a source is judged from the *data*, not from the exit code of its task.  The limits are the project's own rules where one exists
(``market_data_max_age_days``, ``fx_max_age_days``, ``report_max_age_hours``, the discovery report's market-data rule; delivered by ``get_data_freshness``);
for the rest a limit follows from the schedule: "current" is the normal age right before the next regular run (cadence plus a grace period), "stale" is
more than one missed run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

# freshness levels
OK, AGING, STALE, UNKNOWN = "ok", "aging", "stale", "unknown"

# the data source keys of ``get_data_freshness``
MARKET_DATA, FX, EVENTS_NEWS, FUNDAMENTALS, DISCOVERY, INTELLIGENCE = "market_data", "fx", "events_news", "fundamentals", "discovery", "market_intelligence"
ETF_DATA = "etf_data"
HARD, SOFT = "hard", "soft"

# limits that follow from the schedule (hours): "current" up to the first, "stale" beyond the second
NEWS_FRESH_H, NEWS_STALE_H = 36.0, 72.0  # daily collector: normal age up to ~25 h, plus a grace period; stale after more than two missed runs
FUNDAMENTALS_FRESH_H, FUNDAMENTALS_STALE_H = 8 * 24.0, 15 * 24.0  # weekly task: the age of its last SUCCESSFUL RUN (not of the newest filing): a week plus a day; stale after a missed week and more
DISCOVERY_FRESH_H = 36.0  # daily report; stale = the project's REPORT_STALE rule (evaluation date older than discovery_report_max_age_days)
ISHARES_FRESH_D = 10  # iShares publishes daily; the weekly run (Sunday) leaves data of the Friday before, up to a week old, plus a grace period of a few days
INVESCO_FRESH_D = 45  # Invesco publishes the index composition monthly (the stand is a month end): up to ~45 days is normal; the loaders' own 120-day limit decides "stale"
MANUAL_FRESH_D = 90  # the manually kept SPXS sector allocation (factsheet): informational, never a failure of the task
INTELLIGENCE_FRESH_H = 15.0  # twice a day (08:15 / 17:45): the longest regular gap is the night, 14.5 h; stale = the project's report_max_age_hours (24)


@dataclass(frozen=True)
class Need:
    source: str  # the data source key the task reads
    task: str  # the task that produces it
    kind: str  # HARD (the result is wrong or incomplete without it) or SOFT (the task goes on without it)
    reason: str  # what the script reads, in words


@dataclass(frozen=True)
class TaskSpec:
    name: str
    title: str
    produces: str  # the data source key of its output
    needs: tuple[Need, ...] = ()
    reads_by: str = ""  # who uses the output (not a task)
    note: str = ""


MD, FXT, EN, FS, CD, MI, ER = ("Trading-MarketData-Backfill", "Trading-FXRates-Backfill", "Trading-EventsNews-Backfill", "Trading-Fundamentals-SEC",
                               "Trading-Candidate-Discovery", "Trading-Market-Intelligence", "Trading-ETF-Refresh")

SPECS: dict[str, TaskSpec] = {
    MD: TaskSpec(MD, "Marktdaten", MARKET_DATA, (), "Bewertung, Entscheidungen und Planner der MCP-Tools",
                 "Tageskurse und Snapshots (Yahoo); braucht keinen anderen Task."),
    FXT: TaskSpec(FXT, "FX-Kurse", FX, (), "EUR-Bewertung von USD/GBP/AUD/KRW-Positionen in den MCP-Tools", "EZB-Referenzkurse; braucht keinen anderen Task."),
    EN: TaskSpec(EN, "Events / News", EVENTS_NEWS, (), "Market Intelligence (liest die gespeicherten News und SEC-Events)", "Unternehmens-News und SEC-Events; braucht keinen anderen Task."),
    FS: TaskSpec(FS, "Fundamentals (SEC)", FUNDAMENTALS, (), "Fundamental-Bewertung der MCP-Tools", "Wöchentlich und eigenständig; schreibt nur neue Filings."),
    CD: TaskSpec(CD, "Candidate Discovery", DISCOVERY, (
        Need(FX, FXT, HARD, "liest fx_rates für die EUR-Umrechnung (ohne verwendbaren Kurs wird die Währung abgelehnt: UNSUPPORTED_CURRENCY; Median-Handelsvolumen in EUR)"),
    ), "Market Intelligence (Headlines der Kandidaten), get_candidate_discovery", "Holt die Kursverläufe selbst von Yahoo: braucht weder Marktdaten noch News aus der Datenbank."),
    MI: TaskSpec(MI, "Market Intelligence", INTELLIGENCE, (
        Need(EVENTS_NEWS, EN, HARD, "liest die gespeicherten Unternehmens-News und SEC-Events aus der Datenbank"),
        Need(DISCOVERY, CD, SOFT, "liest den letzten Discovery-Report für die Yahoo-Headlines der Kandidaten (ohne Report läuft der Task weiter: DISCOVERY_REPORT_UNAVAILABLE)"),
    ), "get_market_intelligence, Opportunity View, Watchlist (News-Status)", "Braucht weder Marktdaten noch FX."),
    ER: TaskSpec(ER, "ETF-Daten", ETF_DATA, (), "Effective Exposure und Einzelwert-Guard, Sektor-Look-through der MCP-Tools",
                 "Wöchentlich sonntags; Holdings (XUSE, IS3N, SPXS) und Sektoren (XUSE, IS3N) aus den Anbieterdaten. Der SPXS-Sektor ist manuell (Invesco veröffentlicht ihn nur als PDF) "
                 "und macht den Task nie zu einem Fehler. Bei einem Fehler bleiben die alten gültigen Dateien bestehen."),
}

SOURCE_TEXT = {MARKET_DATA: "Marktdaten", FX: "FX-Kurse", EVENTS_NEWS: "Events / News", FUNDAMENTALS: "Fundamentals", DISCOVERY: "Discovery-Report", INTELLIGENCE: "Intelligence-Report",
               ETF_DATA: "ETF-Daten"}
LEVEL_TEXT = {OK: "aktuell", AGING: "älter", STALE: "veraltet", UNKNOWN: "unbekannt"}
NO_ERROR_RESULTS = (0, 267009, 267011)  # OK, running, never run


@dataclass(frozen=True)
class RunFacts:
    """What the Task Scheduler knows about the last run of the task that produces a source (used where the data cannot show whether the task is healthy)."""

    last_run: Optional[datetime] = None
    last_result: Optional[int] = None
    missed_runs: Optional[int] = None


@dataclass
class Freshness:
    source: str
    level: str = UNKNOWN
    as_of: str = "—"  # the data stand for the table cell
    detail: str = ""  # all facts and the rule applied, for the tooltip
    last_success: Optional[datetime] = None  # a time at which the task provably produced its output (from the data), local time


def _dt(value: Any) -> Optional[datetime]:
    """A timestamp of the database or a report (``2026-10-14T08:56:16.9+00:00`` or ``2026-10-09 15:25:23``, UTC if it carries no zone) -> local naive time."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().replace(tzinfo=None)


def _day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def business_days_between(start: date, end: date) -> int:
    """Weekdays in ``(start, end]``: the number of trading days a daily price series can be behind (weekends do not count)."""
    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def fmt_dt(value: Optional[datetime]) -> str:
    return "—" if value is None else value.strftime("%d.%m.%Y %H:%M")


def fmt_day(value: Optional[date]) -> str:
    return "—" if value is None else value.strftime("%d.%m.%Y")


def _age_hours(then: Optional[datetime], now: datetime) -> Optional[float]:
    return None if then is None else max(0.0, (now - then).total_seconds() / 3600.0)


def _age_text(hours: Optional[float]) -> str:
    if hours is None:
        return "—"
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} Tage"


def _by_hours(source: str, then: Optional[datetime], now: datetime, fresh_h: float, stale_h: float, rule: str) -> tuple[str, Optional[float]]:
    age = _age_hours(then, now)
    if age is None:
        return UNKNOWN, None
    return (OK if age <= fresh_h else AGING if age <= stale_h else STALE), age


def _by_trading_days(stand: Optional[date], today: date, max_age_days: int) -> tuple[str, Optional[int], Optional[int]]:
    """``(level, trading days behind, calendar days old)``: current = at most one trading day behind, stale = older than the project's calendar-day rule."""
    if stand is None:
        return UNKNOWN, None, None
    behind, calendar = business_days_between(stand, today), (today - stand).days
    return (OK if behind <= 1 else STALE if calendar > max_age_days else AGING), behind, calendar


def evaluate(source: str, payload: Optional[Mapping[str, Any]], now: datetime, run: Optional[RunFacts] = None) -> Freshness:
    """The :class:`Freshness` of one data source from the ``get_data_freshness`` answer (``None`` = the answer is not available)."""
    result = Freshness(source)
    sources = payload.get("sources") if isinstance(payload, Mapping) and isinstance(payload.get("sources"), Mapping) else {}
    facts = sources.get(source) if isinstance(sources.get(source), Mapping) else None
    rules = payload.get("rules") if isinstance(payload, Mapping) and isinstance(payload.get("rules"), Mapping) else {}
    if source == FUNDAMENTALS and run is not None:  # judged by the task's own rhythm, not by the data (see below); the data only inform
        return _fundamentals(facts=sources.get(source) if isinstance(sources.get(source), Mapping) else None, run=run, now=now)
    if facts is None:
        result.detail = "Kein Datenstand verfügbar (MCP nicht verbunden oder Quelle nicht gemeldet)."
        return result
    if facts.get("available") is False:
        result.detail = f"Datenstand nicht lesbar: {facts.get('error') or 'unbekannter Fehler'}"
        return result
    today = now.date()
    if source == MARKET_DATA:
        limit = int(rules.get("market_data_max_age_days") or 5)
        oldest, newest = _day(facts.get("oldest_held_bar_date")), _day(facts.get("latest_bar_date"))
        stand = oldest or newest
        result.level, behind, calendar = _by_trading_days(stand, today, limit)
        result.as_of = fmt_day(stand) if stand else "—"
        fetched = _dt(facts.get("snapshot_fetched_at") or facts.get("bars_fetched_at"))
        result.last_success = fetched
        result.detail = (f"Letzter Tageskurs im Bestand: {fmt_day(newest)}; ältester letzter Kurs einer gehaltenen Position: {fmt_day(oldest)} "
                         f"({behind if behind is not None else '—'} Handelstage zurück); letzter Abruf: {fmt_dt(fetched)}. "
                         f"Regel: aktuell bis 1 Handelstag, veraltet nach {limit} Kalendertagen (DataQualityConfig).")
        if facts.get("held_without_bars"):
            result.level, result.detail = STALE, result.detail + f" {facts['held_without_bars']} gehaltene Position(en) ohne Kurs."
    elif source == FX:
        limit = int(rules.get("fx_max_age_days") or 5)
        oldest, newest = _day(facts.get("oldest_currency_date")), _day(facts.get("latest_rate_date"))
        stand = oldest or newest
        result.level, behind, calendar = _by_trading_days(stand, today, limit)
        result.as_of = fmt_day(stand) if stand else "—"
        fetched = _dt(facts.get("fetched_at"))
        result.last_success = fetched
        result.detail = (f"Letzter Kurs: {fmt_day(newest)}; ältester letzter Kurs einer Währung: {fmt_day(oldest)}"
                         f"{' (' + str(facts['oldest_currency']) + ')' if facts.get('oldest_currency') else ''}; {facts.get('currencies', '—')} Währungen; letzter Abruf: {fmt_dt(fetched)}. "
                         f"Regel: aktuell bis 1 Handelstag, veraltet nach {limit} Kalendertagen (FXConfig).")
    elif source == EVENTS_NEWS:
        news = _dt(facts.get("news_fetched_at"))
        events = _dt(facts.get("events_created_at"))
        result.level, age = _by_hours(source, news, now, NEWS_FRESH_H, NEWS_STALE_H, "")
        result.as_of = fmt_dt(news)
        result.last_success = news
        result.detail = (f"Letzte neue News gespeichert: {fmt_dt(news)} (jüngster Artikel: {fmt_dt(_dt(facts.get('latest_news_published_at')))}); "
                         f"letztes SEC-Event gespeichert: {fmt_dt(events)} (Event vom {fmt_day(_day(facts.get('latest_event_date')))}). "
                         f"Regel (aus dem Rhythmus): aktuell bis {NEWS_FRESH_H:.0f} h, veraltet nach {NEWS_STALE_H:.0f} h; SEC-Events erscheinen unregelmäßig und zählen nicht in die Bewertung. "
                         "Es werden nur neue Zeilen geschrieben: ein Lauf ohne neue Artikel ändert den Stand nicht.")
    elif source == ETF_DATA:
        return _etf_freshness(facts, now)
    elif source == FUNDAMENTALS:  # without the scheduler facts only the information can be shown: the age of the newest filing says nothing about the health of the task
        result.as_of = "—"
        result.detail = _fundamentals_info(facts) + " Ohne Scheduler-Daten ist keine Bewertung möglich (es werden nur neue Filings geschrieben)."
    elif source in (DISCOVERY, INTELLIGENCE):
        if facts.get("status") != "AVAILABLE":
            result.level = STALE if facts.get("reason") else UNKNOWN
            result.as_of = "kein Report"
            result.detail = f"Kein gültiger Report ({facts.get('reason') or 'unbekannt'}). Er wird nur nach einem vollständigen, gültigen Lauf geschrieben."
            return result
        generated = _dt(facts.get("generated_at"))
        result.last_success = generated
        age = _age_hours(generated, now)
        result.as_of = fmt_dt(generated)
        evaluation = _day(facts.get("evaluation_as_of"))
        if source == DISCOVERY:
            limit = int(rules.get("discovery_report_max_age_days") or 5)
            evaluation_age = (today - evaluation).days if evaluation else None
            result.level = (UNKNOWN if age is None else STALE if evaluation_age is not None and evaluation_age > limit else OK if age <= DISCOVERY_FRESH_H else AGING)
            result.detail = (f"Letzter erfolgreicher Lauf (Report erzeugt): {fmt_dt(generated)}, Stand der Auswertung {fmt_day(evaluation)}. "
                             f"Regel: aktuell bis {DISCOVERY_FRESH_H:.0f} h, veraltet wenn der Stand älter als {limit} Tage ist (REPORT_STALE, wie get_candidate_discovery).")
        else:
            limit_h = float(rules.get("intelligence_report_max_age_hours") or 24)
            result.level = UNKNOWN if age is None else OK if age <= INTELLIGENCE_FRESH_H else AGING if age <= limit_h else STALE
            result.detail = (f"Letzter erfolgreicher Lauf (Report erzeugt): {fmt_dt(generated)}, Stand der Auswertung {fmt_day(evaluation)}. "
                             f"Regel: aktuell bis {INTELLIGENCE_FRESH_H:.0f} h (längste reguläre Pause: die Nacht), veraltet nach {limit_h:.0f} h (report_max_age_hours, wie get_market_intelligence).")
    return result


@dataclass(frozen=True)
class EtfDataset:
    """One dataset of the private ETF files (holdings or sector allocation of one ETF) with the verdict on its stand."""

    symbol: str
    dataset: str  # "holdings" | "lookthrough"
    provider: str
    manual: bool
    as_of: Optional[date]
    age_days: Optional[int]
    status: str  # the loader's verdict: APPLIED / STALE / MISSING / INVALID
    level: str  # ok / aging / stale / unknown (a manual dataset is judged for display only)
    rows: Optional[int] = None
    detail: str = ""


def etf_datasets(facts: Optional[Mapping[str, Any]], now: datetime) -> list[EtfDataset]:
    """The six datasets in the order of the page (holdings of XUSE, IS3N, SPXS, then the sector allocations) from the ``etf_data`` source of ``get_data_freshness``.

    The loaders' own limit (``stale_after_days``, 120) decides "stale"; below it the stand is "current" up to the provider's cadence (iShares: ``ISHARES_FRESH_D`` days,
    Invesco's monthly list: ``INVESCO_FRESH_D``) and "older" beyond.  The manual SPXS sector allocation is judged against ``MANUAL_FRESH_D`` and the loader limit for the display only.
    """
    if not isinstance(facts, Mapping) or facts.get("available") is False:
        return []
    stale_after = int(facts.get("stale_after_days") or 120)
    today = now.date()
    out: list[EtfDataset] = []
    for kind in ("holdings", "lookthrough"):
        for item in facts.get(kind) or []:
            as_of = _day(item.get("as_of"))
            age = (today - as_of).days if as_of else None
            status = str(item.get("status") or "MISSING")
            manual = bool(item.get("manual"))
            invesco = str(item.get("provider") or "").lower().startswith("invesco")
            limit = MANUAL_FRESH_D if manual else INVESCO_FRESH_D if invesco else ISHARES_FRESH_D
            if status in ("MISSING", "INVALID") or age is None:
                level = STALE if not manual else UNKNOWN
            elif age > stale_after:
                level = STALE
            else:
                level = OK if age <= limit else AGING
            out.append(EtfDataset(str(item.get("symbol") or "?"), kind, str(item.get("provider") or ""), manual, as_of, age, status, level, item.get("rows"), str(item.get("detail") or "")))
    return out


def etf_overall(datasets: list[EtfDataset]) -> str:
    """The worst level of the AUTOMATICALLY refreshed datasets; the manual one never decides (it is shown, not counted)."""
    automatic = [d.level for d in datasets if not d.manual]
    if not automatic:
        return UNKNOWN
    return STALE if STALE in automatic else AGING if AGING in automatic else UNKNOWN if UNKNOWN in automatic else OK


def etf_label(d: EtfDataset) -> str:
    return f"{d.symbol} {'Holdings' if d.dataset == 'holdings' else 'Sektoren'}" + (" (manuell)" if d.manual else "")


def _etf_freshness(facts: Optional[Mapping[str, Any]], now: datetime) -> Freshness:
    result = Freshness(ETF_DATA)
    datasets = etf_datasets(facts, now)
    if not datasets:
        result.detail = "Kein ETF-Datenstand verfügbar (MCP nicht verbunden oder Quelle nicht gemeldet)."
        return result
    result.level = etf_overall(datasets)
    weekly = [d for d in datasets if not d.manual and d.as_of and not d.provider.lower().startswith("invesco")]
    monthly = [d for d in datasets if not d.manual and d.as_of and d.provider.lower().startswith("invesco")]
    oldest = min((d.as_of for d in weekly), default=None)
    result.as_of = (fmt_day(oldest) if oldest else "—") + "".join(f" · {d.symbol} {fmt_day(d.as_of)}" for d in monthly if oldest is None or d.as_of < oldest)
    lines = [f"{etf_label(d)}: {fmt_day(d.as_of)} ({d.age_days} Tage alt, {LEVEL_TEXT[d.level]}{', ' + d.status if d.status not in ('APPLIED',) else ''})" if d.as_of
             else f"{etf_label(d)}: {d.status}" for d in datasets]
    result.detail = ("Stand je Datensatz: " + "; ".join(lines) + f". Regel: aktuell bis {ISHARES_FRESH_D} Tage (iShares, wöchentlicher Lauf) bzw. {INVESCO_FRESH_D} Tage (Invesco, monatliche Liste), "
                     f"veraltet nach {int(facts.get('stale_after_days') or 120)} Tagen (Grenze der Loader, unverändert). Der manuelle SPXS-Sektor wird gezeigt, bestimmt aber weder den Stand noch den Task-Status.")
    return result


def _fundamentals_info(facts: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(facts, Mapping) or facts.get("available") is False:
        return "Fundamentals-Stand nicht lesbar."
    return (f"Zuletzt gespeichert (neues Filing): {fmt_dt(_dt(facts.get('fetched_at')))}; jüngste Periode {fmt_day(_day(facts.get('latest_period_end')))}, "
            f"jüngstes Filing {fmt_day(_day(facts.get('latest_filing_date')))}.")


def _fundamentals(facts: Optional[Mapping[str, Any]], run: RunFacts, now: datetime) -> Freshness:
    """The health of the weekly fundamentals task from its LAST SUCCESSFUL RUN and its rhythm.

    The task only writes *new* filings, so "no new filing for 15 days" is normal and says nothing about the task; the newest filing and period are shown for
    information only.  Current = the last successful run is at most a week plus a day old (``FUNDAMENTALS_FRESH_H``), older = one run is missing,
    stale = more than ``FUNDAMENTALS_STALE_H``.  A run the scheduler reports as missed (``NumberOfMissedRuns``) or a last run that failed is at least "older"; a task
    that never ran is unknown.
    """
    result = Freshness(FUNDAMENTALS)
    success = run.last_run if run.last_result == 0 else None
    result.last_success = success
    info = _fundamentals_info(facts)
    rule = (f"Regel (wöchentlicher Task): aktuell bis {FUNDAMENTALS_FRESH_H / 24:.0f} Tage nach dem letzten erfolgreichen Lauf, veraltet nach {FUNDAMENTALS_STALE_H / 24:.0f} Tagen; "
            "es werden nur neue Filings geschrieben, ein fehlendes neues Filing macht den Task nicht veraltet.")
    if success is None:
        never = run.last_result in (None, 267011)
        result.level = UNKNOWN if never else AGING
        result.as_of = "noch nie gelaufen" if never else "letzter Lauf fehlgeschlagen"
        result.detail = f"Letzter erfolgreicher Lauf: nicht bekannt ({'noch nie gelaufen' if never else 'der letzte Lauf ist fehlgeschlagen'}). {info} {rule}"
        return result
    age = _age_hours(success, now)
    result.level = OK if age is not None and age <= FUNDAMENTALS_FRESH_H else AGING if age is not None and age <= FUNDAMENTALS_STALE_H else STALE
    if run.missed_runs and result.level == OK:
        result.level = AGING
    result.as_of = "Lauf " + success.strftime("%d.%m.%Y")
    result.detail = (f"Letzter erfolgreicher Lauf: {fmt_dt(success)} ({_age_text(age)} her)"
                     + (f"; verpasste Läufe laut Scheduler: {run.missed_runs}" if run.missed_runs else "") + f". {info} {rule}")
    return result


def dependency_state(need: Need, freshness: Mapping[str, Freshness], results: Mapping[str, Optional[int]]) -> tuple[str, str]:
    """``(level, text)`` of one dependency from the freshness of the source and the last result of the task that produces it.

    ``level`` is ``ok`` / ``warning`` / ``blocker`` for a hard need and ``ok`` / ``info`` for a soft one; the text says what is wrong.
    """
    fresh = freshness.get(need.source)
    label = SOURCE_TEXT.get(need.source, need.source)
    problems: list[str] = []
    code = results.get(need.task)
    if code not in (None, *NO_ERROR_RESULTS):
        problems.append(f"letzter Lauf von {need.task.replace('Trading-', '')} fehlgeschlagen ({result_code(code)})")
    elif code == 267011:
        problems.append(f"{need.task.replace('Trading-', '')} ist noch nie gelaufen")
    if fresh is None or fresh.level == UNKNOWN:
        problems.append(f"{label}: Datenstand unbekannt")
    elif fresh.level == STALE:
        problems.append(f"{label} veraltet (Stand {fresh.as_of})")
    elif fresh.level == AGING:
        problems.append(f"{label} etwas älter (Stand {fresh.as_of})")
    if not problems:
        return OK, f"{label}: {LEVEL_TEXT[OK]} (Stand {fresh.as_of if fresh else '—'})"
    severe = any("fehlgeschlagen" in p or "veraltet" in p or "noch nie" in p for p in problems)
    if need.kind == HARD:
        return ("blocker" if severe else "warning"), "; ".join(problems)
    return "info", "; ".join(problems)


def result_code(code: Optional[int]) -> str:
    return "—" if code is None else f"0x{code & 0xFFFFFFFF:08X}" if code > 255 else str(code)
