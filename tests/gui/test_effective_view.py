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

if HAVE_QT:
    from gui.app import SECTORS_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from test_main_window import FakeBackend

    import fixtures

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")


def company(name, direct, etf, contributors=(), matched=True, ticker="", isin="", category=None):
    total = 10000.0
    category = category or ("direct_matched" if direct > 0 else "etf_only")
    return {"key": "k:" + name, "name": name, "ticker": ticker, "isin": isin, "matched": matched, "security_id": 1 if matched else None,
            "direct_value_eur": direct, "etf_value_eur": etf, "effective_value_eur": direct + etf, "direct_weight_pct": direct / total * 100, "etf_weight_pct": etf / total * 100,
            "effective_weight_pct": (direct + etf) / total * 100, "contributors": list(contributors), "etf_count": len(contributors), "match_bases": [], "category": category}


TSMC = company("TSMC (ADR)", 620.0, 111.92, [{"etf": "IS3N", "etf_security_id": 8, "value_eur": 111.92, "weight_in_etf_pct": 13.99, "match": "alias", "holding_name": "TAIWAN SEMICONDUCTOR MANUFACTURING", "etf_as_of": "2026-10-12"}],
               ticker="TSM", isin="TESTISIN-T1")
NVDA = company("NVIDIA", 0.0, 390.0, [{"etf": "SPXS", "etf_security_id": 9, "value_eur": 390.0, "weight_in_etf_pct": 7.71, "match": "isin", "holding_name": "NVIDIA ORD", "etf_as_of": "2026-08-28"}], matched=False)
PLAIN = company("Direct Only Inc", 300.0, 0.0)
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
        self.assertEqual(ve.COMPANY_HEADERS, ["Firma", "Direkt €", "Direkt %", "ETF €", "ETF %", "Effektiv €", "Effektiv %", "ETF-Beiträge"])
        tsmc, nvda, plain, unclear = vm.company_rows
        self.assertEqual(tsmc[:7], ["TSMC (ADR)", "620 €", "6,20 %", "112 €", "1,12 %", "732 €", "7,32 %"])
        self.assertEqual(tsmc[7], "IS3N 112 € (13,99 % im ETF)")
        self.assertEqual(nvda[1:3], [ve.DASH, ve.DASH])  # no direct position: a dash, not 0
        self.assertEqual(plain[3:5], [ve.DASH, ve.DASH])
        self.assertEqual(plain[7], ve.DASH)

    def test_a_company_in_both_is_marked_and_counted_once(self) -> None:
        vm = ve.build_effective(ENVELOPE)
        self.assertEqual(vm.company_levels, ["info", "", "", "warning"])  # the overlap row is marked, an unclear identity is highlighted
        self.assertEqual([c.overlap for c in vm.companies], [True, False, False, False])
        self.assertEqual(len({c.name for c in vm.companies}), len(vm.companies))

    def test_the_tooltip_names_every_contributing_etf_and_the_match(self) -> None:
        tip = ve.build_effective(ENVELOPE).companies[0].tip
        for text in ("TSMC (ADR) (TSM)", "ISIN: TESTISIN-T1", "Direkt: 620 € (6,20 %)", "Über ETFs: 112 € (1,12 %)", "Effektiv: 732 € (7,32 %)", "IS3N", "13,990 % des ETF",
                     "TAIWAN SEMICONDUCTOR MANUFACTURING", "geprüfte Zuordnung", "Stand 12.10.2026", "Einordnung: direkt gehalten", "Als Wertpapier des Portfolios erkannt: ja"):
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
        self.assertEqual([table.item(r, 0).text() for r in range(4)], ["TSMC (ADR)", "NVIDIA", "Direct Only Inc", "Dup Corp"])
        self.assertEqual(table.item(0, 6).text(), "7,32 %")
        self.assertEqual(table.item(3, 0).text(), "Dup Corp")
        self.assertIn("geprüfte Zuordnung", table.item(0, 3).toolTip())
        etf = self.view.effective_etf_table
        self.assertEqual([etf.item(r, 0).text() for r in range(3)], ["IS3N", "SPXS", "XYZ"])
        self.assertEqual(etf.item(2, 2).text(), "keine Daten")
        self.assertIn("Rundungsabweichung", etf.item(1, 0).toolTip())
        self.assertIn("HOLDINGS_NO_DATA", self.view.effective_notes.text())

    def test_opening_the_page_only_reads(self) -> None:
        self.open_page()
        tools = [c["tool"] for c in self.calls()]
        self.assertEqual(tools.count("get_effective_exposure"), 1)
        self.assertEqual(next(c for c in self.calls() if c["tool"] == "get_effective_exposure")["arguments"]["top"], 25)
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
