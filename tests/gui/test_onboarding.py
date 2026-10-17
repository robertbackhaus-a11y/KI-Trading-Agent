# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Security onboarding in the GUI (Z-AO): the dialog, the unknown-security flow of the Watchlist page, the messages and the policy; fake MCP server, synthetic data only."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp

from gui.services import tool_policy as tp
from gui.viewmodels import onboarding as vo
from gui.viewmodels.reason_codes import KNOWN_REASON_CODES

if HAVE_QT:
    from PySide6.QtWidgets import QApplication

    from gui.app import WATCHLIST_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services import tool_loop as tl
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from test_main_window import FakeBackend

    import fixtures
    import test_watchlist as twl

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")
RAW = re.compile(r"\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b")  # an UPPER_SNAKE technical code


def planned(**security) -> dict:
    return {"security": {"symbol": "ZZNEW", "name": "Znew Systems Inc", "asset_type": "stock", "isin": None, "wkn": None, "exchange": None, "currency": None, "country": None, "active": True, **security},
            "provider_symbols": [], "watchlist": None, "sector": None}


class ViewModelTests(unittest.TestCase):
    def test_form_arguments_contain_exactly_what_was_typed(self) -> None:
        args = vo.form_arguments(" zz1 ", " Name One ", "Aktie")
        self.assertEqual(args, {"symbol": "zz1", "name": "Name One", "asset_type": "stock"})  # nothing optional is filled in, nothing is upper-cased or extended
        full = vo.form_arguments("A", "B", "ETF", isin="I", wkn="W", exchange="X", currency="EUR", country="DE", yahoo="A.DE", fmp="A", add_to_watchlist=True, priority=0)
        self.assertEqual(full["asset_type"], "etf")
        self.assertEqual(full["provider_symbols"], {"Yahoo Finance": "A.DE", "FMP": "A"})
        self.assertEqual((full["add_to_watchlist"], full["watchlist_priority"]), (True, 0))  # a priority of 0 is a value
        self.assertNotIn("watchlist_priority", vo.form_arguments("A", "B", "Aktie", priority=5))  # a priority without the watchlist option is not sent

    def test_ab_the_required_fields(self) -> None:
        self.assertEqual(vo.missing_fields(vo.form_arguments("", "", "Aktie")), ["Symbol", "Name"])
        self.assertEqual(vo.missing_fields(vo.form_arguments("A", "B", "Aktie")), [])

    def test_am_an_every_message_is_readable_and_has_no_raw_code(self) -> None:
        conflict = {"code": "ISIN_CONFLICT", "field": "isin", "value": "ZZ0", "security_id": 6, "symbol": "SIX", "name": "Six Corp", "match": "EXACT"}
        cases = [
            (True, {"result": "READY", "planned": planned(), "warnings": [{"code": "NAME_MATCH", "security_id": 3, "symbol": "X", "name": "Znew Systems Inc"}]}),
            (True, {"result": "CREATED", "security_id": 9, "symbol": "ZZNEW", "name": "Znew Systems Inc", "watchlist": {"status": "WATCH", "priority": None}}),
            (True, {"result": "ALREADY_EXISTS", "existing": {"security_id": 1, "symbol": "AAA", "name": "Alpha", "active": True}, "on_watchlist": False, "conflicts": [{**conflict, "code": "ALREADY_EXISTS"}]}),
            (True, {"result": "ISIN_CONFLICT", "conflicts": [conflict]}), (True, {"result": "SYMBOL_CONFLICT", "conflicts": [{**conflict, "code": "SYMBOL_CONFLICT", "field": "symbol", "match": "STEM"}]}),
            (True, {"result": "WKN_CONFLICT", "conflicts": [{**conflict, "code": "WKN_CONFLICT", "field": "wkn"}]}), (True, {"result": "ALIAS_CONFLICT", "conflicts": [{**conflict, "code": "ALIAS_CONFLICT", "field": "provider_symbols.Yahoo Finance"}]}),
            (True, {"result": "INVALID_INPUT", "errors": [{"field": f, "reason": r} for f in ("symbol", "isin", "provider_symbols.FMP") for r in vo.REASON_TEXT]}),
            (False, {"result": "ERROR", "message": "database is locked", "error": "OperationalError"}), (False, None), (True, {"result": "SOMETHING_NEW"}),
        ]
        for ok, data in cases:
            with self.subTest(result=(data or {}).get("result")):
                outcome = vo.describe_onboarding(ok, data, "Zeitüberschreitung")
                visible = "\n".join([outcome.text, *outcome.lines])
                self.assertFalse(RAW.search(visible), visible)
                self.assertTrue(outcome.text)
                self.assertIn(outcome.level, ("info", "warning", "blocker"))

    def test_ready_summarizes_the_form_and_allows_the_creation(self) -> None:
        outcome = vo.describe_onboarding(True, {"result": "READY", "planned": {**planned(isin="ZZ1"), "provider_symbols": [{"source": "Yahoo Finance", "symbol": "ZZ.DE"}],
                                                                           "watchlist": {"status": "WATCH", "priority": 3}}, "warnings": []})
        self.assertTrue(outcome.can_create)
        self.assertEqual(outcome.text, "Keine Konflikte gefunden.")
        self.assertIn("ISIN: ZZ1", outcome.lines)
        self.assertIn("Provider-Symbole: ZZ.DE (Yahoo-Symbol)", outcome.lines)
        self.assertIn("Sector: nicht zugeordnet", outcome.lines)
        self.assertIn("Watchlist: ja (Priorität 3)", outcome.lines)
        for result in ("ISIN_CONFLICT", "ALREADY_EXISTS", "INVALID_INPUT", "ERROR"):
            self.assertFalse(vo.describe_onboarding(True, {"result": result, "conflicts": [], "errors": [], "existing": {}}).can_create, result)

    def test_a_failed_call_never_claims_a_creation(self) -> None:
        outcome = vo.describe_onboarding(False, {"result": "CREATED", "security_id": 1}, "x")
        self.assertFalse(outcome.created)
        self.assertEqual(outcome.level, "blocker")
        self.assertIn("nichts angelegt", outcome.text)

    def test_the_result_codes_are_known_to_the_presentation(self) -> None:
        for code in ("READY", "CREATED", "ALREADY_EXISTS", "ISIN_CONFLICT", "SYMBOL_CONFLICT", "WKN_CONFLICT", "ALIAS_CONFLICT", "INVALID_INPUT", "ERROR"):
            self.assertIn(code, KNOWN_REASON_CODES)

    def test_the_action_is_gui_only(self) -> None:
        self.assertEqual(tp.classify("onboard_security"), tp.ACTION)
        self.assertFalse(tp.is_read("onboard_security"))
        self.assertTrue(tp.is_gui_action("onboard_security"))
        with self.assertRaises(tp.ToolDenied):
            tp.require_read("onboard_security")  # call() / call_sync() refuse it; only call_action may send it


@QT
class OnboardingWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.calls_file = self.tmp / "calls.jsonl"
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        (self.tmp / "wl.json").write_text(json.dumps(twl.ENVELOPE), encoding="utf-8")
        self.settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", ping_interval=2,
                                 extra_env={"FAKE_ORCH_FILE": str(self.tmp / "orch.json"), "FAKE_WATCHLIST_FILE": str(self.tmp / "wl.json"), "FAKE_CALLS_FILE": str(self.calls_file)})
        self.runner = AsyncRunner()
        self.window = MainWindow(self.settings, self.mcp, FakeBackend(), self.runner, [], save_fn=lambda s: None,
                                 ollama_fn=lambda url: __import__("gui.services.ollama_client", fromlist=["x"]).OllamaStatus(True, "0.35.0", ["m1"]))
        self.addCleanup(self.cleanup)
        self.view = self.window.watchlist
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None), "the dashboard data did not arrive")
        self.window.sidebar.setCurrentRow(WATCHLIST_PAGE)
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 3), "the watchlist did not load")

    def cleanup(self) -> None:
        self.window.close()
        self.runner.wait(5000)
        self.tmpdir.cleanup()

    def calls(self, tool: str = "onboard_security") -> list[dict]:
        rows = [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()] if self.calls_file.exists() else []
        return [c["arguments"] for c in rows if c["tool"] == tool]

    def writes(self) -> list[dict]:
        return [c for c in self.calls() if c["dry_run"] is False]

    def reads(self) -> int:
        return sum(1 for line in self.calls_file.read_text(encoding="utf-8").splitlines() if json.loads(line)["tool"] == "get_watchlist")

    def fill(self, dialog, symbol: str = "ZZNEW", name: str = "Znew Systems Inc", **more) -> None:
        dialog.symbol.setText(symbol)
        dialog.name.setText(name)
        for key, value in more.items():
            getattr(dialog, key).setText(value)

    def checked(self, dialog, until) -> None:
        dialog.check.click()
        self.assertTrue(pump(15, until), dialog.result.text())

    def open_dialog(self):
        return self.view.open_onboard_dialog()

    # Z, AA
    def test_z_aa_the_unknown_flow_offers_the_button_and_opens_the_dialog(self) -> None:
        add = self.view.open_add_dialog()
        self.assertTrue(add.onboard.isHidden())  # not offered before anything is known
        add.identifier.setText("NOSUCH")
        add.add.click()
        self.assertTrue(pump(15, lambda: not add.onboard.isHidden()), add.result.text())
        self.assertEqual(self.calls(), [])  # nothing happens by itself: no jump into the onboarding, no creation
        self.assertIsNone(self.view.onboard_dialog)
        add.onboard.click()
        self.assertIsNotNone(self.view.onboard_dialog)
        self.assertIsNone(self.view.add_dialog)
        self.assertEqual(self.view.onboard_dialog.windowTitle(), "Neues Wertpapier aufnehmen")
        self.view.onboard_dialog.cancel.click()

    def test_the_button_is_not_offered_for_other_answers(self) -> None:
        add = self.view.open_add_dialog()
        add.identifier.setText("NEWA")
        add.check.click()
        self.assertTrue(pump(15, lambda: "Gefunden" in add.result.text()))
        self.assertTrue(add.onboard.isHidden())
        add.identifier.setText("DUP")  # ambiguous: it exists
        add.add.click()
        self.assertTrue(pump(15, lambda: "mehrdeutig" in add.result.text()))
        self.assertTrue(add.onboard.isHidden())
        add.cancel.click()

    # AB
    def test_ab_the_required_fields_are_checked_before_the_server_is_asked(self) -> None:
        dialog = self.open_dialog()
        dialog.check.click()
        self.assertIn("Bitte ausfüllen: Symbol, Name", dialog.result.text())
        self.assertEqual(self.calls(), [])
        dialog.cancel.click()

    # AC, AE
    def test_ac_ae_a_check_enables_the_creation_and_an_edit_disables_it_again(self) -> None:
        dialog = self.open_dialog()
        self.assertFalse(dialog.create.isEnabled())
        self.fill(dialog)
        self.checked(dialog, lambda: dialog.create.isEnabled())
        text = dialog.result.text()
        self.assertIn("Keine Konflikte gefunden.", text)
        self.assertIn("Symbol: ZZNEW", text)
        self.assertIn("Sector: nicht zugeordnet", text)
        self.assertEqual([c["dry_run"] for c in self.calls()], [True])
        self.assertEqual(self.writes(), [])
        dialog.name.setText("Znew Systems Inc.")  # the form no longer matches the check
        dialog.name.textEdited.emit("Znew Systems Inc.")
        self.assertFalse(dialog.create.isEnabled())
        dialog.cancel.click()

    # AD
    def test_ad_a_conflict_is_shown_in_words_and_cannot_be_created(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog, symbol="CLASH", name="Clash Systems", isin="TESTISIN-Z1")
        self.checked(dialog, lambda: "gehört bereits" in dialog.result.text())
        text = dialog.result.text()
        self.assertIn("Other Clash Ltd", text)
        self.assertIn("Six Corp", text)  # all conflicts, not only the first
        self.assertFalse(RAW.search(re.sub(r"<[^>]+>", " ", text)), text)
        self.assertFalse(dialog.create.isEnabled())
        self.assertTrue(dialog.result.toolTip())  # the technical codes are in the tooltip
        self.assertEqual(self.writes(), [])
        dialog.cancel.click()

    def test_invalid_input_and_errors_are_readable(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog, symbol="BADISIN", name="Bad Isin Corp")
        self.checked(dialog, lambda: "korrigieren" in dialog.result.text())
        text = re.sub(r"<[^>]+>", " ", dialog.result.text())
        self.assertIn("falsche Prüfziffer", text)
        self.assertFalse(RAW.search(text), text)
        self.assertFalse(dialog.create.isEnabled())
        self.fill(dialog, symbol="ERR", name="Err Corp")
        self.checked(dialog, lambda: "nichts angelegt" in dialog.result.text())
        self.assertFalse(dialog.create.isEnabled())
        dialog.cancel.click()

    # AF, AH
    def test_af_ah_the_creation_sends_exactly_the_checked_values_and_refreshes_the_list(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog, isin="TESTISIN-Z1", yahoo="ZZNEW.DE")
        self.checked(dialog, lambda: dialog.create.isEnabled())
        reads = self.reads()
        dialog.create.click()
        self.assertTrue(pump(15, lambda: self.view.onboard_dialog is None), dialog.result.text())
        writes = self.writes()
        self.assertEqual(len(writes), 1)
        checked = self.calls()[0]
        self.assertEqual({k: v for k, v in writes[0].items() if k != "dry_run"}, {k: v for k, v in checked.items() if k != "dry_run"})  # the same values as checked
        self.assertEqual((writes[0]["symbol"], writes[0]["isin"], writes[0]["provider_symbols"]), ("ZZNEW", "TESTISIN-Z1", {"Yahoo Finance": "ZZNEW.DE"}))
        self.assertTrue(pump(15, lambda: self.reads() > reads))  # the list is reloaded
        self.assertEqual(self.view.table.rowCount(), 3)  # not added to the watchlist unless asked
        self.assertIn("angelegt", self.view.message.text())

    # AG
    def test_ag_create_with_the_watchlist_option(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog, symbol="ZZWATCH", name="Zwatch Corp")
        dialog.watch.setChecked(True)
        dialog.use_priority.setChecked(True)
        dialog.priority.setValue(7)
        self.checked(dialog, lambda: dialog.create.isEnabled())
        self.assertIn("Watchlist: ja (Priorität 7)", dialog.result.text())
        dialog.create.click()
        self.assertTrue(pump(15, lambda: self.view.onboard_dialog is None))
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 4), "the new entry did not appear")
        self.assertEqual((self.writes()[0]["add_to_watchlist"], self.writes()[0]["watchlist_priority"]), (True, 7))
        self.assertIn("ZZWATCH", [self.view.table.item(r, 0).text() for r in range(self.view.table.rowCount())])

    # AI
    def test_ai_an_existing_security_is_not_created_again_but_can_be_added_to_the_watchlist(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog, symbol="HAVE", name="Have Already Corp")
        self.checked(dialog, lambda: "bereits im Security Master" in dialog.result.text())
        text = dialog.result.text()
        self.assertIn("Have Already Corp", text)
        self.assertIn("security_id: 77", text)
        self.assertFalse(dialog.create.isEnabled())
        self.assertFalse(dialog.to_watchlist.isHidden())
        dialog.to_watchlist.click()
        self.assertTrue(pump(15, lambda: self.view.onboard_dialog is None), dialog.result.text())
        self.assertEqual(self.writes(), [])  # no second security
        self.assertEqual(self.calls("add_watchlist_candidate")[-1]["identifier"], "77")
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 4))

    def test_an_existing_security_that_is_on_the_watchlist_offers_no_second_entry(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog, symbol="HAVE", name="Have Already Corp")
        self.checked(dialog, lambda: "bereits im Security Master" in dialog.result.text())
        dialog.to_watchlist.click()
        self.assertTrue(pump(15, lambda: self.view.onboard_dialog is None))
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 4))
        dialog = self.open_dialog()
        self.fill(dialog, symbol="HAVE", name="Have Already Corp")
        self.checked(dialog, lambda: "bereits im Security Master" in dialog.result.text())
        self.assertTrue(dialog.to_watchlist.isHidden())
        dialog.cancel.click()

    # AJ
    def test_aj_cancel_writes_nothing(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog)
        self.checked(dialog, lambda: dialog.create.isEnabled())
        dialog.cancel.click()
        self.assertIsNone(self.view.onboard_dialog)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.view.table.rowCount(), 3)

    def test_the_creation_needs_a_matching_check(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog)
        dialog.create.setEnabled(True)  # even a button forced on cannot create without a check of exactly this form
        dialog.create.click()
        self.assertEqual(self.calls(), [])
        self.assertIn("zuerst prüfen", dialog.result.text())
        dialog.cancel.click()

    # AK, AL
    def test_ak_the_chat_is_never_offered_the_action(self) -> None:
        catalog = self.mcp.tools
        self.assertIn("onboard_security", catalog)
        names = [t["function"]["name"] for t in tl.offered_tools(catalog)[0]]
        self.assertNotIn("onboard_security", names)
        self.assertNotIn("onboard_security", {"add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority"})

    # AO
    def test_ao_the_offscreen_window_renders_with_the_dialog_open(self) -> None:
        dialog = self.open_dialog()
        self.fill(dialog)
        QApplication.processEvents()
        self.assertFalse(self.window.grab().isNull())
        self.assertFalse(dialog.grab().isNull())
        dialog.cancel.click()


if __name__ == "__main__":
    unittest.main()
