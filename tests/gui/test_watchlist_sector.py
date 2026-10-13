"""Editing the sector on the Watchlist page: the column, its sources, the pending / apply mechanism, blocks and errors; fake MCP server, synthetic data only."""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp

from gui.services import tool_policy as tp
from gui.viewmodels import watchlist as vw
from gui.viewmodels import watchlist_edit as we
from gui.viewmodels.reason_codes import KNOWN_REASON_CODES

if HAVE_QT:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QComboBox, QStyleOptionViewItem

    from gui.app import WATCHLIST_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services import tool_loop as tl
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from test_main_window import FakeBackend

    import fixtures
    import test_watchlist as twl

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")
RAW = re.compile(r"\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b")
SECTOR_COLUMN = vw.HEADERS.index("Sektor")
MARKER = len(vw.HEADERS)
GROUPS = ["ALPHA", "BETA", "GAMMA"]


def envelope() -> dict:
    env = copy.deepcopy(twl.ENVELOPE)
    sectors = {1: ("ALPHA", "public", "ALPHA"),  # AAA: held, from the public map
               2: ("BETA", "local", "ALPHA"),  # BBB: a local entry replaces the public ALPHA
               3: (None, "none", None)}  # CCC: not assigned
    for row in env["rows"]:
        sector, source, public = sectors[row["security_id"]]
        row.update({"sector": sector, "sector_source": source, "sector_public": public})
        if sector is None:
            row.pop("sector")
        if public is None:
            row.pop("sector_public")
    env["sector_groups"] = GROUPS
    return env


class ViewModelTests(unittest.TestCase):
    def row(self, **fields):
        base = {"security_id": 3, "symbol": "CCC", "name": "Charlie", "held": False, "status": "WATCH", "priority": None}
        return vw.build_row({**base, **fields})

    def test_the_cell_shows_the_group_and_marks_a_local_entry(self) -> None:
        self.assertEqual(self.row().cells[SECTOR_COLUMN], "—")
        self.assertEqual(self.row(sector="ALPHA", sector_source="public", sector_public="ALPHA").cells[SECTOR_COLUMN], "ALPHA")
        local = self.row(sector="BETA", sector_source="local", sector_public="ALPHA")
        self.assertEqual((local.cells[SECTOR_COLUMN], local.sector_value, local.sector_source, local.sector_public), ("BETA (lokal)", "BETA", "local", "ALPHA"))
        self.assertEqual(self.row(sector="BETA", sector_source="bogus").sector_source, "none")  # an unknown source is not trusted

    def test_the_options_depend_on_where_the_value_comes_from(self) -> None:
        public = self.row(sector="ALPHA", sector_source="public", sector_public="ALPHA")
        local = self.row(sector="BETA", sector_source="local", sector_public="ALPHA")
        none = self.row()
        self.assertEqual([t for t, _ in we.sector_options(public, GROUPS)], GROUPS)  # nothing local to remove
        self.assertEqual([t for t, _ in we.sector_options(local, GROUPS)], ["Lokale Zuordnung entfernen", *GROUPS])
        self.assertEqual([t for t, _ in we.sector_options(none, GROUPS)], ["Nicht zugeordnet", *GROUPS])
        self.assertEqual(we.sector_options(local, GROUPS)[0][1], None)
        self.assertFalse(we.sector_editable([])[0])
        self.assertTrue(we.sector_editable(GROUPS)[0])

    def test_the_tooltip_tells_the_source_and_what_a_local_entry_replaces(self) -> None:
        self.assertIn("sector_map_v1.json", we.sector_tooltip(self.row(sector="ALPHA", sector_source="public", sector_public="ALPHA")))
        local = we.sector_tooltip(self.row(sector="BETA", sector_source="local", sector_public="ALPHA"))
        self.assertIn("sector_map_local.json", local)
        self.assertIn("ersetzt", local)
        self.assertIn("ALPHA", local)
        self.assertIn("Nicht zugeordnet", we.sector_tooltip(self.row()))

    def test_pending_values_and_their_display(self) -> None:
        local = self.row(sector="BETA", sector_source="local", sector_public="ALPHA")
        self.assertEqual(we.display_sector(local, None), "BETA (lokal)")
        self.assertEqual(we.display_sector(local, {"sector": None}), "ALPHA")  # removing the override: the public value applies again
        self.assertEqual(we.display_sector(local, {"sector": "GAMMA"}), "GAMMA (lokal)")
        self.assertEqual(we.display_sector(self.row(), {"sector": "BETA"}), "BETA (lokal)")
        rows = {3: local}
        self.assertEqual(we.prune({3: {"sector": "BETA"}}, rows), {})  # equal to the backend
        self.assertEqual(we.prune({3: {"sector": None}}, rows), {3: {"sector": None}})
        self.assertEqual(we.prune({3: {"sector": None}}, {3: self.row()}), {})  # unassigned stays unassigned

    def test_every_answer_is_readable_and_the_codes_are_known(self) -> None:
        for result in ("SECTOR_SET", "SECTOR_OVERRIDE_REMOVED", "UNCHANGED", "INVALID_SECTOR", "NOT_FOUND", "INVALID_IDENTIFIER", "NO_MAP_KEY", "SECTOR_MAP_UNAVAILABLE", "LOCAL_MAP_INVALID", "ERROR", "SOMETHING_NEW"):
            with self.subTest(result=result):
                message = we.describe_sector(True, {"result": result, "message": "x"})
                self.assertFalse(RAW.search(message.text), message.text)
                self.assertEqual(message.done, result in ("SECTOR_SET", "SECTOR_OVERRIDE_REMOVED"))
                if result != "SOMETHING_NEW":
                    self.assertIn(result, KNOWN_REASON_CODES)
        self.assertEqual(we.describe_sector(True, {"result": "SECTOR_SET"}).text, "Sektor gespeichert.")
        self.assertIn("Code: LOCAL_MAP_INVALID", we.describe_sector(True, {"result": "LOCAL_MAP_INVALID"}).tip)
        self.assertEqual(we.describe_sector(False, None, "Zeitüberschreitung").level, "blocker")
        self.assertTrue(we.sector_saved(True, {"result": "UNCHANGED"}))
        self.assertFalse(we.sector_saved(True, {"result": "INVALID_SECTOR"}))

    def test_the_action_is_gui_only(self) -> None:
        self.assertEqual(tp.classify("set_security_sector"), tp.ACTION)
        self.assertFalse(tp.is_read("set_security_sector"))
        self.assertTrue(tp.is_gui_action("set_security_sector"))


@QT
class SectorWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.calls_file = self.tmp / "calls.jsonl"
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        (self.tmp / "wl.json").write_text(json.dumps(envelope()), encoding="utf-8")
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

    def writes(self) -> list[tuple[str, dict]]:
        rows = [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()] if self.calls_file.exists() else []
        return [(c["tool"], c["arguments"]) for c in rows if c["tool"].startswith("set_")]

    def row_of(self, symbol: str) -> int:
        return next(r for r in range(self.view.table.rowCount()) if self.view.table.item(r, 0).text() == symbol)

    def cell(self, symbol: str, column: int) -> str:
        return self.view.table.item(self.row_of(symbol), column).text()

    def select(self, symbol: str) -> None:
        self.view.table.selectRow(self.row_of(symbol))
        QApplication.processEvents()

    def choose(self, symbol: str, text: str) -> list[str]:
        delegate = self.view.table.itemDelegateForColumn(SECTOR_COLUMN)
        index = self.view.table.model().index(self.row_of(symbol), SECTOR_COLUMN)
        editor = delegate.createEditor(self.view.table.viewport(), QStyleOptionViewItem(), index)
        self.assertIsInstance(editor, QComboBox, symbol)
        delegate.setEditorData(editor, index)
        shown = [editor.itemText(i) for i in range(editor.count())]
        editor.setCurrentIndex(editor.findText(text))
        delegate.setModelData(editor, self.view.table.model(), index)
        editor.deleteLater()
        pump(0.15)
        return shown

    def test_the_column_shows_the_sector_and_where_it_comes_from(self) -> None:
        self.assertEqual(self.view.table.horizontalHeaderItem(SECTOR_COLUMN).text(), "Sektor")
        self.assertEqual([self.cell(s, SECTOR_COLUMN) for s in ("AAA", "BBB", "CCC")], ["ALPHA", "BETA (lokal)", "—"])
        self.assertIn("sector_map_v1.json", self.view.table.item(self.row_of("AAA"), SECTOR_COLUMN).toolTip())
        override = self.view.table.item(self.row_of("BBB"), SECTOR_COLUMN).toolTip()
        self.assertIn("ersetzt", override)
        self.assertIn("ALPHA", override)  # the replaced public value is recognizable
        self.assertIn("Nicht zugeordnet", self.view.table.item(self.row_of("CCC"), SECTOR_COLUMN).toolTip())

    def test_the_options_are_the_existing_groups_and_a_held_row_is_not_blocked(self) -> None:
        self.assertEqual(self.choose("AAA", "BETA"), GROUPS)  # AAA is held and from the public map: no "remove" option, the sector is not blocked by the position
        self.assertEqual(self.view.pending(), {1: {"sector": "BETA"}})
        self.assertEqual(self.choose("BBB", "GAMMA"), ["Lokale Zuordnung entfernen", *GROUPS])
        self.assertEqual(self.choose("CCC", "ALPHA"), ["Nicht zugeordnet", *GROUPS])
        self.assertEqual(self.writes(), [])  # choosing writes nothing

    def test_a_pending_sector_is_marked_and_shown_as_local(self) -> None:
        self.select("CCC")
        self.choose("CCC", "GAMMA")
        self.assertEqual((self.cell("CCC", SECTOR_COLUMN), self.cell("CCC", MARKER)), ("GAMMA (lokal)", "Geändert"))
        self.assertTrue(self.view.table.item(self.row_of("CCC"), SECTOR_COLUMN).font().bold())
        self.select("CCC")
        self.assertTrue(self.view.apply_button.isEnabled())
        self.view.discard_button.click()
        self.assertEqual((self.view.pending(), self.cell("CCC", SECTOR_COLUMN)), ({}, "—"))
        self.assertEqual(self.writes(), [])

    def test_apply_sets_the_sector_and_reloads(self) -> None:
        self.select("CCC")
        self.choose("CCC", "BETA")
        self.select("CCC")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: self.cell("CCC", SECTOR_COLUMN) == "BETA (lokal)" and self.cell("CCC", MARKER) == ""))
        self.assertEqual(self.writes(), [("set_security_sector", {"security_id": 3, "sector": "BETA"})])
        self.assertIn("Sektor gespeichert.", self.view.message.text())

    def test_removing_the_override_brings_the_public_value_back(self) -> None:
        self.select("BBB")
        self.choose("BBB", "Lokale Zuordnung entfernen")
        self.assertEqual(self.cell("BBB", SECTOR_COLUMN), "ALPHA")  # the pending display already shows what will apply
        self.select("BBB")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: self.cell("BBB", SECTOR_COLUMN) == "ALPHA" and self.cell("BBB", MARKER) == ""))
        self.assertEqual(self.writes(), [("set_security_sector", {"security_id": 2, "sector": None})])
        self.assertIn("sector_map_v1.json", self.view.table.item(self.row_of("BBB"), SECTOR_COLUMN).toolTip())

    def test_a_held_row_can_be_saved(self) -> None:
        self.select("AAA")
        self.choose("AAA", "GAMMA")
        self.select("AAA")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: self.cell("AAA", SECTOR_COLUMN) == "GAMMA (lokal)"))
        self.assertEqual(self.writes(), [("set_security_sector", {"security_id": 1, "sector": "GAMMA"})])

    def test_a_failure_is_readable_and_keeps_the_change_pending(self) -> None:
        self.select("CCC")
        self.choose("CCC", "ALPHA")
        self.choose("CCC", "GAMMA")  # the fake fails exactly this write
        self.select("CCC")
        self.view.apply_button.click()
        self.assertTrue(pump(15, lambda: "Die lokale Sector-Map ist ungültig" in self.view.message.text()))
        text = re.sub(r"<[^>]+>", " ", self.view.message.text())
        self.assertFalse(RAW.search(text), text)
        self.assertIn("Code: LOCAL_MAP_INVALID", self.view.message.toolTip())
        self.assertTrue(pump(15, lambda: self.cell("CCC", MARKER) == "Geändert"))
        self.assertEqual(self.view.pending(), {3: {"sector": "GAMMA"}})

    def test_all_three_fields_are_saved_in_order_strategy_priority_sector(self) -> None:
        self.select("CCC")
        delegate = self.view.table.itemDelegateForColumn(2)
        index = self.view.table.model().index(self.row_of("CCC"), 2)
        editor = delegate.createEditor(self.view.table.viewport(), QStyleOptionViewItem(), index)
        editor.setCurrentIndex(editor.findText("Swing"))
        delegate.setModelData(editor, self.view.table.model(), index)
        editor.deleteLater()
        pump(0.15)
        priority = self.view.table.itemDelegateForColumn(3)
        index = self.view.table.model().index(self.row_of("CCC"), 3)
        line = priority.createEditor(self.view.table.viewport(), QStyleOptionViewItem(), index)
        line.setText("11")
        priority.setModelData(line, self.view.table.model(), index)
        line.deleteLater()
        pump(0.15)
        self.choose("CCC", "BETA")
        self.assertEqual(self.view.pending(), {3: {"strategy": "swing", "priority": 11, "sector": "BETA"}})
        self.select("CCC")
        self.view.apply_button.click()
        self.assertTrue(pump(20, lambda: self.cell("CCC", MARKER) == "" and self.cell("CCC", SECTOR_COLUMN) == "BETA (lokal)"))
        self.assertEqual([w[0] for w in self.writes()], ["set_security_strategy", "set_watchlist_priority", "set_security_sector"])

    def test_sorting_and_filters_keep_the_pending_sector(self) -> None:
        self.select("CCC")
        self.choose("CCC", "GAMMA")
        table = self.view.table
        for column, order in ((0, Qt.SortOrder.DescendingOrder), (SECTOR_COLUMN, Qt.SortOrder.AscendingOrder), (MARKER, Qt.SortOrder.DescendingOrder)):
            table.sortItems(column, order)
            self.assertEqual((self.cell("CCC", SECTOR_COLUMN), self.cell("CCC", MARKER)), ("GAMMA (lokal)", "Geändert"))
        self.view.search.setText("ccc")
        self.assertEqual([table.item(r, 0).text() for r in range(table.rowCount())], ["CCC"])
        self.view.search.setText("")
        self.assertEqual(self.view.pending(), {3: {"sector": "GAMMA"}})

    def test_the_chat_is_never_offered_the_action(self) -> None:
        self.assertIn("set_security_sector", self.mcp.tools)
        self.assertNotIn("set_security_sector", [t["function"]["name"] for t in tl.offered_tools(self.mcp.tools)[0]])

    def test_the_offscreen_window_renders_with_a_pending_sector(self) -> None:
        self.select("BBB")
        self.choose("BBB", "GAMMA")
        QApplication.processEvents()
        self.assertFalse(self.window.grab().isNull())


if __name__ == "__main__":
    unittest.main()
