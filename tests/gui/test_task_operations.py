"""The Tasks page as an operations overview: rhythm from the triggers, last success vs last run, duration, disabled / StartWhenAvailable / IgnoreNew, the technical
dependencies (checked against the code of the scripts), the data freshness, error states, the warning before a manual start and the page offscreen; synthetic data only."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp

from gui.services import task_model as tm
from gui.services import task_service as ts
from gui.services.task_service import TaskInfo, TriggerInfo
from gui.viewmodels.tasks import build_task_rows, summarize

if HAVE_QT:
    from gui.app import TASKS_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from test_main_window import FakeBackend

    import fixtures

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 14, 12, 0)  # a Wednesday, local time
MD, FX, EN, FS, CD, MI = (t.name for t in ts.TASKS)


def utc(hours_ago: float = 0.0, days_ago: float = 0.0) -> str:
    return (NOW.astimezone() - timedelta(hours=hours_ago, days=days_ago)).astimezone(timezone.utc).isoformat()


def day(days_ago: int = 0) -> str:
    return (NOW - timedelta(days=days_ago)).date().isoformat()


def payload(**overrides):
    """A ``get_data_freshness`` answer in which everything is current; ``overrides`` replace whole sources."""
    sources = {
        "market_data": {"available": True, "latest_bar_date": day(0), "oldest_held_bar_date": day(1), "bars_fetched_at": utc(3), "snapshot_fetched_at": utc(3), "held_positions": 12, "held_without_bars": 0},
        "fx": {"available": True, "latest_rate_date": day(1), "oldest_currency_date": day(1), "oldest_currency": "AUD", "currencies": 4, "fetched_at": utc(3)},
        "events_news": {"available": True, "news_fetched_at": utc(3), "latest_news_published_at": utc(4), "events_created_at": utc(0, 5), "latest_event_date": day(6)},
        "fundamentals": {"available": True, "fetched_at": utc(0, 5), "latest_period_end": day(70), "latest_filing_date": day(30)},
        "discovery": {"available": True, "status": "AVAILABLE", "generated_at": utc(20), "evaluation_as_of": day(1)},
        "market_intelligence": {"available": True, "status": "AVAILABLE", "generated_at": utc(6), "evaluation_as_of": day(0)},
    }
    sources.update(overrides)
    return {"ok": True, "operation": "get_data_freshness", "now": utc(0), "sources": sources,
            "rules": {"market_data_max_age_days": 5, "fx_max_age_days": 5, "discovery_report_max_age_days": 5, "intelligence_report_max_age_hours": 24}}


def triggers_for(name: str):
    local = lambda hhmm: datetime(2026, 10, 2, int(hhmm[:2]), int(hhmm[3:])).astimezone().isoformat()  # noqa: E731
    daily = lambda hhmm: {"type": "MSFT_TaskDailyTrigger", "start": local(hhmm), "days_interval": 1, "weeks_interval": 0, "days_of_week": 0, "enabled": True}  # noqa: E731
    logon = {"type": "MSFT_TaskLogonTrigger", "start": "", "days_interval": 0, "weeks_interval": 0, "days_of_week": 0, "enabled": True}
    return {MD: [logon, daily("17:15")], FX: [logon, daily("17:20")], EN: [logon, daily("17:25")],
            FS: [logon, {"type": "MSFT_TaskWeeklyTrigger", "start": local("10:00"), "days_interval": 0, "weeks_interval": 1, "days_of_week": 1, "enabled": True}],
            CD: [daily("17:35")], MI: [daily("08:15"), daily("17:45")]}[name]


def raw(name: str, **overrides):
    item = {"name": name, "found": True, "state": "Ready", "last_run": NOW.astimezone().replace(hour=10, minute=55, second=16).isoformat(), "next_run": NOW.astimezone().replace(hour=17, minute=15).isoformat(),
            "last_result": 0, "missed_runs": 0, "start_when_available": True, "multiple_instances": "IgnoreNew", "time_limit": "PT72H", "triggers": triggers_for(name)}
    item.update(overrides)
    return item


def tasks(**overrides):
    rows = [raw(n, **overrides.get(n, {})) for n in ts.TASK_NAMES]
    infos = ts.parse_tasks(json.dumps(rows))
    for info in infos:  # the log was written 55 s after the start
        info.log_modified, info.log_size = (info.last_run + timedelta(seconds=55)) if info.last_run else None, 4096
    return infos


def rows(freshness=None, overrides=None, logs_dir="C:\\logs"):
    return {r.name: r for r in build_task_rows(tasks(**(overrides or {})), payload() if freshness is None else freshness, NOW, logs_dir)}


class RhythmTests(unittest.TestCase):
    def test_the_rhythm_comes_from_the_triggers(self) -> None:
        by_name = {t.name: ts.rhythm_text(t.triggers) for t in tasks()}
        self.assertEqual(by_name[MD], "täglich 17:15 + bei Anmeldung")
        self.assertEqual(by_name[FS], "wöchentlich So 10:00 + bei Anmeldung")
        self.assertEqual(by_name[CD], "täglich 17:35")
        self.assertEqual(by_name[MI], "2× täglich 08:15, 17:45")

    def test_other_trigger_combinations(self) -> None:
        daily = lambda t, interval=1, enabled=True: TriggerInfo("Daily", t, interval, 1, 0, enabled)  # noqa: E731
        self.assertEqual(ts.rhythm_text([daily("17:15", 2)]), "alle 2 Tage 17:15")
        self.assertEqual(ts.rhythm_text([TriggerInfo("Logon")]), "bei Anmeldung")
        self.assertEqual(ts.rhythm_text([TriggerInfo("Weekly", "10:00", 1, 2, 2 | 16)]), "alle 2 Wochen Mo, Do 10:00")
        self.assertEqual(ts.rhythm_text([daily("17:15", enabled=False)]), "Auslöser deaktiviert")
        self.assertEqual(ts.rhythm_text([daily("17:15"), daily("18:00", enabled=False)]), "täglich 17:15")
        self.assertEqual(ts.rhythm_text([]), "kein Auslöser")
        self.assertEqual([ts.trigger_line(t) for t in (daily("17:15"), TriggerInfo("Logon"), daily("08:00", enabled=False))], ["täglich 17:15", "bei Anmeldung", "täglich 08:00 (deaktiviert)"])

    def test_the_scheduler_answer_is_parsed_including_settings(self) -> None:
        mi = next(t for t in tasks() if t.name == MI)
        self.assertEqual([(t.kind, t.time) for t in mi.triggers], [("Daily", "08:15"), ("Daily", "17:45")])
        self.assertEqual((mi.start_when_available, mi.multiple_instances, mi.time_limit, mi.missed_runs), (True, "IgnoreNew", "PT72H", 0))
        old = ts.parse_tasks(json.dumps([{"name": MD, "found": True, "state": "Ready", "last_result": 0}]))[0]  # an answer without the new fields still parses
        self.assertEqual((old.triggers, old.start_when_available, old.multiple_instances), ([], None, ""))

    def test_the_column_shows_the_rhythm(self) -> None:
        self.assertEqual(rows()[MI].rhythm, "2× täglich 08:15, 17:45")
        self.assertEqual(rows()[MD].rhythm, "täglich 17:15 + bei Anmeldung")


class SuccessAndDurationTests(unittest.TestCase):
    def test_the_last_success_is_not_the_last_run(self) -> None:
        failed_at = NOW.astimezone().replace(hour=11, minute=30, second=0).isoformat()
        row = rows(overrides={FX: {"last_result": 2147942402, "last_run": failed_at}})[FX]  # the last run failed; the data say the last success was 3 h ago
        self.assertIn("(Lauf 14.10. 11:30: Fehler (0x80070002))", row.last_success)
        self.assertTrue(row.last_success.startswith("14.10. 09:00"))  # the data time: 12:00 - 3 h
        self.assertEqual(row.level, "warning")
        self.assertIn("Windows-Ergebnis Fehler (0x80070002)", row.last_error)

    def test_a_successful_last_run_is_the_last_success(self) -> None:
        row = rows()[MD]
        self.assertEqual(row.last_success, "14.10. 10:55")  # exit code 0: the run itself, later than the fetch of the data
        self.assertNotIn("Lauf", row.last_success)

    def test_no_success_is_known_without_data_and_with_a_failed_run(self) -> None:
        row = {r.name: r for r in build_task_rows(tasks(**{MD: {"last_result": 1}}), None, NOW, "")}[MD]
        self.assertTrue(row.last_success.startswith("—"))
        self.assertIn("Letzter Erfolg: nicht bekannt", row.details)

    def test_the_duration_comes_from_start_and_log_end(self) -> None:
        self.assertEqual(rows()[MD].duration, "≈ 55 s")
        info = tasks()[0]
        info.log_modified = info.last_run + timedelta(seconds=118)
        self.assertEqual(build_task_rows([info], payload(), NOW, "")[0].duration, "≈ 1 min 58 s")
        info.log_modified = info.last_run + timedelta(minutes=25)
        self.assertEqual(build_task_rows([info], payload(), NOW, "")[0].duration, "≈ 25 min")

    def test_the_duration_is_unknown_without_a_plausible_log_time(self) -> None:
        info = tasks()[0]
        info.log_modified = info.last_run - timedelta(minutes=1)  # the log is older than the run: nothing was written yet
        self.assertEqual(build_task_rows([info], payload(), NOW, "")[0].duration, "—")
        info.log_modified = info.last_run + timedelta(hours=9)  # written long after: not this run
        self.assertEqual(build_task_rows([info], payload(), NOW, "")[0].duration, "—")
        info.log_modified = None
        self.assertEqual(build_task_rows([info], payload(), NOW, "")[0].duration, "—")

    def test_a_running_task_shows_how_long_it_has_been_running(self) -> None:
        info = tasks(**{MD: {"state": "Running", "last_result": 267009, "last_run": (NOW - timedelta(minutes=3, seconds=20)).astimezone().isoformat()}})[0]
        row = build_task_rows([info], payload(), NOW, "")[0]
        self.assertEqual(row.duration, "läuft seit 3 min 20 s")
        self.assertFalse(row.can_run)
        self.assertEqual(row.level, "")  # 267009 is "running", not an error


class DefinitionTests(unittest.TestCase):
    def test_disabled_start_when_available_and_ignore_new_are_visible(self) -> None:
        row = rows(overrides={FS: {"state": "Disabled"}})
        disabled = row[FS]
        self.assertEqual((disabled.status, disabled.can_run, disabled.run_hint), ("deaktiviert", False, "Task ist deaktiviert"))
        self.assertTrue(disabled.status_line.startswith("deaktiviert"))
        self.assertIn("Aktiviert: Nein", disabled.details)
        self.assertIn("Letzter Erfolg: 14.10.2026 10:55", row[MD].details)  # the tooltip has the full date
        details = row[MD].details
        self.assertIn("Nachholen verpasster Läufe (StartWhenAvailable): Ja", details)
        self.assertIn("Keine zweite Instanz (IgnoreNew): Ja", details)
        self.assertIn("Zeitlimit: 72 Stunden", details)
        self.assertIn("Zeitlimit: 2 Stunden", rows(overrides={MI: {"time_limit": "PT2H"}})[MI].details)
        self.assertIn("Verpasste Läufe: 0", details)
        self.assertIn("Auslöser: bei Anmeldung; täglich 17:15", details)
        off = rows(overrides={MD: {"start_when_available": False, "multiple_instances": "Parallel"}})[MD].details
        self.assertIn("(StartWhenAvailable): Nein", off)
        self.assertIn("Keine zweite Instanz (IgnoreNew): Nein (Mehrfachinstanzen: Parallel)", off)

    def test_the_details_name_the_log_path_and_the_last_error(self) -> None:
        details = rows()[MD].details
        self.assertIn("Log: C:\\logs\\market-data-backfill.log (4 KB", details)
        self.assertIn("Letzter Fehler: keiner erkannt", details)

    def test_a_task_that_is_not_registered(self) -> None:
        only = ts.parse_tasks(json.dumps([{"name": MD, "found": False}]))[0]
        row = build_task_rows([only], payload(), NOW, "")[0]
        self.assertEqual((row.status, row.level, row.can_run, row.status_line), ("nicht registriert", "blocker", False, "nicht registriert"))


class DependencyTests(unittest.TestCase):
    def test_the_dependency_model_is_what_the_scripts_really_read(self) -> None:
        spec = tm.SPECS
        self.assertEqual({n: [(d.source, d.kind) for d in s.needs] for n, s in spec.items()},
                         {MD: [], FX: [], EN: [], FS: [], CD: [(tm.FX, tm.HARD)], MI: [(tm.EVENTS_NEWS, tm.HARD), (tm.DISCOVERY, tm.SOFT)]})
        trading = ROOT / "tools" / "trading"
        discovery = (trading / "candidate_discovery.py").read_text(encoding="utf-8") + (trading / "Discover-TradingCandidates.py").read_text(encoding="utf-8")
        self.assertIn("fx_rates", discovery)  # Discovery: the EUR conversion reads the FX table ...
        self.assertIn("resolve_fx_rate", discovery)
        for table in ("market_data", "news", "events", "fundamentals"):  # ... and nothing of what the other collectors write (the in-memory copy only receives its own Yahoo history)
            self.assertNotRegex(discovery, rf"(?i)prod(uction)?(_conn)?\.execute\([^)]*FROM {table}\b", table)
        self.assertNotIn('"fundamentals"', discovery)
        collector = (trading / "Collect-TradingMarketIntelligence.py").read_text(encoding="utf-8") + (trading / "market_intelligence.py").read_text(encoding="utf-8")
        self.assertRegex(collector, r"FROM news\b")  # Market Intelligence reads the cached news and SEC events ...
        self.assertRegex(collector, r"FROM events\b")
        self.assertIn("read_latest_report(args.discovery_dir)", collector)  # ... and the last discovery report
        self.assertIn("DISCOVERY_REPORT_UNAVAILABLE", collector)  # without which it continues (soft)
        for table in ("fx_rates", "market_data", "market_snapshot"):  # it reads no prices and no FX
            self.assertNotRegex(collector, rf"FROM {table}\b", table)

    def test_stale_fx_is_a_blocker_for_discovery_but_not_for_the_other_tasks(self) -> None:
        stale_fx = payload(fx={"available": True, "latest_rate_date": day(9), "oldest_currency_date": day(9), "oldest_currency": "AUD", "currencies": 4, "fetched_at": utc(0, 9)})
        result = rows(stale_fx)
        self.assertEqual(result[FX].data_level, "blocker")
        self.assertIn("Vorläufer: FX-Kurse veraltet", result[CD].run_warning)
        self.assertEqual(result[CD].level, "warning")
        self.assertIn("✗ benötigt: FX-Kurse (Task FXRates-Backfill)", result[CD].details)
        self.assertEqual(result[MI].run_warning, "")  # Market Intelligence does not read FX
        self.assertEqual(result[MD].run_warning, "")

    def test_a_failed_producer_run_is_named_even_when_its_old_data_look_fine(self) -> None:
        result = rows(overrides={FX: {"last_result": 1}})
        self.assertIn("letzter Lauf von FXRates-Backfill fehlgeschlagen (1)", result[CD].run_warning)

    def test_market_intelligence_needs_events_and_news_and_only_prefers_discovery(self) -> None:
        stale_news = payload(events_news={"available": True, "news_fetched_at": utc(0, 4), "latest_news_published_at": utc(0, 4), "events_created_at": utc(0, 9), "latest_event_date": day(9)})
        warning = rows(stale_news)[MI].run_warning
        self.assertIn("Vorläufer: Events / News veraltet", warning)
        no_report = payload(discovery={"available": True, "status": "UNAVAILABLE", "reason": "NO_DISCOVERY_REPORT", "problems": []})
        result = rows(no_report)[MI]
        self.assertIn("Optionaler Vorläufer: Discovery-Report veraltet", result.run_warning)
        self.assertTrue(any(line.startswith("ℹ optional") for line in result.dependency_lines))
        self.assertNotIn("Vorläufer: Events", result.run_warning)

    def test_independent_tasks_have_no_dependencies_and_say_so(self) -> None:
        result = rows()
        for name in (MD, FX, EN, FS):
            self.assertEqual((result[name].dependency_lines, result[name].run_warning), ([], ""))
            self.assertIn("keine – eigenständig", result[name].details)
        self.assertIn("kein Task startet einen anderen", result[MI].details)

    def test_all_prerequisites_current_means_no_warning(self) -> None:
        result = rows()
        for name in (CD, MI):
            self.assertEqual(result[name].run_warning, "", name)
            self.assertTrue(all(line.startswith("✓") for line in result[name].dependency_lines))


class FreshnessTests(unittest.TestCase):
    def level(self, source, facts, when=NOW):
        return tm.evaluate(source, payload(**{source: facts}), when).level

    def test_market_data_is_judged_in_trading_days_and_the_projects_age_rule(self) -> None:
        def facts(oldest):
            return {"available": True, "latest_bar_date": oldest, "oldest_held_bar_date": oldest, "snapshot_fetched_at": utc(3), "held_positions": 3, "held_without_bars": 0}

        self.assertEqual(self.level("market_data", facts(day(1))), tm.OK)  # Tuesday's bar on Wednesday
        self.assertEqual(self.level("market_data", facts(day(2))), tm.AGING)  # Monday's bar: two trading days behind
        self.assertEqual(self.level("market_data", facts(day(6))), tm.STALE)  # older than market_data_max_age_days
        monday = datetime(2026, 10, 12, 9, 0)
        self.assertEqual(self.level("market_data", facts("2026-10-09"), monday), tm.OK)  # Friday's bar on Monday: the weekend is no delay
        self.assertEqual(self.level("market_data", {**facts(day(1)), "held_without_bars": 1}), tm.STALE)  # a held position without any bar

    def test_the_oldest_held_position_decides(self) -> None:
        fresh = tm.evaluate("market_data", payload(market_data={"available": True, "latest_bar_date": day(0), "oldest_held_bar_date": day(7), "snapshot_fetched_at": utc(1)}), NOW)
        self.assertEqual(fresh.level, tm.STALE)
        self.assertEqual(fresh.as_of, tm.fmt_day(NOW.date() - timedelta(days=7)))
        self.assertIn("ältester letzter Kurs einer gehaltenen Position", fresh.detail)

    def test_fx_uses_the_oldest_currency_and_its_own_rule(self) -> None:
        facts = {"available": True, "latest_rate_date": day(1), "oldest_currency_date": day(8), "oldest_currency": "KRW", "currencies": 4, "fetched_at": utc(3)}
        fresh = tm.evaluate("fx", payload(fx=facts), NOW)
        self.assertEqual(fresh.level, tm.STALE)
        self.assertIn("(KRW)", fresh.detail)

    def test_news_and_reports_follow_their_rhythm(self) -> None:
        news = lambda hours: {"available": True, "news_fetched_at": utc(hours), "latest_news_published_at": utc(hours), "events_created_at": utc(0, 9), "latest_event_date": day(9)}  # noqa: E731
        self.assertEqual([self.level("events_news", news(h)) for h in (20, 50, 80)], [tm.OK, tm.AGING, tm.STALE])
        report = lambda hours, evaluation: {"available": True, "status": "AVAILABLE", "generated_at": utc(hours), "evaluation_as_of": day(evaluation)}  # noqa: E731
        self.assertEqual([self.level("market_intelligence", report(h, 0)) for h in (8, 20, 30)], [tm.OK, tm.AGING, tm.STALE])
        self.assertEqual([self.level("discovery", report(20, 1)), self.level("discovery", report(40, 2)), self.level("discovery", report(20, 7))], [tm.OK, tm.AGING, tm.STALE])

    def fundamentals(self, days_since_run, result=0, missed=0, filing_days=30, fetched_days=20):
        facts = {"available": True, "fetched_at": utc(0, fetched_days), "latest_period_end": day(70), "latest_filing_date": day(filing_days)}
        run = tm.RunFacts(NOW - timedelta(days=days_since_run) if days_since_run is not None else None, result, missed)
        return tm.evaluate("fundamentals", payload(fundamentals=facts), NOW, run)

    def test_a_healthy_weekly_fundamentals_task_is_not_stale_for_lack_of_new_filings(self) -> None:
        fresh = self.fundamentals(2, filing_days=40, fetched_days=40)  # ran fine two days ago, nothing new stored for 40 days
        self.assertEqual(fresh.level, tm.OK)
        self.assertTrue(fresh.as_of.startswith("Lauf "))
        self.assertIn("jüngstes Filing", fresh.detail)  # the newest filing and period are still shown, as information
        self.assertIn("jüngste Periode", fresh.detail)
        self.assertIn("macht den Task nicht veraltet", fresh.detail)
        self.assertEqual(fresh.last_success, NOW - timedelta(days=2))

    def test_fundamentals_follow_the_last_successful_run_and_the_missed_rhythm(self) -> None:
        self.assertEqual([self.fundamentals(d).level for d in (3, 7, 10, 14, 20)], [tm.OK, tm.OK, tm.AGING, tm.AGING, tm.STALE])  # a week plus a day / a missed week / more
        self.assertEqual(self.fundamentals(3, missed=1).level, tm.AGING)  # the scheduler reports a missed run
        failed = self.fundamentals(1, result=1)
        self.assertEqual((failed.level, failed.as_of, failed.last_success), (tm.AGING, "letzter Lauf fehlgeschlagen", None))
        never = self.fundamentals(None, result=267011)
        self.assertEqual((never.level, never.as_of), (tm.UNKNOWN, "noch nie gelaufen"))

    def test_fundamentals_without_scheduler_facts_are_not_judged_by_the_data_age(self) -> None:
        fresh = tm.evaluate("fundamentals", payload(fundamentals={"available": True, "fetched_at": utc(0, 60), "latest_period_end": day(200), "latest_filing_date": day(100)}), NOW)
        self.assertEqual(fresh.level, tm.UNKNOWN)  # never "stale" because of the age of the newest filing
        self.assertIn("jüngstes Filing", fresh.detail)

    def test_the_fundamentals_row_uses_the_task_run_not_the_filing_age(self) -> None:
        data = payload(fundamentals={"available": True, "fetched_at": utc(0, 40), "latest_period_end": day(120), "latest_filing_date": day(60)})
        row = rows(data)[FS]  # the helper tasks ran at 10:55 today with exit code 0
        self.assertEqual((row.data_level, row.level), ("", ""))
        self.assertTrue(row.data_stand.startswith("Lauf 14.10.2026"))
        self.assertIn("Letzter erfolgreicher Lauf", row.details)
        self.assertIn("jüngstes Filing", row.details)
        old_run = (NOW - timedelta(days=20)).astimezone().isoformat()
        stale = rows(data, overrides={FS: {"last_run": old_run}})[FS]
        self.assertEqual(stale.data_level, "blocker")  # a task that has not run successfully for 20 days is stale
        self.assertTrue(stale.data_stand.endswith("veraltet"))

    def test_a_missing_report_is_stale_and_says_so(self) -> None:
        fresh = tm.evaluate("discovery", payload(discovery={"available": True, "status": "UNAVAILABLE", "reason": "NO_DISCOVERY_REPORT"}), NOW)
        self.assertEqual((fresh.level, fresh.as_of), (tm.STALE, "kein Report"))

    def test_without_an_answer_the_stand_is_unknown_never_current(self) -> None:
        for answer in (None, {"ok": False}, {"ok": True, "sources": {}}):
            fresh = tm.evaluate("market_data", answer, NOW)
            self.assertEqual((fresh.level, fresh.as_of), (tm.UNKNOWN, "—"))
        unreadable = tm.evaluate("fx", payload(fx={"available": False, "error": "OperationalError: no such table"}), NOW)
        self.assertEqual(unreadable.level, tm.UNKNOWN)
        self.assertIn("no such table", unreadable.detail)
        row = build_task_rows(tasks(), None, NOW, "")[0]
        self.assertEqual((row.data_stand, row.data_level), ("unbekannt", ""))

    def test_a_task_that_ran_fine_but_has_stale_data_is_visible(self) -> None:
        stale = payload(market_data={"available": True, "latest_bar_date": day(8), "oldest_held_bar_date": day(8), "snapshot_fetched_at": utc(0, 8), "held_positions": 12, "held_without_bars": 0})
        result = rows(stale)
        row = result[MD]
        self.assertEqual(row.result, "OK")  # the exit code is fine ...
        self.assertEqual(row.data_level, "blocker")  # ... the data are not
        self.assertTrue(row.data_stand.endswith("– veraltet"))
        self.assertEqual(row.level, "warning")
        self.assertIn("Daten veraltet: Marktdaten", summarize(list(result.values()), True))

    def test_the_table_shows_the_stand_of_every_source(self) -> None:
        result = rows()
        self.assertEqual(result[MD].data_stand, tm.fmt_day(NOW.date() - timedelta(days=1)))
        self.assertEqual(result[FX].data_stand, tm.fmt_day(NOW.date() - timedelta(days=1)))
        self.assertTrue(result[MI].data_stand.startswith("14.10.2026"))
        self.assertTrue(result[CD].data_stand.startswith("13.10.2026"))  # the generated_at of the last discovery report


class ErrorStateTests(unittest.TestCase):
    def test_a_log_hint_is_visible_although_the_exit_code_is_zero(self) -> None:
        infos = tasks()
        infos[2].log_hint = "Failed: [{'id': 4, 'error': 'HTTP Error 403: Forbidden'}]"
        row = {r.name: r for r in build_task_rows(infos, payload(), NOW, "C:\\logs")}[EN]
        self.assertEqual(row.result, "OK")
        self.assertIn("Log-Hinweis", row.status_line)
        self.assertEqual(row.level, "warning")
        self.assertIn("Letzter Fehler: Hinweis aus dem Log-Ende: Failed: [{'id': 4", row.details)

    def test_the_log_hint_ignores_lines_that_say_nothing_failed(self) -> None:
        benign = "Failed securities   : 0\n  Failed: []\n\"failed\": 0\n  \"errors\": {}\nkeine Fehler\n"
        self.assertEqual(ts.log_hint(benign), "")
        real = "start\n-> ERROR: HTTP Error 403: Forbidden\nTotal: 55.34s\n"
        self.assertEqual(ts.log_hint(real), "-> ERROR: HTTP Error 403: Forbidden")
        self.assertEqual(ts.log_hint("Traceback (most recent call last):\n  File x\nTimeoutError: the read operation timed out"), "TimeoutError: the read operation timed out")
        self.assertTrue(len(ts.log_hint("error " + "x" * 400)) <= 200)

    def test_the_log_hint_and_write_time_are_read_from_the_real_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "events-news-backfill.log").write_text("fine\n  -> ERROR: HTTP Error 403: Forbidden\nEvents: 0 rows\n", encoding="utf-8")
            infos = ts.parse_tasks(json.dumps([raw(EN)]))
            ts.enrich_with_logs(infos, folder)
            self.assertTrue(infos[2].log_hint.endswith("403: Forbidden"))
            self.assertIsNotNone(infos[2].log_modified)
            self.assertIsNone(infos[0].log_modified)  # no log file: nothing is claimed

    def test_the_summary_counts_the_tasks_that_need_a_look(self) -> None:
        result = rows()
        self.assertEqual(summarize(list(result.values()), True), "6 Tasks · alles im Plan")
        self.assertIn("Datenstand nicht verfügbar", summarize(list(result.values()), False))
        bad = rows(overrides={FX: {"last_result": 1}})
        self.assertIn("mit Hinweis", summarize(list(bad.values()), True))


@QT
class OperationsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        self.set_freshness(payload())
        self.settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", ping_interval=2, extra_env={"FAKE_ORCH_FILE": str(self.tmp / "orch.json"), "FAKE_FRESHNESS_FILE": str(self.tmp / "fresh.json")})
        self.backend, self.runner = FakeBackend(tasks=tasks()), AsyncRunner()
        self.window = MainWindow(self.settings, self.mcp, self.backend, self.runner, [], save_fn=lambda s: None,
                                 ollama_fn=lambda url: __import__("gui.services.ollama_client", fromlist=["x"]).OllamaStatus(True, "0.35.0", ["m1"]))
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        self.window.close()
        self.runner.wait(5000)
        self.tmpdir.cleanup()

    def set_freshness(self, data) -> None:
        (self.tmp / "fresh.json").write_text(json.dumps(data), encoding="utf-8")

    def open_page(self) -> None:
        self.window.resize(1400, 800)
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None))
        self.window.sidebar.setCurrentRow(TASKS_PAGE)
        self.assertTrue(pump(15, lambda: len(self.window.tasks.run_buttons) == 6 and self.window._freshness is not None), "tasks and freshness did not arrive")

    def test_the_page_shows_the_operations_columns(self) -> None:
        self.open_page()
        table = self.window.tasks.table
        self.assertEqual([table.horizontalHeaderItem(c).text() for c in range(table.columnCount())],
                         ["Task", "Rhythmus", "Status", "Datenstand", "Letzter Erfolg", "Nächster Lauf", "Dauer", "Aktion", "Log"])
        self.assertEqual(table.item(5, 0).text(), "Market Intelligence")  # the short title; the tooltip names the task
        self.assertEqual(table.item(5, 1).text(), "2× täglich 08:15, 17:45")
        self.assertIn("Trading-Market-Intelligence", table.item(5, 0).toolTip())
        self.assertTrue(table.item(0, 2).text().startswith("bereit · Lauf 14.10. 10:55 · OK"))
        self.assertEqual(table.item(0, 6).text(), "≈ 55 s")
        self.assertIn("Abhängigkeiten", table.item(4, 0).toolTip())
        self.assertIn("StartWhenAvailable", table.item(4, 3).toolTip())
        self.assertEqual(self.window.tasks.message.text(), "6 Tasks · alles im Plan")
        self.assertFalse(self.window.tasks.grab().isNull())

    def test_stale_data_are_coloured_and_summarised(self) -> None:
        self.set_freshness(payload(fx={"available": True, "latest_rate_date": day(9), "oldest_currency_date": day(9), "oldest_currency": "AUD", "currencies": 4, "fetched_at": utc(0, 9)}))
        self.open_page()
        table = self.window.tasks.table
        self.assertTrue(table.item(1, 3).text().endswith("veraltet"))
        self.assertIn("Daten veraltet: FX-Kurse", self.window.tasks.message.text())

    def test_without_the_mcp_answer_the_page_still_works(self) -> None:
        (self.tmp / "fresh.json").write_text(json.dumps({"ok": False, "error": "DATA_FRESHNESS_FAILED", "message": "kaputt"}), encoding="utf-8")
        self.open_page_without_freshness()
        self.assertIn("Datenstand nicht verfügbar", self.window.tasks.message.text())
        self.assertEqual(self.window.tasks.table.item(0, 3).text(), "unbekannt")
        self.assertTrue(self.window.tasks.run_buttons[MD].isEnabled())

    def open_page_without_freshness(self) -> None:
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None))
        self.window.sidebar.setCurrentRow(TASKS_PAGE)
        self.assertTrue(pump(15, lambda: len(self.window.tasks.run_buttons) == 6 and "Datenstand nicht verfügbar" in self.window.tasks.message.text()))

    def test_a_manual_start_warns_about_stale_prerequisites_and_still_starts_only_that_task(self) -> None:
        self.set_freshness(payload(fx={"available": True, "latest_rate_date": day(9), "oldest_currency_date": day(9), "oldest_currency": "AUD", "currencies": 4, "fetched_at": utc(0, 9)}))
        self.open_page()
        view = self.window.tasks
        asked: list[str] = []
        view.confirm = lambda name: (asked.append(view._ask_text(name, view.rows[name].run_warning)), True)[1]
        view.run_buttons[CD].click()
        self.assertTrue(pump(10, lambda: self.backend.started == [CD]))
        self.assertEqual(self.backend.started, [CD])  # no prerequisite task was started
        self.assertIn("Achtung – nötige Daten sind nicht in Ordnung", asked[0])
        self.assertIn("FX-Kurse veraltet", asked[0])
        self.assertIn("Es wird kein anderer Task gestartet", asked[0])

    def test_the_real_confirmation_text_carries_the_warning(self) -> None:
        self.set_freshness(payload(fx={"available": True, "latest_rate_date": day(9), "oldest_currency_date": day(9), "oldest_currency": "AUD", "currencies": 4, "fetched_at": utc(0, 9)}))
        self.open_page()
        view = self.window.tasks
        from PySide6.QtWidgets import QMessageBox

        original = QMessageBox.exec
        QMessageBox.exec = lambda box: QMessageBox.StandardButton.Cancel  # the dialog is not shown; the user declines
        try:
            view._ask(CD)
        finally:
            QMessageBox.exec = original
        self.assertIn("FX-Kurse veraltet", view.last_confirm_text)
        self.assertEqual(self.backend.started, [])

    def test_no_warning_when_the_prerequisites_are_fine(self) -> None:
        self.open_page()
        view = self.window.tasks
        self.assertEqual(view._ask_text(CD, view.rows[CD].run_warning).count("Achtung"), 0)
        from gui.views.tasks import TasksView

        self.assertNotIn("Achtung", TasksView._ask_text(MI))  # the old single-argument form still works

    def test_the_existing_start_function_is_intact(self) -> None:
        self.open_page()
        view = self.window.tasks
        view.confirm = lambda name: False
        view.run_buttons[MD].click()
        pump(0.5)
        self.assertEqual(self.backend.started, [])  # declined: nothing started
        view.confirm = lambda name: True
        view.run_buttons[MD].click()
        self.assertTrue(pump(10, lambda: self.backend.started == [MD]))
        self.assertTrue(pump(10, lambda: "gestartet" in view.message.text()))


if __name__ == "__main__":
    unittest.main()
