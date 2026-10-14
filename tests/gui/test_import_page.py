"""The "Import" page: view model (difference, reconciliation, German texts) and the page against the fake MCP server (check, then import with the token of that check); synthetic data only."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp

from gui.services import tool_policy as tp
from gui.viewmodels import transaction_import as vm

if HAVE_QT:
    from gui.app import IMPORT_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from test_main_window import FakeBackend

    import fixtures

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")

CLOSING = {"status": "MATCH", "counts": {"securities": 3, "transactions": 12, "positions_held": 2, "file_rows": 12, "file_securities": 3},
           "portfolio_value": {"total_market_value_eur": 12345.6, "status": "available", "as_of": "2026-10-14"},
           "file_vs_database": {"share_differences": [], "share_difference_count": 0, "file_rows_not_in_database": [], "file_rows_not_in_database_count": 0, "database_rows_not_in_file": 0},
           "unresolved": [], "unresolved_count": 0, "positions_vs_transactions": {"drift": [], "drift_count": 0, "checked": 3}, "integrity_check": ["ok"], "foreign_key_check": []}
DIFF = {"classification_counts": {"duplicate": 9, "new": 3}, "new_securities": [{"security_id": 90, "symbol": None, "isin": "TESTISIN-T1", "wkn": "TST123", "name": "Test Corp", "currency": "EUR",
                                                                                "asset_type": "stock", "asset_type_defaulted": True}],
        "masterdata_updates": [{"security_id": 5, "symbol": "OLD", "field": "isin", "from": None, "to": "TESTISIN-T2"}],
        "masterdata_conflicts": [{"security_id": 6, "symbol": "SIX", "field": "isin", "database": "TESTISIN-D", "file": "TESTISIN-F", "reason": "the database value differs from the file; not overwritten"}],
        "new_transactions": {"count": 3, "by_type": {"BUY": 2, "SELL": 1}}, "existing_transactions": 9, "historical_skipped": 0,
        "problem_rows": [{"row": 7, "classification": "CONFLICT", "reason": "identical row occurs more than once in this file", "identifier": "TESTISIN-X", "holding": "Dup Corp"}], "problem_row_count": 1,
        "position_changes": [{"security_id": 90, "symbol": "Test Corp", "kind": "new", "shares_before": 0.0, "shares_after": 4.0, "cost_basis_before": 0.0, "cost_basis_after": 200.0},
                             {"security_id": 5, "symbol": "OLD", "kind": "changed", "shares_before": 10.0, "shares_after": 12.5, "cost_basis_before": 100.0, "cost_basis_after": 130.0}],
        "open_items": [{"code": "STRATEGY_ASSIGNMENT_REQUIRED", "security_id": 90, "symbol": "Test Corp", "detail": ""},
                       {"code": "MASTERDATA_CONFLICT", "security_id": 6, "symbol": "SIX", "detail": "isin: database / file"}]}


def envelope(mode: str = "check", written: bool = False, **more) -> dict:
    data = {"ok": True, "mode": mode, "format": "parqet", "source_file": "x.csv", "source_row_count": 12, "plan_token": "tok-check-1", "written": written, "would_write": mode == "check",
            "backup_path": "C:/backup/trading.db.parqet-import.bak" if written else None, "summary": {}, "open_items": DIFF["open_items"], "diff": copy.deepcopy(DIFF), "closing": copy.deepcopy(CLOSING)}
    data.update(more)
    return data


class ViewModelTests(unittest.TestCase):
    def test_the_arguments_are_exactly_what_was_chosen(self) -> None:
        self.assertEqual(vm.request_arguments(" a.csv ", "Parqet-Export", True, False), {"file_path": "a.csv", "format": "parqet", "create_securities": True, "include_historical": False})
        self.assertEqual(vm.request_arguments("b.csv", "Kanonisches CSV", False, True)["format"], "canonical")

    def test_a_check_shows_the_difference_and_allows_the_import(self) -> None:
        view = vm.build(True, envelope())
        self.assertTrue(view.ok and view.can_import)
        self.assertEqual(view.plan_token, "tok-check-1")
        self.assertIn("3 neue Transaktionen", view.headline)
        self.assertIn("Es wurde nichts geschrieben", view.headline)
        self.assertIn("2 Kauf, 1 Verkauf", " ".join(view.lines))
        self.assertEqual(view.securities[0][:3], ["Test Corp", "TESTISIN-T1", "TST123"])
        self.assertIn("angelegt als Aktie", view.securities[0][4])  # the default is declared
        self.assertEqual([r[4] for r in view.masterdata], ["wird ergänzt", "Konflikt – nicht überschrieben"])
        self.assertEqual(view.positions, [["Test Corp", "neu", "0", "4"], ["OLD", "geändert", "10", "12,5"]])
        self.assertEqual(view.problems[0][:3], ["7", "Dup Corp", "Konflikt"])
        self.assertEqual(view.level, "warning")  # conflicts and problem rows are not hidden
        self.assertIn("Übereinstimmung", view.closing[0])
        self.assertIn("12.345,60 EUR", view.closing[2])
        self.assertIn("integrity_check ok", view.closing[4])

    def test_open_items_are_readable_without_raw_codes(self) -> None:
        view = vm.build(True, envelope())
        for row in view.open_items:
            self.assertNotRegex(row[1], r"\b[A-Z]+_[A-Z_]+\b")
        self.assertEqual(view.open_items[0][1], "Strategiezuordnung fehlt")
        self.assertEqual(view.open_items[1][1], "Stammdaten-Konflikt")

    def test_nothing_to_do_is_said_plainly_and_blocks_the_import(self) -> None:
        data = envelope(would_write=False)
        data["diff"].update(new_securities=[], masterdata_updates=[], masterdata_conflicts=[], new_transactions={"count": 0, "by_type": {}}, problem_rows=[], problem_row_count=0, position_changes=[], open_items=[])
        view = vm.build(True, data)
        self.assertFalse(view.can_import)
        self.assertTrue(view.headline.startswith("Keine Änderungen"))
        self.assertEqual(view.level, "info")

    def test_differences_between_file_and_database_are_listed_with_the_reason(self) -> None:
        data = envelope(mode="write", written=True)
        data["closing"].update(status="DIFFERENCES")
        data["closing"]["file_vs_database"].update(share_difference_count=1, share_differences=[{"security_id": 3, "security": "Over Sold", "file_net_shares": -640.0, "database_shares": 0.0, "delta": 640.0, "note": "x"}])
        view = vm.build(True, data)
        self.assertFalse(view.can_import)  # an import was just done
        self.assertTrue(view.written)
        self.assertTrue(view.headline.startswith("Import abgeschlossen"))
        self.assertEqual(view.differences, [["Over Sold", "-640", "0", "Die Datei verkauft mehr Stücke, als sie kauft; die Position wird nie negativ."]])
        self.assertIn("Abweichungen", view.closing[0])

    def test_a_failed_database_check_is_a_blocker(self) -> None:
        data = envelope(mode="write", written=True)
        data["closing"]["integrity_check"] = ["row 1 missing"]
        self.assertEqual(vm.build(True, data).level, "blocker")

    def test_errors_become_a_sentence(self) -> None:
        for res_ok, data, message in ((False, None, "Zeitüberschreitung"), (True, {"ok": False, "message": "file not found"}, ""), (True, {"ok": True}, "")):
            view = vm.build(res_ok, data, message)
            self.assertFalse(view.ok or view.can_import)
            self.assertEqual(view.level, "blocker")
            self.assertTrue(view.headline.startswith("Nicht möglich:"))

    def test_policy_the_two_tools_are_gui_actions_only(self) -> None:
        for name in ("check_transaction_import", "import_transactions"):
            self.assertTrue(tp.is_gui_action(name))
            self.assertFalse(tp.is_read(name))  # never offered to the chat
            with self.assertRaises(tp.ToolDenied):
                tp.require_read(name)


@QT
class ImportPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.calls_file = self.tmp / "calls.jsonl"
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        (self.tmp / "check.json").write_text(json.dumps(envelope()), encoding="utf-8")
        (self.tmp / "done.json").write_text(json.dumps(envelope(mode="write", written=True)), encoding="utf-8")
        (self.tmp / "export.csv").write_text("x", encoding="utf-8")
        self.settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", ping_interval=2,
                                 extra_env={"FAKE_ORCH_FILE": str(self.tmp / "orch.json"), "FAKE_CALLS_FILE": str(self.calls_file), "FAKE_IMPORT_FILE": str(self.tmp / "check.json"),
                                            "FAKE_IMPORT_DONE_FILE": str(self.tmp / "done.json")})
        self.runner = AsyncRunner()
        self.window = MainWindow(self.settings, self.mcp, FakeBackend(), self.runner, [], save_fn=lambda s: None,
                                 ollama_fn=lambda url: __import__("gui.services.ollama_client", fromlist=["x"]).OllamaStatus(True, "0.35.0", ["m1"]))
        self.addCleanup(self.cleanup)
        self.page = self.window.import_page
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None), "the dashboard data did not arrive")
        self.window.sidebar.setCurrentRow(IMPORT_PAGE)

    def cleanup(self) -> None:
        self.window.close()
        self.runner.wait(5000)
        self.tmpdir.cleanup()

    def calls(self, tool: str) -> list[dict]:
        rows = [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()] if self.calls_file.exists() else []
        return [c["arguments"] for c in rows if c["tool"] == tool]

    def check(self) -> None:
        self.page.path.setText(str(self.tmp / "export.csv"))
        self.page.check.click()
        self.assertTrue(pump(15, lambda: self.page.run.isEnabled()), self.page.message.text())

    def test_the_page_is_the_import_page_and_nothing_is_called_by_itself(self) -> None:
        self.assertIs(self.window.stack.currentWidget(), self.page)
        self.assertFalse(self.page.run.isEnabled())
        self.assertEqual(self.calls("check_transaction_import") + self.calls("import_transactions"), [])

    def test_check_then_import_sends_the_token_of_that_check(self) -> None:
        self.check()
        self.assertEqual(len(self.calls("check_transaction_import")), 1)
        self.assertEqual(self.calls("import_transactions"), [])  # a check writes nothing
        self.assertEqual(self.page.tables["positions"].rowCount(), 2)
        self.assertEqual(self.page.tables["securities"].rowCount(), 1)
        self.assertIn("Übereinstimmung", self.page.closing.text())
        asked = []
        self.page.confirm_import = lambda headline: asked.append(headline) or True
        self.page.run.click()
        self.assertTrue(pump(15, lambda: bool(self.calls("import_transactions"))))
        self.assertTrue(pump(15, lambda: "Import abgeschlossen" in self.page.message.text()), self.page.message.text())
        sent = self.calls("import_transactions")[0]
        self.assertEqual((sent["plan_token"], sent["create_securities"], sent["format"]), ("tok-check-1", True, "parqet"))
        self.assertEqual(len(asked), 1)
        self.assertFalse(self.page.run.isEnabled())  # the next import needs the next check

    def test_declining_the_question_sends_nothing(self) -> None:
        self.check()
        self.page.confirm_import = lambda headline: False
        self.page.run.click()
        self.assertTrue(pump(3, lambda: False) is False)
        self.assertEqual(self.calls("import_transactions"), [])
        self.assertTrue(self.page.run.isEnabled())

    def test_editing_the_form_after_a_check_disables_the_import(self) -> None:
        self.check()
        self.page.historical.setChecked(True)
        self.assertFalse(self.page.run.isEnabled())
        self.page.create.setChecked(False)
        self.page.create.setChecked(True)
        self.assertFalse(self.page.run.isEnabled())

    def test_a_failing_check_shows_a_sentence_and_enables_nothing(self) -> None:
        self.page.path.setText(str(self.tmp / "broken.csv"))
        self.page.check.click()
        self.assertTrue(pump(15, lambda: "Nicht möglich" in self.page.message.text()), self.page.message.text())
        self.assertFalse(self.page.run.isEnabled())
        self.assertTrue(self.page.check.isEnabled())

    def test_a_stale_token_is_refused_by_the_tool_and_shown(self) -> None:
        self.check()
        self.page._token = "tok-old"
        self.page.confirm_import = lambda headline: True
        self.page.run.click()
        self.assertTrue(pump(15, lambda: "Nicht möglich" in self.page.message.text()), self.page.message.text())
        self.assertIn("stale", self.page.message.text())
        self.assertFalse(self.page.run.isEnabled())

    def test_without_a_file_the_check_does_not_start(self) -> None:
        self.page.check.click()
        self.assertIn("Bitte eine Datei wählen", self.page.message.text())
        self.assertEqual(self.calls("check_transaction_import"), [])


if __name__ == "__main__":
    unittest.main()
