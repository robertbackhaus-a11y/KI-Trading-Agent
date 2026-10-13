"""Inline editing of strategy and priority on the Watchlist page (R-AK): pending state, explicit "Änderung übernehmen", blocks and errors; fake MCP server, synthetic data only."""

from __future__ import annotations

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
from gui.viewmodels import watchlist_edit as we
from gui.viewmodels.reason_codes import KNOWN_REASON_CODES
from gui.viewmodels.watchlist import build_row

if HAVE_QT:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QMessageBox, QStyleOptionViewItem

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
WRITES = ("set_security_strategy", "set_watchlist_priority")


class ViewModelTests(unittest.TestCase):
    def row(self, **fields):
        base = {"security_id": 3, "symbol": "CCC", "name": "Charlie", "held": False, "status": "WATCH", "priority": None}
        return build_row({**base, **fields})

    def test_the_options_are_the_existing_strategies_only(self) -> None:
        self.assertEqual(we.STRATEGY_OPTIONS, [("Keine", None), ("Swing", "swing"), ("Long-Term", "long_term")])
        self.assertEqual([we.option_index(v) for v in (None, "swing", "long_term", "tactical")], [0, 1, 2, 0])

    def test_which_rows_have_a_strategy_editor(self) -> None:
        self.assertEqual(we.strategy_editable(self.row()), (True, ""))
        self.assertEqual(we.strategy_editable(self.row(strategy="swing")), (True, ""))
        held = we.strategy_editable(self.row(held=True, strategy="swing"))
        self.assertFalse(held[0])
        self.assertIn("gehaltenen Position", held[1])
        self.assertFalse(we.strategy_editable(self.row(strategy="tactical"))[0])  # a value that is not offered is not overwritten blindly

    def test_prune_keeps_only_real_differences(self) -> None:
        rows = {3: self.row(strategy="swing", priority=5), 4: self.row(security_id=4, held=True, strategy="swing")}
        pending = {3: {"strategy": "swing", "priority": 9}, 4: {"strategy": None}, 9: {"priority": 1}}
        self.assertEqual(we.prune(pending, rows), {3: {"priority": 9}})  # equal to the backend, held, or the row is gone

    def test_display_shows_the_pending_values(self) -> None:
        row = self.row(strategy="swing", priority=5)
        self.assertEqual(we.display(row, None), ("Swing", "5"))
        self.assertEqual(we.display(row, {"strategy": None, "priority": None}), ("Ohne Strategie", "—"))

    def test_aj_every_answer_is_readable_and_the_codes_are_known(self) -> None:
        for result in ("STRATEGY_SET", "STRATEGY_REMOVED", "UNCHANGED", "BLOCKED_HELD_POSITION", "BLOCKED_ACTIVE_CAMPAIGN", "NOT_FOUND", "INVALID_IDENTIFIER", "INVALID_STRATEGY",
                       "ASSIGNMENT_HISTORY_UNCLEAR", "ERROR", "SOMETHING_NEW"):
            with self.subTest(result=result):
                message = we.describe_strategy(True, {"result": result, "message": "x"})
                self.assertFalse(RAW.search(message.text), message.text)
                self.assertIn(message.level, ("info", "warning", "blocker"))
                self.assertEqual(message.done, result in ("STRATEGY_SET", "STRATEGY_REMOVED"))
                if result != "SOMETHING_NEW":
                    self.assertIn(result, KNOWN_REASON_CODES)
        self.assertEqual(we.describe_strategy(True, {"result": "STRATEGY_SET"}).text, "Strategie gespeichert.")
        self.assertEqual(we.describe_strategy(True, {"result": "BLOCKED_HELD_POSITION"}).text, "Strategie kann bei einer gehaltenen Position nicht geändert werden.")
        self.assertEqual(we.describe_strategy(True, {"result": "BLOCKED_ACTIVE_CAMPAIGN"}).text, "Strategie kann bei einer aktiven Campaign nicht geändert werden.")
        self.assertIn("Code: BLOCKED_ACTIVE_CAMPAIGN", we.describe_strategy(True, {"result": "BLOCKED_ACTIVE_CAMPAIGN"}).tip)  # the original code is in the tooltip
        failed = we.describe_strategy(False, None, "Zeitüberschreitung")
        self.assertEqual((failed.level, failed.done), ("blocker", False))
        self.assertFalse(we.strategy_saved(True, {"result": "BLOCKED_HELD_POSITION"}))
        self.assertTrue(we.strategy_saved(True, {"result": "UNCHANGED"}))
        self.assertTrue(we.priority_saved(True, {"result": "PRIORITY_SET", "changed": False}))
        self.assertFalse(we.priority_saved(True, {"result": "NOT_FOUND"}))

    def test_ah_ai_the_action_is_gui_only(self) -> None:
        self.assertEqual(tp.classify("set_security_strategy"), tp.ACTION)
        self.assertFalse(tp.is_read("set_security_strategy"))
        self.assertTrue(tp.is_gui_action("set_security_strategy"))


@QT
class EditWindowTests(unittest.TestCase):
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

    # -- helpers ---------------------------------------------------------------------------------------------
    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()] if self.calls_file.exists() else []

    def writes(self) -> list[tuple[str, dict]]:
        return [(c["tool"], c["arguments"]) for c in self.calls() if c["tool"] in WRITES]

    def reads(self) -> int:
        return sum(1 for c in self.calls() if c["tool"] == "get_watchlist")

    def row_of(self, symbol: str) -> int:
        return next(r for r in range(self.view.table.rowCount()) if self.view.table.item(r, 0).text() == symbol)

    def cell(self, symbol: str, column: int) -> str:
        return self.view.table.item(self.row_of(symbol), column).text()

    def select(self, symbol: str) -> None:
        self.view.table.selectRow(self.row_of(symbol))
        QApplication.processEvents()

    def editor(self, symbol: str, column: int):
        delegate = self.view.table.itemDelegateForColumn(column)
        index = self.view.table.model().index(self.row_of(symbol), column)
        widget = delegate.createEditor(self.view.table.viewport(), QStyleOptionViewItem(), index)
        if widget is not None:
            delegate.setEditorData(widget, index)
        return delegate, index, widget

    def choose_strategy(self, symbol: str, text: str) -> None:
        delegate, index, widget = self.editor(symbol, 2)
        self.assertIsNotNone(widget, symbol)
        widget.setCurrentIndex(widget.findText(text))
        delegate.setModelData(widget, self.view.table.model(), index)
        widget.deleteLater()
        pump(0.15)  # the table is redrawn right after the editor has closed

    def enter_priority(self, symbol: str, text: str) -> None:
        delegate, index, widget = self.editor(symbol, 3)
        self.assertIsInstance(widget, QLineEdit)
        widget.setText(text)
        delegate.setModelData(widget, self.view.table.model(), index)
        widget.deleteLater()
        pump(0.15)

    # R, S, T
    def test_r_s_t_the_editors_offer_the_real_strategies_and_a_number_field(self) -> None:
        _, _, combo = self.editor("CCC", 2)
        self.assertIsInstance(combo, QComboBox)
        self.assertEqual([combo.itemText(i) for i in range(combo.count())], ["Keine", "Swing", "Long-Term"])
        self.assertEqual(combo.currentText(), "Keine")
        _, _, held = self.editor("AAA", 2)
        self.assertIsNone(held)  # a held position has no strategy editor
        self.assertIn("gehaltenen Position", self.view.table.item(self.row_of("AAA"), 2).toolTip())
        _, _, number = self.editor("BBB", 3)
        self.assertIsInstance(number, QLineEdit)
        self.assertIsNotNone(number.validator())
        self.assertEqual(self.editor("AAA", 3)[2].text(), "50")  # the priority of a held row can be edited
        for symbol in ("AAA", "BBB", "CCC"):  # the other columns have no editor at all
            flags = self.view.table.item(self.row_of(symbol), 0).flags()
            self.assertFalse(flags & Qt.ItemFlag.ItemIsEditable)

    # V, W
    def test_v_w_nothing_is_written_by_opening_or_choosing(self) -> None:
        self.assertEqual(self.writes(), [])
        self.editor("CCC", 2)
        self.choose_strategy("CCC", "Swing")
        self.enter_priority("CCC", "12")
        self.select("CCC")
        QApplication.processEvents()
        self.assertEqual(self.writes(), [])  # a pending change is only local

    # U
    def test_u_a_change_becomes_pending_and_is_marked(self) -> None:
        self.select("CCC")
        self.assertFalse(self.view.apply_button.isEnabled())
        self.choose_strategy("CCC", "Swing")
        self.assertEqual(self.cell("CCC", 2), "Swing")
        self.assertEqual(self.cell("CCC", 15), "Geändert")
        self.assertTrue(self.view.table.item(self.row_of("CCC"), 2).font().bold())
        self.assertEqual(self.view.pending(), {3: {"strategy": "swing"}})
        self.select("CCC")
        self.assertTrue(self.view.apply_button.isEnabled())
        self.assertTrue(self.view.discard_button.isEnabled())
        self.assertEqual(self.cell("BBB", 15), "")  # only the changed row is marked
        self.choose_strategy("CCC", "Keine")  # back to the backend value ends the pending change
        self.assertEqual(self.view.pending(), {})
        self.assertEqual(self.cell("CCC", 15), "")

    def test_discard_resets_the_row_without_a_request(self) -> None:
        self.select("CCC")
        self.choose_strategy("CCC", "Swing")
        self.enter_priority("CCC", "99")
        self.select("CCC")
        self.view.discard_button.click()
        self.assertEqual((self.view.pending(), self.cell("CCC", 2), self.cell("CCC", 3)), ({}, "Ohne Strategie", "7"))
        self.assertEqual(self.writes(), [])

    # X
    def test_x_save_the_strategy(self) -> None:
        self.select("CCC")
        self.choose_strategy("CCC", "Swing")
        self.select("CCC")
        reads = self.reads()
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: self.cell("CCC", 15) == "" and self.reads() > reads and self.cell("CCC", 2) == "Swing"))
        self.assertEqual(self.writes(), [("set_security_strategy", {"security_id": 3, "strategy": "swing"})])
        self.assertIn("Strategie gespeichert.", self.view.message.text())
        self.assertEqual(self.view.pending(), {})

    # Y
    def test_y_save_the_priority_with_the_existing_action(self) -> None:
        self.select("CCC")
        self.enter_priority("CCC", "20")
        self.select("CCC")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: self.cell("CCC", 3) == "20" and self.cell("CCC", 15) == ""))
        self.assertEqual(self.writes(), [("set_watchlist_priority", {"security_id": 3, "priority": 20})])
        self.assertIn("Priorität gespeichert.", self.view.message.text())

    def test_the_priority_can_be_cleared(self) -> None:
        self.select("CCC")
        self.enter_priority("CCC", "")
        self.assertEqual(self.view.pending(), {3: {"priority": None}})
        self.select("CCC")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: self.cell("CCC", 3) == "—" and self.cell("CCC", 15) == ""))
        self.assertEqual(self.writes(), [("set_watchlist_priority", {"security_id": 3, "priority": None})])

    def test_a_held_row_keeps_an_editable_priority_but_never_sends_a_strategy_write(self) -> None:
        self.select("AAA")
        self.enter_priority("AAA", "60")
        self.assertEqual(self.view.pending(), {1: {"priority": 60}})  # the strategy of a held row cannot even become pending
        self.select("AAA")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: self.cell("AAA", 3) == "60" and self.cell("AAA", 15) == ""))
        self.assertEqual(self.writes(), [("set_watchlist_priority", {"security_id": 1, "priority": 60})])
        self.assertEqual(self.cell("AAA", 2), "Swing")

    # Z
    def test_z_both_changes_are_saved_in_order_strategy_first(self) -> None:
        self.select("CCC")
        self.enter_priority("CCC", "30")
        self.choose_strategy("CCC", "Swing")
        self.select("CCC")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: self.cell("CCC", 2) == "Swing" and self.cell("CCC", 3) == "30" and self.cell("CCC", 15) == ""))
        self.assertEqual([w[0] for w in self.writes()], ["set_security_strategy", "set_watchlist_priority"])
        text = re.sub(r"<[^>]+>", " ", self.view.message.text())
        self.assertIn("Strategie gespeichert.", text)
        self.assertIn("Priorität gespeichert.", text)

    # AB
    def test_ab_an_active_campaign_blocks_in_words_and_keeps_the_pending_change(self) -> None:
        self.select("BBB")
        self.choose_strategy("BBB", "Swing")
        self.select("BBB")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: "aktiven Campaign" in self.view.message.text()))
        self.assertFalse(RAW.search(re.sub(r"<[^>]+>", " ", self.view.message.text())))
        self.assertTrue(pump(15, lambda: self.cell("BBB", 15) == "Geändert"))  # still pending: nothing was saved
        self.assertEqual(self.view.pending(), {2: {"strategy": "swing"}})
        self.assertIn("Code: BLOCKED_ACTIVE_CAMPAIGN", self.view.message.toolTip())

    # AC, AD
    def test_ac_a_failed_strategy_stops_the_priority_and_is_not_shown_as_success(self) -> None:
        self.select("CCC")
        self.choose_strategy("CCC", "Long-Term")  # the fake fails this write
        self.enter_priority("CCC", "40")
        self.select("CCC")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: "nicht gespeichert" in self.view.message.text()))
        self.assertEqual([w[0] for w in self.writes()], ["set_security_strategy"])  # the priority was not sent blindly
        self.assertNotIn("gespeichert.", re.sub("nicht gespeichert", "", self.view.message.text()))
        self.assertTrue(pump(15, lambda: self.cell("CCC", 15) == "Geändert"))
        self.assertEqual(self.view.pending(), {3: {"strategy": "long_term", "priority": 40}})

    def test_a_partial_success_keeps_only_the_failed_part_pending(self) -> None:
        self.select("CCC")
        self.choose_strategy("CCC", "Swing")
        self.enter_priority("CCC", "41")
        self.select("CCC")
        with mock.patch.object(self.window.mcp, "call_action", wraps=self.window.mcp.call_action) as spy:
            self.view.apply_button.click()
            self.assertTrue(pump(15, lambda: self.cell("CCC", 3) == "41" and self.cell("CCC", 15) == ""))
        self.assertEqual([c.args[0] for c in spy.call_args_list], ["set_security_strategy", "set_watchlist_priority"])

    # AE
    def test_ae_a_refresh_with_pending_changes_asks_and_the_default_is_cancel(self) -> None:
        self.select("CCC")
        self.choose_strategy("CCC", "Swing")
        reads = self.reads()
        asked = []
        self.view.confirm_discard = lambda: (asked.append(1), False)[1]
        self.view.refresh.click()
        self.assertEqual((len(asked), self.reads(), self.view.pending()), (1, reads, {3: {"strategy": "swing"}}))  # cancelled: nothing is lost, no request
        defaults = []

        def fake_exec(box):
            defaults.append(box.defaultButton() is box.button(QMessageBox.StandardButton.Cancel))
            return QMessageBox.StandardButton.Cancel

        self.view.confirm_discard = self.view._ask_discard
        with mock.patch.object(QMessageBox, "exec", new=fake_exec):
            self.view.refresh.click()
        self.assertEqual(defaults, [True])  # "Abbrechen" is the default button
        self.assertEqual(self.view.pending(), {3: {"strategy": "swing"}})
        self.view.confirm_discard = lambda: True
        self.view.refresh.click()
        self.assertTrue(pump(15, lambda: self.reads() > reads and self.view.pending() == {}))

    def test_a_refresh_by_the_application_keeps_the_pending_changes(self) -> None:
        self.select("CCC")
        self.choose_strategy("CCC", "Swing")
        reads = self.reads()
        self.window.refresh_watchlist()  # for example after another action
        self.assertTrue(pump(15, lambda: self.reads() > reads))
        QApplication.processEvents()
        self.assertEqual(self.view.pending(), {3: {"strategy": "swing"}})
        self.assertEqual(self.cell("CCC", 2), "Swing")

    # AF, AG
    def test_af_ag_sorting_and_filters_keep_the_pending_state(self) -> None:
        self.select("CCC")
        self.choose_strategy("CCC", "Swing")
        table = self.view.table
        for column, order in ((0, Qt.SortOrder.DescendingOrder), (4, Qt.SortOrder.AscendingOrder), (14, Qt.SortOrder.DescendingOrder), (0, Qt.SortOrder.AscendingOrder)):
            table.sortItems(column, order)
            self.assertEqual((self.cell("CCC", 2), self.cell("CCC", 15)), ("Swing", "Geändert"))  # the marker travels with its row
            self.assertEqual(self.cell("AAA", 15), "")
        self.view.search.setText("ccc")
        self.assertEqual([table.item(r, 0).text() for r in range(table.rowCount())], ["CCC"])
        self.view.search.setText("")
        self.view.strategy_filter.setCurrentText("Long-Term")
        self.assertEqual([table.item(r, 0).text() for r in range(table.rowCount())], ["BBB"])
        self.view.strategy_filter.setCurrentText("Alle")
        self.assertEqual(self.view.pending(), {3: {"strategy": "swing"}})
        self.assertEqual(self.writes(), [])

    # AH
    def test_ah_the_chat_is_never_offered_the_action(self) -> None:
        self.assertIn("set_security_strategy", self.mcp.tools)
        self.assertNotIn("set_security_strategy", [t["function"]["name"] for t in tl.offered_tools(self.mcp.tools)[0]])

    # AK
    def test_ak_the_offscreen_window_renders_with_pending_changes(self) -> None:
        self.select("CCC")
        self.choose_strategy("CCC", "Swing")
        self.enter_priority("BBB", "3")
        QApplication.processEvents()
        self.assertFalse(self.window.grab().isNull())
        self.assertEqual(sorted(self.view.pending()), [2, 3])
        context = self.view.table.viewport()
        self.assertIsNotNone(context)


if __name__ == "__main__":
    unittest.main()
