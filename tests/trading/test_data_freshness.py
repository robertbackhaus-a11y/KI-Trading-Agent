# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The read-only data freshness of what the scheduled tasks produce (``get_data_freshness``): facts per source, isolation of a failing source, the existing age
rules, read-only behaviour; synthetic data only."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))

import data_freshness as df  # noqa: E402
from strategy_config import DataQualityConfig, FXConfig, MarketIntelligenceConfig  # noqa: E402

SCHEMA = """
CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE positions (security_id INTEGER PRIMARY KEY, shares REAL);
CREATE TABLE market_data (id INTEGER PRIMARY KEY, security_id INTEGER, trade_date TEXT, close REAL, source_id INTEGER, fetched_at TEXT);
CREATE TABLE market_snapshot (security_id INTEGER PRIMARY KEY, fetched_at TEXT);
CREATE TABLE fx_rates (id INTEGER PRIMARY KEY, rate_date TEXT, base_currency TEXT, quote_currency TEXT, rate REAL, source TEXT, fetched_at TEXT);
CREATE TABLE news (id INTEGER PRIMARY KEY, security_id INTEGER, published_at TEXT, fetched_at TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, security_id INTEGER, event_date TEXT, created_at TEXT);
CREATE TABLE fundamentals (id INTEGER PRIMARY KEY, security_id INTEGER, period_end TEXT, filing_date TEXT, fetched_at TEXT);
"""


def fake_module(document=None, reason="NO_REPORT", problems=()):
    return SimpleNamespace(read_latest_report=lambda directory: (document, None if document else reason, list(problems)))


def report_module(generated="2026-10-14T06:15:01+00:00", evaluation="2026-10-14"):
    return fake_module({"metadata": {"generated_at": generated, "evaluation_as_of": evaluation}})


class FreshnessCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA)
        c = self.conn
        c.execute("INSERT INTO data_sources VALUES (1, 'Yahoo Finance'), (2, 'Other')")
        c.executemany("INSERT INTO positions VALUES (?, ?)", [(1, 10), (2, 5), (3, 0)])  # 3 is not held
        c.executemany("INSERT INTO market_data(security_id, trade_date, close, source_id, fetched_at) VALUES (?, ?, 1, ?, ?)",
                      [(1, "2026-10-13", 1, "2026-10-14T08:56:16+00:00"), (1, "2026-10-14", 1, "2026-10-14T08:56:16+00:00"), (2, "2026-10-12", 1, "2026-10-13T08:00:00+00:00"),
                       (3, "2026-10-14", 1, "2026-10-14T08:56:16+00:00"), (2, "2026-10-20", 2, "2026-10-20T00:00:00+00:00")])  # a bar of another source is ignored
        c.execute("INSERT INTO market_snapshot VALUES (1, '2026-10-14T08:56:20+00:00')")
        c.executemany("INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', ?, 1.1, 'ECB', ?)",
                      [("2026-10-13", "USD", "2026-10-14T08:55:00+00:00"), ("2026-10-09", "KRW", "2026-10-10T08:55:00+00:00"), ("2026-10-12", "GBP", "2026-10-13T08:55:00+00:00")])
        c.executemany("INSERT INTO news(security_id, published_at, fetched_at) VALUES (1, ?, ?)", [("2026-10-14T08:48:13+00:00", "2026-10-14T08:56:17+00:00"), ("2026-10-10T00:00:00+00:00", "2026-10-11T00:00:00+00:00")])
        c.execute("INSERT INTO events(security_id, event_date, created_at) VALUES (1, '2026-10-08', '2026-10-09 15:25:23')")
        c.execute("INSERT INTO fundamentals(security_id, period_end, filing_date, fetched_at) VALUES (1, '2026-08-02', '2026-09-10', '2026-10-09T06:04:49+00:00')")

    def build(self, **kwargs):
        args = {"discovery_module": report_module("2026-10-13T15:35:02+00:00", "2026-10-13"), "intelligence_module": report_module(),
                "discovery_dir": Path("d"), "intelligence_dir": Path("i")}
        args.update(kwargs)
        return df.build_data_freshness(self.conn, **args)


class FactTests(FreshnessCase):
    def test_market_data_facts(self) -> None:
        facts = self.build()["sources"]["market_data"]
        self.assertTrue(facts["available"])
        self.assertEqual(facts["latest_bar_date"], "2026-10-14")  # the bar of the other source (10-20) does not count
        self.assertEqual(facts["oldest_held_bar_date"], "2026-10-12")  # the held position with the oldest latest bar
        self.assertEqual((facts["held_positions"], facts["held_without_bars"]), (2, 0))
        self.assertEqual(facts["snapshot_fetched_at"], "2026-10-14T08:56:20+00:00")

    def test_a_held_position_without_bars_is_counted(self) -> None:
        self.conn.execute("INSERT INTO positions VALUES (9, 1)")
        facts = self.build()["sources"]["market_data"]
        self.assertEqual((facts["held_positions"], facts["held_without_bars"]), (3, 1))

    def test_fx_facts_name_the_oldest_currency(self) -> None:
        facts = self.build()["sources"]["fx"]
        self.assertEqual((facts["latest_rate_date"], facts["oldest_currency_date"], facts["oldest_currency"], facts["currencies"]), ("2026-10-13", "2026-10-09", "KRW", 3))
        self.assertEqual(facts["fetched_at"], "2026-10-14T08:55:00+00:00")

    def test_news_events_and_fundamentals_facts(self) -> None:
        sources = self.build()["sources"]
        self.assertEqual(sources["events_news"], {"available": True, "news_fetched_at": "2026-10-14T08:56:17+00:00", "latest_news_published_at": "2026-10-14T08:48:13+00:00",
                                                  "events_created_at": "2026-10-09 15:25:23", "latest_event_date": "2026-10-08"})
        self.assertEqual(sources["fundamentals"], {"available": True, "fetched_at": "2026-10-09T06:04:49+00:00", "latest_period_end": "2026-08-02", "latest_filing_date": "2026-09-10"})

    def test_the_reports_give_the_last_successful_run(self) -> None:
        sources = self.build()["sources"]
        self.assertEqual(sources["discovery"], {"available": True, "status": "AVAILABLE", "generated_at": "2026-10-13T15:35:02+00:00", "evaluation_as_of": "2026-10-13"})
        self.assertEqual(sources["market_intelligence"]["generated_at"], "2026-10-14T06:15:01+00:00")

    def test_a_missing_report_is_unavailable_with_the_reason(self) -> None:
        sources = self.build(discovery_module=fake_module(None, "NO_DISCOVERY_REPORT"), intelligence_module=fake_module(None, "REPORT_INVALID", ["bad json"]))["sources"]
        self.assertEqual((sources["discovery"]["status"], sources["discovery"]["reason"]), ("UNAVAILABLE", "NO_DISCOVERY_REPORT"))
        self.assertEqual((sources["market_intelligence"]["reason"], sources["market_intelligence"]["problems"]), ("REPORT_INVALID", ["bad json"]))

    def test_an_empty_database_has_no_stand_instead_of_an_error(self) -> None:
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        empty.executescript(SCHEMA)
        result = df.build_data_freshness(empty, discovery_module=report_module(), intelligence_module=report_module(), discovery_dir=Path("d"), intelligence_dir=Path("i"))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["sources"]["market_data"]["latest_bar_date"])
        self.assertIsNone(result["sources"]["fx"]["latest_rate_date"])
        self.assertIsNone(result["sources"]["fundamentals"]["fetched_at"])


class RobustnessTests(FreshnessCase):
    def test_one_unreadable_source_does_not_hide_the_others(self) -> None:
        self.conn.execute("DROP TABLE news")
        result = self.build()
        self.assertTrue(result["ok"])
        self.assertFalse(result["sources"]["events_news"]["available"])
        self.assertIn("OperationalError", result["sources"]["events_news"]["error"])
        self.assertTrue(result["sources"]["fx"]["available"] and result["sources"]["market_data"]["available"])

    def test_a_failing_report_module_is_isolated_too(self) -> None:
        def boom(directory):
            raise OSError("disk")

        result = self.build(intelligence_module=SimpleNamespace(read_latest_report=boom))
        self.assertFalse(result["sources"]["market_intelligence"]["available"])
        self.assertTrue(result["sources"]["discovery"]["available"])

    def test_the_rules_are_the_existing_project_rules(self) -> None:
        rules = self.build()["rules"]
        self.assertEqual(rules, {"market_data_max_age_days": DataQualityConfig().market_data_max_age_days, "fx_max_age_days": FXConfig().fx_max_age_days,
                                 "discovery_report_max_age_days": DataQualityConfig().market_data_max_age_days, "intelligence_report_max_age_hours": MarketIntelligenceConfig().report_max_age_hours})

    def test_nothing_is_written_and_the_answer_is_facts_only(self) -> None:
        self.conn.execute("PRAGMA query_only = ON")
        before = self.conn.total_changes
        result = self.build()
        self.assertEqual(self.conn.total_changes, before)
        self.assertNotIn("status", {k for k in result if k != "sources"})  # no verdict at the top level: the clients judge the age with the rules
        source = (ROOT / "tools" / "trading" / "data_freshness.py").read_text(encoding="utf-8")
        for forbidden in ("INSERT", "UPDATE ", "DELETE", "commit(", "urlopen", "subprocess", "Start-ScheduledTask"):
            self.assertNotIn(forbidden, source)

    def test_the_tool_is_registered_without_parameters(self) -> None:
        import importlib.util
        import inspect

        spec = importlib.util.spec_from_file_location("trading_sqlite_freshness_test", ROOT / "mcp-tools" / "trading_sqlite.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertEqual(list(inspect.signature(module.Tools.get_data_freshness).parameters), ["self"])


if __name__ == "__main__":
    unittest.main()
