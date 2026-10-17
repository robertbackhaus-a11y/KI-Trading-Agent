# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The ISIN column of the Watchlist page: a normal, visible column (displayed behind the name) whose mapping, sorting, search and neighbouring editors are intact."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp

from gui.viewmodels import watchlist as vw

if HAVE_QT:
    from gui.app import WATCHLIST_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from gui.views import watchlist as view_mod
    from test_main_window import FakeBackend

    import fixtures
    import test_watchlist as twl

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")
ISIN = vw.HEADERS.index("ISIN")


class MappingTests(unittest.TestCase):
    def test_the_isin_is_in_the_column_model_and_the_row_cells(self) -> None:
        self.assertEqual(vw.HEADERS[ISIN], "ISIN")
        rows = {r.symbol: r for r in vw.build_watchlist(copy.deepcopy(twl.ENVELOPE)).rows}
        self.assertEqual(rows["AAA"].cells[ISIN], "TESTISIN-A1")
        self.assertEqual(rows["BBB"].cells[ISIN], vw.DASH)  # a security without an ISIN shows a dash, not another column's value
        self.assertEqual(len(rows["AAA"].cells), len(vw.HEADERS))
        self.assertEqual(len(rows["AAA"].sort_keys), len(vw.HEADERS))

    def test_the_isin_is_searchable_and_the_editable_columns_keep_their_indices(self) -> None:
        rows = vw.build_watchlist(copy.deepcopy(twl.ENVELOPE)).rows
        self.assertEqual([r.symbol for r in vw.apply_filter(rows, text="testisin-a")], ["AAA"])
        self.assertEqual((vw.HEADERS[2], vw.HEADERS[3], vw.HEADERS[-1]), ("Strategie", "Priorität", "Sektor"))
        self.assertEqual((view_mod.STRATEGY_COLUMN, view_mod.PRIORITY_COLUMN, view_mod.SECTOR_COLUMN, view_mod.EDIT_COLUMN), (2, 3, len(vw.HEADERS) - 1, len(vw.HEADERS)))


@QT
class IsinColumnTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        (self.tmp / "wl.json").write_text(json.dumps(twl.ENVELOPE), encoding="utf-8")
        self.settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", ping_interval=2, extra_env={"FAKE_ORCH_FILE": str(self.tmp / "orch.json"), "FAKE_WATCHLIST_FILE": str(self.tmp / "wl.json")})
        self.runner = AsyncRunner()
        self.window = MainWindow(self.settings, self.mcp, FakeBackend(), self.runner, [], save_fn=lambda s: None,
                                 ollama_fn=lambda url: __import__("gui.services.ollama_client", fromlist=["x"]).OllamaStatus(True, "0.35.0", ["m1"]))
        self.addCleanup(self.cleanup)
        self.view = self.window.watchlist

    def cleanup(self) -> None:
        self.window.close()
        self.runner.wait(5000)
        self.tmpdir.cleanup()

    def open_page(self) -> None:
        self.window.resize(1200, 760)
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None))
        self.window.sidebar.setCurrentRow(WATCHLIST_PAGE)
        self.assertTrue(pump(15, lambda: self.view.table.rowCount() == 3))

    def test_the_isin_is_a_visible_column_inside_the_viewport_behind_the_name(self) -> None:
        self.open_page()
        table = self.view.table
        header = table.horizontalHeader()
        self.assertEqual(table.horizontalHeaderItem(ISIN).text(), "ISIN")
        self.assertFalse(header.isSectionHidden(ISIN))
        self.assertEqual(header.visualIndex(ISIN), 2)  # right behind symbol and name
        self.assertEqual(header.visualIndex(1), 1)
        position = header.sectionViewportPosition(ISIN)
        self.assertGreater(header.sectionSize(ISIN), 40)
        self.assertLessEqual(position + header.sectionSize(ISIN), table.viewport().width(), "the ISIN column is scrolled out of sight")
        row = next(r for r in range(3) if table.item(r, 0).text() == "AAA")
        self.assertEqual(table.item(row, ISIN).text(), "TESTISIN-A1")

    def test_no_column_is_wider_than_the_cap(self) -> None:
        self.open_page()
        for c in range(len(vw.HEADERS)):
            self.assertLessEqual(self.view.table.columnWidth(c), view_mod.MAX_COLUMN_WIDTH, vw.HEADERS[c])

    def test_sorting_by_the_isin_works_and_the_editors_stay_on_their_columns(self) -> None:
        self.open_page()
        table = self.view.table
        table.sortItems(ISIN, view_mod.Qt.SortOrder.AscendingOrder)
        values = [table.item(r, ISIN).text() for r in range(3)]
        self.assertEqual(values, sorted(values, key=str.lower))
        for r in range(3):  # after sorting every row still belongs together: the symbol and the ISIN are those of one entry
            symbol, isin = table.item(r, 0).text(), table.item(r, ISIN).text()
            self.assertEqual(isin, {"AAA": "TESTISIN-A1"}.get(symbol, vw.DASH))
        self.assertIsNotNone(table.itemDelegateForColumn(view_mod.STRATEGY_COLUMN))
        self.assertIsNotNone(table.itemDelegateForColumn(view_mod.PRIORITY_COLUMN))
        self.assertIsNotNone(table.itemDelegateForColumn(view_mod.SECTOR_COLUMN))

    def test_the_search_finds_an_entry_by_isin(self) -> None:
        self.open_page()
        self.view.search.setText("testisin-a1")
        pump(1)
        self.assertEqual([self.view.table.item(r, 0).text() for r in range(self.view.table.rowCount())], ["AAA"])


if __name__ == "__main__":
    unittest.main()
