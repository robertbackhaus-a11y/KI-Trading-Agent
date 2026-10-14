"""The read-only sector overview (``get_sector_overview``): exposure sums, the unassigned group, resolution of the two-layer sector map, the daily change with
missing data, ETFs as normal positions, the guard status; synthetic data only."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import sector_assignment as sa  # noqa: E402
import sector_overview as so  # noqa: E402
from _fixtures import MARKET_END, make_connection  # noqa: E402

PUBLIC = {"map_id": "sector_map_v1", "version": 1, "description": "synthetic", "sectors": {"ALPHA": ["POS"], "BETA": ["OTHER"]}}


class OverviewCase(unittest.TestCase):
    """Positions: POS (stock, EUR 600, ALPHA), WATCH (stock, EUR 300, not in the map), ETF (etf, EUR 100, not in the map, no price history)."""

    def setUp(self) -> None:
        self.conn = make_connection()
        self.addCleanup(self.conn.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.universe = Path(tmp.name)
        self.write_public(PUBLIC)
        self.conn.execute("UPDATE positions SET shares = 10, avg_cost = 50, currency = 'EUR' WHERE security_id = 1")
        self.conn.execute("UPDATE market_snapshot SET price = 60, currency = 'EUR' WHERE security_id = 1")
        stamp = MARKET_END.isoformat() + "T00:00:00+00:00"
        for sid, shares, price in ((2, 3, 100.0), (3, 1, 100.0)):
            self.conn.execute("INSERT INTO positions(security_id, shares, avg_cost, remaining_cost_basis, currency, invested_amount, realized_gain, transaction_count) VALUES (?, ?, 10, 10, 'EUR', 10, 0, 1)", (sid, shares))
            self.conn.execute("INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (?, ?, ?, 'EUR')", (sid, stamp, price))
        self.conn.execute("DELETE FROM market_data WHERE security_id = 3")  # the ETF has no daily bars

    def write_public(self, payload) -> None:
        (self.universe / sa.PUBLIC_NAME).write_text(json.dumps(payload), encoding="utf-8")

    def write_local(self, sectors) -> None:
        (self.universe / sa.LOCAL_NAME).write_text(json.dumps({"map_id": "sector_map_local", "version": 1, "sectors": sectors}), encoding="utf-8")

    def overview(self, **kwargs) -> dict:
        return so.build_sector_overview(self.conn, as_of=MARKET_END.isoformat(), universe_dir=self.universe, **kwargs)

    def group(self, result: dict, sector):
        return next(g for g in result["sectors"] if g["sector"] == sector)


class ExposureTests(OverviewCase):
    def test_the_sector_values_add_up_to_the_portfolio_value(self) -> None:
        result = self.overview()
        self.assertAlmostEqual(result["total_market_value_eur"], 1000.0)
        self.assertAlmostEqual(sum(g["market_value_eur"] for g in result["sectors"]), result["total_market_value_eur"])
        self.assertAlmostEqual(sum(g["weight_pct"] for g in result["sectors"]), 100.0)
        self.assertAlmostEqual(sum(p["weight_pct"] for g in result["sectors"] for p in g["positions"]), 100.0)
        self.assertTrue(result["checks"]["reconciles"])
        self.assertEqual(result["position_count"], sum(g["position_count"] for g in result["sectors"]))
        self.assertAlmostEqual(result["assigned_value_eur"] + result["unassigned_value_eur"], 1000.0)

    def test_unassigned_positions_are_not_swallowed(self) -> None:
        result = self.overview()
        unassigned = self.group(result, None)
        self.assertFalse(unassigned["assigned"])
        self.assertEqual(sorted(p["symbol"] for p in unassigned["positions"]), ["ETF", "WATCH"])
        self.assertAlmostEqual(unassigned["market_value_eur"], 400.0)
        self.assertEqual(unassigned["guard_status"], so.GUARD_NOT_ASSESSED)
        self.assertIsNone(unassigned["limit_pct"])
        self.assertEqual(result["sectors"][-1]["sector"], None)  # listed last

    def test_a_missing_sector_map_leaves_every_position_unassigned_with_a_warning(self) -> None:
        (self.universe / sa.PUBLIC_NAME).unlink()
        result = self.overview()
        self.assertEqual([g["sector"] for g in result["sectors"]], [None])
        self.assertAlmostEqual(result["sectors"][0]["market_value_eur"], 1000.0)
        self.assertTrue(any(w.startswith("SECTOR_MAP_UNAVAILABLE") for w in result["warnings"]))

    def test_a_position_without_a_valuation_is_listed_apart_and_never_counted_as_zero(self) -> None:
        self.conn.execute("DELETE FROM market_snapshot WHERE security_id = 2")
        result = self.overview()
        self.assertEqual([p["symbol"] for p in result["unvalued"]], ["WATCH"])
        self.assertAlmostEqual(result["total_market_value_eur"], 700.0)
        self.assertTrue(any(w.startswith("UNVALUED_POSITIONS") for w in result["warnings"]))
        self.assertIsNone(result["checks"]["context_total_eur"])  # the portfolio context has no total either while a position is unvalued
        self.assertIsNone(result["checks"]["reconciles"])


class ResolutionTests(OverviewCase):
    def test_the_local_entry_overrides_the_public_one_and_adds_unmapped_symbols(self) -> None:
        self.write_local({"BETA": ["POS", "WATCH"]})
        result = self.overview()
        self.assertIsNone(next((g for g in result["sectors"] if g["sector"] == "ALPHA"), None))
        beta = self.group(result, "BETA")
        self.assertEqual(sorted(p["symbol"] for p in beta["positions"]), ["POS", "WATCH"])
        self.assertEqual({p["sector_source"] for p in beta["positions"]}, {"local"})
        self.assertEqual(self.group(result, None)["position_count"], 1)

    def test_the_overview_uses_the_same_map_as_the_guard(self) -> None:
        import portfolio_risk as pr

        self.write_local({"BETA": ["WATCH"]})
        guard = pr.read_sector_map(self.universe / sa.PUBLIC_NAME)
        result = self.overview()
        for g in result["sectors"]:
            for p in g["positions"]:
                self.assertEqual(g["sector"], guard.get(p["symbol"]), p["symbol"])


class PerformanceTests(OverviewCase):
    def bars(self, sid: int):
        return [r[0] for r in self.conn.execute("SELECT close FROM market_data WHERE security_id = ? ORDER BY trade_date DESC LIMIT 2", (sid,))]

    def test_the_daily_change_is_close_over_previous_close(self) -> None:
        latest, previous = self.bars(1)
        result = self.overview()
        position = next(p for g in result["sectors"] for p in g["positions"] if p["symbol"] == "POS")
        self.assertAlmostEqual(position["day_change_pct"], (latest / previous - 1) * 100)
        self.assertEqual(position["day_change_date"], MARKET_END.isoformat())

    def test_a_missing_change_is_none_never_zero(self) -> None:
        result = self.overview()
        etf = next(p for g in result["sectors"] for p in g["positions"] if p["symbol"] == "ETF")
        self.assertIsNone(etf["day_change_pct"])
        self.assertIsNone(so.group_change_pct([etf | {"market_value_eur": 1.0}])[0])

    def test_the_group_change_is_value_weighted_over_the_positions_that_have_one(self) -> None:
        result = self.overview()
        unassigned = self.group(result, None)
        watch = next(p for p in unassigned["positions"] if p["symbol"] == "WATCH")
        self.assertAlmostEqual(unassigned["day_change_pct"], watch["day_change_pct"])  # the ETF without data does not dilute it
        self.assertAlmostEqual(unassigned["day_coverage_pct"], 300.0 / 400.0 * 100)
        self.assertAlmostEqual(self.group(result, "ALPHA")["day_coverage_pct"], 100.0)

    def test_the_weighting_is_exact(self) -> None:
        positions = [{"market_value_eur": 110.0, "day_change_pct": 10.0}, {"market_value_eur": 90.0, "day_change_pct": -10.0}]
        change, coverage = so.group_change_pct(positions)
        self.assertAlmostEqual(change, (200.0 / (100.0 + 90.0 / 0.9) - 1) * 100)
        self.assertAlmostEqual(coverage, 100.0)

    def test_a_stale_or_single_bar_gives_no_change(self) -> None:
        self.conn.execute("DELETE FROM market_data WHERE security_id = 1 AND trade_date > ?", ((MARKET_END - timedelta(days=30)).isoformat(),))
        self.assertIsNone(so.day_changes(self.conn, [1], MARKET_END.isoformat())[1]["pct"])
        self.conn.execute("DELETE FROM market_data WHERE security_id = 2 AND trade_date < ?", (MARKET_END.isoformat(),))
        self.assertIsNone(so.day_changes(self.conn, [2], MARKET_END.isoformat())[2]["pct"])


class EtfAndGuardTests(OverviewCase):
    def test_an_etf_is_one_normal_position_and_marked(self) -> None:
        self.write_local({"BETA": ["ETF"]})
        result = self.overview()
        beta = self.group(result, "BETA")
        self.assertEqual([(p["symbol"], p["is_etf"]) for p in beta["positions"]], [("ETF", True)])
        self.assertEqual(beta["etf_count"], 1)
        self.assertEqual(result["position_count"], 3)  # nothing decomposed
        self.assertFalse(next(p for g in result["sectors"] for p in g["positions"] if p["symbol"] == "POS")["is_etf"])

    def test_the_guard_status_follows_the_limit(self) -> None:
        result = self.overview()  # ALPHA holds 60 % of the invested value
        alpha = self.group(result, "ALPHA")
        self.assertEqual((alpha["limit_pct"], alpha["guard_status"]), (30.0, so.GUARD_OVER))
        self.assertAlmostEqual(alpha["headroom_pct"], 30.0 - 60.0)
        relaxed = self.group(self.overview(max_sector_weight=0.8), "ALPHA")
        self.assertEqual((relaxed["limit_pct"], relaxed["guard_status"]), (80.0, so.GUARD_OK))

    def test_the_default_limit_is_the_one_of_the_guard(self) -> None:
        from strategy_config import RiskGuardConfig

        self.assertAlmostEqual(self.overview()["limit_pct"], RiskGuardConfig().max_sector_weight * 100)


class ReadOnlyTests(OverviewCase):
    def test_nothing_is_written(self) -> None:
        before = self.conn.total_changes
        self.write_local({"BETA": ["WATCH"]})
        files = sorted(p.name for p in self.universe.iterdir())
        self.overview()
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(sorted(p.name for p in self.universe.iterdir()), files)  # no .bak, no temp file

    def test_the_tool_is_registered_read_only_and_never_a_write(self) -> None:
        import importlib.util
        import inspect

        spec = importlib.util.spec_from_file_location("trading_sqlite_overview_test", ROOT / "mcp-tools" / "trading_sqlite.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertEqual(list(inspect.signature(module.Tools.get_sector_overview).parameters), ["self", "as_of"])
        source = (ROOT / "tools" / "trading" / "sector_overview.py").read_text(encoding="utf-8")
        for forbidden in ("INSERT", "UPDATE ", "DELETE", "write_text", "os.replace", "commit("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
