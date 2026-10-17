# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The "Sektoren" page: treemap geometry, colours, the view model, and the page against the fake MCP server (offscreen); synthetic data only."""

from __future__ import annotations

import copy
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


def position(sid, symbol, value, change, etf=False, strategy="long_term", sector="X", origin="direct", category=None, etf_weight=None, etf_value=None):
    return {"security_id": sid, "symbol": symbol, "name": symbol + " Corp", "asset_type": "etf" if etf else "stock", "is_etf": etf, "strategy": strategy,
            "sector": sector, "market_value_eur": value, "day_change_pct": change, "weight_pct": value / 10.0, "origin": origin, "category": category,
            "etf_weight_pct": etf_weight, "etf_value_eur": etf_value, "etf_as_of": "2026-07-31" if origin == "etf_lookthrough" else None}


def group(sector, positions, status="OK", limit=30.0, kind=None, label=None):
    kind = kind or ("sector" if sector is not None else "unassigned")
    value = sum(p["market_value_eur"] for p in positions)
    direct = sum(p["market_value_eur"] for p in positions if p["origin"] == "direct")
    changed = [p for p in positions if p["day_change_pct"] is not None]
    change = (sum(p["market_value_eur"] * p["day_change_pct"] for p in changed) / sum(p["market_value_eur"] for p in changed)) if changed else None
    assessed = kind == "sector"
    return {"sector": sector, "kind": kind, "label": label or sector or "", "assigned": assessed, "market_value_eur": value, "weight_pct": value / 10.0,
            "direct_value_eur": direct, "etf_value_eur": value - direct, "direct_weight_pct": direct / 10.0, "etf_weight_pct": (value - direct) / 10.0,
            "position_count": len({p["security_id"] for p in positions}), "direct_count": len({p["security_id"] for p in positions if p["origin"] == "direct"}),
            "day_change_pct": change, "day_coverage_pct": sum(p["market_value_eur"] for p in changed) / value * 100 if value else None, "guard_status": status,
            "limit_pct": limit if assessed else None, "headroom_pct": limit - value / 10.0 if assessed else None, "etf_count": len({p["security_id"] for p in positions if p["is_etf"]}),
            "positions": positions}


ENVELOPE = {
    "ok": True, "operation": "get_sector_overview", "as_of": "2026-10-13", "base_currency": "EUR", "total_market_value_eur": 1000.0, "position_count": 4, "limit_pct": 30.0,
    "day_change_pct": 0.5, "day_coverage_pct": 90.0, "lookthrough_enabled": True, "assigned_value_eur": 700.0, "direct_value_eur": 700.0, "etf_value_eur": 300.0,
    "etf_category_value_eur": 150.0, "unassigned_value_eur": 150.0, "unvalued": [], "warnings": [],
    "lookthrough": {"etf_count": 1, "etf_value_eur": 300.0, "covered_value_eur": 300.0, "coverage_pct": 100.0, "mapped_value_eur": 150.0, "mapped_pct": 50.0},
    "etfs": [{"security_id": 3, "symbol": "WORLD", "name": "World ETF", "market_value_eur": 300.0, "status": "APPLIED", "as_of": "2026-07-31", "provider": "Provider", "mapped_pct": 50.0}],
    "sectors": [group("ALPHA", [position(1, "AAA", 400.0, 2.0, sector="ALPHA"), position(2, "BBB", 200.0, -1.0, sector="ALPHA"),
                                position(3, "WORLD", 150.0, None, etf=True, sector="ALPHA", origin="etf_lookthrough", category="Financials", etf_weight=50.0, etf_value=300.0)], "OVER_LIMIT"),
                group(None, [position(3, "WORLD", 150.0, None, etf=True, sector=None, origin="etf_lookthrough", category="Information Technology", etf_weight=50.0, etf_value=300.0)],
                      "NOT_ASSESSED", kind="etf_category", label="Information Technology"),
                group(None, [position(4, "CCC", 100.0, 0.0, sector=None)], "NOT_ASSESSED")],
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
    def test_the_table_rows_show_exposure_direct_etf_and_the_guard(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        self.assertTrue(vm.available)
        self.assertEqual([r[0] for r in vm.sector_rows], ["ALPHA", "Information Technology (ETF, nicht zuordenbar)", "Nicht zugeordnet"])
        alpha, category, unassigned = vm.sector_rows
        self.assertEqual(alpha[:5], ["ALPHA", "750 €", "75,0 %", "600 €", "150 €"])
        self.assertEqual(alpha[5], "2 direkt, 1 ETF")
        self.assertEqual(alpha[7], "Über Limit (Limit 30 %, über um 45,0 %)")
        self.assertEqual((category[3], category[4], category[7]), (vs.DASH, "150 €", "nicht bewertet"))
        self.assertEqual((unassigned[3], unassigned[4], unassigned[5]), ("100 €", vs.DASH, "1 direkt"))
        self.assertEqual(vm.sector_levels, ["blocker", "info", "info"])
        self.assertEqual(vs.SECTOR_HEADERS, ["Sektor", "Exposure €", "Exposure %", "Direkt €", "ETF-Look-through €", "Positionen / ETF-Beiträge", "Tagesperformance", "Guard / Limit"])

    def test_the_position_table_says_where_every_entry_comes_from(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        origins = {row[0]: row[2] for row in vm.position_rows}
        self.assertEqual(origins["AAA"], "direkt")
        self.assertEqual(origins["WORLD ETF · Financials"], "ETF-Look-through (Financials, 50,0 % des ETF)")
        self.assertEqual(origins["WORLD ETF · Information Technology"], "ETF-Look-through (Information Technology, 50,0 % des ETF)")
        self.assertEqual(len(vm.position_rows), 5)

    def test_the_tooltip_tells_direct_from_look_through(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        direct = vm.sectors[0].positions[0]
        part = vm.sectors[0].positions[2]
        self.assertIn("Direkte Position (Einzelaktie)", direct.tip)
        self.assertIn("Beitrag aus ETF-Look-through", part.tip)
        self.assertIn("Financials", part.tip)
        self.assertIn("31.07.2026", part.tip)
        self.assertEqual((direct.lookthrough, part.lookthrough), (False, True))

    def test_company_entries_of_an_etf_are_named_and_marked(self) -> None:
        env = copy.deepcopy(ENVELOPE)
        company = position(3, "WORLD", 50.0, None, etf=True, sector="ALPHA", origin="etf_company", etf_weight=16.7, etf_value=300.0)
        company.update(companies=[{"name": "Position Corp", "value_eur": 30.0}, {"name": "Tkr Corp", "value_eur": 20.0}], company_count=3, etf_as_of="2026-10-14")
        env["sectors"][0]["positions"].append(company)
        env["etf_company_value_eur"] = 50.0
        vm = vs.build_sectors(env)
        row = next(r for r in vm.position_rows if r[0] == "WORLD ETF · Einzelfirmen")
        self.assertEqual(row[2], "ETF-Einzelfirmen (Position Corp, Tkr Corp +1; 16,7 % des ETF)")
        entry = vm.sectors[0].positions[-1]
        self.assertTrue(entry.lookthrough)
        self.assertIn("Beitrag aus ETF-Einzelfirmen", entry.tip)
        self.assertIn("14.10.2026", entry.tip)
        self.assertIn(("Davon ETF-Einzelfirmen (Sektor der Firma bekannt)", "50 € (5,0 %)"), vm.summary)
        self.assertTrue(any("Einzelfirmen" in n for n in vm.notes))

    def test_the_totals_match_the_portfolio_value(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        self.assertAlmostEqual(sum(s.value for s in vm.sectors), 750.0 + 150.0 + 100.0)
        self.assertAlmostEqual(sum(p.value for s in vm.sectors for p in s.positions), 1000.0)
        self.assertEqual(sum(len(s.positions) for s in vm.sectors), len(vm.position_rows))
        summary = dict(vm.summary)
        self.assertEqual(summary["Investierter Wert"], "1.000 €")
        self.assertEqual(summary["Direkt"], "700 € (70,0 %)")
        self.assertEqual(summary["Über ETF-Look-through"], "300 € (30,0 %)")
        self.assertEqual(summary["ETF-Kategorien nicht zuordenbar"], "150 € (15,0 %)")
        self.assertEqual(summary["Nicht zugeordnet"], "150 € (15,0 %)")
        self.assertEqual(summary["Look-through-Abdeckung"], "100 % des ETF-Werts (1 ETFs), davon 50 % einer Sektor-Gruppe zugeordnet")

    def test_the_etf_table_shows_status_stand_provider_and_mapped_share(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        self.assertEqual(vm.etf_rows, [["WORLD", "300 €", "angewendet", "31.07.2026", "Provider", "50 %"]])
        self.assertEqual(vm.etf_levels, [""])
        missing = copy.deepcopy(ENVELOPE)
        missing["etfs"] = [{"symbol": "WORLD", "market_value_eur": 300.0, "status": "NO_DATA", "as_of": None, "provider": None, "mapped_pct": None}]
        gap = vs.build_sectors(missing)
        self.assertEqual(gap.etf_rows, [["WORLD", "300 €", "keine Daten", vs.DASH, vs.DASH, vs.DASH]])
        self.assertEqual(gap.etf_levels, ["warning"])  # missing look-through data stays visible

    def test_a_missing_change_is_a_dash_and_neutral(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        world = next(p for s in vm.sectors for p in s.positions if p.symbol == "WORLD")
        self.assertIsNone(world.change_pct)
        self.assertEqual(world.color, treemap.NEUTRAL)
        row = next(r for r in vm.position_rows if r[0].startswith("WORLD"))
        self.assertEqual((row[5], row[6]), ("—", "ETF"))
        flat = next(p for s in vm.sectors for p in s.positions if p.symbol == "CCC")
        self.assertEqual(vs.fmt_signed_pct(flat.change_pct), "0,00 %")  # 0 % is not "no data"
        self.assertNotEqual(flat.color, treemap.NEUTRAL)
        self.assertEqual(vs.fmt_signed_pct(2.0), "+2,00 %")
        self.assertEqual(vs.fmt_signed_pct(None), "—")

    def test_an_etf_without_look_through_data_is_a_direct_position_marked_as_etf(self) -> None:
        env = copy.deepcopy(ENVELOPE)
        env["sectors"] = [group(None, [position(3, "WORLD", 300.0, None, etf=True, sector=None)], "NOT_ASSESSED")]
        world = vs.build_sectors(env).sectors[0].positions[0]
        self.assertEqual((world.lookthrough, world.label), (False, "WORLD ETF"))
        self.assertIn("ETF ohne Look-through-Daten", world.tip)

    def test_treemap_groups_have_the_group_values(self) -> None:
        vm = vs.build_sectors(ENVELOPE)
        groups = vm.treemap_groups()
        self.assertEqual([round(g[1]) for g in groups], [750, 150, 100])
        self.assertEqual([round(v) for _, _, members in groups for _, v in members], [400, 200, 150, 150, 100])

    def test_errors_and_empty_answers(self) -> None:
        self.assertFalse(vs.build_sectors(None).available)
        self.assertEqual(vs.build_sectors({"ok": False, "message": "kaputt"}).error, "kaputt")
        self.assertFalse(vs.build_sectors({"ok": True}).available)
        empty = vs.build_sectors({"ok": True, "sectors": [], "total_market_value_eur": 0.0})
        self.assertTrue(empty.available)
        self.assertEqual(empty.sector_rows, [])
        junk = vs.build_sectors({"ok": True, "sectors": [{"sector": "A", "positions": [{"symbol": "Z", "market_value_eur": "x"}, "bad"]}, 5]})
        self.assertEqual(junk.sectors[0].positions, [])  # a position without a number is skipped, nothing crashes
        old = vs.build_sectors({"ok": True, "sectors": [{"sector": "A", "market_value_eur": 10.0, "positions": []}], "total_market_value_eur": 10.0})  # an answer without the look-through fields
        self.assertEqual(old.sector_rows[0][:5], ["A", "10 €", "0,0 %", vs.DASH, vs.DASH])

    def test_unvalued_positions_and_warnings_are_shown_as_notes(self) -> None:
        env = dict(ENVELOPE, unvalued=[{"symbol": "ZZZ"}], warnings=["UNVALUED_POSITIONS: 1 open position(s) without a EUR valuation are not part of the values"])
        notes = vs.build_sectors(env).notes
        self.assertTrue(any("ZZZ" in n for n in notes))
        self.assertTrue(any(n.startswith("UNVALUED_POSITIONS") for n in notes))
        self.assertTrue(any("ETF-Look-through" in n and "Risk Guard" in n for n in notes))


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
        self.assertTrue(pump(15, lambda: self.view.sector_table.rowCount() == 3), "the sector overview did not load")

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
        self.assertEqual([v.sector_table.item(r, 0).text() for r in range(3)], ["ALPHA", "Information Technology (ETF, nicht zuordenbar)", "Nicht zugeordnet"])
        self.assertEqual(v.sector_table.item(1, 7).text(), "nicht bewertet")
        self.assertEqual(v.position_table.rowCount(), 5)
        self.assertEqual([v.etf_table.item(0, c).text() for c in range(3)], ["WORLD", "300 €", "angewendet"])
        self.assertTrue(v.treemap.tiles)
        self.assertEqual(len(v.treemap.tiles), 3)
        self.assertEqual(sum(len(t.children) for t in v.treemap.tiles), 5)
        tip = v.treemap.tile_at(__import__("PySide6.QtCore", fromlist=["QPointF"]).QPointF(*[c + s / 2 for c, s in zip(v.treemap.tiles[0].children[0].rect[:2], v.treemap.tiles[0].children[0].rect[2:])]))
        self.assertIn("Marktwert", tip)
        part = v.treemap.tiles[0].children[2]  # the ETF contribution inside ALPHA says that it is not a direct position
        centre = __import__("PySide6.QtCore", fromlist=["QPointF"]).QPointF(part.rect[0] + part.rect[2] / 2, part.rect[1] + part.rect[3] / 2)
        self.assertIn("ETF-Look-through", v.treemap.tile_at(centre))

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
        self.assertEqual((self.view.sector_table.rowCount(), self.view.etf_table.rowCount()), (0, 0))
        self.assertTrue(self.view.button.isEnabled())
        self.view.treemap.grab()  # painting without data does not fail


if __name__ == "__main__":
    unittest.main()
