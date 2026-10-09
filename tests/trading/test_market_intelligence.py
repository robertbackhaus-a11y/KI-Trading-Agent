"""Market intelligence: normalization, rule-based classification, mapping, atomic report, MCP reader (no signal changes)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import inspect
import io
import json
import socket
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discovery_report  # noqa: E402
import intelligence_report as ir  # noqa: E402
import market_intelligence as mi  # noqa: E402
from strategy_config import MarketIntelligenceConfig  # noqa: E402

NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)
AS_OF = "2026-10-07"
FETCHED = NOW.isoformat()
SECTOR_MAP = {"NOVA": "SEMICONDUCTORS", "ALFA": "SEMICONDUCTORS", "AXIO": "SEMICONDUCTORS", "GOLF.DE": "DEFENSE_AEROSPACE", "CORV": "ENERGY", "XNRG": "ENERGY", "COPR": "ENERGY", "CHAR": "HEALTHCARE_PHARMA", "ECHO.AS": "SEMICONDUCTORS"}
PORTFOLIO = {"ALFA": {"name": "Alpha Semi (ADR)"}, "GOLF.DE": {"name": "Golf Defense"}, "CHAR": {"name": "Charlie Pharma & Co"}, "ECHO.AS": {"name": "Echo Holding"}}
WATCHLIST = {**PORTFOLIO, "AXIO": {"name": "Axiom Micro Devices"}, "CORV": {"name": "Corvex Corp."}}
DISCOVERY = {"NOVA": {"name": "Nova Graphics", "status": "DISCOVERY_READY", "rank": 3}, "XNRG": {"name": "Xenon Energy", "status": "DISCOVERY_READY", "rank": 9}}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = load_module("collect_cli_test", ROOT / "tools" / "trading" / "Collect-TradingMarketIntelligence.py")
tools_module = load_module("trading_sqlite_mi_test", ROOT / "mcp-tools" / "trading_sqlite.py")
Tools = tools_module.Tools
Tools._runtime_module_cache["intelligence_report"] = ir

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>"""
ITEM = "<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate><description>{desc}</description></item>"


def rss(*entries) -> bytes:
    return RSS.format(items="".join(ITEM.format(title=t, link=l, date=d, desc=desc) for t, l, d, desc in entries)).encode("utf-8")


def run(*, feeds=None, news=None, sec=None, portfolio=PORTFOLIO, watchlist=WATCHLIST, discovery=DISCOVERY, as_of=AS_OF, cfg=None):
    return mi.build_market_intelligence(
        evaluation_as_of=as_of, fetched_at=FETCHED, feed_items=feeds or {}, news_items=news or [], sec_rows=sec or [],
        portfolio=portfolio, watchlist=watchlist, discovery=discovery, sector_map=SECTOR_MAP, config=cfg)


def feed(source, title, link, published="2026-10-06T10:00:00+00:00", summary=None):
    return {source: [{"title": title, "link": link, "published_at": published, "summary": summary}]}


def news_item(symbol, title, url="https://finance.yahoo.com/a/1.html", publisher="Zacks", published="2026-10-06T10:00:00+00:00"):
    return {"symbol": symbol, "title": title, "url": url, "publisher": publisher, "published_at": published}


def classify_news(title, publisher="Zacks", symbol="ALFA"):
    return run(news=[news_item(symbol, title, publisher=publisher)])["events"][0]


class NormalizationTests(unittest.TestCase):
    def test_feed_parsing_and_event_fields(self):
        payload = rss(("Monetary policy decisions", "https://www.ecb.europa.eu//press/pr/date/2026/html/ecb.mp261001~abc", "Thu, 01 Oct 2026 12:00:00 +0200", "&lt;p&gt;The  Governing Council&lt;/p&gt;"))
        items = mi.parse_feed(payload)
        self.assertEqual(items[0]["published_at"], "2026-10-01T10:00:00+00:00")
        self.assertEqual(items[0]["summary"], "The Governing Council")
        event = mi.feed_events("ECB_PRESS", items, fetched_at=FETCHED)[0]
        for key in ("event_id", "published_at", "event_date", "source", "source_url", "category", "headline", "summary", "importance", "impact", "confidence", "fetched_at"):
            self.assertIn(key, event)
        self.assertEqual((event["event_date"], event["source"], event["affected_regions"]), ("2026-10-01", "ECB_PRESS", ["EU"]))

    def test_atom_and_iso_dates_and_relative_links(self):
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>CPI up</title><link href="https://x.org/a"/><updated>2026-10-02T07:51:08-04:00</updated></entry></feed>'
        self.assertEqual(mi.parse_feed(atom)[0]["published_at"], "2026-10-02T11:51:08+00:00")
        eia = mi.feed_events("EIA_PRESS", [{"title": "Outlook", "link": "/pressroom/releases/press593.php", "published_at": "2026-10-06T17:00:00+00:00", "summary": None}], fetched_at=FETCHED)[0]
        self.assertEqual(eia["source_url"], "https://www.eia.gov/pressroom/releases/press593.php")

    def test_items_without_title_link_or_date_are_dropped(self):
        payload = rss(("", "https://a/1", "Thu, 01 Oct 2026 12:00:00 +0200", ""), ("T", "", "Thu, 01 Oct 2026 12:00:00 +0200", ""), ("T", "https://a/2", "not a date", ""))
        self.assertEqual(mi.parse_feed(payload), [])

    def test_summaries_are_short_and_no_article_text_is_stored(self):
        long_text = "word " * 500
        item = mi.parse_feed(rss(("Title", "https://a/1", "Thu, 01 Oct 2026 12:00:00 +0200", long_text)))[0]
        self.assertLessEqual(len(item["summary"]), 200)
        event = run(feeds={"BEA": [item]})["events"][0]
        self.assertLessEqual(len(event["summary"]), 200)
        self.assertNotIn("full_text", event)

    def test_sec_rows_become_events(self):
        row = {"symbol": "ALFA", "event_type": "earnings", "event_date": "2026-10-05", "title": "8-K: Results", "notes": "accession=0001-26-1; doc=x.htm; items=2.02"}
        event = mi.sec_events([row], fetched_at=FETCHED)[0]
        self.assertEqual((event["source_type"], event["category"], event["importance"], event["confidence"]), ("SEC_FILING", "EARNINGS", "MEDIUM", "HIGH"))
        self.assertEqual(event["published_at"], "2026-10-05T00:00:00+00:00")
        self.assertEqual(mi.sec_events([{**row, "event_type": "sec_8k_9.01"}], fetched_at=FETCHED), [])
        self.assertEqual(mi.sec_events([{**row, "event_type": "something_new"}], fetched_at=FETCHED)[0]["reason_codes"], ["SEC_EVENT_TYPE_UNMAPPED"])


class DuplicateAndIdTests(unittest.TestCase):
    def test_same_url_for_several_symbols_is_one_event_with_all_symbols(self):
        url = "https://finance.yahoo.com/markets/stock-market-today-1.html?guccounter=1"
        result = run(news=[news_item("ALFA", "Stock market today: Fed", url=url), news_item("NOVA", "Stock market today: Fed", url=url.split("?")[0] + "/")])
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["affected_symbols"], ["ALFA", "NOVA"])

    def test_the_same_fed_release_in_two_feeds_is_one_event_with_the_better_classification(self):
        link = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm"
        feeds = {"FED_PRESS": [{"title": "Federal Reserve issues FOMC statement", "link": link, "published_at": "2026-10-05T18:00:00+00:00", "summary": None}],
                 "FED_MONETARY": [{"title": "Federal Reserve issues FOMC statement", "link": link, "published_at": "2026-10-05T18:00:00+00:00", "summary": None}]}
        events = run(feeds=feeds)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["category"], events[0]["importance"], events[0]["source"]), ("MONETARY_POLICY", "HIGH", "FED_MONETARY"))

    def test_ids_are_stable_independent_of_fetch_time_and_order_and_distinct_for_different_items(self):
        a = news_item("ALFA", "One", url="https://x.com/a")
        b = news_item("ALFA", "Two", url="https://x.com/b")
        first = run(news=[a, b])
        second = mi.build_market_intelligence(evaluation_as_of=AS_OF, fetched_at="2030-01-01T00:00:00+00:00", feed_items={}, news_items=[b, a], sec_rows=[], portfolio=PORTFOLIO, watchlist=WATCHLIST, discovery=DISCOVERY, sector_map=SECTOR_MAP)
        self.assertEqual([e["event_id"] for e in first["events"]], [e["event_id"] for e in second["events"]])
        self.assertEqual(len({e["event_id"] for e in first["events"]}), 2)
        self.assertEqual(mi.event_id("NEWS", mi.canonical_url("https://X.com/a/?q=1#f")), mi.event_id("NEWS", mi.canonical_url("https://x.com/a")))


class ClassificationTests(unittest.TestCase):
    def test_official_feed_categories_and_importance(self):
        cases = [
            ("ECB_PRESS", "Monetary policy decisions", "https://x/press/pr/mp", "MONETARY_POLICY", "HIGH"),
            ("ECB_PRESS", "Account of the monetary policy meeting of 9-10 September", "https://x/press/pr/a", "MONETARY_POLICY", "MEDIUM"),
            ("ECB_PRESS", "Frank Elderson: Effective supervision", "https://x/press/key/a", "REGULATORY", "LOW"),
            ("ECB_PRESS", "Christine Lagarde: Interview with CNBC", "https://x/press/key/b", "MONETARY_POLICY", "LOW"),
            ("ECB_STATS", "Euro area bank interest rate statistics", "https://x/press/stats/a", "MACRO", "LOW"),
            ("FED_MONETARY", "Federal Reserve issues FOMC statement", "https://x/a", "MONETARY_POLICY", "HIGH"),
            ("FED_MONETARY", "Minutes of the Federal Open Market Committee", "https://x/b", "MONETARY_POLICY", "MEDIUM"),
            ("FED_PRESS", "Federal Reserve Board announces approval of application by Isabella Bank", "https://x/c", "REGULATORY", "LOW"),
            ("BLS_CPI", "CPI for all items increases 0.4% in August", "https://x/d", "INFLATION", "MEDIUM"),
            ("BLS_EMPLOYMENT", "Payroll employment increases by 162,000", "https://x/e", "LABOR_MARKET", "MEDIUM"),
            ("BEA", "GDP (Third Estimate), Corporate Profits", "https://x/f", "ECONOMIC_GROWTH", "MEDIUM"),
            ("BEA", "Personal Income and Outlays, August 2026", "https://x/g", "INFLATION", "MEDIUM"),
            ("BEA", "U.S. International Trade in Goods and Services", "https://x/h", "MACRO", "LOW"),
            ("DESTATIS", "Inflationsrate im September 2026 bei 2,1 %", "https://x/i", "INFLATION", "MEDIUM"),
            ("DESTATIS", "Produktion im August 2026: +2,0 % zum Vormonat", "https://x/j", "ECONOMIC_GROWTH", "MEDIUM"),
            ("DESTATIS", "Erwerbstätigkeit im August 2026", "https://x/k", "LABOR_MARKET", "MEDIUM"),
            ("DESTATIS", "Einnahmen aus Hundesteuer stagnieren", "https://x/l", "MACRO", "LOW"),
            ("EIA_PRESS", "EIA expects a mixed picture for winter fuel costs", "https://x/m", "ENERGY", "MEDIUM"),
            ("EIA_TODAY", "Crude oil prices and refinery margins increased", "https://x/n", "ENERGY", "LOW"),
        ]
        for source, title, link, category, importance in cases:
            with self.subTest(title=title):
                got = mi.classify_feed_item(source, title, link)
                self.assertEqual((got[0], got[1]), (category, importance))
                self.assertIn(got[0], mi.CATEGORIES)

    def test_scheduled_releases_are_medium_because_a_surprise_cannot_be_assessed(self):
        for source in ("BLS_CPI", "BLS_EMPLOYMENT"):
            event = run(feeds=feed(source, "Release", "https://x/" + source))["events"][0]
            self.assertEqual(event["importance"], "MEDIUM")
            self.assertIn("MACRO_RELEASE_SURPRISE_NOT_ASSESSED", event["reason_codes"])
            self.assertEqual(event["impact_basis"], "NOT_ASSESSABLE_WITHOUT_CONSENSUS")

    def test_sec_item_importance(self):
        expected = {"acquisition_disposition": ("M_AND_A", "HIGH"), "control_change": ("M_AND_A", "HIGH"), "delisting_notice": ("REGULATORY", "HIGH"),
                    "earnings": ("EARNINGS", "MEDIUM"), "material_agreement": ("COMPANY", "MEDIUM"), "officer_director_change": ("COMPANY", "LOW"),
                    "foreign_issuer_report": ("COMPANY", "LOW")}
        for slug, (category, importance) in expected.items():
            with self.subTest(slug=slug):
                row = {"symbol": "ALFA", "event_type": slug, "event_date": "2026-10-05", "title": slug, "notes": ""}
                event = mi.sec_events([row], fetched_at=FETCHED)[0]
                self.assertEqual((event["category"], event["importance"]), (category, importance))

    def test_headline_keywords_set_a_category_but_secondary_sources_are_capped_at_medium(self):
        cases = [("Intel issues profit warning for the fourth quarter", "GUIDANCE"), ("Acme to acquire Beta in a $5 billion deal", "M_AND_A"),
                 ("Regulators open antitrust probe into Acme", "REGULATORY"), ("Acme reports third quarter earnings", "EARNINGS"),
                 ("Analyst upgrades Acme, raises price target", "COMPANY"), ("Treasury yields surge as Fed signals rate hike", "MONETARY_POLICY"),
                 ("Cooler-than-expected inflation data", "INFLATION"), ("Weak jobs report", "LABOR_MARKET"), ("Recession fears grow", "ECONOMIC_GROWTH"),
                 ("New tariffs on chips", "GEOPOLITICS"), ("Oil prices jump after OPEC decision", "ENERGY"), ("Some unrelated lifestyle story", "COMPANY")]
        for title, category in cases:
            with self.subTest(title=title):
                event = classify_news(title)
                self.assertEqual(event["category"], category)
                self.assertIn(event["importance"], ("MEDIUM", "LOW"))
                self.assertEqual(event["confidence"], "LOW")
        capped = classify_news("Intel issues profit warning for the fourth quarter")
        self.assertIn("SECONDARY_SOURCE_CAPPED_AT_MEDIUM", capped["reason_codes"])

    def test_high_needs_a_company_wire_and_a_headline_that_names_the_company(self):
        wire = classify_news("Alpha Semi to acquire a stake in a new fab operator", publisher="Business Wire")
        self.assertEqual((wire["importance"], wire["confidence"]), ("HIGH", "MEDIUM"))
        self.assertIn("HEADLINE_NAMES_COMPANY", wire["reason_codes"])
        unlinked = classify_news("EMS Acquisition of Fencing Supplies completes", publisher="Business Wire")  # does not name Alpha Semi
        self.assertEqual(unlinked["importance"], "MEDIUM")
        self.assertIn("SYMBOL_LINK_NOT_CONFIRMED_BY_HEADLINE_CAPPED_AT_MEDIUM", unlinked["reason_codes"])
        self.assertEqual(classify_news("Golf Defense wins injunction in court", publisher="PR Newswire", symbol="GOLF.DE")["importance"], "HIGH")

    def test_a_bare_ticker_homonym_never_carries_high(self):
        event = run(news=[news_item("AXIO", "EMS Broadens Offering with Acquisition of AXIO Supply", publisher="Business Wire")])["events"][0]
        self.assertEqual((event["category"], event["importance"]), ("M_AND_A", "MEDIUM"))
        self.assertIn("TICKER_ONLY_MATCH_CAPPED_AT_MEDIUM", event["reason_codes"])
        named = run(news=[news_item("AXIO", "Axiom Micro Devices to acquire a software startup", publisher="Business Wire")])["events"][0]
        self.assertEqual(named["importance"], "HIGH")

    def test_stock_pick_articles_are_low(self):
        for title in ("3 Stocks to Buy Before Earnings", "Is Alpha Semiconductor the Best AI Stock to Buy Now?", "Which is a Better Stock to Buy?", "ABEL: Time to Buy, Hold or Sell?"):
            with self.subTest(title=title):
                event = classify_news(title)
                self.assertEqual((event["category"], event["importance"]), ("COMPANY", "LOW"))
                self.assertIn("STOCK_PICK_ARTICLE", event["reason_codes"])

    def test_commodities_is_not_implemented_and_every_category_is_declared(self):
        self.assertNotIn("COMMODITIES", mi.CATEGORIES)
        seen = {mi.classify_headline(t)[0] for t in ("a profit warning", "to acquire x", "fda approval", "earnings", "upgrades", "the fed", "inflation", "jobs report", "gdp", "tariffs", "oil prices", "story")}
        self.assertTrue(seen <= set(mi.CATEGORIES))


class ImpactTests(unittest.TestCase):
    def test_unknown_impact_by_default_and_never_read_from_a_headline(self):
        for title in ("Chip stock plunges after disastrous quarter", "Stock soars on blowout earnings beat", "Shares tumble as guidance is slashed", "Rally continues, record highs"):
            with self.subTest(title=title):
                event = classify_news(title)
                self.assertEqual((event["impact"], event["impact_basis"]), ("UNKNOWN", "NOT_READ_FROM_HEADLINE"))
        self.assertTrue(all(e["impact"] == "UNKNOWN" for e in run(feeds={**feed("BLS_CPI", "CPI rises", "https://x/1"), **feed("ECB_PRESS", "Monetary policy decisions", "https://x/press/pr/2")})["events"]))

    def test_only_the_structure_of_a_primary_filing_can_give_a_negative_direction(self):
        for slug in ("delisting_notice", "sec_8k_1.05", "sec_8k_4.02"):
            row = {"symbol": "ALFA", "event_type": slug, "event_date": "2026-10-05", "title": slug, "notes": ""}
            event = mi.sec_events([row], fetched_at=FETCHED)[0]
            self.assertEqual((event["impact"], event["impact_basis"], event["importance"]), ("NEGATIVE", "STRUCTURE_OF_PRIMARY_FILING", "HIGH"))
        positive_capable = {e["impact"] for e in run(sec=[{"symbol": "ALFA", "event_type": "earnings", "event_date": "2026-10-05", "title": "x", "notes": ""}])["events"]}
        self.assertEqual(positive_capable, {"UNKNOWN"})

    def test_no_event_in_a_full_run_gets_a_positive_mixed_or_neutral_impact(self):
        result = run(feeds=feed("BEA", "GDP", "https://x/g"), news=[news_item("ALFA", "Alpha Semi beats estimates")], sec=[{"symbol": "ALFA", "event_type": "earnings", "event_date": "2026-10-05", "title": "x", "notes": ""}])
        self.assertTrue(result["events"])
        self.assertTrue(all(e["impact"] in ("UNKNOWN", "NEGATIVE") for e in result["events"]))
        self.assertEqual(set(mi.IMPACTS), {"POSITIVE", "NEGATIVE", "MIXED", "NEUTRAL", "UNKNOWN"})


class MappingTests(unittest.TestCase):
    def test_portfolio_watchlist_and_discovery_mapping_by_symbol(self):
        result = run(news=[news_item("ALFA", "Alpha Semi output rises", url="https://x/1"), news_item("AXIO", "AXIO launches chip", url="https://x/2"), news_item("NOVA", "Nova Graphics news", url="https://x/3"), news_item("ZZZ", "Unknown co", url="https://x/4")])
        by = {e["affected_symbols"][0]: e for e in result["events"]}
        self.assertEqual((by["ALFA"]["affected_portfolio_symbols"], by["ALFA"]["affected_watchlist_symbols"], by["ALFA"]["affected_discovery_symbols"]), (["ALFA"], ["ALFA"], []))
        self.assertEqual((by["AXIO"]["affected_portfolio_symbols"], by["AXIO"]["affected_watchlist_symbols"]), ([], ["AXIO"]))
        self.assertEqual((by["NOVA"]["affected_discovery_symbols"], by["NOVA"]["affected_watchlist_symbols"], by["NOVA"]["affected_portfolio_symbols"]), (["NOVA"], [], []))
        self.assertEqual((by["ZZZ"]["affected_portfolio_symbols"], by["ZZZ"]["affected_watchlist_symbols"], by["ZZZ"]["affected_discovery_symbols"]), ([], [], []))
        self.assertEqual((result["portfolio_relevant_count"], result["watchlist_relevant_count"], result["discovery_relevant_count"]), (1, 2, 1))

    def test_a_company_event_stays_with_its_company_but_carries_its_sector(self):
        event = run(news=[news_item("ALFA", "Alpha Semi starts production", url="https://x/1")])["events"][0]
        self.assertEqual(event["affected_symbols"], ["ALFA"])
        self.assertEqual(event["affected_sectors"], ["SEMICONDUCTORS"])
        self.assertEqual(event["affected_discovery_symbols"], [])  # NOVA is in the same sector but is not reached

    def test_an_explicit_sector_event_reaches_the_symbols_of_that_sector(self):
        events = run(feeds=feed("EIA_PRESS", "EIA expects a mixed picture for winter fuel costs", "https://x/eia"))["events"]
        event = events[0]
        self.assertEqual(event["affected_sectors"], ["ENERGY"])
        self.assertEqual(event["affected_watchlist_symbols"], ["CORV"])
        self.assertEqual(event["affected_discovery_symbols"], ["XNRG"])
        self.assertEqual(event["affected_portfolio_symbols"], [])
        self.assertIn("SECTOR_SCOPED_EVENT", event["reason_codes"])
        chips = run(news=[news_item("ALFA", "Chipmakers rally as semiconductor demand grows", url="https://x/c")])["events"][0]
        self.assertEqual(chips["affected_discovery_symbols"], ["NOVA"])
        self.assertEqual(chips["affected_portfolio_symbols"], ["ALFA", "ECHO.AS"])

    def test_macro_events_carry_the_regions_of_the_held_symbols(self):
        ecb = run(feeds=feed("ECB_PRESS", "Monetary policy decisions", "https://x/press/pr/mp"))["events"][0]
        fed = run(feeds=feed("FED_MONETARY", "Federal Reserve issues FOMC statement", "https://x/fomc"))["events"][0]
        destatis = run(feeds=feed("DESTATIS", "Inflationsrate bei 2 %", "https://x/d"))["events"][0]
        self.assertEqual((ecb["affected_regions"], ecb["portfolio_region_exposure"]), (["EU"], ["EU"]))
        self.assertEqual((fed["affected_regions"], fed["portfolio_region_exposure"]), (["US"], ["US"]))
        self.assertEqual((destatis["affected_regions"], destatis["portfolio_region_exposure"]), (["DE"], ["DE"]))
        only_us = run(feeds=feed("ECB_PRESS", "Monetary policy decisions", "https://x/press/pr/mp"), portfolio={"ALFA": {"name": "Alpha Semi"}})["events"][0]
        self.assertEqual(only_us["portfolio_region_exposure"], [])
        self.assertEqual([mi.symbol_regions(s) for s in ("ALFA", "GOLF.DE", "ECHO.AS", "BA.L")], [("US",), ("DE", "EU"), ("NL", "EU"), ("GB",)])

    def test_window_filter_excludes_old_and_future_events(self):
        old = news_item("ALFA", "Old", url="https://x/old", published="2026-09-20T10:00:00+00:00")
        future = news_item("ALFA", "Future", url="https://x/f", published="2026-10-09T10:00:00+00:00")
        fresh = news_item("ALFA", "Fresh", url="https://x/fresh")
        result = run(news=[old, future, fresh])
        self.assertEqual([e["headline"] for e in result["events"]], ["Fresh"])
        self.assertEqual(len(run(news=[old], cfg=MarketIntelligenceConfig(lookback_days=30))["events"]), 1)

    def test_counts_add_up_and_ordering_is_importance_then_portfolio_then_recency(self):
        result = run(feeds=feed("ECB_PRESS", "Monetary policy decisions", "https://x/press/pr/mp"),
                     news=[news_item("ALFA", "Alpha Semi output rises", url="https://x/1", published="2026-10-06T10:00:00+00:00"), news_item("AXIO", "AXIO update", url="https://x/2", published="2026-10-06T12:00:00+00:00"),
                           news_item("ZZZ", "Unknown story", url="https://x/3", published="2026-10-06T14:00:00+00:00")])
        self.assertEqual(result["high_count"] + result["medium_count"] + result["low_count"], result["event_count"])
        self.assertEqual(sum(result["source_counts"].values()), result["event_count"])
        self.assertEqual([e["headline"] for e in result["events"]], ["Monetary policy decisions", "Alpha Semi output rises", "AXIO update", "Unknown story"])


class DeterminismAndSignalTests(unittest.TestCase):
    def test_same_input_gives_the_same_result_in_any_input_order(self):
        news = [news_item("ALFA", f"Story {i}", url=f"https://x/{i}", published=f"2026-10-0{1 + i % 6}T10:00:00+00:00") for i in range(12)]
        a = run(news=news, feeds=feed("BEA", "GDP", "https://x/g"))
        b = run(news=list(reversed(news)), feeds=feed("BEA", "GDP", "https://x/g"))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_inputs_are_not_modified(self):
        portfolio, watchlist, discovery = copy.deepcopy((PORTFOLIO, WATCHLIST, DISCOVERY))
        news = [news_item("ALFA", "x", url="https://x/1")]
        before = copy.deepcopy(news)
        run(news=news, portfolio=portfolio, watchlist=watchlist, discovery=discovery)
        self.assertEqual((portfolio, watchlist, discovery, news), (PORTFOLIO, WATCHLIST, DISCOVERY, before))

    def test_no_signal_order_or_buy_in_the_result(self):
        result = run(news=[news_item("ALFA", "Alpha Semi to acquire stake", publisher="Business Wire")], sec=[{"symbol": "ALFA", "event_type": "delisting_notice", "event_date": "2026-10-05", "title": "x", "notes": ""}])
        self.assertIs(result["signals_changed"], False)
        self.assertIs(result["orders_created"], False)
        text = json.dumps(result["events"])
        for forbidden in ('"BUY"', '"SELL"', '"TRIM"', "ENTRY_READY", '"PROMOTE"', "order_id"):
            self.assertNotIn(forbidden, text)

    def test_the_decision_modules_do_not_know_the_news_modules(self):
        for name in ("trading_orchestrator", "portfolio_action_planner", "swing_promotion", "decision_engine", "candidate_decision", "entry_sizing", "candidate_discovery", "analysis_engine", "swing_lifecycle", "strategy_suggestion"):
            source = (ROOT / "tools" / "trading" / f"{name}.py").read_text(encoding="utf-8")
            for word in ("market_intelligence", "intelligence_report"):
                self.assertNotIn(word, source, f"{name} must not depend on {word}")


def make_db(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO data_sources VALUES (1, 'Yahoo Finance');
        CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT NOT NULL, asset_type TEXT NOT NULL DEFAULT 'stock');
        CREATE TABLE source_symbols (id INTEGER PRIMARY KEY, security_id INTEGER, source_id INTEGER, symbol TEXT);
        CREATE TABLE positions (security_id INTEGER PRIMARY KEY, shares REAL);
        CREATE TABLE watchlist (security_id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE news (id INTEGER PRIMARY KEY, security_id INTEGER, published_at TEXT, title TEXT, url TEXT, publisher TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY, security_id INTEGER, event_type TEXT, event_date TEXT, title TEXT, notes TEXT);
        INSERT INTO security VALUES (1, 'ALFA', 'Alpha Semi (ADR)', 'stock'), (2, 'AXIO', 'Axiom Micro Devices', 'stock'), (3, 'GOLF', 'Golf Defense', 'stock');
        INSERT INTO source_symbols(security_id, source_id, symbol) VALUES (1, 1, 'ALFA'), (2, 1, 'AXIO'), (3, 1, 'GOLF.DE');
        INSERT INTO positions VALUES (1, 10), (3, 5);
        INSERT INTO watchlist VALUES (1, 'WATCH'), (2, 'WATCH'), (3, 'WATCH');
        INSERT INTO news(security_id, published_at, title, url, publisher) VALUES
            (1, '2026-10-06T10:00:00+00:00', 'Alpha Semi to acquire stake in fab', 'https://finance.yahoo.com/a/1.html', 'Business Wire'),
            (3, '2026-10-06T11:00:00+00:00', 'Golf Defense wins order', 'https://finance.yahoo.com/a/2.html', 'Zacks'),
            (2, '2026-08-01T11:00:00+00:00', 'Too old', 'https://finance.yahoo.com/a/3.html', 'Zacks');
        INSERT INTO events(security_id, event_type, event_date, title, notes) VALUES
            (1, 'earnings', '2026-10-05', '8-K: Results', 'accession=0001-26-1; items=2.02'),
            (2, 'delisting_notice', '2026-10-04', '8-K: Notice of Delisting', 'accession=0001-26-2; items=3.01');
        """
    )
    conn.commit()
    conn.close()


def db_fingerprint(path: Path):
    conn = sqlite3.connect(f"file:///{path.as_posix()}?mode=ro", uri=True)
    try:
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            digest = hashlib.sha256()
            for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY 1'):
                digest.update(repr(tuple(row)).encode())
            out[name] = digest.hexdigest()
        return out
    finally:
        conn.close()


class Workspace:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "trading.db"
        make_db(self.db)
        self.sector_map = self.dir / "sector_map.json"
        self.sector_map.write_text(json.dumps({"sectors": {k: [s for s, v in SECTOR_MAP.items() if v == k] for k in set(SECTOR_MAP.values())}}), encoding="utf-8")
        self.reports = self.dir / "market-intelligence"
        self.discovery_dir = self.dir / "candidate-discovery"

    def feeds(self, fail=()):
        data = {
            "ECB_PRESS": rss(("Monetary policy decisions", "https://www.ecb.europa.eu//press/pr/date/2026/html/ecb.mp261001~abc", "Tue, 06 Oct 2026 12:00:00 +0200", "")),
            "FED_MONETARY": rss(("Federal Reserve issues FOMC statement", "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261005a.htm", "Mon, 05 Oct 2026 18:00:00 GMT", "")),
            "BLS_CPI": rss(("CPI for all items increases 0.4% in September", "https://www.bls.gov/news.release/archives/cpi_10062026.htm", "Tue, 06 Oct 2026 07:51:00 -0400", "")),
        }
        urls = {spec["url"]: key for key, spec in mi.FEEDS.items()}

        def fetch(url):
            key = urls[url]
            if key in fail:
                raise OSError("down")
            return data.get(key, rss())

        return fetch

    def write_discovery(self, rows=(("NOVA", "DISCOVERY_READY"), ("XNRG", "DISCOVERY_READY"), ("COPR", "DISCOVERY_WATCH"), ("DOWN", "DISCOVERY_REJECTED"))):
        results = []
        for rank, (symbol, status) in enumerate(rows, 1):
            results.append({key: None for key in discovery_report.RESULT_FIELDS} | {"rank": rank, "symbol": symbol, "name": symbol + " Corp", "status": status, "reason_codes": ["X"], "fundamentals_evaluated": False})
        metadata_counts = {k: sum(1 for r in results if r["status"] == s) for s, k in discovery_report.COUNT_BY_STATUS.items()}
        report = {"schema_version": 1, "metadata": {"schema_version": 1, "generated_at": "2026-10-07T16:00:00+00:00", "evaluation_as_of": AS_OF, "universe_name": "t", "universe_size": len(results), "analyzed_count": len(results), "excluded_count": 0,
                                                   **metadata_counts, "source": {}, "deterministic": True, "results_sha256": discovery_report._digest(results, [])}, "results": results, "excluded": []}
        discovery_report.write_report(report, self.discovery_dir, history=False)

    def run_cli(self, *extra, fetch=None, fetch_news=None, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db-path", str(self.db), "--sector-map", str(self.sector_map), "--discovery-dir", str(self.discovery_dir), "--as-of", AS_OF,
                             "--output-dir", str(self.reports), *extra],
                            fetch=fetch or self.feeds(), fetch_news=fetch_news or (lambda symbol, name: [{"published_at": "2026-10-06T09:00:00+00:00", "title": f"{name} wins order", "url": f"https://finance.yahoo.com/n/{symbol}", "publisher": "Zacks"}]), now=now)
        return code, out.getvalue(), err.getvalue()

    def latest(self):
        return json.loads((self.reports / "latest.json").read_text(encoding="utf-8"))

    def close(self):
        self.tmp.cleanup()


class CollectionAndReportTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.ws.write_discovery()

    def tearDown(self):
        self.ws.close()

    def test_run_writes_latest_and_history_with_valid_schema(self):
        code, out, err = self.ws.run_cli()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sorted(p.name for p in self.ws.reports.iterdir()), ["latest.json", "market-intelligence-20261007-180000.json"])
        report = self.ws.latest()
        self.assertEqual(ir.validate_report(report), [])
        self.assertEqual(report, json.loads((self.ws.reports / "market-intelligence-20261007-180000.json").read_text(encoding="utf-8")))
        meta = report["metadata"]
        for key in ir.METADATA_KEYS:
            self.assertIn(key, meta)
        self.assertEqual((meta["schema_version"], meta["deterministic"], meta["read_only"], meta["orders_created"], meta["signals_changed"]), (1, True, True, False, False))
        self.assertIn("Report:", out)
        self.assertIn("soeben neu erzeugter Report", out)  # the collector prints a fresh run, not "read from the last report"
        self.assertNotIn("kein neuer Lauf", out)

    def test_end_to_end_content_and_mapping(self):
        self.ws.run_cli("--no-history")
        report = self.ws.latest()
        by_head = {e["headline"]: e for e in report["events"]}
        self.assertEqual(by_head["Monetary policy decisions"]["importance"], "HIGH")
        self.assertEqual(by_head["Monetary policy decisions"]["portfolio_region_exposure"], ["DE", "EU"][1:])
        alfa = by_head["Alpha Semi to acquire stake in fab"]
        self.assertEqual((alfa["importance"], alfa["affected_portfolio_symbols"]), ("HIGH", ["ALFA"]))
        self.assertEqual(by_head["8-K: Notice of Delisting"]["impact"], "NEGATIVE")
        self.assertNotIn("Too old", by_head)
        nova = by_head["NOVA Corp wins order"]
        self.assertEqual(nova["affected_discovery_symbols"], ["NOVA"])
        self.assertEqual(report["metadata"]["collection"]["discovery"]["symbols_ok"], 2)  # only READY candidates get headlines
        self.assertEqual(report["metadata"]["collection"]["feeds"]["BLS_CPI"], {"status": "OK", "items": 1})

    def test_the_cli_never_writes_to_the_database(self):
        before = db_fingerprint(self.ws.db)
        self.ws.run_cli()
        self.assertEqual(before, db_fingerprint(self.ws.db))

    def test_partial_feed_failure_is_recorded_and_the_run_still_succeeds(self):
        code, _, _ = self.ws.run_cli(fetch=self.ws.feeds(fail=("ECB_PRESS",)))
        self.assertEqual(code, 0)
        meta = self.ws.latest()["metadata"]
        self.assertEqual(meta["collection"]["feeds"]["ECB_PRESS"]["status"], "FAILED")
        self.assertIn("FEED_FAILED:ECB_PRESS", meta["warnings"])

    def test_failed_run_never_destroys_the_previous_report(self):
        self.ws.run_cli()
        good = (self.ws.reports / "latest.json").read_bytes()
        code, _, err = self.ws.run_cli(fetch=self.ws.feeds(fail=tuple(mi.FEEDS)), now=NOW + timedelta(days=1))
        self.assertEqual(code, 2)
        self.assertIn("previous report was left untouched", err)
        self.assertEqual((self.ws.reports / "latest.json").read_bytes(), good)
        self.assertEqual(len(list(self.ws.reports.glob("market-intelligence-*.json"))), 1)

    def test_error_during_the_atomic_replace_keeps_the_old_report_and_leaves_no_temp_file(self):
        self.ws.run_cli()
        good = (self.ws.reports / "latest.json").read_bytes()
        with mock.patch.object(discovery_report.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.ws.run_cli(now=NOW + timedelta(days=1))
        self.assertEqual((self.ws.reports / "latest.json").read_bytes(), good)
        self.assertEqual([p.name for p in self.ws.reports.iterdir() if p.name.endswith(".tmp")], [])

    def test_an_invalid_report_is_refused_before_replacing(self):
        self.ws.run_cli()
        good = (self.ws.reports / "latest.json").read_bytes()
        broken = json.loads(good)
        broken["metadata"]["high_count"] += 1
        with self.assertRaises(ir.ReportError):
            ir.write_report(broken, self.ws.reports)
        self.assertEqual((self.ws.reports / "latest.json").read_bytes(), good)

    def test_missing_discovery_report_is_only_a_warning(self):
        shutil_dir = self.ws.discovery_dir / "latest.json"
        shutil_dir.unlink()
        code, _, _ = self.ws.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("DISCOVERY_REPORT_UNAVAILABLE:NO_DISCOVERY_REPORT", self.ws.latest()["metadata"]["warnings"])

    def test_report_is_compact_and_has_no_article_text(self):
        self.ws.run_cli("--no-history")
        text = (self.ws.reports / "latest.json").read_text(encoding="utf-8")
        self.assertNotIn("\n  ", text)
        self.assertNotIn("full_text", text)


class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace()
        cls.ws.write_discovery()
        cls.ws.run_cli()
        cls.report = cls.ws.latest()

    @classmethod
    def tearDownClass(cls):
        cls.ws.close()

    def call(self, workspace=None, **kw):
        instance = Tools()
        instance.valves.database_path = str((workspace or self.ws).db)
        with mock.patch.object(ir, "datetime", wraps=datetime) as fake:
            fake.now.return_value = NOW
            return asyncio.run(instance.get_market_intelligence(**kw))

    # ---- slim compact answer
    SLIM_EVENT_KEYS = {"published_at", "importance", "category", "headline", "source", "impact"}
    SIDE_BLOCKS = ("rendered_de", "counts", "source_counts", "collection", "notes", "methodology", "sources")

    def test_reader_returns_slim_data_with_freshness(self):
        res = self.call()
        self.assertEqual((res["ok"], res["status"], res["report_stale"]), (True, "AVAILABLE", False))
        self.assertEqual(res["matching"], self.report["metadata"]["event_count"])
        self.assertEqual(res["importance"], {"HIGH": self.report["metadata"]["high_count"], "MEDIUM": self.report["metadata"]["medium_count"], "LOW": self.report["metadata"]["low_count"]})
        self.assertEqual(res["freshness"], {"status": "FRESH", "generated_at": "2026-10-07T18:00", "age_hours": 0.0, "evaluation_as_of": "2026-10-07"})
        self.assertNotIn("hint", res)

    def test_compact_has_no_side_blocks_and_no_redundant_rendered_text(self):
        res = self.call(symbol="ALFA")
        self.assertEqual(sorted(res), sorted(["ok", "operation", "status", "report_stale", "freshness", "filter", "matching", "importance", "returned", "truncated", "events"]))
        for block in self.SIDE_BLOCKS:
            self.assertNotIn(block, res)
        for event in res["events"]:
            self.assertTrue(self.SLIM_EVENT_KEYS <= set(event))
            for dropped in ("source_url", "summary", "event_id", "fetched_at", "affected_portfolio_symbols", "affected_watchlist_symbols", "affected_discovery_symbols", "affected_symbols", "affected_sectors"):
                self.assertNotIn(dropped, event)

    def test_symbol_query_returns_only_the_requested_symbols_events_with_the_same_classification(self):
        full = self.call(symbol="ALFA", detail=True)
        slim = self.call(symbol="ALFA")
        self.assertGreaterEqual(slim["matching"], 2)
        self.assertEqual(slim["filter"], {"symbol": "ALFA"})
        self.assertTrue(all("ALFA" in e["affected_symbols"] for e in full["events"]))
        self.assertEqual([(e["published_at"][:16], e["importance"], e["category"], e["headline"], e["source"], e["impact"]) for e in full["events"]],
                         [(e["published_at"], e["importance"], e["category"], e["headline"], e["source"], e["impact"]) for e in slim["events"]])
        self.assertEqual(slim["importance"], {level: sum(e["importance"] == level for e in full["events"]) for level in ir.IMPORTANCES})
        self.assertEqual((slim["matching"], slim["returned"], slim["truncated"]), (full["matching"], full["returned"], full["truncated"]))
        everything = self.call(limit=100)
        foreign = [e for e in everything["events"] if "ALFA" not in e.get("affected_symbols", []) and e["headline"] in {x["headline"] for x in slim["events"]}]
        self.assertEqual(foreign, [])
        self.assertTrue(all(e["link"] in ("DIRECT", "SHARED", "SECTOR", "LOOSE") for e in slim["events"]))

    def test_event_link_is_derived_from_the_stored_classification_only(self):
        base = {"source_type": "NEWS", "reason_codes": [], "affected_symbols": ["ALFA"]}
        self.assertEqual(ir.event_link({**base, "source_type": "SEC_FILING", "affected_symbols": ["ALFA", "AXIO"]}), "DIRECT")
        self.assertEqual(ir.event_link({**base, "reason_codes": ["HEADLINE_NAMES_COMPANY"]}), "DIRECT")
        self.assertEqual(ir.event_link({**base, "reason_codes": ["HEADLINE_NAMES_COMPANY"], "affected_symbols": ["BRAV", "MERC"]}), "SHARED")
        self.assertEqual(ir.event_link({**base, "reason_codes": ["SECTOR_SCOPED_EVENT", "HEADLINE_NAMES_COMPANY"]}), "SECTOR")
        self.assertEqual(ir.event_link(base), "LOOSE")

    def test_slim_events_without_a_symbol_filter_keep_the_context_needed_to_read_them(self):
        rows = self.call(scope="MACRO", limit=100)["events"]
        self.assertTrue(rows)
        self.assertTrue(all("link" not in r for r in rows))
        with_regions = [r for r in rows if "affected_regions" in r]
        self.assertTrue(with_regions and all(isinstance(r["affected_regions"], list) and r["affected_regions"] for r in with_regions))
        portfolio = self.call(scope="PORTFOLIO", limit=100)["events"]
        self.assertTrue(all(any(s in ("ALFA", "GOLF.DE") for s in e["affected_symbols"]) for e in portfolio))

    def test_slim_reason_codes_drop_only_the_generic_marker(self):
        event = {"source_type": "NEWS", "affected_symbols": ["ALFA"], "affected_sectors": [], "affected_regions": [], "portfolio_region_exposure": [], "published_at": "2026-10-06T10:00:00+00:00",
                 "importance": "HIGH", "category": "M_AND_A", "headline": "H" * 300, "source": "YAHOO_NEWS", "impact": "UNKNOWN",
                 "reason_codes": ["M_AND_A_KEYWORD", "CLASSIFIED_BY_HEADLINE_KEYWORD", "COMPANY_PRESS_RELEASE", "PUBLISHER:Business Wire", "HEADLINE_NAMES_COMPANY"]}
        compact = ir._slim_event(event, False)
        self.assertEqual(compact["reason_codes"], ["M_AND_A_KEYWORD", "COMPANY_PRESS_RELEASE", "PUBLISHER:Business Wire"])
        self.assertEqual(len(compact["headline"]), 300)  # headlines are never shortened
        symbol_query = ir._slim_event(event, True)
        self.assertEqual((symbol_query["link"], symbol_query["reason_codes"]), ("DIRECT", ["M_AND_A_KEYWORD", "COMPANY_PRESS_RELEASE", "PUBLISHER:Business Wire"]))

    def test_slim_is_much_smaller_than_the_broad_compact_answer(self):
        broad = json.dumps(ir.query_report(self.report, now=NOW, symbol="ALFA", slim=False), ensure_ascii=False)
        slim = json.dumps(ir.query_report(self.report, now=NOW, symbol="ALFA"), ensure_ascii=False)
        self.assertLess(len(slim) * 2, len(broad))

    def test_detail_stays_complete(self):
        detail = self.call(symbol="ALFA", detail=True, limit=3)
        for block in ("rendered_de", "counts", "source_counts", "collection", "notes", "methodology", "freshness", "filter"):
            self.assertIn(block, detail)
        self.assertEqual(sorted(detail["events"][0]), sorted(ir.EVENT_FIELDS))
        self.assertEqual(detail["freshness"]["max_age_hours"], 24)
        self.assertIn("kein neuer Lauf", detail["rendered_de"])

    def test_the_broad_compact_answer_used_by_the_collector_is_unchanged(self):
        broad = ir.query_report(self.report, now=NOW, slim=False)
        for block in ("rendered_de", "counts", "source_counts", "collection", "notes", "freshness", "filter"):
            self.assertIn(block, broad)
        self.assertTrue(set(ir.COMPACT_FIELDS) <= set(broad["events"][0]))
        self.assertNotIn("methodology", broad)

    def test_no_report_is_unavailable_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Tools()
            instance.valves.database_path = str(Path(tmp) / "trading.db")
            res = asyncio.run(instance.get_market_intelligence())
        self.assertEqual((res["ok"], res["status"], res["reason"]), (True, "UNAVAILABLE", "NO_MARKET_INTELLIGENCE_REPORT"))
        self.assertIn("never starts", res["hint"])

    def test_corrupt_or_tampered_report_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "market-intelligence"
            directory.mkdir()
            instance = Tools()
            instance.valves.database_path = str(Path(tmp) / "trading.db")
            (directory / "latest.json").write_text("{nope", encoding="utf-8")
            self.assertEqual(asyncio.run(instance.get_market_intelligence())["reason"], "REPORT_INVALID")
            tampered = copy.deepcopy(self.report)
            tampered["events"][0]["importance"] = "LOW" if tampered["events"][0]["importance"] != "LOW" else "HIGH"
            (directory / "latest.json").write_text(json.dumps(tampered), encoding="utf-8")
            res = asyncio.run(instance.get_market_intelligence())
            self.assertEqual((res["status"], res["reason"]), ("UNAVAILABLE", "REPORT_INVALID"))

    def test_stale_report_is_flagged_but_still_returned(self):
        old = copy.deepcopy(self.report)
        old["metadata"]["generated_at"] = "2026-10-05T06:00:00+00:00"
        res = ir.query_report(old, now=NOW)
        self.assertTrue(res["report_stale"])
        self.assertEqual(res["freshness"]["status"], "REPORT_STALE")
        self.assertGreaterEqual(res["freshness"]["age_hours"], 60)
        self.assertTrue(res["hint"].startswith("REPORT_STALE"))
        self.assertTrue(res["events"])
        self.assertEqual(ir.query_report(old, now=NOW, max_age_hours=100)["freshness"]["status"], "FRESH")
        detail = ir.query_report(old, now=NOW, detail=True)
        self.assertEqual(detail["freshness"]["max_age_hours"], 24)
        self.assertTrue(detail["notes"][0].startswith("REPORT_STALE"))

    def test_filter_by_scope(self):
        portfolio = self.call(scope="PORTFOLIO")
        self.assertTrue(portfolio["events"] and all(any(s in ("ALFA", "GOLF.DE") for s in e["affected_symbols"]) for e in portfolio["events"]))
        discovery = self.call(scope="DISCOVERY")
        self.assertTrue(discovery["events"] and all(set(e["affected_symbols"]) & {"NOVA", "XNRG", "COPR"} for e in discovery["events"]))
        macro = self.call(scope="macro")
        self.assertTrue(macro["events"] and all(e["category"] in mi.MACRO_CATEGORIES for e in macro["events"]))
        self.assertEqual(self.call(scope="WATCHLIST")["filter"], {"scope": "WATCHLIST"})
        bad = self.call(scope="EVERYTHING")
        self.assertEqual((bad["ok"], bad["error"]), (False, "INVALID_SCOPE"))

    def test_filter_by_category_importance_symbol_and_sector(self):
        self.assertTrue(all(e["category"] == "INFLATION" for e in self.call(category="inflation")["events"]))
        self.assertEqual(self.call(category="NOPE")["error"], "INVALID_CATEGORY")
        high = self.call(importance="high")
        self.assertTrue(high["events"] and all(e["importance"] == "HIGH" for e in high["events"]))
        self.assertEqual(self.call(importance="URGENT")["error"], "INVALID_IMPORTANCE")
        alfa = self.call(symbol="alfa", detail=True)
        self.assertTrue(alfa["events"] and all("ALFA" in e["affected_symbols"] for e in alfa["events"]))
        self.assertTrue(all("GOLF.DE" in e["affected_symbols"] for e in self.call(symbol="GOLF", detail=True)["events"]))  # base symbol matches the listing
        self.assertEqual(self.call(symbol="NOPE")["returned"], 0)
        self.assertTrue(all("SEMICONDUCTORS" in e["affected_sectors"] for e in self.call(sector="semiconductors")["events"]))

    def test_limit_truncation_and_caps(self):
        res = self.call(limit=2)
        self.assertEqual((res["returned"], res["truncated"]), (2, res["matching"] > 2))
        if res["truncated"]:
            self.assertIn(f"{res['matching'] - 2} more events match", res["hint"])
        self.assertEqual(self.call(limit=0)["returned"], 1)
        self.assertEqual(self.call(limit=10_000, detail=True)["filter"]["limit_applied"], ir.MAX_LIMIT_DETAIL)
        self.assertEqual(self.call(limit=10_000, detail=True)["filter"]["limit_requested"], 10_000)
        big = copy.deepcopy(self.report)
        big["events"] = [{**big["events"][0], "event_id": f"{i:016x}", "headline": f"Headline {i}"} for i in range(120)]
        self.assertEqual(ir.query_report(big, now=NOW, limit=10_000)["returned"], ir.MAX_LIMIT_COMPACT)

    def test_every_response_stays_below_the_mcp_result_limit(self):
        big = copy.deepcopy(self.report)
        template = big["events"][0]
        big["events"] = [{**template, "event_id": f"{i:016x}", "headline": "H" * 180, "summary": "S" * 200, "reason_codes": ["R_" + "X" * 30] * 4} for i in range(300)]
        meta = big["metadata"]
        meta.update(event_count=300, high_count=sum(e["importance"] == "HIGH" for e in big["events"]), medium_count=sum(e["importance"] == "MEDIUM" for e in big["events"]), low_count=sum(e["importance"] == "LOW" for e in big["events"]))
        for kw in ({"limit": 10_000}, {"limit": 10_000, "slim": False}, {"limit": 10_000, "detail": True}, {"limit": 25, "detail": True}):
            res = ir.query_report(big, now=NOW, **kw)
            self.assertLess(len(json.dumps(res, ensure_ascii=False)), 40_000)
            self.assertTrue(res["truncated"])
            self.assertEqual(res["returned"], len(res["events"]))
        self.assertIn("RESPONSE_TRIMMED_TO_SIZE_BUDGET", ir.query_report(big, now=NOW, limit=50, detail=True)["notes"][-1])
        small = ir.query_report(self.report, now=NOW, limit=5)
        self.assertNotIn("RESPONSE_TRIMMED", json.dumps(small))

    def test_the_slim_size_guard_drops_the_last_events_and_says_so(self):
        big = copy.deepcopy(self.report)
        big["events"] = [{**big["events"][0], "event_id": f"{i:016x}", "headline": "H" * 180} for i in range(60)]
        with mock.patch.object(ir, "RESPONSE_CHAR_BUDGET", 4_000):
            res = ir.query_report(big, now=NOW, limit=10_000)
        self.assertLessEqual(len(json.dumps(res, ensure_ascii=False)), 4_000)
        self.assertTrue(res["truncated"] and 1 <= res["returned"] < res["matching"])
        self.assertIn("RESPONSE_TRIMMED_TO_SIZE_BUDGET", res["hint"])

    def test_compact_and_detail_fields(self):
        compact = self.call(limit=3)["events"][0]
        self.assertTrue(self.SLIM_EVENT_KEYS <= set(compact))
        self.assertNotIn("source_url", compact)
        detail = self.call(limit=3, detail=True)
        self.assertEqual(sorted(detail["events"][0]), sorted(ir.EVENT_FIELDS))
        self.assertIn("methodology", detail)
        self.assertNotIn("methodology", self.call(limit=3))

    def test_compact_lists_are_trimmed_for_broad_events(self):
        many = copy.deepcopy(self.report)
        many["events"][0]["affected_symbols"] = [f"S{i}" for i in range(30)]
        many["metadata"]["events_sha256"] = ir._digest(many["events"])
        row = ir.query_report(many, now=NOW)["events"][0]
        self.assertEqual((len(row["affected_symbols"]), row["affected_symbols_total"]), (ir.SLIM_SYMBOLS, 30))
        broad = ir.query_report(many, now=NOW, slim=False)["events"][0]
        self.assertEqual((len(broad["affected_symbols"]), broad["affected_symbols_total"]), (ir.MAX_SYMBOLS_COMPACT, 30))

    def test_sorting_is_deterministic_and_matches_the_stored_order(self):
        a, b = self.call(limit=100), self.call(limit=100)
        self.assertEqual(json.dumps(a["events"]), json.dumps(b["events"]))
        self.assertEqual([e["headline"] for e in a["events"]], [e["headline"] for e in self.report["events"]])

    def test_rendered_german_output_exists_in_detail_only(self):
        self.assertNotIn("rendered_de", self.call(limit=5))
        text = self.call(limit=5, detail=True)["rendered_de"]
        for fragment in ("| Zeit (UTC) | Wichtigkeit | Kategorie | Schlagzeile | Quelle | Impact | Betrifft |", "kein neuer Lauf", "kein Signal, kein Kauf und kein Verkauf"):
            self.assertIn(fragment, text)

    def test_reader_makes_no_external_request_and_never_touches_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "market-intelligence"
            directory.mkdir()
            (directory / "latest.json").write_text(json.dumps(self.report), encoding="utf-8")
            instance = Tools()
            instance.valves.database_path = str(Path(tmp) / "does-not-exist.db")
            with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network access")), \
                 mock.patch("urllib.request.urlopen", side_effect=AssertionError("urlopen")), \
                 mock.patch.object(Tools, "_readonly_import_connection", side_effect=AssertionError("db access")), \
                 mock.patch.object(Tools, "_connect", side_effect=AssertionError("db access")), \
                 mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db access")):
                res = instance._get_market_intelligence_sync("ALL", None, "HIGH", None, None, 5, True)
                slim = instance._get_market_intelligence_sync("ALL", None, None, "ALFA", None, 5, False)
            self.assertEqual(res["status"], "AVAILABLE")
            self.assertEqual(slim["status"], "AVAILABLE")
            self.assertEqual(asyncio.run(instance.get_market_intelligence(importance="HIGH"))["status"], "AVAILABLE")

    def test_reader_changes_neither_the_report_nor_the_database(self):
        before_report = (self.ws.reports / "latest.json").read_bytes()
        before_db = db_fingerprint(self.ws.db)
        self.call(scope="ALL", limit=100)
        self.call(symbol="ALFA", detail=True)
        self.assertEqual((self.ws.reports / "latest.json").read_bytes(), before_report)
        self.assertEqual(before_db, db_fingerprint(self.ws.db))
        self.assertEqual([p.name for p in self.ws.reports.iterdir() if p.name.endswith(".tmp")], [])

    def test_reader_module_has_no_network_database_or_engine_imports(self):
        source = Path(ir.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib", "http.client", "requests", "sqlite3", "candidate_discovery", "analysis_engine"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)

    def test_no_buy_sell_or_order_in_the_reader_output(self):
        for kw in ({"limit": 100, "detail": True}, {"limit": 100}, {"symbol": "ALFA"}):
            text = json.dumps(self.call(**kw)["events"])
            for forbidden in ('"BUY"', '"SELL"', '"TRIM"', "ENTRY_READY", '"PROMOTE"', "order_id"):
                self.assertNotIn(forbidden, text)


class ToolSurfaceTests(unittest.TestCase):
    ORIGINAL = sorted(["sql_execute", "database_tables", "database_schema", "table_info", "database_status", "rank_watchlist", "run_trading_orchestrator",
                       "evaluate_swing_candidate", "evaluate_swing_candidates", "approve_swing_promotion", "suggest_strategy_assignments", "evaluate_watchlist_candidates"])

    def public(self):
        return sorted(n for n, m in inspect.getmembers(Tools()) if not n.startswith("_") and callable(m) and (inspect.ismethod(m) or inspect.isfunction(m)))

    def test_fifteen_tools_and_the_thirteen_earlier_ones_are_unchanged(self):
        names = self.public()
        self.assertEqual(len(names), 15)
        self.assertEqual(sorted(set(names) - {"get_market_intelligence", "get_opportunity_view"}), sorted([*self.ORIGINAL, "get_candidate_discovery"]))
        self.assertIn("get_market_intelligence", names)

    def test_the_new_tool_has_only_read_filters(self):
        params = list(inspect.signature(Tools().get_market_intelligence).parameters)
        self.assertEqual(params, ["scope", "category", "importance", "symbol", "sector", "limit", "detail"])
        self.assertEqual(list(inspect.signature(Tools().get_candidate_discovery).parameters), ["status", "limit", "detail", "symbol"])
        self.assertEqual(list(inspect.signature(Tools().run_trading_orchestrator).parameters), ["as_of"])

    def test_valve_derives_the_report_directory_from_the_database_path(self):
        t = Tools()
        base = Path(tempfile.gettempdir()) / "trading-layout" / "data"  # path arithmetic only, nothing is created
        t.valves.database_path = str(base / "trading.db")
        self.assertEqual(t._market_intelligence_report_directory(), base / "market-intelligence")
        elsewhere = Path(tempfile.gettempdir()) / "elsewhere"
        t.valves.market_intelligence_report_dir = str(elsewhere)
        self.assertEqual(t._market_intelligence_report_directory(), elsewhere)


class SectorMapAndConfigTests(unittest.TestCase):
    def test_shipped_sector_map_is_valid_and_covers_the_universe(self):
        mapping = mi.load_sector_map(json.loads((ROOT / "tools" / "trading" / "universe" / "sector_map_v1.json").read_text(encoding="utf-8")))
        universe = json.loads((ROOT / "tools" / "trading" / "universe" / "swing_large_cap_v1.json").read_text(encoding="utf-8"))["entries"]
        self.assertEqual([e["symbol"] for e in universe if e["symbol"] not in mapping], [])
        self.assertEqual(mapping["NVDA"], "SEMICONDUCTORS")

    def test_optional_local_sector_map_is_merged_and_never_required(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "sector_map_v1.json"
            base.write_text(json.dumps({"sectors": {"ENERGY": ["AAA"], "SOFTWARE_AI": ["BBB"]}}), encoding="utf-8")
            self.assertEqual(cli.read_sector_map(base), {"AAA": "ENERGY", "BBB": "SOFTWARE_AI"})
            (Path(directory) / "sector_map_local.json").write_text(json.dumps({"sectors": {"ENERGY": ["CCC"], "FUNDS_ETF": ["DDD"]}}), encoding="utf-8")
            self.assertEqual(cli.read_sector_map(base), {"AAA": "ENERGY", "BBB": "SOFTWARE_AI", "CCC": "ENERGY", "DDD": "FUNDS_ETF"})
            (Path(directory) / "sector_map_local.json").write_text(json.dumps({"sectors": {"FUNDS_ETF": ["AAA"]}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                cli.read_sector_map(base)

    def test_a_symbol_in_two_sectors_is_rejected(self):
        with self.assertRaises(ValueError):
            mi.load_sector_map({"sectors": {"A": ["X"], "B": ["X"]}})
        with self.assertRaises(ValueError):
            mi.load_sector_map({"sectors": {}})

    def test_config_defaults(self):
        cfg = MarketIntelligenceConfig()
        self.assertEqual((cfg.lookback_days, cfg.report_max_age_hours), (7, 24))
        self.assertEqual(set(mi.FEEDS), {"ECB_PRESS", "ECB_STATS", "FED_MONETARY", "FED_PRESS", "BLS_CPI", "BLS_EMPLOYMENT", "BEA", "DESTATIS", "EIA_TODAY", "EIA_PRESS"})


if __name__ == "__main__":
    unittest.main()
