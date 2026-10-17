# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The "Einzelwertrisiko / Effective Exposure" section of the Sektoren page: view model (direct / ETF / effective, contributors, coverage, notes) and the page offscreen against the
fake MCP server; synthetic data only."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp

from gui.viewmodels import effective as ve
from gui.viewmodels import treemap

if HAVE_QT:
    from PySide6.QtCore import Qt

    from gui.app import SECTORS_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from gui.views.sectors import ExposureTreemapWidget
    from test_main_window import FakeBackend

    import fixtures

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")


def company(name, direct, etf, contributors=(), matched=True, ticker="", isin="", category=None, security_id=None, key=None):
    total = 10000.0
    category = category or ("direct_matched" if direct > 0 else "etf_only")
    out = {"key": key or "k:" + name, "name": name, "ticker": ticker, "isin": isin, "matched": matched, "security_id": security_id if security_id is not None else (1 if matched else None),
           "direct_value_eur": direct, "etf_value_eur": etf, "effective_value_eur": direct + etf, "direct_weight_pct": direct / total * 100, "etf_weight_pct": etf / total * 100,
           "effective_weight_pct": (direct + etf) / total * 100, "contributors": list(contributors), "etf_count": len(contributors), "match_bases": [], "category": category}
    return out


TSMC = company("TSMC (ADR)", 620.0, 111.92, [{"etf": "IS3N", "etf_security_id": 8, "value_eur": 111.92, "weight_in_etf_pct": 13.99, "match": "alias", "holding_name": "TAIWAN SEMICONDUCTOR MANUFACTURING", "etf_as_of": "2026-10-12"}],
               ticker="TSM", isin="TESTISIN-T1")
NVDA = company("NVIDIA", 0.0, 390.0, [{"etf": "SPXS", "etf_security_id": 9, "value_eur": 390.0, "weight_in_etf_pct": 7.71, "match": "isin", "holding_name": "NVIDIA ORD", "etf_as_of": "2026-08-28"}], matched=False)
PLAIN = company("Direct Only Inc", 300.0, 0.0, security_id=2)
UNCLEAR = company("Dup Corp", 0.0, 50.0, matched=False, category="ambiguous_unmatched")

ENVELOPE = {
    "ok": True, "operation": "get_effective_exposure", "as_of": "2026-10-13", "total_market_value_eur": 10000.0, "company_count": 4, "direct_company_count": 2, "etf_company_count": 3,
    "overlap_company_count": 1, "top": [TSMC, NVDA, PLAIN, UNCLEAR], "top_count": 4, "top_share_pct": 14.2, "unvalued": [], "warnings": ["HOLDINGS_NO_DATA: XYZ stays one unresolved block (no holdings for this ISIN)"],
    "etfs": [
        {"security_id": 8, "symbol": "IS3N", "name": "EM ETF", "value_eur": 800.0, "status": "APPLIED", "as_of": "2026-10-12", "provider": "iShares", "source": "https://example.invalid/is3n", "kind": "physical",
         "holdings_count": 3004, "equity_count": 2957, "equity_weight_pct": 97.97, "other_weight_pct": 2.03, "direct_matched_count": 1, "direct_matched_weight_pct": 13.99,
         "etf_only_count": 2947, "etf_only_weight_pct": 83.98, "ambiguous_unmatched_count": 9, "ambiguous_unmatched_weight_pct": 0.0,
         "isin_weight_pct": 0.0, "weights_total_pct": 98.51, "weights_drift_pct": None, "rounding_drift_eur": 0.0},
        {"security_id": 9, "symbol": "SPXS", "name": "S&P ETF", "value_eur": 5000.0, "status": "APPLIED", "as_of": "2026-08-28", "provider": "Invesco", "source": "https://example.invalid/spxs", "kind": "synthetic_index",
         "holdings_count": 503, "equity_count": 503, "equity_weight_pct": 102.43, "other_weight_pct": 0.0, "direct_matched_count": 4, "direct_matched_weight_pct": 2.2,
         "etf_only_count": 499, "etf_only_weight_pct": 100.23, "ambiguous_unmatched_count": 0, "ambiguous_unmatched_weight_pct": 0.0,
         "isin_weight_pct": 100.0, "weights_total_pct": 102.43, "weights_drift_pct": 2.43, "rounding_drift_eur": -121.5, "note": "Synthetic ETF: economic exposure = the index."},
        {"security_id": 7, "symbol": "XYZ", "name": "No data ETF", "value_eur": 100.0, "status": "NO_DATA", "detail": "no holdings for this ISIN", "as_of": None, "provider": None, "source": None, "kind": None},
    ],
    "coverage": {"etf_value_eur": 5900.0, "resolved_etf_value_eur": 5800.0, "resolved_pct": 98.3, "unresolved_etf_value_eur": 100.0, "other_value_eur": 16.2, "other_pct_of_total": 0.16,
                 "rounding_drift_eur": -121.5, "equity_holdings": 3460, "direct_matched_holdings": 5, "etf_only_holdings": 3446, "ambiguous_unmatched_holdings": 9},
    "buckets": {"direct_matched": {"value_eur": 920.0, "pct_of_total": 9.2, "company_count": 2}, "etf_only": {"value_eur": 8700.0, "pct_of_total": 87.0, "company_count": 4000},
                "ambiguous_unmatched": {"value_eur": 150.0, "pct_of_total": 1.5, "company_count": 9, "unresolved_etf_value_eur": 100.0, "ambiguous_value_eur": 50.0, "name_only_value_eur": 0.0},
                "other": {"value_eur": 16.2, "pct_of_total": 0.16}, "rounding_drift": {"value_eur": -121.5, "pct_of_total": -1.215}},
    "checks": {"companies_eur": 9883.8, "other_eur": 16.2, "unresolved_eur": 100.0, "context_total_eur": 10000.0, "reconciles": True},
}


class ViewModelTests(unittest.TestCase):
    def test_direct_etf_and_effective_are_separate_columns(self) -> None:
        vm = ve.build_effective(ENVELOPE)
        self.assertTrue(vm.available)
        self.assertEqual(ve.COMPANY_HEADERS, ["Unternehmen / Symbol", "Direkt €", "Direkt %", "ETF €", "ETF %", "Effektiv €", "Effektiv %", "Beitragende ETFs", "Datenstand"])
        tsmc, nvda, plain, unclear = vm.company_rows
        self.assertEqual(tsmc[:7], ["TSMC (ADR) · TSM", "620 €", "6,20 %", "112 €", "1,12 %", "732 €", "7,32 %"])
        self.assertEqual(tsmc[7:], ["IS3N 112 € (13,99 % im ETF)", "IS3N 12.10."])
        self.assertEqual(nvda[1:3], [ve.DASH, ve.DASH])  # no direct position: a dash, not 0
        self.assertEqual(plain[3:5], [ve.DASH, ve.DASH])
        self.assertEqual(plain[7:], [ve.DASH, ve.DASH])  # no ETF share: no contributors and no holdings stand
        self.assertEqual(nvda[1:3], [ve.DASH, ve.DASH])
        self.assertEqual(nvda[3:7], ["390 €", "3,90 %", "390 €", "3,90 %"])  # an ETF-only company: direct a dash, ETF = effective

    def test_a_company_in_both_is_marked_and_counted_once(self) -> None:
        vm = ve.build_effective(ENVELOPE)
        self.assertEqual(vm.company_levels, ["info", "", "", "warning"])  # the overlap row is marked, an unclear identity is highlighted
        self.assertEqual([c.overlap for c in vm.companies], [True, False, False, False])
        self.assertEqual(len({c.name for c in vm.companies}), len(vm.companies))

    def test_the_tooltip_names_every_contributing_etf_and_the_match(self) -> None:
        tip = ve.build_effective(ENVELOPE).companies[0].tip
        for text in ("TSMC (ADR) (TSM)", "ISIN: TESTISIN-T1", "Direkt: 620 € (6,20 %)", "Über ETFs: 112 € (1,12 %)", "Effektiv: 732 € (7,32 %)", "IS3N", "13,990 % des ETF",
                     "TAIWAN SEMICONDUCTOR MANUFACTURING", "geprüfte Zuordnung", "Datenstand 12.10.2026 (iShares)", "Quelle: https://example.invalid/is3n", "Einordnung: direkt gehalten", "Als Wertpapier des Portfolios erkannt: ja"):
            self.assertIn(text, tip)
        nvda_tip = ve.build_effective(ENVELOPE).companies[1].tip
        self.assertIn("Einordnung: nur über ETFs (eindeutig identifiziert)", nvda_tip)  # not being in the Security Master is no defect
        self.assertIn("nein (eigene Firma aus den ETF-Holdings, eindeutig identifiziert)", nvda_tip)
        self.assertIn("Einordnung: unklar / nicht zuordenbar", ve.build_effective(ENVELOPE).companies[3].tip)

    def test_the_etf_table_shows_coverage_matching_and_missing_data(self) -> None:
        vm = ve.build_effective(ENVELOPE)
        is3n, spxs, missing = vm.etf_rows
        self.assertEqual(is3n[:4], ["IS3N", "800 €", "angewendet", "12.10.2026"])
        self.assertEqual(is3n[5:], ["3004 (2957 Aktien)", "98,0 %", "14,0 % (1)", "84,0 % (2947)", "0,0 % (9)", "2,0 %"])
        self.assertEqual(spxs[10], "0,0 % / +2,43 %")  # other / the rounding drift of the published index list
        self.assertIn("synthetisch (Index-Zusammensetzung) · Invesco", spxs[4])
        self.assertEqual(missing[2:], ["keine Daten"] + [ve.DASH] * 8)
        self.assertEqual(vm.etf_levels, ["", "", "warning"])  # an ETF without data is highlighted
        self.assertIn("Rundungsabweichung 2,43 % über 100 %", vm.etf_tips[1])
        self.assertIn("nicht skaliert", vm.etf_tips[1])  # the weights are not normalised
        self.assertIn("Nicht aufgelöst", vm.etf_tips[2])

    def test_the_summary_has_coverage_matching_and_the_reconciliation(self) -> None:
        summary = dict(ve.build_effective(ENVELOPE).summary)
        self.assertEqual(summary["Firmen insgesamt"], "4 (direkt 2, über ETFs 3, beides 1)")
        self.assertEqual(summary["Top 4"], "14,2 % des Werts")
        self.assertNotIn("Rundungsabweichung", " ".join(c.name for c in ve.build_effective(ENVELOPE).companies))  # the drift is a reconciliation bucket, never a company
        self.assertEqual(summary["ETF-Holdings-Abdeckung"], "98 % des ETF-Werts")
        self.assertEqual(summary["Direkt gehalten (inkl. ETF-Anteil)"], "920 € (9,20 %)")
        self.assertEqual(summary["Nur über ETFs (eindeutig identifiziert)"], "8.700 € (87,00 %)")  # identified companies, not "unmatched"
        self.assertEqual(summary["Unklar / nicht zuordenbar"], "150 € (1,50 %)")
        self.assertEqual(summary["Sonstiges (Cash, Derivate, Rest)"], "16 € (0,16 %)")
        self.assertEqual(summary["Rundungsabweichung (Index-Liste)"], "-122 € (-1,22 %)")
        self.assertEqual(summary["Summen stimmen"], "ja")
        broken = dict(ENVELOPE, checks={"reconciles": False})
        self.assertEqual(dict(ve.build_effective(broken).summary)["Summen stimmen"], "NEIN")

    def test_warnings_and_the_explanation_are_notes(self) -> None:
        notes = ve.build_effective(ENVELOPE).notes
        self.assertTrue(any(n.startswith("HOLDINGS_NO_DATA") for n in notes))
        self.assertTrue(any("Index-Zusammensetzung" in n and "Risk Guard ist unverändert" in n for n in notes))

    def test_errors_and_junk_never_crash(self) -> None:
        self.assertFalse(ve.build_effective(None).available)
        self.assertEqual(ve.build_effective({"ok": False, "message": "kaputt"}).error, "kaputt")
        self.assertFalse(ve.build_effective({"ok": True}).available)
        empty = ve.build_effective({"ok": True, "top": [], "etfs": []})
        self.assertTrue(empty.available and empty.company_rows == [])
        junk = ve.build_effective({"ok": True, "top": ["x", {"name": "A", "effective_value_eur": "?"}, 5], "etfs": ["y", {"symbol": "Q", "status": "APPLIED"}], "coverage": 3})
        self.assertEqual(junk.company_rows, [])  # neither a string nor a row without its main figure is a company
        self.assertEqual(junk.etf_rows[0][0], "Q")


class HeadlineTests(unittest.TestCase):
    def test_the_headline_names_the_biggest_effective_position_with_its_split(self) -> None:
        vm = ve.build_effective(ENVELOPE)
        self.assertEqual(vm.headline, "TSMC (ADR) · TSM – 7,32 % effektiv")
        self.assertEqual(vm.headline_detail, "Direkt 6,20 % · ETF 1,12 % (Anteil direkt / ETF: 85 % / 15 %)")

    def test_the_biggest_position_is_by_effective_weight_not_by_list_order(self) -> None:
        env = dict(ENVELOPE, top=[PLAIN, NVDA, TSMC])
        self.assertTrue(ve.build_effective(env).headline.startswith("TSMC (ADR)"))
        etf_only = dict(ENVELOPE, top=[NVDA, PLAIN])
        vm = ve.build_effective(etf_only)
        self.assertEqual(vm.headline, "NVIDIA – 3,90 % effektiv")
        self.assertEqual(vm.headline_detail, "Direkt — · ETF 3,90 % (Anteil direkt / ETF: 0 % / 100 %)")  # no direct position: a dash, not 0

    def test_the_chips_count_the_companies_over_5_and_10_percent(self) -> None:
        big = company("Big Co", 1200.0, 0.0)  # 12 %
        mid = company("Mid Co", 400.0, 200.0)  # 6 %
        chips = {label: value for label, value, _ in ve.build_effective(dict(ENVELOPE, top=[big, mid, TSMC, NVDA])).chips}
        self.assertEqual((chips["Firmen über 5 %"], chips["Firmen über 10 %"]), ("3", "1"))  # Big, Mid and TSMC (7.3 %); only Big is over 10 %
        none = {label: value for label, value, _ in ve.build_effective(dict(ENVELOPE, top=[PLAIN])).chips}
        self.assertEqual((none["Firmen über 5 %"], none["Firmen über 10 %"]), ("0", "0"))

    def test_current_stale_and_missing_holdings_are_visible(self) -> None:
        fresh = {label: (value, level) for label, value, level in ve.build_effective(dict(ENVELOPE, etfs=[e for e in ENVELOPE["etfs"] if e["status"] == "APPLIED"])).chips}
        self.assertEqual(fresh["Holdings-Daten"], ("aktuell – ältester Stand 28.08.2026, 46 Tage", ""))
        stale_etfs = [dict(ENVELOPE["etfs"][0]), dict(ENVELOPE["etfs"][1], status="STALE", as_of="2026-05-01")]
        stale = {label: (value, level) for label, value, level in ve.build_effective(dict(ENVELOPE, etfs=stale_etfs)).chips}
        self.assertEqual(stale["Holdings-Daten"], ("veraltet: SPXS (Stand 01.05.2026)", "warning"))
        missing = {label: (value, level) for label, value, level in ve.build_effective(ENVELOPE).chips}
        self.assertEqual(missing["Holdings-Daten"][1], "blocker")
        self.assertIn("XYZ (keine Daten)", missing["Holdings-Daten"][0])

    def test_a_stale_etf_is_marked_in_the_company_row_and_the_tooltip(self) -> None:
        stale_etfs = [dict(e, status="STALE") if e["symbol"] == "IS3N" else e for e in ENVELOPE["etfs"]]
        vm = ve.build_effective(dict(ENVELOPE, etfs=stale_etfs))
        self.assertEqual(vm.company_rows[0][8], "IS3N 12.10. ⚠ veraltet")
        self.assertIn("veraltet", vm.companies[0].tip)

    def test_the_sort_keys_are_numbers_and_the_data_come_sorted_by_effective_weight(self) -> None:
        vm = ve.build_effective(ENVELOPE)
        self.assertEqual(vm.company_keys[0][6], vm.companies[0].effective_weight)
        self.assertIsNone(vm.company_keys[1][1])  # a dash sorts last, it is not 0
        self.assertIsNone(vm.company_keys[2][3])
        self.assertEqual([k[6] for k in vm.company_keys], sorted((k[6] for k in vm.company_keys), reverse=True))

    def test_the_drift_bucket_never_becomes_a_company(self) -> None:
        vm = ve.build_effective(ENVELOPE)
        self.assertIn("Rundungsabweichung (Index-Liste)", dict(vm.summary))  # shown as a bucket in the summary ...
        self.assertTrue(all("rundung" not in row[0].lower() and "drift" not in row[0].lower() for row in vm.company_rows))  # ... and in no company row
        self.assertEqual(len(vm.company_rows), len(ENVELOPE["top"]))


def tile_env(companies, **extra):
    """An effective-exposure answer around ``companies`` (total 10 000 EUR); the buckets carry a negative rounding drift that must never become a tile."""
    listed = sum(c["effective_value_eur"] for c in companies)
    env = dict(ENVELOPE, top=list(companies), top_count=len(companies), company_count=extra.pop("company_count", len(companies)),
               checks={"companies_eur": extra.pop("companies_eur", listed), "other_eur": 16.2, "rounding_drift_eur": -121.5, "unresolved_eur": 0.0, "context_total_eur": 10000.0, "reconciles": True},
               buckets={"direct_matched": {"value_eur": 1.0}, "etf_only": {"value_eur": 1.0}, "ambiguous_unmatched": {"value_eur": 0.0, "unresolved_etf_value_eur": extra.pop("unresolved", 0.0)},
                        "other": {"value_eur": extra.pop("other", 16.2), "pct_of_total": 0.16}, "rounding_drift": {"value_eur": -121.5, "pct_of_total": -1.215}})
    env.update(extra)
    return env


GOOGLE = company("Alphabet Inc.", 0.0, 276.0, [{"etf": "SPXS", "etf_security_id": 9, "value_eur": 276.0, "weight_in_etf_pct": 5.5, "match": "isin", "holding_name": "ALPHABET CL A ORD + CL C ORD",
                                                 "etf_as_of": "2026-08-28"}], matched=False, security_id=None, key="grp:alphabetinc")


class TreemapDataTests(unittest.TestCase):
    def test_a_company_is_exactly_one_tile_with_direct_and_etf_merged(self) -> None:
        tiles = [t for t in ve.build_effective(ENVELOPE).tiles if t.kind == "company"]
        tsmc = [t for t in tiles if t.label.startswith("TSMC")]
        self.assertEqual(len(tsmc), 1)
        self.assertAlmostEqual(tsmc[0].value, 620.0 + 111.92)  # the area is the effective value
        self.assertAlmostEqual(tsmc[0].weight_pct, 7.3192, places=3)
        self.assertEqual(len({t.label for t in tiles}), len(tiles))  # no company twice

    def test_an_etf_only_company_is_a_tile_with_its_etf_value_and_no_performance_colour(self) -> None:
        nvda = next(t for t in ve.build_effective(ENVELOPE).tiles if t.label == "NVIDIA")
        self.assertAlmostEqual(nvda.value, 390.0)
        self.assertAlmostEqual(nvda.weight_pct, 3.9)
        self.assertEqual(nvda.color, treemap.NEUTRAL)

    def test_the_tiles_are_sorted_by_effective_value_and_the_values_add_up(self) -> None:
        env = tile_env([company("Small", 100.0, 0.0), company("Large", 900.0, 100.0), company("Mid", 300.0, 0.0)])
        tiles = ve.build_effective(env).tiles
        self.assertEqual([t.label for t in tiles if t.kind == "company"], ["Large", "Mid", "Small"])
        self.assertAlmostEqual(sum(t.value for t in tiles if t.kind == "company"), 1400.0)  # no double counting: the same figures as the table
        companies = [t.value for t in tiles if t.kind == "company"]
        self.assertEqual(companies, sorted(companies, reverse=True))  # the largest tile first
        self.assertAlmostEqual(sum(t.value for t in tiles), 1400.0 + 16.2)  # the companies plus the separate "Sonstiges"

    def test_a_repeated_company_key_is_added_up_never_counted_twice(self) -> None:
        a = company("Alphabet Inc.", 0.0, 100.0, key="grp:alphabetinc", matched=False)
        b = company("Alphabet Inc.", 0.0, 50.0, key="grp:alphabetinc", matched=False)
        tiles = [t for t in ve.build_effective(tile_env([a, b, PLAIN])).tiles if t.kind == "company"]
        self.assertEqual([t.label for t in tiles].count("Alphabet Inc."), 1)
        self.assertAlmostEqual(next(t for t in tiles if t.label == "Alphabet Inc.").value, 150.0)

    def test_share_classes_of_one_issuer_are_one_tile(self) -> None:
        tiles = [t for t in ve.build_effective(tile_env([GOOGLE, PLAIN])).tiles if t.kind == "company"]
        self.assertEqual([t.label for t in tiles if "Alphabet" in t.label], ["Alphabet Inc."])  # A and C are one company: the backend's issuer group arrives as one row
        tip = next(t for t in tiles if t.label == "Alphabet Inc.").tip
        self.assertIn("Gattungen eines Emittenten", tip)

    def test_the_rounding_drift_is_never_a_tile_and_cash_is_a_separate_block(self) -> None:
        tiles = ve.build_effective(tile_env([TSMC, NVDA])).tiles
        self.assertTrue(all(t.value > 0 for t in tiles))  # the drift is negative
        self.assertFalse([t for t in tiles if "rund" in t.label.lower() or "drift" in t.label.lower()])
        other = [t for t in tiles if t.kind == "other"]
        self.assertEqual([t.label for t in other], ["Sonstiges"])
        self.assertAlmostEqual(other[0].value, 16.2)
        self.assertEqual([t.kind for t in tiles], ["company", "company", "other"])  # the cash block comes last, never between companies

    def test_the_remaining_small_companies_are_one_separate_block(self) -> None:
        env = tile_env([TSMC, NVDA], company_count=4000, companies_eur=9883.8)
        tiles = ve.build_effective(env).tiles
        rest = [t for t in tiles if t.kind == "rest"]
        self.assertEqual(len(rest), 1)
        self.assertEqual(rest[0].label, "Weitere 3998 Firmen")
        self.assertAlmostEqual(rest[0].value, 9883.8 - (731.92 + 390.0))
        self.assertAlmostEqual(sum(t.value for t in tiles if t.kind != "other"), 9883.8)  # all companies together, once

    def test_no_rest_block_when_every_company_is_listed(self) -> None:
        self.assertFalse([t for t in ve.build_effective(tile_env([TSMC, NVDA])).tiles if t.kind == "rest"])

    def test_unresolved_etfs_are_their_own_block(self) -> None:
        tiles = ve.build_effective(tile_env([TSMC], unresolved=100.0)).tiles
        self.assertEqual([t.label for t in tiles if t.kind == "unresolved"], ["ETFs ohne Holdings"])

    def test_the_colour_is_the_daily_change_of_the_companys_own_share_only(self) -> None:
        sector_env = {"sectors": [{"positions": [
            {"security_id": 1, "symbol": "TSM", "is_etf": False, "origin": "direct", "day_change_pct": 2.5},
            {"security_id": 9, "symbol": "SPXS", "is_etf": True, "origin": "direct", "day_change_pct": 5.0},  # an ETF held as one position
            {"security_id": 9, "symbol": "SPXS", "is_etf": True, "origin": "etf_lookthrough", "day_change_pct": 5.0},  # its look-through entry
            {"security_id": 7, "symbol": "NODATA", "is_etf": False, "origin": "direct", "day_change_pct": None}]}]}
        performance = ve.direct_performance(sector_env)
        self.assertEqual(performance, {1: 2.5})  # neither the ETF nor its look-through entries
        tiles = {t.label: t for t in ve.build_effective(ENVELOPE, performance=performance).tiles}
        self.assertEqual(tiles["TSMC (ADR) · TSM"].color, treemap.performance_color(2.5))
        self.assertEqual(tiles["NVIDIA"].color, treemap.NEUTRAL)  # held only through ETFs: no performance of its own
        self.assertEqual(tiles["Direct Only Inc"].color, treemap.NEUTRAL)  # direct, but the performance is not available
        self.assertEqual(ve.build_effective(ENVELOPE).tiles[0].color, treemap.NEUTRAL)  # without any sector answer everything stays neutral

    def test_an_etf_performance_is_never_taken_over_for_a_company_inside_it(self) -> None:
        etf_company = company("In The ETF", 0.0, 200.0, security_id=9, matched=True)  # a company matched to a security id that happens to be an ETF's id
        tiles = ve.build_effective(tile_env([etf_company]), performance={9: 5.0}).tiles
        self.assertEqual(tiles[0].color, treemap.NEUTRAL)  # no direct position of its own: the colour is not looked up at all

    def test_the_tile_tooltip_carries_the_figures_the_etfs_the_stand_and_the_hint(self) -> None:
        tile = next(t for t in ve.build_effective(ENVELOPE, performance={1: 2.5}).tiles if t.label.startswith("TSMC"))
        for text in ("Effektiv: 732 € (7,32 %)", "Direkt: 620 € (6,20 %)", "Über ETFs: 112 € (1,12 %)", "IS3N", "Datenstand 12.10.2026 (iShares)", "Quelle: https://example.invalid/is3n",
                     "Zuordnungshinweis", "geprüfte Zuordnung", "Tagesperformance der Aktie: +2,50 %"):
            self.assertIn(text, tile.tip)
        nvda = next(t for t in ve.build_effective(ENVELOPE).tiles if t.label == "NVIDIA")
        self.assertIn("nicht verfügbar (die Performance eines ETF wird nie als Unternehmensperformance verwendet)", nvda.tip)
        self.assertNotIn("Zuordnungshinweis", nvda.tip)  # an ISIN match needs no hint

    def test_without_data_there_are_no_tiles(self) -> None:
        self.assertEqual(ve.build_effective({"ok": False, "message": "kaputt"}).tiles, [])
        self.assertEqual(ve.build_effective({"ok": True, "top": [], "etfs": []}).tiles, [])


@QT
class ExposureTreemapPageTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.widget = ExposureTreemapWidget()
        self.addCleanup(self.widget.deleteLater)

    def test_the_areas_are_proportional_to_the_effective_values(self) -> None:
        env = tile_env([company("A", 6000.0, 0.0), company("B", 2500.0, 500.0), company("C", 600.0, 0.0), company("D", 300.0, 0.0)], other=100.0)
        tiles = ve.build_effective(env).tiles
        self.widget.resize(900, 500)
        self.widget.set_tiles(tiles)
        self.assertEqual(len(self.widget.rects), len(tiles))
        areas = [r[2] * r[3] for r in self.widget.rects]
        total = sum(areas)
        for tile, area in zip(tiles, areas):
            self.assertAlmostEqual(area / total, tile.value / sum(t.value for t in tiles), places=6)  # area = effective value, exactly
        self.assertTrue(all(r[2] >= 0 and r[3] >= 0 for r in self.widget.rects))  # no negative sizes
        for i, a in enumerate(self.widget.rects):  # and no overlap
            for b in self.widget.rects[i + 1:]:
                self.assertFalse(min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]) > 1e-6 and min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]) > 1e-6)
        self.assertGreaterEqual(areas[0], max(areas[1:]) - 1e-6)  # the largest company has the largest tile

    def test_it_paints_and_the_hover_text_is_the_tile_tooltip(self) -> None:
        tiles = ve.build_effective(ENVELOPE, performance={1: 2.5}).tiles
        self.widget.resize(900, 500)
        self.widget.set_tiles(tiles)
        self.assertFalse(self.widget.grab().isNull())
        x, y, w, h = self.widget.rects[0]
        from PySide6.QtCore import QPointF

        tip = self.widget.tile_at(QPointF(x + w / 2, y + h / 2))
        self.assertEqual(tip, tiles[0].tip)
        self.assertIsNone(self.widget.tile_at(QPointF(-5, -5)))

    def test_without_tiles_it_says_so_and_still_paints(self) -> None:
        self.widget.resize(300, 200)
        self.widget.set_tiles([])
        self.assertFalse(self.widget.grab().isNull())


@QT
class EffectivePageTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.calls_file = self.tmp / "calls.jsonl"
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        (self.tmp / "effective.json").write_text(json.dumps(ENVELOPE), encoding="utf-8")
        self.settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", ping_interval=2,
                                 extra_env={"FAKE_ORCH_FILE": str(self.tmp / "orch.json"), "FAKE_EFFECTIVE_FILE": str(self.tmp / "effective.json"), "FAKE_CALLS_FILE": str(self.calls_file)})
        self.runner = AsyncRunner()
        self.window = MainWindow(self.settings, self.mcp, FakeBackend(), self.runner, [], save_fn=lambda s: None,
                                 ollama_fn=lambda url: __import__("gui.services.ollama_client", fromlist=["x"]).OllamaStatus(True, "0.35.0", ["m1"]))
        self.addCleanup(self.cleanup)
        self.view = self.window.sectors

    def cleanup(self) -> None:
        self.window.close()
        self.runner.wait(5000)
        self.tmpdir.cleanup()

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()] if self.calls_file.exists() else []

    def open_page(self) -> None:
        self.window.resize(1400, 800)
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None))
        self.window.sidebar.setCurrentRow(SECTORS_PAGE)
        self.assertTrue(pump(15, lambda: self.view.company_table.rowCount() == 4), "the effective exposure did not load")

    def test_the_section_is_its_own_tab_and_the_sector_tab_is_unchanged(self) -> None:
        self.open_page()
        self.assertEqual([self.view.tabs.tabText(i) for i in range(self.view.tabs.count())], ["Sektoren", "Einzelwertrisiko"])
        self.assertEqual(self.view.tabs.currentIndex(), 0)  # the sector overview stays the first thing the page shows
        self.assertIsNotNone(self.view.treemap)
        self.assertIsNotNone(self.view.sector_table)

    def test_the_tables_show_the_companies_and_the_etf_coverage(self) -> None:
        self.open_page()
        table = self.view.company_table
        self.assertEqual([table.item(r, 0).text() for r in range(4)], ["TSMC (ADR) · TSM", "NVIDIA", "Direct Only Inc", "Dup Corp"])
        self.assertEqual(table.item(0, 6).text(), "7,32 %")
        self.assertEqual(table.item(3, 0).text(), "Dup Corp")
        self.assertEqual([table.item(0, c).text() for c in range(1, 7)], ["620 €", "6,20 %", "112 €", "1,12 %", "732 €", "7,32 %"])  # TSMC: direct / ETF / effective separated
        self.assertEqual([table.item(1, c).text() for c in range(1, 7)], ["—", "—", "390 €", "3,90 %", "390 €", "3,90 %"])  # NVIDIA-like: ETF only
        self.assertIn("geprüfte Zuordnung", table.item(0, 3).toolTip())
        etf = self.view.effective_etf_table
        self.assertEqual([etf.item(r, 0).text() for r in range(3)], ["IS3N", "SPXS", "XYZ"])
        self.assertEqual(etf.item(2, 2).text(), "keine Daten")
        self.assertIn("Rundungsabweichung", etf.item(1, 0).toolTip())
        self.assertIn("HOLDINGS_NO_DATA", self.view.effective_notes.text())

    def test_the_effective_columns_are_highlighted_and_the_headline_is_shown(self) -> None:
        self.open_page()
        table = self.view.company_table
        for column in (5, 6):
            self.assertTrue(table.item(0, column).font().bold(), column)
            self.assertNotEqual(table.item(0, column).background().color().name(), table.item(0, 2).background().color().name())
        self.assertFalse(table.item(0, 2).font().bold())  # direct and ETF stay plain
        text = self.view.effective_headline.text()
        self.assertIn("Größte effektive Einzelposition", text)
        self.assertIn("TSMC (ADR) · TSM – 7,32 % effektiv", text)
        self.assertIn("Direkt 6,20 % · ETF 1,12 %", text)
        chips = [self.view.effective_chips.itemAt(i).widget().text() for i in range(self.view.effective_chips.count()) if self.view.effective_chips.itemAt(i).widget() is not None]
        self.assertTrue(any("Firmen über 5 %" in c for c in chips) and any("Firmen über 10 %" in c for c in chips) and any("Holdings-Daten" in c for c in chips))

    def test_the_default_sort_is_effective_weight_descending_and_the_user_can_sort(self) -> None:
        self.open_page()
        table = self.view.company_table
        weights = [float(table.item(r, 6).text().replace(" %", "").replace(",", ".")) for r in range(table.rowCount())]
        self.assertEqual(weights, sorted(weights, reverse=True))
        table.sortItems(4, Qt.SortOrder.AscendingOrder)  # the user sorts by the ETF share; "—" (no ETF share) is sorted last, never as 0
        self.assertEqual(table.item(table.rowCount() - 1, 4).text(), "—")
        self.view.show_effective(ve.build_effective(ENVELOPE))  # a refresh keeps that choice
        self.assertEqual(table.horizontalHeader().sortIndicatorSection(), 4)

    def test_a_tooltip_names_the_etf_the_source_and_the_stand(self) -> None:
        self.open_page()
        tip = self.view.company_table.item(0, 1).toolTip()
        for text in ("IS3N", "Datenstand 12.10.2026 (iShares)", "Quelle: https://example.invalid/is3n"):
            self.assertIn(text, tip)
        self.assertEqual(self.view.company_table.item(0, 8).text(), "IS3N 12.10.")

    def test_the_exposure_treemap_is_in_the_tab_and_the_sector_treemap_stays_in_the_sector_tab(self) -> None:
        self.open_page()
        self.assertIsNot(self.view.exposure_treemap, self.view.treemap)  # an own data source: the sector treemap is not involved
        tiles = self.view.exposure_treemap.tiles
        self.assertEqual([t.label for t in tiles if t.kind == "company"], ["TSMC (ADR) · TSM", "NVIDIA", "Direct Only Inc", "Dup Corp"])  # one tile per company; cash and unresolved ETFs are separate blocks
        self.assertEqual(sorted(t.kind for t in tiles if t.kind != "company"), ["other", "unresolved"])
        self.assertEqual(sum(1 for t in self.view.exposure_treemap.tiles if t.label.startswith("TSMC")), 1)
        self.assertIs(self.view.tabs.widget(0).findChild(type(self.view.treemap)), self.view.treemap)  # the sector treemap is still on the Sektoren tab
        self.view.tabs.setCurrentIndex(1)
        self.assertFalse(self.view.grab().isNull())

    def test_opening_the_page_only_reads(self) -> None:
        self.open_page()
        tools = [c["tool"] for c in self.calls()]
        self.assertEqual(tools.count("get_effective_exposure"), 1)
        self.assertEqual(next(c for c in self.calls() if c["tool"] == "get_effective_exposure")["arguments"]["top"], 40)
        self.assertEqual([t for t in tools if t.startswith(("set_", "add_", "remove_", "onboard", "sql", "approve"))], [])

    def test_an_error_keeps_the_page_usable_and_the_tab_paints(self) -> None:
        self.open_page()
        self.view.show_effective(ve.build_effective({"ok": False, "message": "kaputt"}))
        self.assertIn("kaputt", self.view.effective_message.text())
        self.assertEqual((self.view.company_table.rowCount(), self.view.effective_etf_table.rowCount()), (0, 0))
        self.view.tabs.setCurrentIndex(1)
        self.assertFalse(self.view.grab().isNull())


if __name__ == "__main__":
    unittest.main()
