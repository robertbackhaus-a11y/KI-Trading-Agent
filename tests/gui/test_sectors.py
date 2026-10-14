"""The "Sektoren" page: treemap geometry, colours, the view model, and the page against the fake MCP server (offscreen); synthetic data only."""

from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import FAKE_SERVER, HAVE_QT, pump, qapp

from gui.services import tool_policy as tp
from gui.viewmodels import sectors as vs
from gui.viewmodels import treemap

if HAVE_QT:
    from gui.app import PAGES, SECTORS_PAGE, MainWindow
    from gui.services import mcp_client as mc
    from gui.services.background import AsyncRunner
    from gui.services.settings import Settings
    from test_main_window import FakeBackend

    import fixtures

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")


def position(sid, symbol, value, change, etf=False, strategy="long_term", sector="X"):
    return {"security_id": sid, "symbol": symbol, "name": symbol + " Corp", "asset_type": "etf" if etf else "stock", "is_etf": etf, "strategy": strategy,
            "sector": sector, "market_value_eur": value, "day_change_pct": change, "weight_pct": value / 10.0}


def group(sector, positions, status="OK", limit=30.0):
    value = sum(p["market_value_eur"] for p in positions)
    changed = [p for p in positions if p["day_change_pct"] is not None]
    change = (sum(p["market_value_eur"] * p["day_change_pct"] for p in changed) / sum(p["market_value_eur"] for p in changed)) if changed else None
    return {"sector": sector, "assigned": sector is not None, "market_value_eur": value, "weight_pct": value / 10.0, "position_count": len(positions), "day_change_pct": change,
            "day_coverage_pct": sum(p["market_value_eur"] for p in changed) / value * 100 if value else None, "guard_status": status,
            "limit_pct": None if sector is None else limit, "headroom_pct": None if sector is None else limit - value / 10.0, "etf_count": sum(1 for p in positions if p["is_etf"]), "positions": positions}


ENVELOPE = {
    "ok": True, "operation": "get_sector_overview", "as_of": "2026-10-13", "base_currency": "EUR", "total_market_value_eur": 1000.0, "position_count": 4, "limit_pct": 30.0,
    "day_change_pct": 0.5, "day_coverage_pct": 90.0, "assigned_value_eur": 600.0, "unassigned_value_eur": 400.0, "unvalued": [], "warnings": [],
    "sectors": [group("ALPHA", [position(1, "AAA", 400.0, 2.0, sector="ALPHA"), position(2, "BBB", 200.0, -1.0, sector="ALPHA")], "OVER_LIMIT"),
                group(None, [position(3, "WORLD", 300.0, None, etf=True, sector=None), position(4, "CCC", 100.0, 0.0, sector=None)], "NOT_ASSESSED")],
}


class GeometryTests(unittest.TestCase):
    def check_partition(self, values, rect):
        rects = treemap.squarify(values, rect)
        total_area = rect[2] * rect[3]
        positive = [(v, r) for v, r in zip(values, rects) if v > 0]
        for v, r in positive:
            self.assertIsNotNone(r)
            self.assertGreaterEqual(min(r[2], r[3]), 0.0)  # no negative size
            self.assertAlmostEqual(r[2] * r[3], v / sum(x for x, _ in positive) * total_area, places=6)  # area proportional to the value
            self.assertGreaterEqual(r[0], rect[0] - 1e-9)
            self.assertGreaterEqual(r[1], rect[1] - 1e-9)
            self.assertLessEqual(r[0] + r[2], rect[0] + rect[2] + 1e-9)
            self.assertLessEqual(r[1] + r[3], rect[1] + rect[3] + 1e-9)
        for i, (_, a) in enumerate(positive):
            for _, b in positive[i + 1:]:
                overlap_w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                overlap_h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                self.assertFalse(overlap_w > 1e-6 and overlap_h > 1e-6, "tiles overlap")
        self.assertAlmostEqual(sum(r[2] * r[3] for _, r in positive), total_area, places=4)  # no gap either
        return rects

    def test_squarify_partitions_the_rectangle_without_overlap_or_negative_areas(self) -> None:
        rng = random.Random(7)
        for _ in range(200):
            values = [rng.choice([rng.uniform(0.01, 1000.0), 1.0]) for _ in range(rng.randint(1, 25))]
            self.check_partition(values, (rng.uniform(0, 50), rng.uniform(0, 50), rng.uniform(40, 1500), rng.uniform(40, 900)))

    def test_zero_negative_and_missing_values_get_no_tile(self) -> None:
        rects = self.check_partition([5.0, 0.0, -3.0, 2.0], (0, 0, 100, 50))
        self.assertEqual((rects[1], rects[2]), (None, None))
        self.assertEqual(treemap.squarify([], (0, 0, 10, 10)), [])
        self.assertEqual(treemap.squarify([1.0], (0, 0, 0, 10)), [None])
        self.assertEqual(treemap.squarify([0.0, 0.0], (0, 0, 10, 10)), [None, None])

    def test_the_layout_keeps_children_inside_their_group_and_group_tiles_apart(self) -> None:
        rng = random.Random(3)
        for _ in range(60):
            groups = [(str(g), 0.0, [(str(p), rng.uniform(1, 100)) for p in range(rng.randint(1, 8))]) for g in range(rng.randint(1, 8))]
            groups = [(k, sum(v for _, v in members), members) for k, _, members in groups]
            tiles = treemap.layout(groups, (2, 2, rng.uniform(200, 1400), rng.uniform(150, 800)))
            self.assertEqual(len(tiles), len(groups))
            for t in tiles:
                x, y, w, h = t.rect
                self.assertGreaterEqual(min(w, h), 0.0)
                for c in t.children:
                    cx, cy, cw, ch = c.rect
                    self.assertGreaterEqual(min(cw, ch), 0.0)
                    self.assertTrue(cx >= x - 1e-9 and cy >= y + t.caption_height - 1e-9 and cx + cw <= x + w + 1e-9 and cy + ch <= y + h + 1e-9)
                    self.assertAlmostEqual(c.value > 0, True)
                for i, a in enumerate(t.children):
                    for b in t.children[i + 1:]:
                        self.assertFalse(min(a.rect[0] + a.rect[2], b.rect[0] + b.rect[2]) - max(a.rect[0], b.rect[0]) > 1e-6 and min(a.rect[1] + a.rect[3], b.rect[1] + b.rect[3]) - max(a.rect[1], b.rect[1]) > 1e-6)
            for i, a in enumerate(tiles):
                for b in tiles[i + 1:]:
                    self.assertFalse(min(a.rect[0] + a.rect[2], b.rect[0] + b.rect[2]) - max(a.rect[0], b.rect[0]) > 1e-6 and min(a.rect[1] + a.rect[3], b.rect[1] + b.rect[3]) - max(a.rect[1], b.rect[1]) > 1e-6)

    def test_a_degenerate_rectangle_never_yields_a_negative_size(self) -> None:
        for rect in ((0, 0, 3, 3), (0, 0, 1, 500), (0, 0, 0, 0)):
            for tile in treemap.layout([("a", 5.0, [("p", 5.0)]), ("b", 1.0, [("q", 1.0)])], rect):
                self.assertGreaterEqual(min(tile.rect[2], tile.rect[3]), 0.0)
                for child in tile.children:
                    self.assertGreaterEqual(min(child.rect[2], child.rect[3]), 0.0)

    def test_colours_gain_loss_and_neutral(self) -> None:
        self.assertEqual(treemap.performance_color(None), treemap.NEUTRAL)
        self.assertEqual(treemap.performance_color(float("nan")), treemap.NEUTRAL)
        self.assertNotEqual(treemap.performance_color(0.0), treemap.NEUTRAL)  # 0 % is data, "no data" is a different grey
        up, down = treemap.performance_color(5.0), treemap.performance_color(-5.0)
        self.assertEqual((up, down), (treemap.performance_color(3.0), treemap.performance_color(-3.0)))  # saturates at +-3 %
        self.assertGreater(int(up[3:5], 16), int(up[1:3], 16))  # green dominates
        self.assertGreater(int(down[1:3], 16), int(down[3:5], 16))  # red dominates
        self.assertEqual(sorted({len(treemap.performance_color(x)) for x in (-9, -1, 0, 0.4, 2, 9)}), [7])


class ViewModelTests(unittest.TestCase):
    def test_the_table_rows_and_the_unassigned_area(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        self.assertTrue(vm.available)
        self.assertEqual([r[0] for r in vm.sector_rows], ["ALPHA", "Nicht zugeordnet"])
        alpha, unassigned = vm.sector_rows
        self.assertEqual(alpha[:4], ["ALPHA", "600 €", "60,0 %", "2"])
        self.assertEqual(alpha[5], "Über Limit (Limit 30 %, über um 30,0 %)")
        self.assertEqual(unassigned[3], "2 (davon 1 ETF)")
        self.assertEqual(unassigned[5], "nicht bewertet")
        self.assertIn("des Werts", unassigned[4])  # partial coverage is said
        self.assertEqual(vm.sector_levels, ["blocker", "info"])
        self.assertEqual(vs.SECTOR_HEADERS, ["Sektor", "Marktwert €", "Anteil %", "Positionen", "Tagesperformance", "Guard / Limit"])

    def test_the_totals_match_the_portfolio_value(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        self.assertAlmostEqual(sum(s.value for s in vm.sectors), ENVELOPE["total_market_value_eur"])
        self.assertAlmostEqual(sum(p.value for s in vm.sectors for p in s.positions), ENVELOPE["total_market_value_eur"])
        self.assertEqual(sum(len(s.positions) for s in vm.sectors), len(vm.position_rows))
        self.assertEqual(dict(vm.summary)["Investierter Wert"], "1.000 €")
        self.assertEqual(dict(vm.summary)["Nicht zugeordnet"], "400 € (40,0 %)")

    def test_a_missing_change_is_a_dash_and_neutral(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        world = next(p for s in vm.sectors for p in s.positions if p.symbol == "WORLD")
        self.assertIsNone(world.change_pct)
        self.assertEqual(world.color, treemap.NEUTRAL)
        row = next(r for r in vm.position_rows if r[0].startswith("WORLD"))
        self.assertEqual((row[0], row[4], row[5]), ("WORLD ETF", "—", "ETF"))
        flat = next(p for s in vm.sectors for p in s.positions if p.symbol == "CCC")
        self.assertEqual(vs.fmt_signed_pct(flat.change_pct), "0,00 %")  # 0 % is not "no data"
        self.assertNotEqual(flat.color, treemap.NEUTRAL)
        self.assertEqual(vs.fmt_signed_pct(2.0), "+2,00 %")
        self.assertEqual(vs.fmt_signed_pct(None), "—")

    def test_etfs_are_marked_in_the_tooltip_and_not_decomposed(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        world = next(p for s in vm.sectors for p in s.positions if p.symbol == "WORLD")
        self.assertIn("ETF (als eine Position, ohne Zerlegung)", world.tip)
        self.assertEqual(len([p for s in vm.sectors for p in s.positions]), 4)

    def test_treemap_groups_have_the_group_values(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        groups = vm.treemap_groups()
        self.assertEqual([round(g[1]) for g in groups], [600, 400])
        self.assertEqual([round(v) for _, _, members in groups for _, v in members], [400, 200, 300, 100])

    def test_errors_and_empty_answers(self) -> None:
        self.assertFalse(vs.build_sectors(None).available)
        self.assertEqual(vs.build_sectors({"ok": False, "message": "kaputt"}).error, "kaputt")
        self.assertFalse(vs.build_sectors({"ok": True}).available)
        empty = vs.build_sectors({"ok": True, "sectors": [], "total_market_value_eur": 0.0})
        self.assertTrue(empty.available)
        self.assertEqual(empty.sector_rows, [])
        junk = vs.build_sectors({"ok": True, "sectors": [{"sector": "A", "positions": [{"symbol": "Z", "market_value_eur": "x"}, "bad"]}, 5]})
        self.assertEqual(junk.sectors[0].positions, [])  # a position without a number is skipped, nothing crashes

    def test_unvalued_positions_and_warnings_are_shown_as_notes(self) -> None:
        env = dict(ENVELOPE, unvalued=[{"symbol": "ZZZ"}], warnings=["UNVALUED_POSITIONS: 1 open position(s) without a EUR valuation are not part of the values"])
        notes = vs.build_sectors(env).notes
        self.assertTrue(any("ZZZ" in n for n in notes))
        self.assertTrue(any(n.startswith("UNVALUED_POSITIONS") for n in notes))
        self.assertTrue(any("keine Zerlegung" in n for n in notes))


class PolicyTests(unittest.TestCase):
    def test_get_sector_overview_is_a_read_tool_and_not_a_gui_action(self) -> None:
        self.assertEqual(tp.classify("get_sector_overview"), tp.READ)
        self.assertTrue(tp.is_read("get_sector_overview"))
        self.assertFalse(tp.is_gui_action("get_sector_overview"))


@QT
class SectorsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.calls_file = self.tmp / "calls.jsonl"
        (self.tmp / "orch.json").write_text(json.dumps(fixtures.orchestrator_envelope(5_000.0)), encoding="utf-8")
        (self.tmp / "sectors.json").write_text(json.dumps(ENVELOPE), encoding="utf-8")
        self.settings = Settings(mcp_python_path=sys.executable, mcp_server_path=str(FAKE_SERVER), trading_db_path=str(self.tmp / "trading.db"), trading_logs_dir=str(self.tmp))
        self.mcp = mc.McpService(self.settings, self.tmp / "mcp-stderr.log", ping_interval=2,
                                 extra_env={"FAKE_ORCH_FILE": str(self.tmp / "orch.json"), "FAKE_SECTORS_FILE": str(self.tmp / "sectors.json"), "FAKE_CALLS_FILE": str(self.calls_file)})
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
        self.window.resize(1200, 800)
        self.window.show()
        self.mcp.start()
        self.assertTrue(pump(40, lambda: self.window._orch is not None), "the dashboard data did not arrive")
        self.window.sidebar.setCurrentRow(SECTORS_PAGE)
        self.assertTrue(pump(15, lambda: self.view.sector_table.rowCount() == 2), "the sector overview did not load")

    def test_the_page_exists_loads_on_opening_and_only_reads(self) -> None:
        self.assertEqual(PAGES[SECTORS_PAGE], "Sektoren")
        self.assertIs(self.window.stack.widget(SECTORS_PAGE), self.view)
        self.open_page()
        tools = [c["tool"] for c in self.calls()]
        self.assertEqual(tools.count("get_sector_overview"), 1)
        self.assertEqual([t for t in tools if t.startswith(("set_", "add_", "remove_", "onboard", "sql", "approve"))], [])

    def test_tables_and_treemap_show_the_data(self) -> None:
        self.open_page()
        v = self.view
        self.assertEqual([v.sector_table.item(r, 0).text() for r in range(2)], ["ALPHA", "Nicht zugeordnet"])
        self.assertEqual(v.sector_table.item(1, 5).text(), "nicht bewertet")
        self.assertEqual(v.position_table.rowCount(), 4)
        self.assertTrue(v.treemap.tiles)
        self.assertEqual(len(v.treemap.tiles), 2)
        self.assertEqual(sum(len(t.children) for t in v.treemap.tiles), 4)
        tip = v.treemap.tile_at(__import__("PySide6.QtCore", fromlist=["QPointF"]).QPointF(*[c + s / 2 for c, s in zip(v.treemap.tiles[0].children[0].rect[:2], v.treemap.tiles[0].children[0].rect[2:])]))
        self.assertIn("Marktwert", tip)

    def test_the_treemap_paints_offscreen_and_a_screenshot_can_be_taken(self) -> None:
        self.open_page()
        pixmap = self.view.treemap.grab()
        self.assertFalse(pixmap.isNull())
        image = pixmap.toImage()
        colors = {image.pixelColor(x, y).name() for x in range(0, image.width(), 7) for y in range(0, image.height(), 7)}
        self.assertGreater(len(colors), 4)  # tiles with different colours were drawn
        shot = self.window.grab()
        self.assertFalse(shot.isNull())
        target = Path(__import__("os").environ.get("SECTORS_SCREENSHOT", ""))
        if str(target):
            shot.save(str(target))

    def test_an_error_keeps_the_page_usable(self) -> None:
        self.open_page()
        self.window.sectors.show_vm(vs.build_sectors({"ok": False, "message": "kaputt"}))
        self.assertIn("kaputt", self.view.message.text())
        self.assertEqual(self.view.sector_table.rowCount(), 0)
        self.assertTrue(self.view.button.isEnabled())
        self.view.treemap.grab()  # painting without data does not fail


if __name__ == "__main__":
    unittest.main()
