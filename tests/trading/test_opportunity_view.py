"""Unified opportunity view: join of engine candidates, discovery report and market-intelligence report (context only)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import inspect
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discovery_report as dr  # noqa: E402
import intelligence_report as ir  # noqa: E402
import opportunity_view as ov  # noqa: E402
from market_intelligence import MACRO_CATEGORIES  # noqa: E402

NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)
FETCHED = NOW.isoformat()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tools_module = load_module("trading_sqlite_opp_test", ROOT / "mcp-tools" / "trading_sqlite.py")
Tools = tools_module.Tools

# ---------------------------------------------------------------- synthetic engine result (shape of the orchestrator primitive)
SYMBOL_MAP = {2: "ALFA", 5: "BRAV", 10: "GOLF.DE", 18: "AXIO", 63: "BRNT", 30: "CYRN", 40: "DUNM", 77: "EPSI"}


def promotion(security_id, symbol, name, recommendation="PROMOTE", momentum=50.0, price=100.0, priority=10):
    return {"security_id": security_id, "symbol": symbol, "name": name, "recommendation": recommendation, "momentum_score": momentum, "current_price_eur": price,
            "technical_quality": "available", "fundamental_quality": "partial", "event_risk_quality": "available", "valuation_quality": "unavailable", "watchlist_priority": priority,
            "plan_token": "secret-token"}


def candidate(security_id, symbol, name, status, rank, score, momentum=50.0, price=100.0, priority=10, proposed=(30000.0, 50.0)):
    return {"security_id": security_id, "symbol": symbol, "name": name, "entry_status": status, "rank": rank, "entry_score": score, "momentum_score": momentum, "confidence": 0.6,
            "price_eur": price, "quality": {"technical": "available", "fundamental": "partial", "event_risk": "available", "valuation": "unavailable"}, "reason_codes": [status],
            "watchlist_priority": priority, "sizing": {"proposed_capital_eur": proposed[0], "proposed_quantity": proposed[1], "price_eur": price} if proposed else None}


def make_engine():
    return {
        "evaluation_as_of": "2026-10-07", "global_status": "AVAILABLE", "global_issues": [{"code": "ALLOCATION_OUTSIDE_TARGET", "details": ["SWING_ALLOCATION_BELOW_MIN"], "severity": "NON_BLOCKING_WARNING"}],
        "promotion_results": [
            promotion(18, "AXIO", "Axiom Micro Devices", momentum=62.5, price=598.0), promotion(63, "BRNT", "Brent Networks Inc.", momentum=58.0, price=120.0),
            promotion(30, "CYRN", "Cyrene Wireless", momentum=20.0, price=150.0), promotion(40, "DUNM", "Dunmore Capital", momentum=40.0, price=700.0),
            promotion(77, "EPSI", "Epsilon Technology Incorporated", recommendation="KEEP_WATCHING", momentum=-21.0, price=45.0),
        ],
        "existing_position_results": [
            {"security_id": 2, "symbol": "ALFA", "name": "Alpha Semiconductor Manufacturing", "strategy": "swing", "priority": "P2", "decision": {"action": "TRIM"}},
            {"security_id": 10, "symbol": "GOLF", "name": "Golf Defense AG", "strategy": "swing", "priority": "P1", "decision": {"action": "SELL"}},
            {"security_id": 5, "symbol": "BRAV", "name": "Bravo Therapeutics", "strategy": "swing", "priority": "P2", "decision": {"action": "HOLD"}},
        ],
        "portfolio_action_plan": {
            "status": "AVAILABLE",
            "entry_candidates": [
                candidate(18, "AXIO", "Axiom Micro Devices", "ENTRY_READY", 1, 41.5, 62.5, 598.0), candidate(63, "BRNT", "Brent Networks Inc.", "ENTRY_READY", 2, 35.25, 58.0, 120.0, proposed=(12345.678901234, 65.0)),
                candidate(30, "CYRN", "Cyrene Wireless", "WAIT_FOR_TRIGGER", None, 12.0, 20.0, 150.0, proposed=None), candidate(40, "DUNM", "Dunmore Capital", "BLOCKED_BY_ALLOCATION", None, 25.0, 40.0, 700.0),
            ],
            "planned_entries": [{"security_id": 18, "symbol": "AXIO", "rank": 1, "capital_eur": 29900.0, "quantity": 50.0, "entry_score": 41.5, "reason": "ENTRY_READY"}],
            "deferred_entries": [{"security_id": 63, "symbol": "BRNT", "rank": 2, "entry_score": 35.25, "reason": "STOPPED_CAPITAL_EXHAUSTED"}],
            "entry_summary": {"ENTRY_READY": 2, "WAIT_FOR_TRIGGER": 1, "BLOCKED_BY_ALLOCATION": 1},
            "current_state": {"cash_eur": 5000.0, "deployable_cash_eur": 0.0, "minimum_cash_reserve_eur": 10000.0, "swing_pct": 20.0, "swing_target_pct": [30.0, 40.0], "long_term_pct": 80.0, "long_term_target_pct": [60.0, 70.0],
                              "swing_value_eur": 40000.0, "total_market_value_eur": 200000.0, "remaining_swing_capacity_eur": 36000.0, "allocation_guardrails": ["SWING_ALLOCATION_BELOW_MIN"]},
            "post_action_state": {"cash_after_actions_eur": 40000.0, "deployable_cash_eur": 30000.0, "expected_proceeds_gross_eur": 38000.0, "expected_proceeds_net_conservative_eur": 35000.0,
                                  "proceeds_basis": "net_conservative", "taxes_modelled": True, "fees_modelled": False, "swing_pct": 12.0, "remaining_swing_capacity_eur": 60000.0},
            "final_simulated_state": {"cash_eur": 10100.0, "deployable_cash_eur": 100.0, "planned_entries_total_eur": 29900.0, "remaining_swing_capacity_eur": 30000.0, "swing_pct": 19.9,
                                      "cash_above_reserve": True, "swing_within_max": True},
        },
    }


# ---------------------------------------------------------------- discovery report
def disc_result(rank, symbol, name, status="DISCOVERY_READY", score=26.0, price=200.0):
    return {key: None for key in dr.RESULT_FIELDS} | {"rank": rank, "symbol": symbol, "name": name, "status": status, "discovery_score": score, "momentum_score": 50.0, "confidence": 0.6,
                                                      "current_price_eur": price, "technical_quality": "available", "fundamental_quality": None, "event_risk_quality": None,
                                                      "reason_codes": ["TREND_INTACT", "MOMENTUM_ABOVE_THRESHOLD"], "fundamentals_evaluated": False}


def make_discovery(generated_at=NOW, evaluation_as_of="2026-10-07", extra=()):
    results = [disc_result(1, "NOVA", "Nova Graphics", price=130.0), disc_result(2, "ABEL", "Abelon", price=160.0), disc_result(3, "BRNT", "Brent Networks", price=120.0),
               disc_result(4, "XNRG", "Xenon Energy", price=90.0), disc_result(5, "CHEAP", "Cheap Corp", price=10.0), disc_result(6, "EPSI", "Epsilon Technology", price=45.0),
               disc_result(7, "PLAT", "Plateau Networks", status="DISCOVERY_WATCH", price=240.0), *extra]
    counts = {key: sum(r["status"] == status for r in results) for status, key in dr.COUNT_BY_STATUS.items()}
    return {"schema_version": 1, "metadata": {"schema_version": 1, "generated_at": generated_at.isoformat(), "evaluation_as_of": evaluation_as_of, "universe_name": "t", "universe_size": len(results), "analyzed_count": len(results),
                                              "excluded_count": 0, **counts, "source": {}, "deterministic": True, "results_sha256": dr._digest(results, [])}, "results": results, "excluded": []}


# ---------------------------------------------------------------- market-intelligence report
def event(i, headline, symbols, importance="MEDIUM", category="COMPANY", source_type="NEWS", source="YAHOO_NEWS", impact="UNKNOWN", reasons=(), published="2026-10-06T10:00:00+00:00", regions=()):
    return {"event_id": f"{i:016x}", "published_at": published, "event_date": published[:10], "source": source, "source_type": source_type, "source_url": f"https://x/{i}", "category": category,
            "headline": headline, "summary": None, "affected_symbols": list(symbols), "affected_portfolio_symbols": [], "affected_watchlist_symbols": [], "affected_discovery_symbols": [],
            "affected_sectors": [], "affected_regions": list(regions), "portfolio_region_exposure": list(regions), "importance": importance, "impact": impact, "impact_basis": "NOT_READ_FROM_HEADLINE",
            "confidence": "MEDIUM", "reason_codes": list(reasons), "fetched_at": FETCHED}


def standard_events():
    return [
        event(1, "Abelon receives FDA approval for new drug", ["ABEL"], "HIGH", "REGULATORY", reasons=["HEADLINE_NAMES_COMPANY"]),
        event(2, "Axiom Micro Devices issues profit warning", ["AXIO"], "MEDIUM", "GUIDANCE", reasons=["GUIDANCE_KEYWORD"]),
        event(3, "3 Stocks to Buy Before Earnings", ["BRNT"], "LOW", "COMPANY", reasons=["STOCK_PICK_ARTICLE"]),
        event(4, "4 Top Chip Stocks With Dependable Earnings to Ride the AI Boom", ["NOVA", "ALFA"], "MEDIUM", "EARNINGS"),
        event(5, "Corvex to divest midstream assets", ["XNRG"], "HIGH", "M_AND_A", reasons=["SECTOR_SCOPED_EVENT"]),
        event(6, "8-K: Notice of Delisting", ["ALFA"], "HIGH", "REGULATORY", "SEC_FILING", "SEC_FILINGS", "NEGATIVE", ["SEC_DELISTING"]),
        event(7, "Federal Reserve issues FOMC statement", [], "HIGH", "MONETARY_POLICY", "OFFICIAL_FEED", "FED_MONETARY", regions=["US"]),
        event(8, "CPI for all items increases 0.4%", [], "MEDIUM", "INFLATION", "OFFICIAL_FEED", "BLS_CPI", regions=["US"]),
        event(9, "New tariffs on chips announced", ["NOVA"], "MEDIUM", "GEOPOLITICS"),
        event(10, "EIA expects higher winter fuel costs", [], "MEDIUM", "ENERGY", "OFFICIAL_FEED", "EIA_PRESS"),
        event(11, "Bravo wins injunction against Mercer", ["BRAV"], "MEDIUM", "REGULATORY"),
        event(12, "Golf Defense unrelated lifestyle story", ["GOLF.DE"], "LOW", "COMPANY"),
    ]


def make_mi(events=None, generated_at=NOW, evaluation_as_of="2026-10-07"):
    events = list(standard_events() if events is None else events)
    result = {"evaluation_as_of": evaluation_as_of, "lookback_days": 7, "lookback_since": "2026-09-30", "source_counts": {"YAHOO_NEWS": len(events)}, "events": events,
              "high_count": sum(e["importance"] == "HIGH" for e in events), "medium_count": sum(e["importance"] == "MEDIUM" for e in events), "low_count": sum(e["importance"] == "LOW" for e in events),
              "portfolio_relevant_count": 0, "watchlist_relevant_count": 0, "discovery_relevant_count": 0, "macro_event_count": sum(e["category"] in MACRO_CATEGORIES for e in events), "methodology": {}, "warnings": []}
    return ir.build_report(result, generated_at=generated_at, collection={})


def build(engine="default", discovery="default", mi="default", **kw):
    engine = make_engine() if engine == "default" else engine
    discovery = make_discovery() if discovery == "default" else discovery
    mi = make_mi() if mi == "default" else mi
    return ov.build_opportunity_view(engine=engine, symbol_map=SYMBOL_MAP, discovery=discovery, discovery_reason=None if discovery else "NO_DISCOVERY_REPORT",
                                     mi=mi, mi_reason=None if mi else "NO_MARKET_INTELLIGENCE_REPORT", now=kw.pop("now", NOW), **kw)


def rows(view):
    return {r["symbol"]: r for r in view["opportunities"]}


# ---------------------------------------------------------------- tests
class AvailabilityTests(unittest.TestCase):
    def test_discovery_and_market_intelligence_present(self):
        view = build()
        self.assertEqual(view["metadata"]["status"], "AVAILABLE")
        self.assertEqual((view["metadata"]["sources"]["discovery"]["status"], view["metadata"]["sources"]["market_intelligence"]["status"]), ("AVAILABLE", "AVAILABLE"))
        self.assertEqual(view["warnings"][0]["code"], "ENGINE_ISSUE:ALLOCATION_OUTSIDE_TARGET")
        self.assertEqual(sorted(view), ["discovery_candidates", "market_context", "metadata", "opportunities", "portfolio_context", "warnings", "watchlist_candidates"])

    def test_discovery_report_missing_gives_partial_and_keeps_the_rest(self):
        view = build(discovery=None)
        self.assertEqual(view["metadata"]["status"], "PARTIAL")
        self.assertEqual(view["metadata"]["sources"]["discovery"], {"status": "UNAVAILABLE", "reason": "NO_DISCOVERY_REPORT", "generated_at": None, "evaluation_as_of": None, "age_hours": None, "stale": None})
        self.assertEqual({r["source"] for r in view["opportunities"]}, {"WATCHLIST"})
        self.assertIn("DISCOVERY_REPORT_UNAVAILABLE", [w["code"] for w in view["warnings"]])
        self.assertNotEqual(rows(view)["AXIO"]["market_intelligence_summary"]["status"], "NEWS_UNAVAILABLE")

    def test_market_intelligence_missing_gives_partial_and_news_unavailable(self):
        view = build(mi=None)
        self.assertEqual(view["metadata"]["status"], "PARTIAL")
        self.assertEqual(view["market_context"]["status"], "UNAVAILABLE")
        self.assertEqual(view["market_context"]["macro_context"], {"status": "UNAVAILABLE", "categories": {}})
        self.assertEqual({r["market_intelligence_summary"]["status"] for r in view["opportunities"]}, {"NEWS_UNAVAILABLE"})
        self.assertEqual({p["market_intelligence_summary"]["status"] for p in view["portfolio_context"]["positions"]}, {"NEWS_UNAVAILABLE"})
        self.assertIn("MARKET_INTELLIGENCE_REPORT_UNAVAILABLE", [w["code"] for w in view["warnings"]])
        self.assertTrue(any(r["source"] == "DISCOVERY" for r in view["opportunities"]))

    def test_both_reports_missing_still_shows_the_engine_and_all_missing_is_unavailable(self):
        view = build(discovery=None, mi=None)
        self.assertEqual(view["metadata"]["status"], "PARTIAL")
        self.assertEqual({r["source"] for r in view["opportunities"]}, {"WATCHLIST"})
        self.assertEqual(view["portfolio_context"]["after_plan"]["planned_entries_count"], 1)
        nothing = build(engine=None, discovery=None, mi=None, engine_error="boom")
        self.assertEqual((nothing["metadata"]["status"], nothing["opportunities"]), ("UNAVAILABLE", []))
        self.assertEqual(ov.unavailable({"engine": "boom"})["status"], "UNAVAILABLE")

    def test_engine_missing_but_reports_present_shows_discovery_candidates(self):
        view = build(engine=None, engine_error="ORCHESTRATOR_FAILED")
        self.assertEqual(view["metadata"]["status"], "PARTIAL")
        self.assertEqual({r["source"] for r in view["opportunities"]}, {"DISCOVERY"})
        self.assertEqual(view["portfolio_context"]["engine_status"], "UNAVAILABLE")
        self.assertIn("ENGINE_UNAVAILABLE", [w["code"] for w in view["warnings"]])
        self.assertTrue(all(r["capital_fit"] is None for r in view["opportunities"]))


class SourceAndGroupTests(unittest.TestCase):
    def setUp(self):
        self.view = build()
        self.rows = rows(self.view)

    def test_symbol_only_in_watchlist(self):
        row = self.rows["AXIO"]
        self.assertEqual((row["source"], row["discovery_rank"], row["discovery_status"], row["promotion_status"]), ("WATCHLIST", None, None, "PROMOTE"))

    def test_symbol_only_in_discovery(self):
        row = self.rows["NOVA"]
        self.assertEqual((row["source"], row["entry_status"], row["promotion_status"], row["discovery_rank"], row["discovery_status"]), ("DISCOVERY", None, None, 1, "DISCOVERY_READY"))
        self.assertEqual((row["fundamental_quality"], row["plan_status"], row["current_price_eur"]), ("not_evaluated", "NOT_APPLICABLE", 130.0))

    def test_symbol_in_both_keeps_engine_data_and_adds_the_discovery_fields(self):
        row = self.rows["BRNT"]
        self.assertEqual((row["source"], row["entry_status"], row["entry_rank"], row["discovery_rank"], row["discovery_score"], row["discovery_status"]), ("BOTH", "ENTRY_READY", 2, 3, 26.0, "DISCOVERY_READY"))
        self.assertEqual(len([r for r in self.view["opportunities"] if r["yahoo_symbol"] == "BRNT"]), 1)

    def test_discovery_symbol_already_known_to_the_engine_is_both_with_a_warning(self):
        row = self.rows["EPSI"]
        self.assertEqual((row["source"], row["promotion_status"], row["entry_status"], row["group"]), ("BOTH", "KEEP_WATCHING", None, "DISCOVERY_READY"))
        self.assertIn("DISCOVERY_SYMBOL_ALREADY_KNOWN", row["reason_codes"])
        self.assertIn("DISCOVERY_SYMBOL_ALREADY_KNOWN", [w["code"] for w in self.view["warnings"]])

    def test_entry_ready_wait_for_trigger_discovery_ready_and_other_groups_and_order(self):
        groups = {s: r["group"] for s, r in self.rows.items()}
        self.assertEqual((groups["AXIO"], groups["BRNT"], groups["CYRN"], groups["NOVA"], groups["DUNM"]), ("ENTRY_READY", "ENTRY_READY", "WAIT_FOR_TRIGGER", "DISCOVERY_READY", "OTHER"))
        self.assertEqual([r["symbol"] for r in self.view["opportunities"]], ["AXIO", "BRNT", "CYRN", "NOVA", "ABEL", "XNRG", "CHEAP", "EPSI", "DUNM"])

    def test_discovery_watch_and_keep_watching_are_not_opportunities(self):
        self.assertNotIn("PLAT", self.rows)
        self.assertEqual(self.view["watchlist_candidates"]["symbols"], ["AXIO", "BRNT", "CYRN", "EPSI", "DUNM"])
        self.assertNotIn("EPSI", [r["symbol"] for r in self.view["opportunities"] if r["source"] == "WATCHLIST"])

    def test_plan_status_comes_from_the_planner(self):
        self.assertEqual((self.rows["AXIO"]["plan_status"], self.rows["AXIO"]["planned_capital_eur"], self.rows["AXIO"]["planned_quantity"]), ("PLANNED", 29900.0, 50.0))
        self.assertEqual((self.rows["BRNT"]["plan_status"], self.rows["BRNT"]["plan_reason"]), ("DEFERRED", "STOPPED_CAPITAL_EXHAUSTED"))
        self.assertEqual(self.rows["CYRN"]["plan_status"], "NOT_PLANNED")

    def test_capital_fit_is_an_informational_comparison_only(self):
        self.assertEqual(self.rows["CHEAP"]["capital_fit"]["fits_one_share_after_plan"], True)
        self.assertEqual(ov.fit_code(self.rows["CHEAP"]["capital_fit"]), "FITS_AFTER_PLAN")
        self.assertEqual(ov.fit_code(self.rows["NOVA"]["capital_fit"]), "COMMITTED_BY_PLANNED_ENTRIES")
        self.assertEqual(ov.fit_code({"fits_one_share_after_plan": False, "fits_one_share_after_actions": False}), "DOES_NOT_FIT")
        self.assertEqual(ov.fit_code(None), "UNKNOWN")
        self.assertIsNone(self.rows["AXIO"]["capital_fit"])


class NewsContextTests(unittest.TestCase):
    def setUp(self):
        self.view = build()
        self.rows = rows(self.view)

    def test_direct_high_news_gives_high_attention(self):
        summary = self.rows["ABEL"]["market_intelligence_summary"]
        self.assertEqual((summary["status"], summary["status_reasons"], summary["counts"]["HIGH"], summary["direct_counts"]["HIGH"]), ("NEWS_HIGH_ATTENTION", ["DIRECT_HIGH_EVENT"], 1, 1))
        self.assertEqual((summary["events"][0]["link"], summary["events"][0]["category"], summary["events"][0]["reason_codes"]), ("DIRECT", "REGULATORY", ["HEADLINE_NAMES_COMPANY"]))

    def test_direct_medium_news_gives_attention(self):
        summary = self.rows["AXIO"]["market_intelligence_summary"]
        self.assertEqual((summary["status"], summary["status_reasons"]), ("NEWS_ATTENTION", ["DIRECT_MEDIUM_EVENT"]))
        self.assertEqual((summary["events"][0]["importance"], summary["events"][0]["category"], summary["events"][0]["impact"]), ("MEDIUM", "GUIDANCE", "UNKNOWN"))

    def test_only_low_news_is_clear(self):
        summary = self.rows["BRNT"]["market_intelligence_summary"]
        self.assertEqual((summary["status"], summary["counts"], summary["events"]), ("NEWS_CLEAR", {"HIGH": 0, "MEDIUM": 0, "LOW": 1}, []))

    def test_a_loosely_linked_medium_event_alone_is_clear_but_stays_counted(self):
        summary = self.rows["NOVA"]["market_intelligence_summary"]
        self.assertEqual((summary["status"], summary["status_reasons"]), ("NEWS_CLEAR", ["ONLY_LOW_OR_LOOSELY_LINKED_MEDIUM_EVENTS"]))
        self.assertEqual((summary["counts"]["MEDIUM"], summary["direct_counts"]["MEDIUM"]), (2, 0))
        self.assertEqual({e["link"] for e in summary["events"]}, {"LOOSE"})

    def test_a_sector_scoped_high_event_is_attention_not_high_attention(self):
        summary = self.rows["XNRG"]["market_intelligence_summary"]
        self.assertEqual((summary["status"], summary["status_reasons"]), ("NEWS_ATTENTION", ["SECTOR_OR_LOOSE_HIGH_EVENT"]))
        self.assertEqual(summary["events"][0]["link"], "SECTOR")

    def test_no_events_is_clear(self):
        summary = self.rows["CYRN"]["market_intelligence_summary"]
        self.assertEqual((summary["status"], summary["status_reasons"]), ("NEWS_CLEAR", ["NO_LINKED_EVENTS"]))

    def test_news_unavailable_without_a_valid_report(self):
        self.assertEqual(ov.news_context("AXIO", "Axiom Micro Devices", None)["status"], "NEWS_UNAVAILABLE")
        self.assertEqual(ov.NEWS_STATUSES, ("NEWS_CLEAR", "NEWS_ATTENTION", "NEWS_HIGH_ATTENTION", "NEWS_UNAVAILABLE"))

    def test_a_bare_ticker_or_homonym_is_not_a_direct_link(self):
        homonym = event(50, "EMS Broadens Offering with Acquisition of AXIO Supply", ["AXIO"], "MEDIUM", "M_AND_A")
        view = build(mi=make_mi([homonym]))
        summary = rows(view)["AXIO"]["market_intelligence_summary"]
        self.assertEqual((summary["status"], summary["events"][0]["link"]), ("NEWS_CLEAR", "LOOSE"))

    def test_a_parenthesised_ticker_is_a_direct_link_but_a_bare_ticker_is_not(self):
        for headline, link in (("Brent Networks (BRNT) Outperforms AI Infrastructure Peers", "DIRECT"), ("Chip maker (NASDAQ:AXIO) jumps", "DIRECT"), ("EMS buys AXIO Supply", "LOOSE")):
            self.assertEqual(ov.event_link({"headline": headline, "reason_codes": [], "source_type": "NEWS"}, "AXIO" if "AXIO" in headline else "BRNT", "Unrelated Name"), link, headline)
        self.assertEqual(ov.event_link({"headline": "Fund (AXIOX) rises", "reason_codes": [], "source_type": "NEWS"}, "AXIO", "Unrelated Name"), "LOOSE")

    def test_a_sec_filing_is_always_a_direct_link(self):
        summary = {r["symbol"]: r for r in build()["portfolio_context"]["positions"]}["ALFA"]["market_intelligence_summary"]
        self.assertEqual((summary["status"], summary["events"][0]["link"], summary["events"][0]["impact"]), ("NEWS_HIGH_ATTENTION", "DIRECT", "NEGATIVE"))

    def test_held_positions_carry_their_action_and_news_status(self):
        positions = {p["symbol"]: p for p in self.view["portfolio_context"]["positions"]}
        self.assertEqual([(p["action"], p["market_intelligence_summary"]["status"]) for p in (positions["GOLF"], positions["ALFA"], positions["BRAV"])],
                         [("SELL", "NEWS_CLEAR"), ("TRIM", "NEWS_HIGH_ATTENTION"), ("HOLD", "NEWS_ATTENTION")])

    def test_macro_context_groups_high_and_medium_macro_events(self):
        categories = self.view["market_context"]["macro_context"]["categories"]
        self.assertEqual(list(categories), ["MONETARY_POLICY", "INFLATION", "GEOPOLITICS", "ENERGY"])
        self.assertEqual((categories["MONETARY_POLICY"]["high"], categories["MONETARY_POLICY"]["official_source_events"], categories["MONETARY_POLICY"]["top_events"][0]["source"]), (1, 1, "FED_MONETARY"))
        self.assertEqual(categories["INFLATION"]["top_events"][0]["portfolio_region_exposure"], ["US"])
        self.assertNotIn("COMPANY", categories)

    def test_high_attention_lists_only_direct_high_events(self):
        attention = self.view["market_context"]["high_attention"]
        self.assertEqual(sorted((a["scope"], a["symbol"]) for a in attention), [("OPPORTUNITY", "ABEL"), ("POSITION", "ALFA")])
        self.assertTrue(all(a["importance"] == "HIGH" for a in attention))


class NoInfluenceTests(unittest.TestCase):
    ENGINE_FIELDS = ("entry_status", "entry_rank", "entry_score", "momentum_score", "confidence", "promotion_status", "plan_status", "planned_capital_eur", "planned_quantity", "plan_reason",
                     "discovery_rank", "discovery_score", "discovery_status", "group", "current_price_eur", "technical_quality", "fundamental_quality", "event_risk_quality")

    def engine_only(self, view):
        return [(r["symbol"], *(r[f] for f in self.ENGINE_FIELDS)) for r in view["opportunities"]]

    def test_scores_are_the_engine_values_and_independent_of_the_news(self):
        engine, discovery = make_engine(), make_discovery()
        view = build(engine=engine, discovery=discovery)
        by = rows(view)
        for c in engine["portfolio_action_plan"]["entry_candidates"]:
            self.assertEqual((by[c["symbol"]]["entry_score"], by[c["symbol"]]["momentum_score"], by[c["symbol"]]["confidence"]), (c["entry_score"], c["momentum_score"], c["confidence"]))
        for r in discovery["results"]:
            if r["status"] == "DISCOVERY_READY" and r["symbol"] in by and by[r["symbol"]]["source"] == "DISCOVERY":
                self.assertEqual((by[r["symbol"]]["discovery_score"], by[r["symbol"]]["discovery_rank"]), (r["discovery_score"], r["rank"]))
        high_news = [event(i, "Abelon receives FDA approval", ["AXIO", "NOVA", "CYRN", "BRNT"], "HIGH", "REGULATORY", reasons=["HEADLINE_NAMES_COMPANY"]) for i in range(60, 63)]
        for variant in (None, make_mi([]), make_mi(standard_events() + high_news)):
            self.assertEqual(self.engine_only(build(mi=variant)), self.engine_only(view))

    def test_ranking_and_order_do_not_depend_on_the_news(self):
        baseline = [r["symbol"] for r in build(mi=None)["opportunities"]]
        heavy = [event(70 + i, f"Axiom Micro Devices issues profit warning {i}", ["CYRN", "DUNM", "CHEAP"], "HIGH", "GUIDANCE") for i in range(5)]
        self.assertEqual([r["symbol"] for r in build(mi=make_mi(standard_events() + heavy))["opportunities"]], baseline)
        self.assertEqual([r["symbol"] for r in build()["opportunities"]], baseline)
        engine_rank = [c["symbol"] for c in sorted((c for c in make_engine()["portfolio_action_plan"]["entry_candidates"] if c["rank"] is not None), key=lambda c: c["rank"])]
        self.assertEqual([s for s in baseline if s in engine_rank], engine_rank)

    def test_position_sizing_and_capital_figures_are_passed_through_unchanged(self):
        engine = make_engine()
        plan = engine["portfolio_action_plan"]
        for mi in (None, make_mi()):
            view = build(engine=engine, mi=mi)
            ctx = view["portfolio_context"]
            self.assertEqual([(p["symbol"], p["capital_eur"], p["quantity"]) for p in ctx["planned_entries"]], [("AXIO", 29900.0, 50.0)])
            self.assertEqual((ctx["capital"]["cash_eur"], ctx["after_actions"]["cash_after_actions_eur"], ctx["after_actions"]["expected_proceeds_net_conservative_eur"]),
                             (5000.0, 40000.0, 35000.0))
            self.assertEqual((ctx["after_plan"]["planned_entries_total_eur"], ctx["after_plan"]["remaining_buying_capacity_eur"], ctx["after_plan"]["remaining_swing_capacity_eur"]), (29900.0, 100.0, 30000.0))
            self.assertEqual((ctx["capital"]["swing_pct"], ctx["capital"]["swing_target_pct"]), (20.0, [30.0, 40.0]))
        self.assertEqual(plan["planned_entries"][0]["capital_eur"], 29900.0)

    def test_sell_trim_hold_stay_as_decided_by_the_engine(self):
        engine = make_engine()
        expected = [(p["symbol"], p["decision"]["action"]) for p in engine["existing_position_results"]]
        for mi in (None, make_mi(), make_mi([event(80, "Golf Defense news", ["GOLF.DE"], "HIGH", "REGULATORY", reasons=["HEADLINE_NAMES_COMPANY"])])):
            positions = build(engine=engine, mi=mi)["portfolio_context"]["positions"]
            self.assertEqual([(p["symbol"], p["action"]) for p in positions], expected)

    def test_inputs_are_not_modified_and_the_result_is_deterministic(self):
        engine, discovery, mi = make_engine(), make_discovery(), make_mi()
        before = copy.deepcopy((engine, discovery, mi))
        first = build(engine=engine, discovery=discovery, mi=mi)
        self.assertEqual((engine, discovery, mi), before)
        second = build(engine=copy.deepcopy(engine), discovery=copy.deepcopy(discovery), mi=copy.deepcopy(mi))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_result_flags_and_absence_of_orders_signals_and_combined_scores(self):
        view = build()
        self.assertEqual((view["metadata"]["read_only"], view["metadata"]["orders_created"], view["metadata"]["signals_changed"], view["metadata"]["deterministic"]), (True, False, False, True))
        text = json.dumps(view)
        for forbidden in ("combined_score", "opportunity_score", "news_bonus", "news_score", "plan_token", "secret-token", "order_id"):
            self.assertNotIn(forbidden, text)

    def test_decision_modules_do_not_know_the_opportunity_view(self):
        for name in ("trading_orchestrator", "portfolio_action_planner", "swing_promotion", "decision_engine", "candidate_decision", "entry_sizing", "candidate_discovery", "analysis_engine",
                     "swing_lifecycle", "strategy_suggestion", "market_intelligence", "discovery_report"):
            source = (ROOT / "tools" / "trading" / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("opportunity_view", source, name)

    def test_view_module_has_no_network_database_or_process_imports(self):
        source = Path(ov.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib", "http.client", "requests", "sqlite3", "subprocess", "Collect-Trading", "Discover-Trading", "candidate_discovery", "analysis_engine", "trading_orchestrator", "portfolio_action_planner"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)
        for forbidden in ("INSERT", "UPDATE ", "DELETE ", "commit("):
            self.assertNotIn(forbidden, source)


class FreshnessTests(unittest.TestCase):
    def test_fresh_reports_are_not_stale(self):
        sources = build()["metadata"]["sources"]
        self.assertEqual((sources["discovery"]["stale"], sources["market_intelligence"]["stale"]), (False, False))
        self.assertEqual((sources["discovery"]["generated_at"], sources["discovery"]["evaluation_as_of"], sources["market_intelligence"]["evaluation_as_of"]), (NOW.isoformat(), "2026-10-07", "2026-10-07"))

    def test_stale_discovery_is_shown_and_flagged(self):
        view = build(discovery=make_discovery(generated_at=NOW - timedelta(days=10), evaluation_as_of="2026-09-27"))
        self.assertTrue(view["metadata"]["sources"]["discovery"]["stale"])
        self.assertFalse(view["metadata"]["sources"]["market_intelligence"]["stale"])
        self.assertEqual(view["metadata"]["status"], "AVAILABLE")
        self.assertIn("DISCOVERY_REPORT_STALE", [w["code"] for w in view["warnings"]])
        self.assertTrue(any(r["source"] == "DISCOVERY" for r in view["opportunities"]))

    def test_stale_market_intelligence_is_shown_and_flagged(self):
        view = build(mi=make_mi(generated_at=NOW - timedelta(hours=40)))
        self.assertTrue(view["metadata"]["sources"]["market_intelligence"]["stale"])
        self.assertFalse(view["metadata"]["sources"]["discovery"]["stale"])
        self.assertIn("MARKET_INTELLIGENCE_REPORT_STALE", [w["code"] for w in view["warnings"]])
        self.assertEqual(rows(view)["ABEL"]["market_intelligence_summary"]["status"], "NEWS_HIGH_ATTENTION")  # still context
        self.assertEqual(build(mi=make_mi(generated_at=NOW - timedelta(hours=23)))["metadata"]["sources"]["market_intelligence"]["stale"], False)


def contains_none(node):
    if isinstance(node, dict):
        return any(v is None or contains_none(v) for v in node.values())
    if isinstance(node, list):
        return any(contains_none(v) for v in node)
    return False


def many_entry_ready(count=12):
    """Engine result with `count` ENTRY_READY candidates (the real list has 12)."""
    engine = make_engine()
    plan = engine["portfolio_action_plan"]
    for i in range(count):
        sid = 200 + i
        engine["promotion_results"].append(promotion(sid, f"E{i:02d}", f"Entry Company {i}", momentum=50.0 - i, price=100.0 + i))
        plan["entry_candidates"].append(candidate(sid, f"E{i:02d}", f"Entry Company {i}", "ENTRY_READY", 10 + i, 20.0 - i * 0.1, 50.0 - i, 100.0 + i))
        plan["deferred_entries"].append({"security_id": sid, "symbol": f"E{i:02d}", "rank": 10 + i, "entry_score": 20.0 - i * 0.1, "reason": "STOPPED_CAPITAL_EXHAUSTED"})
    return engine


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.view = build()

    def q(self, **kw):
        return ov.query_view(self.view, **kw)

    def symbols(self, res):
        return [r["symbol"] for r in res["opportunities"]]

    # ---- compact is really compact
    def test_compact_has_no_side_blocks_and_no_rendered_text(self):
        res = self.q()
        self.assertEqual(sorted(res), sorted(["ok", "operation", "status", "read_only", "orders_created", "signals_changed", "evaluation_as_of", "freshness", "capital",
                                              "matching", "returned", "truncated", "opportunities", "warnings"]))
        for forbidden in ("rendered_de", "market_context", "discovery_candidates", "watchlist_candidates", "portfolio_context", "sources", "counts", "notes", "methodology", "positions", "filter", "hint"):
            self.assertNotIn(forbidden, res)
        text = json.dumps(res, ensure_ascii=False)
        for macro_or_global in ("Federal Reserve issues FOMC", "CPI for all items", "New tariffs", "EIA expects", "high_attention", "macro_context", "deferred_entries"):
            self.assertNotIn(macro_or_global, text)

    def test_compact_rows_are_slim_and_have_no_empty_fields(self):
        res = self.q()
        self.assertFalse(contains_none(res), json.dumps(res)[:400])
        axio = next(r for r in res["opportunities"] if r["symbol"] == "AXIO")
        self.assertEqual(axio, {
            "symbol": "AXIO", "name": "Axiom Micro Devices", "source": "WATCHLIST", "entry_status": "ENTRY_READY", "entry_rank": 1, "entry_score": 41.5, "momentum_score": 62.5, "confidence": 0.6,
            "current_price_eur": 598.0, "plan": "PLANNED", "planned_capital_eur": 29900.0, "planned_quantity": 50.0, "proposed_capital_eur": 30000.0, "proposed_quantity": 50.0,
            "quality": "tech=available fund=partial event=available",
            "news": {"status": "NEWS_ATTENTION", "counts": "H0/M1/L0", "direct": "H0/M1",
                     "event": {"date": "2026-10-06", "category": "GUIDANCE", "importance": "MEDIUM", "link": "DIRECT", "headline": "Axiom Micro Devices issues profit warning", "reasons": ["GUIDANCE_KEYWORD"]}}})
        self.assertNotIn("market_intelligence_summary", axio)
        self.assertLessEqual(len(axio["news"]["event"]["headline"]), 90)

    def test_compact_rows_carry_stop_reasons_news_and_capital_fit(self):
        rows_ = {r["symbol"]: r for r in self.q()["opportunities"]}
        brnt = rows_["BRNT"]
        self.assertEqual((brnt["plan"], brnt["plan_reason"], brnt["proposed_capital_eur"], brnt["proposed_quantity"], brnt["news"]["status"]), ("DEFERRED", "STOPPED_CAPITAL_EXHAUSTED", 12345.68, 65.0, "NEWS_CLEAR"))
        self.assertNotIn("planned_capital_eur", brnt)
        nova = rows_["NOVA"]
        self.assertEqual((nova["source"], nova["discovery_rank"], nova["discovery_score"], nova["capital_fit"], nova["quality"]), ("DISCOVERY", 1, 26.0, "COMMITTED_BY_PLANNED_ENTRIES", "tech=available fund=not_evaluated event=n/a"))
        self.assertEqual(nova["reasons"], ["DISCOVERY_TREND_INTACT", "DISCOVERY_MOMENTUM_ABOVE_THRESHOLD"])
        self.assertNotIn("plan", nova)
        self.assertEqual(rows_["CHEAP"]["capital_fit"], "FITS_AFTER_PLAN")
        self.assertEqual(rows_["EPSI"]["promotion_status"], "KEEP_WATCHING")
        self.assertEqual(rows_["CYRN"]["entry_status"], "WAIT_FOR_TRIGGER")
        self.assertNotIn("proposed_capital_eur", rows_["CYRN"])
        self.assertEqual(rows_["ABEL"]["news"]["event"]["importance"], "HIGH")

    def test_compact_capital_frame_is_minimal_and_from_the_planner(self):
        capital = self.q()["capital"]
        self.assertEqual(capital, {"cash_eur": 5000.0, "cash_after_actions_eur": 40000.0, "net_proceeds_conservative_eur": 35000.0, "planned_entries_total_eur": 29900.0,
                                   "remaining_buying_capacity_eur": 100.0, "swing_pct": 20.0, "swing_target_pct": [30.0, 40.0]})
        self.assertEqual(self.q()["freshness"], {"discovery": {"as_of": "2026-10-07", "generated_at": "2026-10-07T18:00", "stale": False},
                                                 "market_intelligence": {"as_of": "2026-10-07", "generated_at": "2026-10-07T18:00", "stale": False}})
        self.assertEqual(self.q()["warnings"], ["ENGINE_ISSUE:ALLOCATION_OUTSIDE_TARGET", "DISCOVERY_SYMBOL_ALREADY_KNOWN"])

    def test_compact_is_much_smaller_than_detail(self):
        compact, detail = json.dumps(self.q(symbol="AXIO,BRNT")), json.dumps(self.q(symbol="AXIO,BRNT", detail=True))
        self.assertLess(len(compact) * 4, len(detail))

    # ---- symbol filter returns exactly the requested symbols
    def test_symbol_filter_returns_only_the_requested_symbols_and_their_news(self):
        res = self.q(symbol="axio, brnt")
        self.assertEqual((self.symbols(res), res["matching"], res["returned"], res["filter"]), (["AXIO", "BRNT"], 2, 2, {"symbol": ["AXIO", "BRNT"]}))
        text = json.dumps(res, ensure_ascii=False)
        for foreign in ("Abelon", "Bravo", "Corvex", "NOVA", "ALFA", "Golf Defense"):
            self.assertNotIn(foreign, text)
        self.assertNotIn("positions", res)  # no held symbol requested

    def test_symbol_filter_adds_only_the_requested_held_positions(self):
        res = self.q(symbol="ALFA,GOLF,AXIO")
        self.assertEqual(self.symbols(res), ["AXIO"])
        self.assertEqual([(p["symbol"], p["action"], p["news"]["status"]) for p in res["positions"]], [("ALFA", "TRIM", "NEWS_HIGH_ATTENTION"), ("GOLF", "SELL", "NEWS_CLEAR")])
        self.assertEqual(res["positions"][0]["news"]["event"]["category"], "REGULATORY")
        self.assertNotIn("BRAV", json.dumps(res))

    # ---- ENTRY_READY is delivered completely
    def test_status_entry_ready_returns_all_current_hits_without_truncation(self):
        view = build(engine=many_entry_ready(10))  # 2 + 10 = 12 ENTRY_READY, like the real list
        res = ov.query_view(view, status="ENTRY_READY")
        self.assertEqual((res["matching"], res["returned"], res["truncated"]), (12, 12, False))
        self.assertNotIn("hint", res)
        self.assertGreaterEqual(ov.DEFAULT_LIMIT, 12)
        self.assertLessEqual(len(json.dumps(res, ensure_ascii=False)), ov.RESPONSE_CHAR_BUDGET)
        self.assertEqual([r["entry_rank"] for r in res["opportunities"]][:3], [1, 2, 10])

    def test_a_truncated_answer_says_how_many_rows_are_missing(self):
        res = self.q(limit=2)
        self.assertEqual((res["returned"], res["matching"], res["truncated"]), (2, 9, True))
        self.assertIn("7 more rows match", res["hint"])
        self.assertEqual(self.q(limit=0)["returned"], 1)
        extra = tuple(disc_result(100 + i, f"S{i}", f"Name {i}", score=20.0 - i * 0.01, price=50.0) for i in range(60))
        big = build(discovery=make_discovery(extra=extra))
        self.assertEqual(ov.query_view(big, limit=10_000)["returned"], ov.MAX_LIMIT_COMPACT)

    # ---- detail stays complete
    def test_detail_keeps_every_block(self):
        res = self.q(detail=True, limit=1000)
        self.assertEqual(res["filter"]["limit_applied"], ov.MAX_LIMIT_DETAIL)
        for key in ("sources", "counts", "portfolio_context", "watchlist_candidates", "discovery_candidates", "market_context", "methodology", "rendered_de", "notes", "warnings"):
            self.assertIn(key, res)
        row = res["opportunities"][0]
        for key in (*ov.ROW_FIELDS, *ov.DETAIL_EXTRA_FIELDS, "market_intelligence_summary"):
            self.assertIn(key, row)
        self.assertIn("status_reasons", row["market_intelligence_summary"])
        self.assertIn("deferred_entries", res["portfolio_context"])
        self.assertIn("macro_context", res["market_context"])
        abbv = self.q(detail=True, symbol="ABEL")["opportunities"][0]
        self.assertIn("source_url", abbv["market_intelligence_summary"]["events"][0])
        self.assertIsInstance(abbv["capital_fit"], dict)
        self.assertEqual(self.q(detail=True, symbol="BRNT")["opportunities"][0]["proposed_capital_eur"], 12345.68)

    def test_rendered_text_is_only_part_of_the_detail_result(self):
        self.assertNotIn("rendered_de", self.q(symbol="AXIO"))
        self.assertIn("rendered_de", self.q(symbol="AXIO", detail=True))

    def test_detail_positions_follow_actions_or_news_attention(self):
        positions = {p["symbol"]: p for p in self.q(detail=True)["portfolio_context"]["positions"]}
        self.assertEqual(sorted(positions), ["ALFA", "BRAV", "GOLF"])
        self.assertIn("events", self.q(detail=True)["portfolio_context"]["positions"][0])

    def test_detail_discovery_section_is_not_cut_by_the_limit(self):
        res = self.q(limit=1, detail=True)
        self.assertEqual([r["symbol"] for r in res["discovery_candidates"]["shown"]], ["BRNT", "NOVA", "ABEL", "XNRG", "CHEAP", "EPSI"])
        self.assertEqual(res["discovery_candidates"]["shown_total"], 6)
        self.assertIn("| NOVA | 1 |", res["rendered_de"])
        self.assertIn("(Opportunities gekürzt", res["rendered_de"])

    def test_rendered_german_output(self):
        text = self.q(detail=True)["rendered_de"]
        for fragment in ("## Opportunity View (Status AVAILABLE", "### Aktuelle geplante Entries", "| Symbol | Entry Status | Rank | Kapital | News Context |", "### Weitere Watchlist-Kandidaten (nicht im Plan)",
                         "### Neue Discovery-Chancen", "| Symbol | Discovery Rank | Score | News Context | Status | Kapital |", "### High-Attention", "### Kapital", "konservativer Nettoerlös",
                         "verbleibende Kaufkapazität", "Keine zusätzlichen Kaufvorschläge", "Es gibt keinen kombinierten Score", "aktuell"):
            self.assertIn(fragment, text)
        self.assertIn("| AXIO | ENTRY_READY | 1 | 29.900 EUR | NEWS_ATTENTION |", text)
        self.assertIn("| ABEL | 2 | 26.0 | NEWS_HIGH_ATTENTION | DISCOVERY_READY | nur aus Verkaufserlösen, durch geplante Entries bereits verplant |", text)
        self.assertIn("| CHEAP | 5 | 26.0 | NEWS_CLEAR | DISCOVERY_READY | passt ins freie Kapital |", text)

    def test_rendering_a_missing_report_is_explicit(self):
        text = ov.query_view(build(discovery=None, mi=None), detail=True)["rendered_de"]
        self.assertIn("Discovery: nicht verfügbar (NO_DISCOVERY_REPORT)", text)
        self.assertIn("Market Intelligence: nicht verfügbar (NO_MARKET_INTELLIGENCE_REPORT)", text)
        self.assertIn("keine Discovery-Kandidaten", text)

    # ---- filters and validation
    def test_filters(self):
        self.assertEqual(self.symbols(self.q(source="DISCOVERY")), ["NOVA", "ABEL", "XNRG", "CHEAP"])
        self.assertEqual(self.symbols(self.q(source="both")), ["BRNT", "EPSI"])
        self.assertEqual(self.symbols(self.q(status="entry_ready")), ["AXIO", "BRNT"])
        self.assertEqual(self.symbols(self.q(status="WAIT")), ["CYRN"])
        self.assertEqual(self.symbols(self.q(status="DISCOVERY_READY")), ["NOVA", "ABEL", "XNRG", "CHEAP", "EPSI"])
        self.assertEqual(self.symbols(self.q(news_status="NEWS_HIGH_ATTENTION")), ["ABEL"])
        self.assertEqual(self.symbols(self.q(news_status="relevant")), ["AXIO", "ABEL", "XNRG"])
        self.assertEqual(self.symbols(self.q(source="DISCOVERY", status="DISCOVERY_READY", news_status="NOT_HIGH")), ["NOVA", "XNRG", "CHEAP"])
        self.assertEqual(self.symbols(self.q(symbol="ABEL,NOVA,PLAT")), ["NOVA", "ABEL"])
        self.assertEqual(self.q(symbol="GOLF")["returned"], 0)
        self.assertEqual([p["symbol"] for p in self.q(symbol="GOLF")["positions"]], ["GOLF"])  # held position by base symbol
        self.assertEqual(self.q(source="DISCOVERY", status="ENTRY_READY")["returned"], 0)

    def test_invalid_filters(self):
        for kw, error in (({"source": "X"}, "INVALID_SOURCE"), ({"status": "X"}, "INVALID_STATUS"), ({"news_status": "X"}, "INVALID_NEWS_STATUS")):
            res = self.q(**kw)
            self.assertEqual((res["ok"], res["error"]), (False, error))

    # ---- size guard
    def big_view(self):
        extra = tuple(disc_result(100 + i, f"S{i}", "Name " * 30, score=20.0 - i * 0.01, price=50.0) for i in range(300))
        events = [event(500 + i, "Headline " + "x" * 90, [f"S{i}"], "MEDIUM", "COMPANY", reasons=["A" * 40] * 5) for i in range(300)]
        many = [event(900 + i, "Bravo Therapeutics wins injunction " + "x" * 60, ["BRAV", "AXIO", "BRNT"], "MEDIUM", "REGULATORY", reasons=["A" * 40] * 6) for i in range(12)]
        return build(discovery=make_discovery(extra=extra), mi=make_mi(standard_events() + events + many))

    def test_big_views_stay_below_the_mcp_limit(self):
        view = self.big_view()
        for kw in ({"limit": 1000}, {"limit": 1000, "detail": True}, {"limit": 1000, "symbol": "BRAV,AXIO,BRNT", "detail": True}, {"limit": 1000, "symbol": "BRAV,AXIO,BRNT"}):
            res = ov.query_view(view, **kw)
            self.assertLess(len(json.dumps(res, ensure_ascii=False)), 40_000, kw)
            self.assertLessEqual(len(json.dumps(res, ensure_ascii=False)), ov.RESPONSE_CHAR_BUDGET, kw)
        self.assertNotIn("RESPONSE_TRIMMED", json.dumps(ov.query_view(self.view)))

    def test_the_compact_size_guard_drops_the_last_rows_and_says_so(self):
        view = self.big_view()
        with mock.patch.object(ov, "RESPONSE_CHAR_BUDGET", 6_000):
            res = ov.query_view(view, limit=1000)
        self.assertLessEqual(len(json.dumps(res, ensure_ascii=False)), 6_000)
        self.assertTrue(res["truncated"] and 1 <= res["returned"] < res["matching"])
        self.assertIn("RESPONSE_TRIMMED_TO_SIZE_BUDGET", res["hint"])
        self.assertEqual([r["symbol"] for r in res["opportunities"]], [r["symbol"] for r in ov.query_view(view, limit=1000)["opportunities"]][: res["returned"]])

    def test_the_detail_size_guard_reduces_secondary_sections_before_dropping_opportunities(self):
        view = self.big_view()
        with mock.patch.object(ov, "RESPONSE_CHAR_BUDGET", 16_000):
            res = ov.query_view(view, detail=True, limit=1000)
        notes = " ".join(res["notes"])
        for label in ("methodology reduced", "position events reduced", "macro context reduced"):
            self.assertIn(label, notes)
        self.assertNotIn("methodology", res)
        self.assertTrue(all("events" not in p for p in res["portfolio_context"]["positions"]))
        self.assertIn("fewer opportunities returned", notes)
        self.assertTrue(res["truncated"] and res["returned"] >= 1)
        with mock.patch.object(ov, "RESPONSE_CHAR_BUDGET", 10**9):
            untouched = ov.query_view(view, detail=True, limit=1000)
        self.assertEqual(untouched["returned"], ov.MAX_LIMIT_DETAIL)
        self.assertIn("methodology", untouched)
        self.assertEqual([name for name, _ in ov._EXTRA_REDUCTIONS], ["methodology", "position events", "macro context", "discovery list"])


# ---------------------------------------------------------------- MCP reader (fake orchestrator, real report files)
def fingerprint(path: Path):
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


class McpWorkspace:
    def __init__(self, *, discovery=True, mi=True, orchestrator=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "trading.db"
        conn = sqlite3.connect(self.db)
        conn.executescript("CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO data_sources VALUES (1, 'Yahoo Finance');"
                           "CREATE TABLE source_symbols (id INTEGER PRIMARY KEY, security_id INTEGER, source_id INTEGER, symbol TEXT);")
        conn.executemany("INSERT INTO source_symbols(security_id, source_id, symbol) VALUES (?, 1, ?)", list(SYMBOL_MAP.items()))
        conn.commit()
        conn.close()
        if discovery:
            dr.write_report(make_discovery(generated_at=datetime.now(timezone.utc), evaluation_as_of=datetime.now(timezone.utc).date().isoformat()), self.dir / "candidate-discovery", history=False)
        if mi:
            ir.write_report(make_mi(generated_at=datetime.now(timezone.utc), evaluation_as_of=datetime.now(timezone.utc).date().isoformat()), self.dir / "market-intelligence", history=False)
        self.calls = []
        engine = make_engine()

        def run(conn, as_of=None):
            self.calls.append(conn)
            if orchestrator:
                orchestrator(conn)
            return SimpleNamespace(primitive=lambda: copy.deepcopy(engine))

        self.patch = mock.patch.dict(Tools._runtime_module_cache, {"discovery_report": dr, "intelligence_report": ir, "opportunity_view": ov,
                                                                    "trading_orchestrator": SimpleNamespace(run_trading_orchestrator=run)})
        self.patch.start()
        self.tools = Tools()
        self.tools.valves.database_path = str(self.db)

    def call(self, **kw):
        return asyncio.run(self.tools.get_opportunity_view(**kw))

    def close(self):
        self.patch.stop()
        self.tmp.cleanup()


class McpReaderTests(unittest.TestCase):
    def test_reads_engine_and_both_reports(self):
        ws = McpWorkspace()
        self.addCleanup(ws.close)
        res = ws.call(limit=5)
        self.assertEqual((res["ok"], res["status"], res["returned"], res["matching"]), (True, "AVAILABLE", 5, 9))
        self.assertEqual([r["symbol"] for r in res["opportunities"]], ["AXIO", "BRNT", "CYRN", "NOVA", "ABEL"])
        self.assertEqual((res["freshness"]["discovery"]["stale"], res["freshness"]["market_intelligence"]["stale"]), (False, False))
        self.assertEqual(res["opportunities"][0]["news"]["status"], "NEWS_ATTENTION")
        self.assertNotIn("rendered_de", res)
        self.assertEqual(len(ws.calls), 1)

    def test_engine_connection_is_read_only_and_the_database_is_not_written(self):
        attempts = []

        def try_write(conn):
            for statement in ("INSERT INTO source_symbols(security_id, source_id, symbol) VALUES (999, 1, 'X')", "CREATE TABLE evil (x)", "DELETE FROM source_symbols"):
                try:
                    conn.execute(statement)
                    attempts.append("WRITTEN")
                except sqlite3.OperationalError as exc:
                    attempts.append(str(exc))

        ws = McpWorkspace(orchestrator=try_write)
        self.addCleanup(ws.close)
        before = fingerprint(ws.db)
        data_before = ws.db.read_bytes()
        res = ws.call()
        self.assertEqual(res["status"], "AVAILABLE")
        self.assertEqual(len(attempts), 3)
        self.assertNotIn("WRITTEN", attempts)
        self.assertTrue(all("readonly" in a or "read-only" in a or "query_only" in a for a in attempts), attempts)
        self.assertEqual(before, fingerprint(ws.db))
        self.assertEqual(data_before, ws.db.read_bytes())
        self.assertEqual(sorted(p.name for p in ws.dir.iterdir()), ["candidate-discovery", "market-intelligence", "trading.db"])

    def test_no_external_request_no_collector_and_no_subprocess(self):
        ws = McpWorkspace()
        self.addCleanup(ws.close)
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network access")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("urlopen")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess")), \
                mock.patch.object(os, "system", side_effect=AssertionError("os.system")):
            res = ws.tools._get_opportunity_view_sync("ALL", "ALL", None, None, 5, True)
        self.assertEqual(res["status"], "AVAILABLE")
        method_source = inspect.getsource(Tools._get_opportunity_view_sync) + inspect.getsource(Tools.get_opportunity_view)
        for forbidden in ("Collect-Trading", "Discover-Trading", "urllib", "subprocess", "fetch", "requests"):
            self.assertNotIn(forbidden, method_source)

    def test_discovery_report_missing_gives_partial(self):
        ws = McpWorkspace(discovery=False)
        self.addCleanup(ws.close)
        res = ws.call()
        self.assertEqual((res["status"], res["freshness"]["discovery"]), ("PARTIAL", {"status": "UNAVAILABLE", "reason": "NO_DISCOVERY_REPORT"}))
        self.assertTrue(res["opportunities"])
        self.assertNotIn("DISCOVERY", {r["source"] for r in res["opportunities"]})

    def test_market_intelligence_missing_gives_partial_with_news_unavailable(self):
        ws = McpWorkspace(mi=False)
        self.addCleanup(ws.close)
        res = ws.call()
        self.assertEqual((res["status"], res["freshness"]["market_intelligence"]), ("PARTIAL", {"status": "UNAVAILABLE", "reason": "NO_MARKET_INTELLIGENCE_REPORT"}))
        self.assertEqual({r["news"]["status"] for r in res["opportunities"]}, {"NEWS_UNAVAILABLE"})
        detail = ws.call(detail=True)
        self.assertEqual(detail["market_context"]["status"], "UNAVAILABLE")
        self.assertIn("Market Intelligence: nicht verfügbar (NO_MARKET_INTELLIGENCE_REPORT)", detail["rendered_de"])

    def test_both_reports_and_engine_missing_is_unavailable(self):
        ws = McpWorkspace(discovery=False, mi=False)
        self.addCleanup(ws.close)
        ws.patch.stop()
        failing = SimpleNamespace(run_trading_orchestrator=mock.Mock(side_effect=RuntimeError("engine down")))
        with mock.patch.dict(Tools._runtime_module_cache, {"discovery_report": dr, "intelligence_report": ir, "opportunity_view": ov, "trading_orchestrator": failing}):
            res = ws.call()
        ws.patch.start()
        self.assertEqual((res["ok"], res["status"]), (True, "UNAVAILABLE"))
        self.assertIn("engine down", res["reasons"]["engine"])

    def test_engine_failure_keeps_the_reports_visible(self):
        ws = McpWorkspace()
        self.addCleanup(ws.close)
        ws.patch.stop()
        failing = SimpleNamespace(run_trading_orchestrator=mock.Mock(side_effect=RuntimeError("engine down")))
        with mock.patch.dict(Tools._runtime_module_cache, {"discovery_report": dr, "intelligence_report": ir, "opportunity_view": ov, "trading_orchestrator": failing}):
            res = ws.call()
        ws.patch.start()
        self.assertEqual(res["status"], "PARTIAL")
        self.assertIn("ENGINE_UNAVAILABLE", res["warnings"])
        self.assertTrue(res["opportunities"])
        self.assertNotIn("capital", res)

    def test_invalid_filter_and_filter_passing(self):
        ws = McpWorkspace()
        self.addCleanup(ws.close)
        self.assertEqual(ws.call(source="NOPE")["error"], "INVALID_SOURCE")
        res = ws.call(source="DISCOVERY", news_status="NEWS_HIGH_ATTENTION", symbol="ABEL,NOVA,PLAT")
        self.assertEqual([r["symbol"] for r in res["opportunities"]], ["ABEL"])
        self.assertEqual(res["filter"]["symbol"], ["ABEL", "NOVA", "PLAT"])

    def test_stale_reports_are_flagged_through_the_reader(self):
        ws = McpWorkspace(discovery=False, mi=False)
        self.addCleanup(ws.close)
        old = datetime.now(timezone.utc) - timedelta(days=10)
        dr.write_report(make_discovery(generated_at=old, evaluation_as_of=old.date().isoformat()), ws.dir / "candidate-discovery", history=False)
        ir.write_report(make_mi(generated_at=old, evaluation_as_of=old.date().isoformat()), ws.dir / "market-intelligence", history=False)
        res = ws.call()
        self.assertEqual(res["status"], "AVAILABLE")
        self.assertEqual((res["freshness"]["discovery"]["stale"], res["freshness"]["market_intelligence"]["stale"]), (True, True))
        self.assertIn("DISCOVERY_REPORT_STALE", res["warnings"])
        self.assertIn("STALE", ws.call(detail=True)["rendered_de"])

    def test_corrupt_report_is_reported_as_unavailable_not_trusted(self):
        ws = McpWorkspace()
        self.addCleanup(ws.close)
        (ws.dir / "market-intelligence" / "latest.json").write_text("{nope", encoding="utf-8")
        res = ws.call()
        self.assertEqual((res["status"], res["freshness"]["market_intelligence"]["reason"]), ("PARTIAL", "REPORT_INVALID"))


class ToolSurfaceTests(unittest.TestCase):
    ORIGINAL = sorted(["sql_execute", "database_tables", "database_schema", "table_info", "database_status", "rank_watchlist", "run_trading_orchestrator",
                       "evaluate_swing_candidate", "evaluate_swing_candidates", "approve_swing_promotion", "suggest_strategy_assignments", "evaluate_watchlist_candidates"])

    def public(self):
        return sorted(n for n, m in inspect.getmembers(Tools()) if not n.startswith("_") and callable(m) and (inspect.ismethod(m) or inspect.isfunction(m)))

    def test_fifteen_tools_and_the_fourteen_existing_ones_are_unchanged(self):
        names = self.public()
        self.assertEqual(len(names), 15)
        self.assertEqual(sorted(set(names) - {"get_opportunity_view"}), sorted([*self.ORIGINAL, "get_candidate_discovery", "get_market_intelligence"]))

    def test_existing_signatures_are_unchanged(self):
        t = Tools()
        self.assertEqual(list(inspect.signature(t.get_market_intelligence).parameters), ["scope", "category", "importance", "symbol", "sector", "limit", "detail"])
        self.assertEqual(list(inspect.signature(t.get_candidate_discovery).parameters), ["status", "limit", "detail", "symbol"])
        self.assertEqual(list(inspect.signature(t.run_trading_orchestrator).parameters), ["as_of"])

    def test_the_new_tool_has_only_read_filters(self):
        parameters = inspect.signature(Tools().get_opportunity_view).parameters
        self.assertEqual(list(parameters), ["source", "status", "news_status", "symbol", "limit", "detail"])
        self.assertEqual(parameters["limit"].default, ov.DEFAULT_LIMIT)

    def test_tool_descriptions_are_short_and_keep_the_essentials(self):
        t = Tools()
        docs = {n: inspect.getdoc(getattr(t, n)) for n in ("get_candidate_discovery", "get_market_intelligence", "get_opportunity_view")}
        self.assertLessEqual(sum(len(d) for d in docs.values()), 2_600)
        for name, doc in docs.items():
            self.assertLessEqual(len(doc), 1_200, name)
            self.assertIn("read-only", doc.lower(), name)
            self.assertRegex(doc, r"no (web|scan)", name)
        self.assertIn("no DB", docs["get_candidate_discovery"])
        self.assertIn("no DB write", docs["get_opportunity_view"])
        mi_doc = docs["get_market_intelligence"]
        for phrase in ("compact result is sufficient", "omit every optional parameter whose default suffices", "do not set it explicitly", "set only if a specific count is required",
                       "raw dump of all stored fields and substantially larger output", "explicitly requests detailed/raw data", "compact result lacks information required"):
            self.assertIn(phrase, mi_doc)
        self.assertIn("detail only when explicitly needed", docs["get_opportunity_view"])
        for name, params in {"get_candidate_discovery": ["status", "limit", "detail", "symbol"], "get_market_intelligence": ["scope", "category", "importance", "symbol", "sector", "limit", "detail"],
                             "get_opportunity_view": ["source", "status", "news_status", "symbol", "limit", "detail"]}.items():
            self.assertEqual(list(inspect.signature(getattr(t, name)).parameters), params)
            for param in params:
                self.assertIn(f":param {param}:", docs[name], f"{name}.{param}")

    def test_loader_name_collision_free(self):
        self.assertIs(Tools._runtime_module_cache.get("opportunity_view", ov) is not None, True)


if __name__ == "__main__":
    unittest.main()
