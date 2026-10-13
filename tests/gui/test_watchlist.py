"""Watchlist page (S-AJ): view model, filters, the three explicit actions against a fake MCP server, the policy and the chat boundary."""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp

from gui.services import tool_policy as tp
from gui.viewmodels import watchlist as vw
from gui.viewmodels.reason_codes import KNOWN_REASON_CODES, Presented

if HAVE_QT:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox, QPushButton

    from gui.app import PAGES, WATCHLIST_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services import tool_loop as tl
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from gui.views.watchlist import AddCandidateDialog, SortItem
    from test_main_window import FakeBackend

    import fixtures

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")
RAW = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")

ROWS = [
    {"security_id": 1, "symbol": "AAA", "name": "Alpha Corp", "isin": "TESTISIN-A1", "strategy": "swing", "held": True, "priority": 50, "status": "WATCH", "rank": 2, "score": 31.54,
     "quality": "OK", "as_of": "2026-10-12", "assessment": "WATCH", "entry_status": "ENTRY_READY", "promotion": "PROMOTE", "news_status": "NEWS_ATTENTION", "annotated": True,
     "added_at": "2026-09-21 16:55:19"},
    {"security_id": 2, "symbol": "BBB", "name": "Beta Holdings Inc", "isin": None, "strategy": "long_term", "held": False, "priority": None, "status": "WATCH", "rank": 10, "score": 12.0,
     "quality": "LIMITED", "as_of": "2026-10-11", "assessment": "INSUFFICIENT_DATA", "promotion": "KEEP_WATCHING", "news_status": "NEWS_CLEAR", "added_at": "2026-09-22 10:00:00"},
    {"security_id": 3, "symbol": "CCC", "name": "Charlie Systems Corp", "isin": None, "strategy": None, "held": False, "priority": 7, "status": "PAUSED", "quality": "MARKET_DATA_MISSING",
     "news_status": "NEWS_UNAVAILABLE", "added_at": "2026-09-23 10:00:00"},
]
ENVELOPE = {"ok": True, "operation": "get_watchlist", "read_only": True, "status": "PARTIAL", "counts": {"total": 3, "held": 1}, "matching": 3, "returned": 3, "truncated": False, "rows": ROWS,
            "warnings": [{"code": "MARKET_INTELLIGENCE_REPORT_UNAVAILABLE", "message": "x"}]}


def cell_texts(vm: vw.WatchlistVM) -> list[str]:
    return [str(c) for row in vm.rows for c in row.cells]


class ViewModelTests(unittest.TestCase):
    def test_the_rows_show_readable_values_and_a_dash_for_missing_ones(self) -> None:
        vm = vw.build_watchlist(ENVELOPE)
        self.assertTrue(vm.available)
        self.assertEqual(vm.summary, "3 Einträge · davon 1 im Bestand")
        first, second, third = (dict(zip(vw.HEADERS, r.cells)) for r in vm.rows)
        self.assertEqual((first["Symbol"], first["Name"], first["Strategie"], first["Priorität"], first["Rang"], first["Score"], first["Bestand"]),
                         ("AAA", "Alpha Corp", "Swing", "50", "2", "31,5", "Ja"))
        self.assertEqual((first["Status"], first["Einstufung"], first["Einstieg"], first["Daten"], first["News"], first["Bewertet am"], first["ISIN"]),
                         ("Beobachtung", "Beobachtung", "Einstieg möglich", "OK", "Beachten", "2026-10-12", "TESTISIN-A1"))
        self.assertEqual((second["Strategie"], second["Priorität"], second["Daten"], second["Einstufung"], second["Einstieg"], second["ISIN"]),
                         ("Long-Term", "—", "Daten eingeschränkt", "Daten unzureichend", "Weiter beobachten", "—"))
        self.assertEqual((third["Strategie"], third["Rang"], third["Score"], third["Daten"], third["Einstufung"], third["Einstieg"], third["Bewertet am"], third["Status"]),
                         ("Ohne Strategie", "—", "—", "Marktdaten fehlen", "—", "—", "—", "Paused"))
        self.assertEqual(vm.notes, ["News-Bericht fehlt"])

    def test_i_no_raw_reason_code_is_visible(self) -> None:
        for cell in cell_texts(vw.build_watchlist(ENVELOPE)):
            self.assertFalse(RAW.match(cell), cell)

    def test_the_status_cells_carry_the_original_code_in_their_tooltip(self) -> None:
        row = vw.build_watchlist(ENVELOPE).rows[0]
        for index, code in ((6, "WATCH"), (8, "ENTRY_READY"), (9, "OK"), (10, "NEWS_ATTENTION")):
            cell = row.cells[index]
            self.assertIsInstance(cell, Presented)
            self.assertIn(f"Code: {code}", cell.tip)

    def test_every_status_code_of_the_tool_is_in_the_central_table(self) -> None:
        for code in ("WATCH", "OK", "LIMITED", "INSUFFICIENT", "MARKET_DATA_MISSING", "BUY", "DEFERRED", "INSUFFICIENT_DATA", "PROMOTE", "KEEP_WATCHING", "REJECT", "DATA_INSUFFICIENT",
                     "NEWS_CLEAR", "NEWS_ATTENTION", "NEWS_HIGH_ATTENTION", "NEWS_UNAVAILABLE", "ENTRY_READY", "WAIT_FOR_TRIGGER", "RANKING_UNAVAILABLE", "DECISIONS_UNAVAILABLE",
                     "ENGINE_UNAVAILABLE", "MARKET_INTELLIGENCE_REPORT_UNAVAILABLE"):
            self.assertIn(code, KNOWN_REASON_CODES)

    def test_sort_keys_sort_numbers_by_value_and_missing_values_last(self) -> None:
        rows = vw.build_watchlist(ENVELOPE).rows
        self.assertEqual([r.sort_keys[4] for r in rows], [2, 10, None])  # rank 10 after rank 2 (as numbers, not as text)
        self.assertEqual([r.sort_keys[5] for r in rows], [31.54, 12.0, None])
        self.assertEqual(rows[0].sort_keys[0], "aaa")

    # U, V, W
    def test_u_v_w_filters(self) -> None:
        rows = vw.build_watchlist(ENVELOPE).rows
        ids = lambda found: [r.security_id for r in found]
        self.assertEqual(ids(vw.apply_filter(rows)), [1, 2, 3])
        self.assertEqual(ids(vw.apply_filter(rows, text="alp")), [1])  # symbol / name / ISIN, case-insensitive
        self.assertEqual(ids(vw.apply_filter(rows, text="testisin-a")), [1])
        self.assertEqual(ids(vw.apply_filter(rows, text="  BBB ")), [2])
        self.assertEqual(ids(vw.apply_filter(rows, status="Paused")), [3])
        self.assertEqual(ids(vw.apply_filter(rows, strategy="Swing")), [1])
        self.assertEqual(ids(vw.apply_filter(rows, strategy="Ohne Strategie")), [3])
        self.assertEqual(ids(vw.apply_filter(rows, quality="Daten eingeschränkt")), [2])
        self.assertEqual(ids(vw.apply_filter(rows, text="a", status="Beobachtung", strategy="Swing")), [1])
        self.assertEqual(ids(vw.apply_filter(rows, text="nothing")), [])
        options = vw.filter_options(rows)
        self.assertEqual(options["status"], ["Alle", "Beobachtung", "Paused"])
        self.assertEqual(options["strategy"][0], "Alle")

    def test_unavailable_or_malformed_answers_give_an_error_not_an_exception(self) -> None:
        for bad in (None, {}, [], "x", {"ok": False, "message": "boom"}, {"ok": True}, {"ok": True, "rows": "no"}):
            vm = vw.build_watchlist(bad)
            self.assertFalse(vm.available, bad)
            self.assertTrue(vm.error, bad)
        self.assertEqual(vw.build_watchlist({"ok": False, "message": "boom"}).error, "boom")
        odd = copy.deepcopy(ENVELOPE)
        odd["rows"] = [None, {}, {"security_id": "x", "priority": "high", "rank": True, "score": "n/a"}, 5]
        vm = vw.build_watchlist(odd)
        self.assertTrue(vm.available)
        self.assertEqual(len(vm.rows), 2)
        self.assertTrue(all(c for row in vm.rows for c in row.cells))

    def test_a_truncated_answer_is_said_so(self) -> None:
        env = dict(ENVELOPE, truncated=True, matching=500, returned=3)
        self.assertIn("Gekürzt: 3 von 500 Einträgen", vw.build_watchlist(env).notes)

    def test_there_is_no_invented_priority_range_only_whole_numbers(self) -> None:
        import watchlist_admin

        self.assertTrue(watchlist_admin.PRIORITY_MIN <= vw.SPIN_MIN < vw.SPIN_MAX <= watchlist_admin.PRIORITY_MAX)  # the widget limit lies inside the storage range
        for fine in (None, "", 0, 100, 101, -1, 1000, vw.SPIN_MAX, vw.SPIN_MIN):
            self.assertIsNone(vw.check_priority(fine), fine)
        for bad in (1.5, "5", True, vw.SPIN_MAX + 1, vw.SPIN_MIN - 1):
            self.assertTrue(vw.check_priority(bad), bad)

    # Z, AA and the other messages
    def test_the_messages_of_every_action_result(self) -> None:
        describe = lambda data, ok=True: vw.describe_action(ok, data)
        added = describe({"result": "ADDED", "symbol": "AAA", "name": "Alpha Corp"})
        self.assertEqual((added.level, added.done), ("info", True))
        self.assertIn("AAA – Alpha Corp wurde zur Watchlist hinzugefügt", added.text)
        found = describe({"result": "WOULD_ADD", "symbol": "AAA", "name": "Alpha Corp", "isin": "TESTISIN-A1"})
        self.assertEqual((found.done, found.text), (False, "Gefunden: AAA – Alpha Corp · ISIN TESTISIN-A1"))
        duplicate = describe({"result": "ALREADY_EXISTS", "symbol": "AAA", "name": "Alpha Corp"})
        self.assertEqual((duplicate.level, duplicate.done), ("warning", False))
        self.assertIn("steht bereits auf der Watchlist", duplicate.text)
        unknown = describe({"result": "SECURITY_NOT_FOUND"})
        self.assertEqual((unknown.level, unknown.done), ("blocker", False))
        self.assertIn("Wertpapier ist noch nicht im Security Master.", unknown.text)
        self.assertIn("Über „Neues Wertpapier aufnehmen“ kann es mit eigenen Angaben angelegt werden.", unknown.text)
        ambiguous = describe({"result": "AMBIGUOUS_IDENTIFIER", "candidates": [{"security_id": 4, "symbol": "DUP", "name": "One"}, {"security_id": 5, "symbol": "DUP", "name": "Two"}]})
        self.assertIn("mehrdeutig", ambiguous.text)
        self.assertIn("ID 4", ambiguous.text)
        self.assertIn("ID 5", ambiguous.text)
        self.assertEqual(describe({"result": "INVALID_IDENTIFIER"}).level, "warning")
        self.assertEqual(describe({"result": "INVALID_PRIORITY", "message": "priority must be a whole number (or null to clear it)"}).text, "priority must be a whole number (or null to clear it)")
        removed = describe({"result": "REMOVED", "symbol": "AAA", "name": "Alpha Corp", "still_held": True, "had_notes": True})
        self.assertEqual((removed.done, "Die Position bleibt unverändert" in removed.text, "Notizen" in removed.text), (True, True, True))
        self.assertTrue(describe({"result": "NOT_FOUND"}).done)  # the list is stale: reload it
        self.assertEqual((describe({"result": "PRIORITY_SET", "changed": True, "priority": 70}).done, describe({"result": "PRIORITY_SET", "changed": False, "priority": 70}).done), (True, False))
        failed = vw.describe_action(False, {"ok": False, "result": "ERROR", "message": "database is locked"})
        self.assertEqual((failed.level, failed.done), ("blocker", False))
        self.assertIn("database is locked", failed.text)
        self.assertIn("Zeitüberschreitung", vw.describe_action(False, None, "Zeitüberschreitung nach 30 s").text)
        self.assertEqual(describe({"result": "SOMETHING_NEW"}).level, "blocker")


class PolicyTests(unittest.TestCase):
    # AF, AG, AH
    def test_af_get_watchlist_is_a_read_tool(self) -> None:
        self.assertEqual(tp.classify("get_watchlist"), tp.READ)
        self.assertTrue(tp.is_read("get_watchlist"))

    def test_ag_the_new_actions_are_actions(self) -> None:
        for name in ("add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "onboard_security", "set_security_strategy"):
            self.assertEqual(tp.classify(name), tp.ACTION, name)
            self.assertFalse(tp.is_read(name), name)
            self.assertTrue(tp.is_gui_action(name), name)
            with self.assertRaises(tp.ToolDenied):
                tp.require_read(name)

    def test_ah_nothing_else_may_be_triggered_by_the_gui(self) -> None:
        self.assertEqual(tp.GUI_ACTIONS, {"add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "onboard_security", "set_security_strategy"})
        for name in ("sql_execute", "approve_swing_promotion", "brand_new_tool", "get_watchlist", "database_status", "", "ADD_WATCHLIST_CANDIDATE"):
            self.assertFalse(tp.is_gui_action(name), name)
        self.assertEqual(tp.classify("brand_new_tool"), tp.ACTION)


@QT
class WatchlistWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.calls_file = self.tmp / "calls.jsonl"
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        (self.tmp / "wl.json").write_text(json.dumps(ENVELOPE), encoding="utf-8")
        self.settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", ping_interval=2,
                                 extra_env={"FAKE_ORCH_FILE": str(self.tmp / "orch.json"), "FAKE_WATCHLIST_FILE": str(self.tmp / "wl.json"), "FAKE_CALLS_FILE": str(self.calls_file)})
        self.runner = AsyncRunner()
        self.window = MainWindow(self.settings, self.mcp, FakeBackend(), self.runner, [], save_fn=lambda s: None,
                                 ollama_fn=lambda url: __import__("gui.services.ollama_client", fromlist=["x"]).OllamaStatus(True, "0.35.0", ["m1"]))
        self.addCleanup(self.cleanup)
        self.view = self.window.watchlist

    def cleanup(self) -> None:
        self.window.close()
        self.runner.wait(5000)
        self.tmpdir.cleanup()

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()] if self.calls_file.exists() else []

    def actions(self) -> list[dict]:
        return [c for c in self.calls() if c["tool"] != "get_watchlist"]

    def open_page(self) -> None:
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None), "the dashboard data did not arrive")
        self.window.sidebar.setCurrentRow(WATCHLIST_PAGE)
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 3), "the watchlist did not load")

    def reads(self) -> int:
        return sum(1 for c in self.calls() if c["tool"] == "get_watchlist")

    def row_of(self, symbol: str) -> int:
        return next(r for r in range(self.view.table.rowCount()) if self.view.table.item(r, 0).text() == symbol)

    def texts(self) -> list[str]:
        return [self.view.table.item(r, 0).text() for r in range(self.view.table.rowCount())]

    # S, AJ
    def test_s_aj_the_page_exists_between_planner_and_tasks_and_loads_on_opening(self) -> None:
        w = self.window
        self.assertEqual(PAGES[WATCHLIST_PAGE], "Watchlist")
        self.assertEqual([w.sidebar.item(i).text() for i in range(w.sidebar.count())], list(PAGES))
        self.assertIs(w.stack.widget(WATCHLIST_PAGE), self.view)
        self.open_page()
        self.assertFalse(w.grab().isNull())
        self.assertEqual(self.actions(), [])  # opening and reading never calls an action
        self.assertEqual(self.reads(), 1)

    def test_the_page_asks_for_the_table_shape_and_never_for_the_diagnosis_view(self) -> None:
        self.open_page()
        reads = [c for c in self.calls() if c["tool"] == "get_watchlist"]
        self.assertEqual(len(reads), 1)
        args = reads[0].get("args") or reads[0].get("arguments") or {}
        self.assertEqual((args.get("table"), args.get("detail"), args.get("limit")), (True, False, 200))

    # T
    def test_t_the_table_loads_with_the_columns_and_readable_values(self) -> None:
        self.open_page()
        table = self.view.table
        self.assertEqual([table.horizontalHeaderItem(c).text() for c in range(table.columnCount())], [*vw.HEADERS, "Änderung"])
        self.assertEqual(self.view.summary.text(), "3 Einträge · davon 1 im Bestand")
        self.assertEqual(sorted(self.texts()), ["AAA", "BBB", "CCC"])
        row = self.row_of("AAA")
        self.assertEqual(table.item(row, 8).text(), "Einstieg möglich")
        self.assertIn("Code: ENTRY_READY", table.item(row, 8).toolTip())  # the original code stays in the tooltip
        self.assertEqual(self.view.refreshed.text().startswith("Letzte Aktualisierung: "), True)
        for r in range(table.rowCount()):
            for c in range(table.columnCount()):
                self.assertFalse(RAW.match(table.item(r, c).text()), table.item(r, c).text())  # I

    def test_sorting_is_numeric_and_empty_values_come_last(self) -> None:
        self.open_page()
        table = self.view.table
        table.sortItems(4, Qt.SortOrder.AscendingOrder)  # rank
        self.assertEqual(self.texts(), ["AAA", "BBB", "CCC"])  # 2, 10, none (as numbers: 10 is after 2)
        table.sortItems(0, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.texts(), ["CCC", "BBB", "AAA"])
        self.assertIsInstance(table.item(0, 0), SortItem)

    # U, V, W
    def test_u_v_w_the_filters_narrow_the_table_without_a_new_request(self) -> None:
        self.open_page()
        before = len(self.calls())
        self.view.search.setText("alph")
        self.assertEqual(self.texts(), ["AAA"])
        self.view.search.setText("")
        self.view.status_filter.setCurrentText("Paused")
        self.assertEqual(self.texts(), ["CCC"])
        self.view.status_filter.setCurrentText("Alle")
        self.view.strategy_filter.setCurrentText("Long-Term")
        self.assertEqual(self.texts(), ["BBB"])
        self.view.strategy_filter.setCurrentText("Alle")
        self.view.quality_filter.setCurrentText("OK")
        self.assertEqual(self.texts(), ["AAA"])
        self.view.quality_filter.setCurrentText("Alle")
        self.assertEqual(len(self.texts()), 3)
        self.assertEqual(len(self.calls()), before)  # filtering is local: no request, no write

    # X
    def test_x_the_add_dialog_is_minimal_and_says_that_nothing_is_created(self) -> None:
        self.open_page()
        dialog = self.view.open_add_dialog()
        self.assertIsInstance(dialog, AddCandidateDialog)
        self.assertEqual((dialog.windowTitle(), dialog.add.text(), dialog.cancel.text(), dialog.check.text()), ("Kandidat hinzufügen", "Hinzufügen", "Abbrechen", "Prüfen"))
        self.assertFalse(dialog.priority.isEnabled())
        dialog.use_priority.setChecked(True)
        self.assertTrue(dialog.priority.isEnabled())
        self.assertEqual((dialog.priority.minimum(), dialog.priority.maximum()), (vw.SPIN_MIN, vw.SPIN_MAX))  # a widget limit, not a business range
        labels = " ".join(label.text() for label in dialog.findChildren(type(self.view.summary)))
        self.assertIn("Security Master", labels)
        dialog.add.click()  # an empty identifier is refused locally
        self.assertIn("Bitte einen Identifier eingeben", dialog.result.text())
        self.assertEqual(self.actions(), [])
        dialog.cancel.click()
        self.assertIsNone(self.view.add_dialog)

    # Y
    def test_y_a_successful_add_closes_the_dialog_and_refreshes_the_list(self) -> None:
        self.open_page()
        dialog = self.view.open_add_dialog()
        dialog.identifier.setText("newc")  # the fake knows this security; it is not on the list yet
        dialog.use_priority.setChecked(False)
        reads = self.reads()
        dialog.add.click()
        self.assertTrue(pump(15, lambda: self.view.add_dialog is None), "the dialog did not close")
        self.assertEqual(self.actions(), [{"tool": "add_watchlist_candidate", "arguments": {"identifier": "newc", "priority": None, "dry_run": False}}])
        self.assertTrue(pump(15, lambda: self.reads() > reads))
        self.assertIn("wurde zur Watchlist hinzugefügt", self.view.message.text())

    def test_a_new_entry_appears_after_the_refresh_with_its_priority(self) -> None:
        self.open_page()
        self.view.add_requested.emit("NEWB", 40)
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 4))
        self.assertEqual(self.actions()[-1]["arguments"], {"identifier": "NEWB", "priority": 40, "dry_run": False})
        self.assertEqual(self.view.table.item(self.row_of("NEWB"), 3).text(), "40")

    def test_the_preview_resolves_without_writing(self) -> None:
        self.open_page()
        dialog = self.view.open_add_dialog()
        dialog.identifier.setText("NEWA")
        dialog.check.click()
        self.assertTrue(pump(15, lambda: "Gefunden" in dialog.result.text()))
        self.assertIn("New Alpha Corp", dialog.result.text())
        self.assertEqual(self.actions(), [{"tool": "add_watchlist_candidate", "arguments": {"identifier": "NEWA", "priority": None, "dry_run": True}}])
        self.assertEqual(self.view.table.rowCount(), 3)  # nothing was added
        self.assertTrue(dialog.add.isEnabled() and dialog.check.isEnabled())
        self.assertIsNotNone(self.view.add_dialog)  # the dialog stays open after a preview
        dialog.cancel.click()

    # Z
    def test_z_a_duplicate_is_reported_and_the_dialog_stays_open(self) -> None:
        self.open_page()
        self.view.add_requested.emit("NEWB", None)
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 4))  # NEWB is added once ...
        dialog = self.view.open_add_dialog()
        dialog.identifier.setText("NEWB")
        dialog.add.click()  # ... and a second time it is a duplicate
        self.assertTrue(pump(15, lambda: "steht bereits auf der Watchlist" in dialog.result.text()))
        self.assertIsNotNone(self.view.add_dialog)
        self.assertEqual(self.view.table.rowCount(), 4)
        dialog.cancel.click()

    # AA
    def test_aa_an_unknown_security_gets_the_security_master_message(self) -> None:
        self.open_page()
        dialog = self.view.open_add_dialog()
        dialog.identifier.setText("NOSUCH")
        dialog.add.click()
        self.assertTrue(pump(15, lambda: "Security Master" in dialog.result.text()))
        text = dialog.result.text()
        self.assertIn("Wertpapier ist noch nicht im Security Master.", text)
        self.assertIn("Neues Wertpapier aufnehmen", text)
        self.assertEqual(self.view.table.rowCount(), 3)
        self.assertEqual(len(self.actions()), 1)  # one attempt, nothing else (no retry, no creation call)
        dialog.cancel.click()

    def test_an_ambiguous_identifier_lists_the_candidates(self) -> None:
        self.open_page()
        dialog = self.view.open_add_dialog()
        dialog.identifier.setText("DUP")
        dialog.add.click()
        self.assertTrue(pump(15, lambda: "mehrdeutig" in dialog.result.text()))
        self.assertIn("ID 104", dialog.result.text())
        dialog.cancel.click()

    def test_a_tool_error_is_shown_and_the_dialog_is_usable_again(self) -> None:
        self.open_page()
        dialog = self.view.open_add_dialog()
        dialog.identifier.setText("ERR")
        dialog.add.click()
        self.assertTrue(pump(15, lambda: "database is locked" in dialog.result.text()))
        self.assertTrue(dialog.add.isEnabled())
        dialog.cancel.click()

    # AB
    def test_ab_removing_needs_the_confirmation_whose_default_is_cancel(self) -> None:
        self.open_page()
        self.view.table.selectRow(self.row_of("AAA"))
        asked: list = []
        self.view.confirm_remove = lambda row: (asked.append(row.symbol), False)[1]  # the user declines
        self.view.remove_button.click()
        pump(0.5)
        self.assertEqual((asked, self.actions()), (["AAA"], []))
        seen = {}

        def fake_exec(box):
            seen.update(default=box.standardButton(box.defaultButton()), text=box.text(), title=box.windowTitle())
            return QMessageBox.StandardButton.Cancel

        self.view.confirm_remove = self.view._ask_remove
        with mock.patch.object(QMessageBox, "exec", fake_exec):
            self.view.remove_button.click()
        self.assertEqual(seen["default"], QMessageBox.StandardButton.Cancel)
        self.assertIn("AAA aus der Watchlist entfernen?", seen["text"])
        self.assertIn("Position bleibt unverändert", seen["text"])  # AAA is held
        self.assertIn("Einstiegsgrund, These oder Notizen", seen["text"])  # and its notes would be lost
        self.assertEqual(self.actions(), [])

    # AC
    def test_ac_a_confirmed_removal_calls_the_action_and_refreshes(self) -> None:
        self.open_page()
        self.view.table.selectRow(self.row_of("BBB"))
        self.view.confirm_remove = lambda row: True
        reads = self.reads()
        self.view.remove_button.click()
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 2))
        self.assertEqual(self.actions(), [{"tool": "remove_watchlist_candidate", "arguments": {"security_id": 2}}])
        self.assertGreater(self.reads(), reads)
        self.assertNotIn("BBB", self.texts())
        self.assertIn("wurde von der Watchlist entfernt", self.view.message.text())

    def test_the_context_menu_and_the_buttons_follow_the_selection(self) -> None:
        self.open_page()
        self.view.table.clearSelection()
        self.assertFalse(self.view.remove_button.isEnabled())
        self.view.table.selectRow(0)
        self.assertTrue(self.view.remove_button.isEnabled())
        self.assertEqual(self.view.table.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)

    # AD
    def test_ad_priority_has_exactly_one_gui_path_inline_edit_pending_apply(self) -> None:
        import gui.views.watchlist as view_module

        self.open_page()
        texts = [b.text() for b in self.view.findChildren(QPushButton)]
        self.assertFalse([t for t in texts if "Priorität ändern" in t], texts)  # no second, immediately writing path
        self.assertFalse(hasattr(view_module, "PriorityDialog"))
        for name in ("priority_button", "ask_priority", "priority_requested", "_on_priority"):
            self.assertFalse(hasattr(self.view, name), name)
        self.assertFalse(hasattr(self.window, "set_watchlist_priority"))
        self.view.table.selectRow(self.row_of("BBB"))
        pump(0.2)
        self.assertEqual(self.actions(), [])  # selecting a row writes nothing
        # the one remaining path: the priority cell has an editor, the value stays pending until "Änderung übernehmen" (see test_watchlist_edit)
        delegate = self.view.table.itemDelegateForColumn(3)
        index = self.view.table.model().index(self.row_of("BBB"), 3)
        from PySide6.QtWidgets import QLineEdit, QStyleOptionViewItem

        self.assertIsInstance(delegate.createEditor(self.view.table.viewport(), QStyleOptionViewItem(), index), QLineEdit)

    # AE, AH
    def test_ae_the_chat_is_never_offered_an_action_tool(self) -> None:
        self.open_page()
        catalog = self.mcp.tools
        self.assertTrue({"add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "get_watchlist"} <= set(catalog))
        tools, _ = tl.offered_tools(catalog)
        offered = {t["function"]["name"] for t in tools}
        self.assertIn("get_watchlist", offered)
        self.assertFalse(offered & {"add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority", "sql_execute", "approve_swing_promotion"})

    def test_ah_call_and_call_sync_still_refuse_the_actions_and_call_action_refuses_the_rest(self) -> None:
        self.open_page()
        results: list = []
        self.mcp.call("add_watchlist_candidate", {"identifier": "AAA"}, on_done=results.append)
        self.mcp.call_action("sql_execute", {"sql": "select 1"}, on_done=results.append)
        self.mcp.call_action("approve_swing_promotion", {}, on_done=results.append)
        self.mcp.call_action("get_watchlist", {}, on_done=results.append)
        self.mcp.call_action("brand_new_tool", {}, on_done=results.append)
        self.assertTrue(pump(10, lambda: len(results) == 5))
        self.assertEqual([r.kind for r in results], ["DENIED"] * 5)
        self.assertEqual(self.mcp.call_sync("remove_watchlist_candidate", {"security_id": 1}).kind, "DENIED")
        self.assertEqual(self.actions(), [])  # none of them reached the server

    def test_the_planner_and_dashboard_still_have_no_action_buttons_and_the_watchlist_has_exactly_these(self) -> None:
        self.open_page()
        texts = sorted(b.text() for b in self.view.findChildren(QPushButton))
        self.assertEqual(texts, ["Aktualisieren", "Kandidat hinzufügen …", "Von der Watchlist entfernen …", "Änderung verwerfen", "Änderung übernehmen"])
        for word in ("kaufen", "verkaufen", "order", "approve", "promote", "sql"):
            self.assertFalse(any(word in t.lower() for t in texts), word)

    def test_reopening_the_page_reloads_only_a_stale_list(self) -> None:
        self.open_page()
        reads = self.reads()
        self.window.sidebar.setCurrentRow(0)
        self.window.sidebar.setCurrentRow(WATCHLIST_PAGE)
        pump(0.5)
        self.assertEqual(self.reads(), reads)  # fresh: no second request
        from datetime import datetime, timedelta

        self.window._watchlist_at = datetime.now() - timedelta(seconds=120)
        self.window.sidebar.setCurrentRow(0)
        self.window.sidebar.setCurrentRow(WATCHLIST_PAGE)
        self.assertTrue(pump(10, lambda: self.reads() == reads + 1))

    def test_a_failed_refresh_keeps_the_last_list(self) -> None:
        self.open_page()
        self.window._on_watchlist(mc.McpResult("get_watchlist", False, error="Verbindung verloren", kind="TRANSPORT"))
        self.assertEqual(len(self.texts()), 3)
        self.assertIn("Aktualisierung fehlgeschlagen (Verbindung verloren)", self.view.message.text())


class ArchitectureTests(unittest.TestCase):
    def test_the_viewmodel_is_pure_and_the_actions_are_wired_only_in_the_app(self) -> None:
        import ast

        gui = support.ROOT / "gui"
        vm = (gui / "viewmodels" / "watchlist.py").read_text(encoding="utf-8")
        imported = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) and n.module else "" for n in ast.walk(ast.parse(vm)) if isinstance(n, ast.ImportFrom) and n.level == 0}
        self.assertFalse({"PySide6", "sqlite3", "mcp", "subprocess"} & imported)
        names = ("add_watchlist_candidate", "remove_watchlist_candidate", "set_watchlist_priority")
        for path in sorted(gui.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if any(n in text for n in names):
                self.assertIn(path.name, {"tool_policy.py", "app.py", "mcp_client.py"}, path.name)
            self.assertNotRegex(text, r"\.(call|call_sync)\(\s*[\"'](add|remove)_watchlist|\.(call|call_sync)\(\s*[\"']set_watchlist", path.name)
            if "call_action(" in text:
                self.assertIn(path.name, {"app.py", "mcp_client.py"}, path.name)
        for chat_module in ("chat_service.py", "tool_loop.py", "ollama_client.py"):
            text = (gui / "services" / chat_module).read_text(encoding="utf-8")
            self.assertNotIn("call_action", text, chat_module)
            self.assertNotIn("GUI_ACTIONS", text, chat_module)
            self.assertFalse(any(n in text for n in names), chat_module)


if __name__ == "__main__":
    unittest.main()
