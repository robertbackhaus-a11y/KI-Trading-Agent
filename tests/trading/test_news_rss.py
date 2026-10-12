"""Company-wire RSS in the existing market-intelligence pipeline: normalization, alias attribution, cross-source deduplication, feed health,
failure isolation, report/reader diagnostics and an alias-consistent opportunity view.  Synthetic securities, feeds and news only."""

from __future__ import annotations

import copy
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import intelligence_report as ir  # noqa: E402
import market_intelligence as mi  # noqa: E402
import opportunity_view as ov  # noqa: E402
import test_market_intelligence as mit  # noqa: E402  (module import: its test classes must not be collected here a second time)
import test_opportunity_view as otv  # noqa: E402
from security_aliases import AliasIndex, SecurityAlias  # noqa: E402

NOW = mit.NOW
AS_OF = mit.AS_OF
FETCHED = mit.FETCHED
CONFIG = ROOT / "tools" / "trading" / "universe" / "news_feeds_v1.json"

IDX = AliasIndex([
    SecurityAlias(1, "ALFA", ("ALFA",), "US0000000001", "A0AAAA", "Alpha Semi (ADR)", (("YAHOO", "ALFA"),)),
    SecurityAlias(2, "GOLF.DE", ("GOLF", "GOLF.DE"), "DE0000000002", None, "Golf Defense", (("YAHOO", "GOLF.DE"),)),
    SecurityAlias(3, "AI", ("AI",), None, None, "Artificial Systems Corp", (("YAHOO", "AI"),)),
    SecurityAlias(4, "ON", ("ON",), None, None, "Ontario Devices Inc", (("YAHOO", "ON"),)),
    SecurityAlias(5, "AXIO", ("AXIO",), None, None, "Axiom Micro Devices", (("YAHOO", "AXIO"),)),
])
PORTFOLIO = {"ALFA": {"name": "Alpha Semi (ADR)", "security_id": 1}, "GOLF.DE": {"name": "Golf Defense", "security_id": 2}, "AI": {"name": "Artificial Systems Corp", "security_id": 3}}
WATCHLIST = {**PORTFOLIO, "AXIO": {"name": "Axiom Micro Devices", "security_id": 5}, "ON": {"name": "Ontario Devices Inc", "security_id": 4}}
SPEC = {"url": "https://feeds.example/wire.rss", "label": "Wire", "publisher": "PR Newswire", "source": "PR_NEWSWIRE"}
SPECS = {"WIRE_A": SPEC, "WIRE_B": {**SPEC, "url": "https://feeds.example/wire-b.rss"}}
PUBLISHED = "2026-10-06T10:00:00+00:00"


def wire_item(title, link, lead="", published=PUBLISHED, guid=None):
    return {"title": title, "link": link, "published_at": published, "summary": (lead or None) and lead[:200], "guid": guid or link, "lead": lead or None}


def build(rss=None, news=None, **kw):
    return mi.build_market_intelligence(
        evaluation_as_of=AS_OF, fetched_at=FETCHED, feed_items={}, news_items=news or [], sec_rows=[], portfolio=PORTFOLIO, watchlist=WATCHLIST, discovery={},
        sector_map=mit.SECTOR_MAP, rss_items=rss or {}, news_feeds=SPECS, alias_index=IDX, **kw)


def events_of(result):
    return result["events"]


# ------------------------------------------------------------------------------------------------------------------------- G
class RssNormalizationTests(unittest.TestCase):
    def test_g_a_wire_release_becomes_a_normalized_event_of_the_same_model(self) -> None:
        item = wire_item("Alpha Semi raises its outlook for the full year", "https://www.prnewswire.com/news-releases/alpha-1.html",
                         "NEW YORK, Oct. 6, 2026 /PRNewswire/ -- Alpha Semi (ADR) (NASDAQ: ALFA) today raised its outlook.", guid="prn-guid-1")
        (event,) = events_of(build({"WIRE_A": [item]}))
        self.assertEqual((event["source"], event["source_type"], event["source_event_id"]), ("PR_NEWSWIRE", "NEWS", "prn-guid-1"))
        self.assertEqual((event["headline"], event["source_url"], event["published_at"]), (item["title"], item["link"], PUBLISHED))
        self.assertEqual((event["security_ids"], event["affected_symbols"], event["affected_portfolio_symbols"]), ([1], ["ALFA"], ["ALFA"]))
        self.assertEqual(event["matches"], [{"security_id": 1, "matched_by": "TEXT_EXCHANGE_TICKER", "matched_alias": "NASDAQ ALFA"}])
        self.assertEqual(event["impact"], "UNKNOWN")
        self.assertTrue(len(event["summary"]) <= 200)
        self.assertEqual(sorted(k for k in event if k.startswith("_")), [])  # no internal bookkeeping leaks into the event
        for key in mit.ir.EVENT_FIELDS:
            self.assertIn(key, event)

    def test_g_importance_comes_from_the_existing_keyword_table_not_from_the_source(self) -> None:
        guidance = wire_item("Alpha Semi raises its outlook", "https://x/1", "Alpha Semi (ADR) (NASDAQ: ALFA)")
        plain = wire_item("Alpha Semi announces a webinar", "https://x/2", "Alpha Semi (ADR) (NASDAQ: ALFA)")
        by_head = {e["headline"]: e for e in events_of(build({"WIRE_A": [guidance, plain]}))}
        self.assertEqual((by_head["Alpha Semi raises its outlook"]["category"], by_head["Alpha Semi raises its outlook"]["importance"]), ("GUIDANCE", "HIGH"))
        self.assertEqual((by_head["Alpha Semi announces a webinar"]["category"], by_head["Alpha Semi announces a webinar"]["importance"]), ("COMPANY", "LOW"))
        self.assertIn("COMPANY_PRESS_RELEASE", by_head["Alpha Semi raises its outlook"]["reason_codes"])
        # the same guidance headline from a non-wire secondary site stays capped at MEDIUM: the wire is exempt from the cap, nothing is raised
        yahoo = mit.news_item("ALFA", "Alpha Semi raises its outlook", url="https://finance.yahoo.com/a/9.html", publisher="Zacks")
        (secondary,) = [e for e in events_of(build(news=[yahoo]))]
        self.assertEqual(secondary["importance"], "MEDIUM")

    def test_g_unrelated_releases_are_not_events_and_are_counted(self) -> None:
        unrelated = wire_item("A distant company opens a new office", "https://x/3", "Elsewhere Corp (NASDAQ: ZZZZ) today opened an office")
        result = build({"WIRE_A": [unrelated, wire_item("Alpha Semi news", "https://x/4", "Alpha Semi (ADR) (NASDAQ: ALFA)")]})
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["news_feed_stats"]["WIRE_A"], {"fetched": 2, "unrelated": 1, "accepted": 1, "deduplicated": 0})

    def test_attribution_needs_an_explicit_identification(self) -> None:
        sentence = "AI is changing how ON the lights work; IT and ALFA are words here"
        self.assertEqual(events_of(build({"WIRE_A": [wire_item("AI stocks to watch", "https://x/5", sentence)]})), [])
        by_isin = wire_item("Notice for holders", "https://x/6", "ISIN DE0000000002 will be adjusted")
        (event,) = events_of(build({"WIRE_A": [by_isin]}))
        self.assertEqual((event["security_ids"], event["matches"][0]["matched_by"]), ([2], "TEXT_ISIN"))
        short = wire_item("Artificial Systems launches product", "https://x/7", "Artificial Systems (NYSE: AI) today launched")
        (short_event,) = events_of(build({"WIRE_A": [short]}))
        self.assertEqual((short_event["security_ids"], short_event["matches"][0]["matched_alias"]), ([3], "NYSE AI"))

    def test_news_outside_the_lookback_window_is_dropped(self) -> None:
        old = wire_item("Alpha Semi old news", "https://x/8", "Alpha Semi (ADR) (NASDAQ: ALFA)", published="2026-08-01T10:00:00+00:00")
        self.assertEqual(events_of(build({"WIRE_A": [old]})), [])

    def test_parse_feed_keeps_guid_and_a_longer_lead_and_counts_skipped_entries(self) -> None:
        payload = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
                   b"<item><title>Alpha Semi news</title><link>https://www.prnewswire.com/news-releases/a.html</link><guid>guid-a</guid>"
                   b"<pubDate>Tue, 06 Oct 2026 10:00:00 +0000</pubDate><description>&lt;p&gt;" + b"x " * 150 + b" (NASDAQ: ALFA)&lt;/p&gt;</description></item>"
                   b"<item><title>No date</title><link>https://x/y</link></item></channel></rss>")
        items, skipped = mi.parse_feed_detailed(payload)
        self.assertEqual((len(items), skipped, items[0]["guid"]), (1, 1, "guid-a"))
        self.assertTrue(len(items[0]["summary"]) <= 200 < len(items[0]["lead"]) <= 600)
        self.assertEqual(mi.parse_feed(payload), items)  # the long-standing entry point is unchanged
        with self.assertRaises(Exception):
            mi.parse_feed_detailed(b"<html>not a feed")

    def test_the_shipped_feed_configuration_is_valid_and_lists_only_public_https_feeds(self) -> None:
        payload = json.loads(CONFIG.read_text(encoding="utf-8"))
        feeds = mi.load_news_feeds(payload)
        self.assertTrue(feeds and all(spec["url"].startswith("https://www.prnewswire.com/rss/") for spec in feeds.values()))
        self.assertEqual({spec["source"] for spec in feeds.values()}, {"PR_NEWSWIRE"})
        self.assertEqual([entry["source"] for entry in payload["not_integrated"]], ["GlobeNewswire", "Reuters"])
        self.assertNotIn("globenewswire.com", CONFIG.read_text(encoding="utf-8").split('"not_integrated"')[0])
        for broken in ({"version": 2, "feeds": []}, {"version": 1, "feeds": [{"key": "lower", "url": "https://x", "publisher": "P", "source": "S"}]},
                       {"version": 1, "feeds": [{"key": "A_B", "url": "http://x", "publisher": "P", "source": "SRC"}]},
                       {"version": 1, "feeds": [{"key": "A_B", "url": "https://x", "publisher": "P", "source": "SRC"}, {"key": "A_B", "url": "https://y", "publisher": "P", "source": "SRC"}]}):
            with self.assertRaises(ValueError):
                mi.load_news_feeds(broken)
        self.assertEqual(mi.load_news_feeds({"version": 1, "feeds": [{"key": "OFF", "url": "https://x", "publisher": "P", "source": "SRC", "enabled": False}]}), {})


# ------------------------------------------------------------------------------------------------------------------------- I, J
class DeduplicationTests(unittest.TestCase):
    HEAD = "Alpha Semi raises its outlook for the full year"

    def test_i_the_same_news_from_yahoo_and_rss_is_one_event_that_remembers_both_sources(self) -> None:
        rss = wire_item(self.HEAD, "https://www.prnewswire.com/news-releases/alpha-1.html", "Alpha Semi (ADR) (NASDAQ: ALFA)", published="2026-10-06T09:00:00+00:00")
        yahoo = mit.news_item("ALFA", self.HEAD + " - Yahoo Finance", url="https://finance.yahoo.com/news/alpha-semi-raises-outlook-123.html", publisher="PR Newswire",
                              published="2026-10-06T11:30:00+00:00")
        result = build({"WIRE_A": [rss]}, news=[yahoo])
        (event,) = result["events"]
        self.assertEqual(event["source"], "PR_NEWSWIRE")  # the wire copy represents the event
        self.assertEqual([d["source"] for d in event["duplicate_sources"]], ["YAHOO_NEWS"])
        self.assertEqual(event["published_at"], "2026-10-06T09:00:00+00:00")  # earliest publication
        self.assertEqual(result["source_counts"], {"PR_NEWSWIRE": 1})
        self.assertEqual(result["news_feed_stats"]["WIRE_A"]["deduplicated"], 0)  # the RSS copy is the one that was kept

    def test_the_headline_normalization_ignores_tickers_publisher_suffixes_case_and_punctuation(self) -> None:
        self.assertEqual(mi.headline_key("Alpha Semi (NASDAQ: ALFA) Raises Outlook, for the full year! - Business Wire"), mi.headline_key("ALPHA SEMI raises outlook for the full year"))
        self.assertEqual(mi.url_key("https://www.PRNewswire.com/news-releases/x.html/?utm=1#frag"), mi.url_key("https://prnewswire.com/news-releases/x.html"))

    def test_the_same_url_is_one_event_even_with_different_headlines(self) -> None:
        a = mit.news_item("ALFA", "Alpha Semi wins an order", url="https://finance.yahoo.com/n/1.html?x=1")
        b = mit.news_item("ALFA", "Order win for Alpha Semi", url="https://www.finance.yahoo.com/n/1.html")
        self.assertEqual(len(build(news=[a, b])["events"]), 1)

    def test_a_different_security_or_a_wide_time_gap_keeps_the_events_apart(self) -> None:
        a = mit.news_item("ALFA", "Quarterly dividend declared by the board of directors", url="https://finance.yahoo.com/n/a.html")
        other_company = mit.news_item("AXIO", "Quarterly dividend declared by the board of directors", url="https://finance.yahoo.com/n/b.html")
        self.assertEqual(len(build(news=[a, other_company])["events"]), 2)  # a generic headline of two companies is not one article
        later = mit.news_item("ALFA", "Quarterly dividend declared by the board of directors", url="https://finance.yahoo.com/n/c.html", published="2026-10-03T10:00:00+00:00")
        self.assertEqual(len(build(news=[a, later])["events"]), 2)  # more than 48 hours apart
        near = mit.news_item("ALFA", "Quarterly dividend declared by the board of directors", url="https://finance.yahoo.com/n/d.html", published="2026-10-06T20:00:00+00:00")
        self.assertEqual(len(build(news=[a, near])["events"]), 1)

    def test_a_short_recurring_headline_is_only_the_same_article_within_a_few_hours(self) -> None:
        a = mit.news_item("ALFA", "Most Active Stocks Today", url="https://finance.yahoo.com/n/m1.html", published="2026-10-06T08:00:00+00:00")
        same_day = mit.news_item("ALFA", "Most Active Stocks Today", url="https://finance.yahoo.com/n/m2.html", published="2026-10-06T10:00:00+00:00")
        next_day = mit.news_item("ALFA", "Most Active Stocks Today", url="https://finance.yahoo.com/n/m3.html", published="2026-10-07T08:00:00+00:00")
        self.assertEqual(len(build(news=[a, same_day])["events"]), 1)
        self.assertEqual(len(build(news=[a, next_day])["events"]), 2)  # the next edition of a recurring column is a new event

    def test_j_the_same_release_in_two_rss_feeds_is_one_event_and_feed_health_counts_it(self) -> None:
        item = wire_item(self.HEAD, "https://www.prnewswire.com/news-releases/alpha-1.html", "Alpha Semi (ADR) (NASDAQ: ALFA)", guid="guid-1")
        copy_in_b = {**item, "link": "https://www.prnewswire.com/news-releases/alpha-1.html?tc=eml_cleartime"}
        result = build({"WIRE_A": [item], "WIRE_B": [copy_in_b]})
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["news_feed_stats"], {"WIRE_A": {"fetched": 1, "unrelated": 0, "accepted": 1, "deduplicated": 0},
                                                       "WIRE_B": {"fetched": 1, "unrelated": 0, "accepted": 1, "deduplicated": 1}})

    def test_the_source_event_id_identifies_the_same_item(self) -> None:
        one = wire_item("Alpha Semi news A", "https://www.prnewswire.com/news-releases/a1.html", "Alpha Semi (ADR) (NASDAQ: ALFA)", guid="same-guid")
        two = wire_item("Alpha Semi news B (updated)", "https://www.prnewswire.com/news-releases/a2.html", "Alpha Semi (ADR) (NASDAQ: ALFA)", guid="same-guid")
        self.assertEqual(len(build({"WIRE_A": [one], "WIRE_B": [two]})["events"]), 1)

    def test_a_repeated_run_does_not_multiply_or_change_the_events(self) -> None:
        feeds = {"WIRE_A": [wire_item(self.HEAD, "https://www.prnewswire.com/news-releases/alpha-1.html", "Alpha Semi (ADR) (NASDAQ: ALFA)")]}
        first, second = build(copy.deepcopy(feeds)), build(copy.deepcopy(feeds))
        self.assertEqual(first["events"], second["events"])
        report_a = ir.build_report(first, generated_at=NOW)
        report_b = ir.build_report(second, generated_at=NOW)
        self.assertEqual(report_a["metadata"]["events_sha256"], report_b["metadata"]["events_sha256"])
        self.assertEqual(len({e["event_id"] for e in first["events"]}), len(first["events"]))

    def test_official_and_sec_events_are_never_merged_by_headline(self) -> None:
        feed_a = mit.feed("ECB_PRESS", "Monetary policy decisions", "https://www.ecb.europa.eu/press/a")
        feed_b = mit.feed("FED_MONETARY", "Monetary policy decisions", "https://www.federalreserve.gov/b")
        events = mi.build_market_intelligence(evaluation_as_of=AS_OF, fetched_at=FETCHED, feed_items={**feed_a, **feed_b}, news_items=[], sec_rows=[], portfolio={}, watchlist={},
                                              discovery={}, sector_map=mit.SECTOR_MAP)["events"]
        self.assertEqual(len(events), 2)


# ------------------------------------------------------------------------------------------------------------------------- semantics
class SemanticsTests(unittest.TestCase):
    def test_l_an_explicitly_identified_release_is_direct(self) -> None:
        (event,) = events_of(build({"WIRE_A": [wire_item("Alpha Semi announces a webinar", "https://x/1", "Alpha Semi (ADR) (NASDAQ: ALFA) will host a call")]}))
        self.assertIn("HEADLINE_NAMES_COMPANY", event["reason_codes"])
        self.assertEqual(ir.event_link(event), "DIRECT")
        self.assertEqual(ov.event_link(event, "ALFA", "Alpha Semi (ADR)", 1), "DIRECT")

    def test_m_a_sector_event_is_not_made_direct_by_alias_resolution(self) -> None:
        sector = mit.news_item("AXIO", "Semiconductor stocks rally on chip demand", url="https://finance.yahoo.com/n/s.html", publisher="Zacks")
        (event,) = events_of(build(news=[sector]))
        self.assertIn("SECTOR_SCOPED_EVENT", event["reason_codes"])
        for security_id, symbol, name in ((5, "AXIO", "Axiom Micro Devices"), (1, "ALFA", "Alpha Semi (ADR)")):
            self.assertNotEqual(ov.event_link(event, symbol, name, security_id), "DIRECT")
        # a provider-attached symbol whose headline names nobody stays LOOSE, whatever the alias
        loose = mit.news_item("ALFA", "Stocks that moved on Tuesday", url="https://finance.yahoo.com/n/l.html", publisher="Zacks")
        (loose_event,) = events_of(build(news=[loose]))
        self.assertEqual(ov.event_link(loose_event, "ALFA", "Alpha Semi (ADR)", 1), "LOOSE")
        self.assertEqual(loose_event["matches"][0]["matched_by"], "SOURCE_IDENTIFIER")

    def test_n_impact_and_news_never_change_a_direction(self) -> None:
        events = events_of(build({"WIRE_A": [wire_item("Alpha Semi shares plunge after record profit warning", "https://x/2", "Alpha Semi (ADR) (NASDAQ: ALFA)")]}))
        self.assertEqual({e["impact"] for e in events}, {"UNKNOWN"})
        self.assertEqual({e["impact_basis"] for e in events}, {"NOT_READ_FROM_HEADLINE"})

    def test_structured_provider_metadata_is_matched_by_security_id_and_source_identifier(self) -> None:
        cached = {**mit.news_item("ALFA", "Alpha Semi supplier news"), "security_id": 1}
        fresh = mit.news_item("GOLF.DE", "Golf Defense supplier news", url="https://finance.yahoo.com/n/g.html")  # discovery-style: only the Yahoo symbol is known
        by_symbol = {e["affected_symbols"][0]: e for e in events_of(build(news=[cached, fresh]))}
        self.assertEqual(by_symbol["ALFA"]["matches"][0]["matched_by"], "SECURITY_ID")
        self.assertEqual((by_symbol["GOLF.DE"]["matches"][0]["matched_by"], by_symbol["GOLF.DE"]["security_ids"]), ("SOURCE_IDENTIFIER", [2]))

    def test_discovery_candidates_outside_the_security_master_can_still_be_identified(self) -> None:
        discovery = {"NOVA": {"name": "Nova Graphics", "status": "DISCOVERY_READY", "rank": 1}}
        item = wire_item("Nova Graphics launches", "https://x/9", "Nova Graphics (NASDAQ: NOVA) launched a product")
        result = mi.build_market_intelligence(evaluation_as_of=AS_OF, fetched_at=FETCHED, feed_items={}, news_items=[], sec_rows=[], portfolio=PORTFOLIO, watchlist=WATCHLIST,
                                              discovery=discovery, sector_map=mit.SECTOR_MAP, rss_items={"WIRE_A": [item]}, news_feeds=SPECS, alias_index=IDX)
        (event,) = result["events"]
        self.assertEqual((event["affected_symbols"], event["affected_discovery_symbols"], event["security_ids"]), (["NOVA"], ["NOVA"], []))


# ------------------------------------------------------------------------------------------------------------------------- H + feed health
class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = mit.Workspace()
        self.ws.write_discovery()
        self.addCleanup(self.ws.close)
        self.urls = {key: spec["url"] for key, spec in mi.load_news_feeds(json.loads(CONFIG.read_text(encoding="utf-8"))).items()}

    def fetch(self, **by_key):
        official = self.ws.feeds()
        by_url = {self.urls[key]: payload for key, payload in by_key.items()}

        def fetch(url):
            if url in by_url:
                payload = by_url[url]
                if isinstance(payload, Exception):
                    raise payload
                return payload
            if url in self.urls.values():
                return mit.rss()  # a configured feed the test does not care about: empty
            return official(url)

        return fetch

    def release(self, title="Alpha Semi (ADR) wins a large order", link="https://www.prnewswire.com/news-releases/alpha-order.html", lead="(NASDAQ: ALFA) today announced an order"):
        return mit.rss((title, link, "Tue, 06 Oct 2026 12:00:00 +0200", f"&lt;p&gt;NEW YORK /PRNewswire/ -- Alpha Semi {lead}&lt;/p&gt;"))

    def test_h_broken_feeds_do_not_stop_the_collector(self) -> None:
        fetch = self.fetch(PRN_ALL=self.release(), PRN_HEALTH=OSError("down"), PRN_ENERGY=b"<html>blocked", PRN_TELECOM=mit.rss())
        code, out, err = self.ws.run_cli(fetch=fetch)
        self.assertEqual((code, err), (0, ""))
        report = self.ws.latest()
        self.assertEqual(ir.validate_report(report), [])
        warnings = report["metadata"]["warnings"]
        self.assertTrue({"RSS_FEED_FAILED:PRN_HEALTH", "RSS_FEED_FAILED:PRN_ENERGY"} <= set(warnings))
        self.assertFalse([w for w in warnings if "UNAVAILABLE" in w and "NEWS_FEED" not in w and "DISCOVERY" not in w])
        feeds = report["metadata"]["collection"]["news_feeds"]
        self.assertEqual((feeds["PRN_HEALTH"]["status"], feeds["PRN_HEALTH"]["parse_errors"]), ("FAILED", 0))
        self.assertEqual((feeds["PRN_ENERGY"]["status"], feeds["PRN_ENERGY"]["parse_errors"]), ("FAILED", 1))
        self.assertEqual(feeds["PRN_TELECOM"]["status"], "EMPTY")
        self.assertEqual({k: feeds["PRN_ALL"][k] for k in ("status", "parsed", "accepted", "deduplicated", "parse_errors")}, {"status": "OK", "parsed": 1, "accepted": 1, "deduplicated": 0, "parse_errors": 0})
        self.assertEqual([e["source"] for e in report["events"] if e["source"] == "PR_NEWSWIRE"], ["PR_NEWSWIRE"])
        self.assertTrue(any(e["source"] == "ECB_PRESS" for e in report["events"]))  # the other sources are unaffected

    def test_every_rss_feed_failing_is_only_warnings(self) -> None:
        fetch = self.fetch(**{key: OSError("down") for key in self.urls})
        code, _, err = self.ws.run_cli(fetch=fetch)
        self.assertEqual((code, err), (0, ""))
        report = self.ws.latest()
        self.assertEqual(len([w for w in report["metadata"]["warnings"] if w.startswith("RSS_FEED_FAILED")]), len(self.urls))
        self.assertGreater(report["metadata"]["event_count"], 0)

    def test_the_rss_feeds_do_not_replace_the_official_feeds_as_the_precondition_of_a_run(self) -> None:
        fetch = self.fetch(PRN_ALL=self.release())
        official_failing = self.ws.feeds(fail=tuple(mi.FEEDS))

        def only_rss(url):
            return fetch(url) if url in self.urls.values() else official_failing(url)

        code, _, err = self.ws.run_cli(fetch=only_rss)
        self.assertEqual(code, 2)  # unchanged: no official feed answered -> failed run, previous report untouched
        self.assertFalse((self.ws.reports / "latest.json").exists())

    def test_rss_can_be_switched_off_and_a_missing_configuration_is_a_warning(self) -> None:
        code, _, _ = self.ws.run_cli("--no-news-feeds", fetch=self.fetch(PRN_ALL=self.release()))
        self.assertEqual(code, 0)
        self.assertNotIn("PR_NEWSWIRE", self.ws.latest()["metadata"]["source_counts"])
        self.assertEqual(self.ws.latest()["metadata"]["collection"]["news_feeds"], {})
        code, _, _ = self.ws.run_cli("--news-feeds", str(self.ws.dir / "missing.json"), fetch=self.fetch())
        self.assertEqual(code, 0)
        self.assertIn("NEWS_FEED_CONFIG_UNAVAILABLE:FileNotFoundError", self.ws.latest()["metadata"]["warnings"])

    def test_the_collector_identifies_by_the_security_master_and_deduplicates_against_cached_yahoo_news(self) -> None:
        # the cached Yahoo row 'Alpha Semi to acquire stake in fab' (security 1) and a wire copy of the same headline
        item = mit.rss(("Alpha Semi to acquire stake in fab", "https://www.prnewswire.com/news-releases/alpha-fab.html", "Tue, 06 Oct 2026 14:00:00 +0200",
                        "&lt;p&gt;Alpha Semi (NASDAQ: ALFA) will acquire a stake&lt;/p&gt;"))
        code, _, _ = self.ws.run_cli(fetch=self.fetch(PRN_ALL=item))
        self.assertEqual(code, 0)
        events = [e for e in self.ws.latest()["events"] if e["headline"] == "Alpha Semi to acquire stake in fab"]
        self.assertEqual(len(events), 1)
        self.assertEqual(sorted({d["source"] for d in events[0].get("duplicate_sources", [])} | {events[0]["source"]}), ["PR_NEWSWIRE", "YAHOO_NEWS"])
        self.assertEqual(events[0]["security_ids"], [1])
        self.assertEqual(self.ws.latest()["metadata"]["collection"]["news_feeds"]["PRN_ALL"]["deduplicated"], 0)

    def test_the_collector_run_only_reads_the_database_and_is_repeatable(self) -> None:
        before = mit.db_fingerprint(self.ws.db)
        fetch = self.fetch(PRN_ALL=self.release())
        self.assertEqual(self.ws.run_cli(fetch=fetch)[0], 0)
        first = self.ws.latest()
        self.assertEqual(self.ws.run_cli(fetch=fetch)[0], 0)
        second = self.ws.latest()
        self.assertEqual(mit.db_fingerprint(self.ws.db), before)  # no database write
        self.assertEqual(first["metadata"]["events_sha256"], second["metadata"]["events_sha256"])
        self.assertEqual(first["metadata"]["event_count"], second["metadata"]["event_count"])


# ------------------------------------------------------------------------------------------------------------------------- report / reader
def report_with_identity():
    wire = wire_item("Golf Defense wins a large order", "https://www.prnewswire.com/news-releases/golf-1.html", "Golf Defense (XETRA: GOLF) today announced", guid="g-1")
    yahoo = mit.news_item("GOLF.DE", "Golf Defense wins a large order", url="https://finance.yahoo.com/n/golf.html", publisher="PR Newswire", published="2026-10-06T12:00:00+00:00")
    other = {**mit.news_item("ALFA", "Alpha Semi supplier news"), "security_id": 1}
    result = build({"WIRE_A": [wire]}, news=[yahoo, other])
    return ir.build_report(result, generated_at=NOW, collection={}), result


class ReportAndReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report, cls.result = report_with_identity()

    def query(self, **kw):
        return ir.query_report(self.report, now=NOW, **kw)

    def test_the_report_stays_valid_and_carries_an_alias_table(self) -> None:
        self.assertEqual(ir.validate_report(self.report), [])
        self.assertEqual(sorted(self.report["aliases"]), ["1", "2", "3", "4", "5"])  # event securities plus every portfolio / watchlist security
        self.assertEqual(self.report["aliases"]["2"]["isin"], "DE0000000002")

    def test_a_report_without_the_new_optional_fields_is_still_valid_and_queryable(self) -> None:
        legacy = copy.deepcopy(self.report)
        legacy.pop("aliases")
        for event in legacy["events"]:
            for key in ir.OPTIONAL_EVENT_FIELDS:
                event.pop(key, None)
        legacy["metadata"]["events_sha256"] = ir._digest(legacy["events"])
        self.assertEqual(ir.validate_report(legacy), [])
        answer = ir.query_report(legacy, now=NOW, symbol="GOLF.DE")
        self.assertEqual(answer["matching"], 1)

    def test_a_malformed_optional_field_is_rejected(self) -> None:
        broken = copy.deepcopy(self.report)
        broken["events"][0]["security_ids"] = ["x"]
        broken["metadata"]["events_sha256"] = ir._digest(broken["events"])
        self.assertTrue(any("security_ids" in p for p in ir.validate_report(broken)))

    def test_k_the_reader_finds_the_same_events_by_symbol_alias_or_isin(self) -> None:
        answers = {query: self.query(symbol=query) for query in ("GOLF.DE", "golf", "DE0000000002")}
        headlines = {query: sorted(e["headline"] for e in answer["events"]) for query, answer in answers.items()}
        self.assertEqual(len({tuple(h) for h in headlines.values()}), 1)
        self.assertEqual(headlines["golf"], ["Golf Defense wins a large order"])
        self.assertEqual(self.query(symbol="ALFA")["matching"], self.query(symbol="US0000000001")["matching"])
        self.assertEqual(self.query(symbol="ZZZZ")["matching"], 0)

    def test_k_a_sector_scoped_event_is_found_under_every_alias_of_the_security(self) -> None:
        sector = mit.news_item("AXIO", "Defense stocks rally on NATO spending", url="https://finance.yahoo.com/n/sector.html", publisher="Zacks")
        result = build(news=[sector])
        (event,) = result["events"]
        self.assertIn("GOLF.DE", event["affected_symbols"])  # reached through the sector only: no security id of GOLF in the event
        self.assertNotIn(2, event["security_ids"])
        report = ir.build_report(result, generated_at=NOW, collection={})
        self.assertIn("2", report["aliases"])  # a portfolio security without own events still resolves in the reader
        counts = {query: ir.query_report(report, now=NOW, symbol=query)["matching"] for query in ("GOLF.DE", "golf", "DE0000000002")}
        self.assertEqual(len(set(counts.values())), 1, counts)
        self.assertGreaterEqual(counts["DE0000000002"], 1)

    def test_o_the_slim_answer_does_not_grow_and_carries_no_diagnostics(self) -> None:
        slim = self.query(symbol="GOLF.DE")
        for event in slim["events"]:
            self.assertFalse(set(ir.OPTIONAL_EVENT_FIELDS) & set(event))
            self.assertLessEqual(set(event), {"published_at", "importance", "category", "headline", "source", "impact", "link", "reason_codes", "affected_regions", "portfolio_region_exposure"})
        stripped = copy.deepcopy(self.report)
        stripped.pop("aliases")
        for event in stripped["events"]:
            for key in ir.OPTIONAL_EVENT_FIELDS:
                event.pop(key, None)
        stripped["metadata"]["events_sha256"] = ir._digest(stripped["events"])
        self.assertEqual(json.dumps(ir.query_report(stripped, now=NOW, symbol="GOLF.DE"), sort_keys=True), json.dumps(slim, sort_keys=True))
        self.assertEqual(json.dumps(ir.query_report(stripped, now=NOW), sort_keys=True), json.dumps(self.query(), sort_keys=True))

    def test_p_detail_shows_the_matching_diagnostics_and_the_duplicate_sources(self) -> None:
        (event,) = self.query(symbol="GOLF.DE", detail=True)["events"]
        self.assertEqual(event["security_ids"], [2])
        self.assertEqual(event["source_event_id"], "g-1")
        self.assertEqual([m["matched_by"] for m in event["matches"]], ["TEXT_EXCHANGE_TICKER", "SOURCE_IDENTIFIER"])
        self.assertEqual([d["source"] for d in event["duplicate_sources"]], ["YAHOO_NEWS"])

    def test_q_the_reader_and_the_view_never_touch_a_database(self) -> None:
        with mock.patch("sqlite3.connect", side_effect=AssertionError("database access")):
            self.query(symbol="GOLF.DE", detail=True)
            self.query()
            view = otv.ov.build_opportunity_view(engine=otv.make_engine(), symbol_map=otv.SYMBOL_MAP, discovery=otv.make_discovery(), mi=self.report, alias_index=IDX, now=NOW)
            otv.ov.query_view(view, symbol="GOLF")


# ------------------------------------------------------------------------------------------------------------------------- K / R opportunity view
OV_INDEX = AliasIndex([
    SecurityAlias(2, "ALFA", ("ALFA",), "US0000000001", "A0AAAA", "Alpha Semiconductor Manufacturing", (("YAHOO", "ALFA"),)),
    SecurityAlias(10, "GOLF.DE", ("GOLF", "GOLF.DE"), "DE0000000002", None, "Golf Defense AG", (("YAHOO", "GOLF.DE"),)),
    SecurityAlias(18, "AXIO", ("AXIO",), "US0000000018", None, "Axiom Micro Devices", (("YAHOO", "AXIO"),)),
])


def identity_event(i, headline, symbols, security_ids, matched_by="SECURITY_ID", **kw):
    event = otv.event(i, headline, symbols, **kw)
    event["security_ids"] = list(security_ids)
    event["matches"] = [{"security_id": s, "matched_by": matched_by, "matched_alias": "x"} for s in security_ids]
    return event


class OpportunityViewAliasTests(unittest.TestCase):
    def view(self, events, **kw):
        report = otv.make_mi(events)
        return otv.ov.build_opportunity_view(engine=kw.pop("engine", otv.make_engine()), symbol_map=otv.SYMBOL_MAP, discovery=kw.pop("discovery", otv.make_discovery()), mi=report,
                                             alias_index=kw.pop("alias_index", OV_INDEX), now=NOW, **kw)

    def test_k_canonical_alias_and_isin_queries_give_the_same_security_and_news(self) -> None:
        # the event only carries a Frankfurt symbol form plus the security_id; the engine knows the security as 'GOLF' / Yahoo 'GOLF.DE'
        events = [identity_event(1, "Golf Defense receives an order", ["GOLF.F"], [10], importance="HIGH", reasons=["HEADLINE_NAMES_COMPANY"])]
        view = self.view(events)
        answers = {q: otv.ov.query_view(view, symbol=q, detail=True) for q in ("GOLF", "GOLF.DE", "DE0000000002")}
        positions = {q: [p["symbol"] for p in a["portfolio_context"]["positions"]] for q, a in answers.items()}
        self.assertEqual(positions, {"GOLF": ["GOLF"], "GOLF.DE": ["GOLF"], "DE0000000002": ["GOLF"]})
        statuses = {q: a["portfolio_context"]["positions"][0]["news_status"] for q, a in answers.items()}
        self.assertEqual(set(statuses.values()), {"NEWS_HIGH_ATTENTION"})  # found by security_id although the symbol form differs
        counts = {q: a["portfolio_context"]["positions"][0]["news_counts"] for q, a in answers.items()}
        self.assertEqual(len({json.dumps(c, sort_keys=True) for c in counts.values()}), 1)

    def test_the_news_status_does_not_depend_on_the_symbol_form(self) -> None:
        by_symbol = [otv.event(1, "Axiom Micro Devices receives an order", ["AXIO"], importance="HIGH", reasons=["HEADLINE_NAMES_COMPANY"])]
        by_id = [identity_event(1, "Axiom Micro Devices receives an order", ["AXIO-ALT"], [18], importance="HIGH", reasons=["HEADLINE_NAMES_COMPANY"])]
        status = lambda events: otv.ov.build_opportunity_view(engine=otv.make_engine(), symbol_map=otv.SYMBOL_MAP, discovery=otv.make_discovery(), mi=otv.make_mi(events),  # noqa: E731
                                                              alias_index=OV_INDEX, now=NOW)["opportunities"]
        rows_a = {r["symbol"]: r["market_intelligence_summary"]["status"] for r in status(by_symbol)}
        rows_b = {r["symbol"]: r["market_intelligence_summary"]["status"] for r in status(by_id)}
        self.assertEqual(rows_a["AXIO"], rows_b["AXIO"])

    def test_no_event_is_counted_twice_when_it_matches_by_symbol_and_by_id(self) -> None:
        events = [identity_event(1, "Axiom Micro Devices receives an order", ["AXIO"], [18], importance="HIGH", reasons=["HEADLINE_NAMES_COMPANY"])]
        row = {r["symbol"]: r for r in self.view(events)["opportunities"]}["AXIO"]
        self.assertEqual(row["market_intelligence_summary"]["counts"], {"HIGH": 1, "MEDIUM": 0, "LOW": 0})

    def test_a_discovery_symbol_in_another_form_is_not_a_second_row(self) -> None:
        extra = [otv.disc_result(8, "AXIO", "Axiom Micro Devices", price=598.0)]
        view = self.view([], discovery=otv.make_discovery(extra=extra))
        self.assertEqual([r["symbol"] for r in view["opportunities"]].count("AXIO"), 1)

    def test_r_the_ranking_and_every_trading_field_are_unchanged_by_the_alias_layer(self) -> None:
        events = [identity_event(1, "Axiom Micro Devices receives an order", ["AXIO"], [18], importance="HIGH", reasons=["HEADLINE_NAMES_COMPANY"])]
        with_alias = self.view(events)
        without_alias = self.view(events, alias_index=None)
        without_news = self.view([])
        strip = lambda view: [{k: v for k, v in r.items() if k not in ("market_intelligence_summary", "aliases")} for r in view["opportunities"]]  # noqa: E731
        self.assertEqual(strip(with_alias), strip(without_alias))
        self.assertEqual(strip(with_alias), strip(without_news))  # news never change a rank, score, size or decision
        self.assertEqual([r["symbol"] for r in with_alias["opportunities"]], [r["symbol"] for r in without_news["opportunities"]])
        self.assertEqual(with_alias["portfolio_context"]["capital"], without_news["portfolio_context"]["capital"])
        self.assertEqual([(p["symbol"], p["action"], p["priority"]) for p in with_alias["portfolio_context"]["positions"]],
                         [(p["symbol"], p["action"], p["priority"]) for p in without_news["portfolio_context"]["positions"]])

    def test_the_internal_alias_lists_never_reach_the_output(self) -> None:
        view = self.view([])
        for detail in (False, True):
            text = json.dumps(otv.ov.query_view(view, detail=detail, symbol="GOLF"), default=str)
            self.assertNotIn('"aliases"', text)

    def test_without_an_alias_index_the_view_behaves_as_before(self) -> None:
        legacy = otv.build()
        self.assertEqual(legacy["metadata"]["status"], "AVAILABLE")


class RestrictionTests(unittest.TestCase):
    def test_the_reader_tools_keep_their_signatures(self) -> None:
        import inspect

        tools = mit.Tools
        self.assertEqual(list(inspect.signature(tools.get_market_intelligence).parameters), ["self", "scope", "category", "importance", "symbol", "sector", "limit", "detail"])
        self.assertEqual(list(inspect.signature(tools.get_opportunity_view).parameters), ["self", "source", "status", "news_status", "symbol", "limit", "detail"])

    def test_the_orchestrator_does_not_import_the_news_pipeline(self) -> None:
        text = (ROOT / "tools" / "trading" / "trading_orchestrator.py").read_text(encoding="utf-8")
        for name in ("market_intelligence", "intelligence_report", "security_aliases", "opportunity_view"):
            self.assertNotIn(f"import {name}", text)
            self.assertNotIn(f"from {name}", text)


if __name__ == "__main__":
    unittest.main()
