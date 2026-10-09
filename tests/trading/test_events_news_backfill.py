"""Tests for Backfill-TradingEventsNews.py: SEC 8-K/6-K parsing, Yahoo news
parsing, idempotency/deduplication, NULL handling, and the existing
event_risk quality transition (UNAVAILABLE -> PARTIAL -> AVAILABLE) that the
real data now exercises.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "trading"
sys.path.insert(0, str(TOOLS_DIR))

spec = importlib.util.spec_from_file_location(
    "events_news_backfill", TOOLS_DIR / "Backfill-TradingEventsNews.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from analysis_engine import _module_quality  # noqa: E402


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT);
        CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            security_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_date TEXT NOT NULL,
            period_end TEXT,
            title TEXT,
            actual_value REAL,
            estimated_value REAL,
            surprise_percent REAL,
            currency TEXT,
            notes TEXT,
            source_id INTEGER
        );
        CREATE TABLE news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            security_id INTEGER NOT NULL,
            published_at TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            url TEXT,
            publisher TEXT,
            category TEXT,
            sentiment REAL,
            is_material INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            source_id INTEGER,
            fetched_at TEXT
        );
        CREATE UNIQUE INDEX idx_news_url ON news(url) WHERE url IS NOT NULL;
        INSERT INTO security(id, symbol, name) VALUES (1, 'CHAR', 'Charlie Pharma');
        INSERT INTO data_sources(id, name) VALUES (1, 'SEC EDGAR'), (2, 'Yahoo Finance');
        """
    )
    return conn


def submissions_fixture(entries: list[tuple[str, str, str, str, str]]) -> dict:
    """entries: (form, filingDate, reportDate, items, accession)"""
    return {
        "filings": {
            "recent": {
                "form": [e[0] for e in entries],
                "filingDate": [e[1] for e in entries],
                "reportDate": [e[2] for e in entries],
                "items": [e[3] for e in entries],
                "accessionNumber": [e[4] for e in entries],
                "primaryDocument": [f"doc{i}.htm" for i in range(len(entries))],
            }
        }
    }


class SecEventParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.today = date.today()
        self.recent = (self.today - timedelta(days=10)).isoformat()
        self.old = (self.today - timedelta(days=mod.EVENTS_LOOKBACK_DAYS + 30)).isoformat()

    def test_8k_earnings_item_maps_to_earnings_event_type(self) -> None:
        data = submissions_fixture([("8-K", self.recent, self.recent, "2.02,9.01", "acc-1")])
        mod.get_json = lambda url, headers: data  # monkeypatch I/O
        events = mod.fetch_sec_events("0000000001")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "earnings")
        self.assertIn("Results of Operations", events[0]["title"])

    def test_8k_unmapped_item_keeps_raw_code_not_guessed(self) -> None:
        data = submissions_fixture([("8-K", self.recent, self.recent, "6.01", "acc-2")])
        mod.get_json = lambda url, headers: data
        events = mod.fetch_sec_events("0000000001")
        self.assertEqual(events[0]["event_type"], "sec_8k_6.01")

    def test_6k_uses_foreign_issuer_report_never_guesses_from_filename(self) -> None:
        data = submissions_fixture([("6-K", self.recent, "", "", "acc-3")])
        mod.get_json = lambda url, headers: data
        events = mod.fetch_sec_events("0000000001")
        self.assertEqual(events[0]["event_type"], "foreign_issuer_report")
        self.assertIsNone(events[0]["actual_value"])
        self.assertIsNone(events[0]["estimated_value"])
        self.assertIsNone(events[0]["surprise_percent"])

    def test_missing_report_date_is_null_not_fabricated(self) -> None:
        data = submissions_fixture([("8-K", self.recent, "", "8.01", "acc-4")])
        mod.get_json = lambda url, headers: data
        events = mod.fetch_sec_events("0000000001")
        self.assertIsNone(events[0]["period_end"])

    def test_old_filing_excluded_by_lookback_window(self) -> None:
        data = submissions_fixture([("8-K", self.old, self.old, "2.02", "acc-5")])
        mod.get_json = lambda url, headers: data
        events = mod.fetch_sec_events("0000000001")
        self.assertEqual(events, [])

    def test_non_8k_6k_forms_are_ignored(self) -> None:
        data = submissions_fixture([("10-Q", self.recent, self.recent, "", "acc-6")])
        mod.get_json = lambda url, headers: data
        events = mod.fetch_sec_events("0000000001")
        self.assertEqual(events, [])


class StoreEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()

    def test_store_events_writes_correct_security_and_source_id(self) -> None:
        events = [{"event_type": "earnings", "event_date": "2026-08-05", "period_end": "2026-08-05",
                   "title": "t", "actual_value": None, "estimated_value": None,
                   "surprise_percent": None, "currency": None, "notes": "n"}]
        mod.store_events(self.conn, security_id=1, source_id=1, events=events)
        row = self.conn.execute("SELECT * FROM events").fetchone()
        self.assertEqual(row["security_id"], 1)
        self.assertEqual(row["source_id"], 1)
        self.assertIsNone(row["actual_value"])

    def test_store_events_is_idempotent_on_rerun(self) -> None:
        events = [{"event_type": "earnings", "event_date": "2026-08-05", "period_end": None,
                   "title": "t", "actual_value": None, "estimated_value": None,
                   "surprise_percent": None, "currency": None, "notes": None}]
        mod.store_events(self.conn, security_id=1, source_id=1, events=events)
        mod.store_events(self.conn, security_id=1, source_id=1, events=events)
        count = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(count, 1)


class YahooNewsParsingTests(unittest.TestCase):
    def test_news_item_maps_required_fields_and_nulls_unavailable_ones(self) -> None:
        mod.get_json = lambda url, headers: {
            "news": [{"title": "Headline", "publisher": "Reuters", "link": "https://x/1", "providerPublishTime": 1700000000}]
        }
        items = mod.fetch_yahoo_news("CHAR")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["title"], "Headline")
        self.assertEqual(item["publisher"], "Reuters")
        self.assertEqual(item["url"], "https://x/1")
        self.assertIsNotNone(item["published_at"])
        # Yahoo's search endpoint never provides these -- must stay NULL.
        self.assertIsNone(item["summary"])
        self.assertIsNone(item["category"])
        self.assertIsNone(item["sentiment"])
        self.assertIsNone(item["expires_at"])

    def test_incomplete_news_item_is_skipped_not_fabricated(self) -> None:
        mod.get_json = lambda url, headers: {
            "news": [{"title": None, "publisher": "Reuters", "link": "https://x/2", "providerPublishTime": 1700000000}]
        }
        items = mod.fetch_yahoo_news("CHAR")
        self.assertEqual(items, [])

    def test_symbol_search_empty_falls_back_to_company_name(self) -> None:
        calls = []

        def fake_search(query):
            calls.append(query)
            if query == "GOLF.DE":
                return []
            return [{"title": "T", "publisher": "P", "link": "https://x/3", "providerPublishTime": 1700000000}]

        mod._search_yahoo_news = fake_search
        items = mod.fetch_yahoo_news("GOLF.DE", name="Golf Defense")
        self.assertEqual(calls, ["GOLF.DE", "Golf Defense"])
        self.assertEqual(len(items), 1)

    def test_no_fallback_query_when_symbol_search_succeeds(self) -> None:
        calls = []

        def fake_search(query):
            calls.append(query)
            return [{"title": "T", "publisher": "P", "link": "https://x/4", "providerPublishTime": 1700000000}]

        mod._search_yahoo_news = fake_search
        mod.fetch_yahoo_news("CHAR", name="Charlie Pharma")
        self.assertEqual(calls, ["CHAR"])


class StoreNewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()

    def _item(self, url="https://x/1"):
        return {"published_at": "2026-09-28T00:00:00+00:00", "title": "T", "summary": None,
                "url": url, "publisher": "P", "category": None, "sentiment": None,
                "is_material": 0, "expires_at": None}

    def test_store_news_writes_correct_ids(self) -> None:
        mod.store_news(self.conn, security_id=1, source_id=2, news_items=[self._item()])
        row = self.conn.execute("SELECT * FROM news").fetchone()
        self.assertEqual(row["security_id"], 1)
        self.assertEqual(row["source_id"], 2)
        self.assertIsNone(row["summary"])
        self.assertIsNone(row["sentiment"])

    def test_store_news_deduplicates_same_url(self) -> None:
        mod.store_news(self.conn, security_id=1, source_id=2, news_items=[self._item(), self._item()])
        count = self.conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        self.assertEqual(count, 1)

    def test_store_news_is_idempotent_across_runs(self) -> None:
        mod.store_news(self.conn, security_id=1, source_id=2, news_items=[self._item()])
        written_second_run = mod.store_news(self.conn, security_id=1, source_id=2, news_items=[self._item()])
        count = self.conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(written_second_run, 0)

    def test_store_news_different_urls_both_kept(self) -> None:
        mod.store_news(self.conn, security_id=1, source_id=2, news_items=[self._item("https://x/1"), self._item("https://x/2")])
        count = self.conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        self.assertEqual(count, 2)


class EventRiskQualityTransitionTests(unittest.TestCase):
    """Confirms the *existing*, unmodified analysis_engine._module_quality()
    behaves correctly for the two-key event_risk case now that real data
    flows through it -- no new quality logic was built."""

    def test_zero_zero_is_unavailable(self) -> None:
        quality = _module_quality({"events": 0, "news": 0}, "2026-09-28")
        self.assertEqual(quality.status.value, "unavailable")

    def test_one_present_one_zero_is_partial(self) -> None:
        quality = _module_quality({"events": 0, "news": 7}, "2026-09-28")
        self.assertEqual(quality.status.value, "partial")

    def test_both_present_is_available(self) -> None:
        quality = _module_quality({"events": 16, "news": 10}, "2026-09-28")
        self.assertEqual(quality.status.value, "available")


if __name__ == "__main__":
    unittest.main()
