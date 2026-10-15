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
        "masterdata_conflicts": [{"security_id": 6, "symbol": "SIX", "field": "isin", "database": "TESTID-D", "file": "TESTID-F", "reason": "the database value differs from the file; not overwritten"}],
        "alias_candidates": [{"identifier": "TESTID-F", "security": "SIX", "security_id": 6, "name": "Six Corp", "wkn": "TST600", "database_isin": "TESTID-D", "same_wkn": True}],
        "strategy_needed": [{"security_id": 90, "symbol": None, "name": "Test Corp", "key": "TESTKEY-1", "shares": 4.0}],
        "identifier_aliases": [],
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
        self.assertEqual(vm.request_arguments(" a.csv ", "Parqet-Export", True, False),
                         {"file_path": "a.csv", "format": "parqet", "create_securities": True, "include_historical": False, "strategies": [], "accept_aliases": [], "start_campaigns": False})
        decided = vm.request_arguments("a.csv", "Parqet-Export", True, False, {"K2": "none", "K1": "swing", "K3": "long_term"}, {"ID-1": "SIX"})
        self.assertEqual((decided["strategies"], decided["accept_aliases"]), (["K1=swing", "K3=long_term"], ["ID-1=SIX"]))  # "none" is a decision of the page, not an argument
        self.assertIs(decided["start_campaigns"], False)  # the page never starts a campaign
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

    def test_the_open_decisions_are_taken_from_the_check(self) -> None:
        view = vm.build(True, envelope())
        self.assertEqual(view.alias_infos["TESTID-F"]["security"], "SIX")
        self.assertTrue(view.alias_infos["TESTID-F"]["same_wkn"])
        self.assertEqual(view.alias_infos["TESTID-F"]["status"], "OFFEN")
        self.assertEqual(view.strategy_infos["TESTKEY-1"]["name"], "Test Corp")
        self.assertEqual(view.refused, [])

    def test_a_refused_alias_blocks_the_import_and_says_why(self) -> None:
        data = envelope()
        data["diff"]["alias_candidates"] = []
        data["diff"]["identifier_aliases"] = [{"identifier": "TESTID-F", "security": "NOSUCH", "security_id": None, "symbol": None, "status": "REFUSED", "reason": "no security with that symbol, ISIN or id"}]
        view = vm.build(True, data)
        self.assertFalse(view.can_import)
        self.assertEqual(view.refused, ["TESTID-F"])
        self.assertEqual(view.level, "blocker")
        self.assertIn("Zuordnung abgelehnt", view.headline)
        self.assertEqual(view.alias_infos["TESTID-F"]["reason"], "no security with that symbol, ISIN or id")

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
        token = self.page._token
        asked = []
        self.page.confirm_import = lambda headline: asked.append(headline) or True
        self.page.run.click()
        self.assertTrue(pump(15, lambda: bool(self.calls("import_transactions"))))
        self.assertTrue(pump(15, lambda: "Import abgeschlossen" in self.page.message.text()), self.page.message.text())
        sent = self.calls("import_transactions")[0]
        self.assertEqual((sent["plan_token"], sent["create_securities"], sent["format"]), (token, True, "parqet"))
        self.assertTrue(token.startswith("tok-"))
        self.assertEqual((sent["strategies"], sent["accept_aliases"], sent["start_campaigns"]), ([], [], False))
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

    # -- decisions on the page -------------------------------------------------------------------------------------
    def all_calls(self) -> list[dict]:
        return self.calls("check_transaction_import") + self.calls("import_transactions")

    def choose(self, kind: str, key: str, text: str) -> None:
        self.page.decision_boxes[(kind, key)].setCurrentText(text)

    def recheck(self) -> None:
        self.page.run.setEnabled(False)
        self.page.check.click()
        self.assertTrue(pump(15, lambda: self.page.run.isEnabled() or "abgelehnt" in self.page.message.text()), self.page.message.text())

    def test_the_open_points_are_shown_as_decisions(self) -> None:
        self.check()
        table = self.page.decisions
        self.assertFalse(table.isHidden())
        self.assertEqual(table.rowCount(), 2)
        rows = {table.item(r, 0).text(): [table.item(r, c).text() for c in (1, 2, 4)] for r in range(2)}
        self.assertEqual(rows["Kennung"][0], "SIX – Six Corp")
        self.assertIn("Parqet-ID TESTID-F ↔ Datenbank TESTID-D", rows["Kennung"][1])
        self.assertIn("WKN TST600", rows["Kennung"][1])
        self.assertIn("gleiche WKN", rows["Kennung"][1])
        self.assertEqual(rows["Kennung"][2], "offen")
        self.assertEqual(rows["Strategie"][0], "Test Corp")
        self.assertIn("4 Stück neu gehalten", rows["Strategie"][1])
        self.assertEqual([self.page.decision_boxes[("strategy", "TESTKEY-1")].itemText(i) for i in range(4)], ["Offen", "Swing", "Long-Term", "Keine"])
        self.assertEqual(self.page.decision_boxes[("strategy", "TESTKEY-1")].currentText(), "Offen")  # nothing is decided by itself
        self.assertEqual(self.all_calls()[0]["strategies"], [])

    def test_an_alias_decision_needs_a_new_check_and_gets_a_new_token(self) -> None:
        self.check()
        old_token = self.page._token
        self.choose("alias", "TESTID-F", "Alias übernehmen")
        self.assertFalse(self.page.run.isEnabled())  # the plan changed: the old token is worth nothing
        self.assertIsNone(self.page._token)
        self.assertIn("erneut prüfen", self.page.message.text())
        self.assertEqual(len(self.calls("check_transaction_import")), 1)  # choosing sends nothing by itself
        self.recheck()
        self.assertEqual(self.calls("check_transaction_import")[-1]["accept_aliases"], ["TESTID-F=SIX"])
        self.assertNotEqual(self.page._token, old_token)
        row = [self.page.decisions.item(r, 4).text() for r in range(self.page.decisions.rowCount()) if self.page.decisions.item(r, 0).text() == "Kennung"][0]
        self.assertEqual(row, "wird übernommen")
        self.assertEqual(self.page.decision_boxes[("alias", "TESTID-F")].currentText(), "Alias übernehmen")  # the choice survives the check
        self.page.confirm_import = lambda headline: True
        self.page.run.click()
        self.assertTrue(pump(15, lambda: "Import abgeschlossen" in self.page.message.text()), self.page.message.text())
        sent = self.calls("import_transactions")[0]
        self.assertEqual(sent["accept_aliases"], ["TESTID-F=SIX"])
        self.assertTrue(sent["plan_token"].startswith("tok-"))

    def test_a_refused_alias_is_shown_and_cannot_be_imported(self) -> None:
        data = envelope()
        data["diff"]["alias_candidates"][0]["security"] = "NOSUCH"
        (self.tmp / "check.json").write_text(json.dumps(data), encoding="utf-8")
        self.check()
        self.choose("alias", "TESTID-F", "Alias übernehmen")
        self.page.check.click()
        self.assertTrue(pump(15, lambda: "Zuordnung abgelehnt" in self.page.message.text()), self.page.message.text())
        self.assertFalse(self.page.run.isEnabled())
        status = [self.page.decisions.item(r, 4).text() for r in range(self.page.decisions.rowCount()) if self.page.decisions.item(r, 0).text() == "Kennung"][0]
        self.assertIn("abgelehnt: no security with that symbol, ISIN or id", status)
        self.choose("alias", "TESTID-F", "Offen")  # taking the decision back lets the check pass again
        self.recheck()
        self.assertTrue(self.page.run.isEnabled())
        self.assertEqual(self.calls("check_transaction_import")[-1]["accept_aliases"], [])
        self.assertEqual(self.calls("import_transactions"), [])

    def test_the_chosen_strategy_is_sent_and_no_campaign_is_started(self) -> None:
        self.check()
        self.choose("strategy", "TESTKEY-1", "Swing")
        self.assertFalse(self.page.run.isEnabled())
        self.recheck()
        last = self.calls("check_transaction_import")[-1]
        self.assertEqual((last["strategies"], last["start_campaigns"]), (["TESTKEY-1=swing"], False))
        status = [self.page.decisions.item(r, 4).text() for r in range(self.page.decisions.rowCount()) if self.page.decisions.item(r, 0).text() == "Strategie"][0]
        self.assertEqual(status, "wird Swing zugeordnet (ohne Campaign)")
        self.choose("strategy", "TESTKEY-1", "Long-Term")
        self.recheck()
        self.assertEqual(self.calls("check_transaction_import")[-1]["strategies"], ["TESTKEY-1=long_term"])
        self.page.confirm_import = lambda headline: True
        self.page.run.click()
        self.assertTrue(pump(15, lambda: "Import abgeschlossen" in self.page.message.text()), self.page.message.text())
        self.assertEqual(self.calls("import_transactions")[0]["strategies"], ["TESTKEY-1=long_term"])
        self.assertTrue(all(c["start_campaigns"] is False for c in self.all_calls()))  # no call of the page ever allows a campaign start

    def test_none_is_a_decision_and_the_import_stays_possible(self) -> None:
        self.check()
        self.choose("strategy", "TESTKEY-1", "Keine")
        self.recheck()
        self.assertEqual(self.calls("check_transaction_import")[-1]["strategies"], [])  # nothing is sent for "Keine"
        status = [self.page.decisions.item(r, 4).text() for r in range(self.page.decisions.rowCount()) if self.page.decisions.item(r, 0).text() == "Strategie"][0]
        self.assertEqual(status, "bewusst ohne Strategie")
        self.assertTrue(self.page.run.isEnabled())

    def test_a_stale_token_after_a_decision_is_refused_by_the_tool(self) -> None:
        self.check()
        old = self.page._token
        self.choose("strategy", "TESTKEY-1", "Swing")
        self.page.confirm_import = lambda headline: True
        self.page.run.click()  # the page itself refuses: no valid check for this form
        self.assertEqual(self.calls("import_transactions"), [])
        self.assertIn("erneut prüfen", self.page.message.text().replace("Bitte zuerst prüfen.", "erneut prüfen"))
        self.page._checked, self.page._token = self.page.arguments(), old  # even a forged state is stopped by the server: the token belongs to other arguments
        self.page.run.setEnabled(True)
        self.page.run.click()
        self.assertTrue(pump(15, lambda: "stale" in self.page.message.text()), self.page.message.text())

    def test_another_file_or_option_drops_the_decisions(self) -> None:
        self.check()
        self.choose("strategy", "TESTKEY-1", "Swing")
        self.choose("alias", "TESTID-F", "Alias übernehmen")
        self.page.historical.setChecked(True)
        self.assertEqual((self.page.strategy_choice, self.page.alias_choice), ({}, {}))
        self.assertTrue(self.page.decisions.isHidden())
        self.assertFalse(self.page.run.isEnabled())

    def test_the_whole_flow_check_decide_check_import_reconcile(self) -> None:
        self.check()
        self.choose("alias", "TESTID-F", "Alias übernehmen")
        self.choose("strategy", "TESTKEY-1", "Long-Term")
        self.recheck()
        self.assertEqual(self.page.tables["positions"].rowCount(), 2)
        self.page.confirm_import = lambda headline: True
        self.page.run.click()
        self.assertTrue(pump(15, lambda: "Import abgeschlossen" in self.page.message.text()), self.page.message.text())
        self.assertIn("Übereinstimmung", self.page.closing.text())  # the reconciliation after the import is on the page
        self.assertFalse(self.page.run.isEnabled())  # one import per check
        sent = self.calls("import_transactions")[0]
        self.assertEqual((sent["strategies"], sent["accept_aliases"], sent["start_campaigns"]), (["TESTKEY-1=long_term"], ["TESTID-F=SIX"], False))

    def test_without_a_file_the_check_does_not_start(self) -> None:
        self.page.check.click()
        self.assertIn("Bitte eine Datei wählen", self.page.message.text())
        self.assertEqual(self.calls("check_transaction_import"), [])


if __name__ == "__main__":
    unittest.main()
