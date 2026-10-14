"""ETF look-through in the sector overview: allocation files, the category mapping, the split of an ETF, sums, double counting, missing / invalid / stale data,
unmapped categories and the guard status on the effective exposure; synthetic data only."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import etf_lookthrough as lt  # noqa: E402
import sector_assignment as sa  # noqa: E402
import sector_overview as so  # noqa: E402
from _fixtures import MARKET_END  # noqa: E402
from test_sector_overview import OverviewCase  # noqa: E402

ETF_ISIN = "IE00TESTETF1"
MAPPING = {"map_id": "etf_sector_mapping_v1", "version": 1, "mapping": {"Financials": "ALPHA", "Health Care": "BETA"},
           "not_mapped": {"Information Technology": "spans several groups", "Cash": "not a sector"}}
ETF_ENTRY = {"name": "Synthetic World ETF", "provider": "Synthetic", "as_of": MARKET_END.isoformat(), "source": "https://example.invalid/factsheet",
             "sectors": {"Financials": 40.0, "Health Care": 30.0, "Information Technology": 25.0, "Cash": 5.0}}


class LookthroughCase(OverviewCase):
    """POS (EUR 600, ALPHA), WATCH (EUR 300, not assigned) and the ETF (EUR 400 after the setUp below)."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.execute("ALTER TABLE security ADD COLUMN isin TEXT")
        self.conn.execute("UPDATE security SET isin = ? WHERE id = 3", (ETF_ISIN,))
        self.conn.execute("UPDATE market_snapshot SET price = 400 WHERE security_id = 3")
        self.write_public({**{"map_id": "sector_map_v1", "version": 1}, "sectors": {"ALPHA": ["POS"], "BETA": ["OTHER"]}})
        self.write_mapping(MAPPING)
        self.write_etfs({ETF_ISIN: ETF_ENTRY})

    def write_mapping(self, payload) -> None:
        (self.universe / lt.MAPPING_NAME).write_text(json.dumps(payload), encoding="utf-8")

    def write_etfs(self, etfs) -> None:
        (self.universe / lt.ALLOCATION_NAME).write_text(json.dumps({"map_id": "etf_lookthrough_local", "version": 1, "etfs": etfs}), encoding="utf-8")

    def group(self, result, sector=None, kind=None, label=None):
        return next(g for g in result["sectors"] if (sector is None or g["sector"] == sector) and (kind is None or g["kind"] == kind) and (label is None or g["label"] == label))

    def entries_of(self, result, symbol):
        return [p for g in result["sectors"] for p in g["positions"] if p["symbol"] == symbol]


class SplitTests(LookthroughCase):
    def test_the_etf_is_split_into_its_categories_and_the_sums_reconcile(self) -> None:
        result = self.overview()
        self.assertAlmostEqual(result["total_market_value_eur"], 1300.0)
        alpha, beta = self.group(result, "ALPHA"), self.group(result, "BETA")
        self.assertAlmostEqual(alpha["market_value_eur"], 760.0)  # 600 direct + 40 % of 400
        self.assertAlmostEqual(alpha["direct_value_eur"], 600.0)
        self.assertAlmostEqual(alpha["etf_value_eur"], 160.0)
        self.assertAlmostEqual(beta["market_value_eur"], 120.0)
        self.assertAlmostEqual(beta["direct_value_eur"], 0.0)
        self.assertAlmostEqual(sum(g["market_value_eur"] for g in result["sectors"]), result["total_market_value_eur"])
        self.assertTrue(result["checks"]["reconciles"])
        self.assertTrue(result["checks"]["groups_sum_matches_total"])
        self.assertAlmostEqual(sum(g["weight_pct"] for g in result["sectors"]), 100.0)
        self.assertAlmostEqual(alpha["direct_weight_pct"] + alpha["etf_weight_pct"], alpha["weight_pct"])

    def test_direct_exposure_is_unchanged_by_the_look_through(self) -> None:
        with_lt, without = self.overview(), self.overview(lookthrough=False)
        self.assertAlmostEqual(self.group(without, "ALPHA")["market_value_eur"], 600.0)
        self.assertAlmostEqual(self.group(with_lt, "ALPHA")["direct_value_eur"], self.group(without, "ALPHA")["market_value_eur"])
        self.assertAlmostEqual(with_lt["direct_value_eur"], 600.0 + 300.0)  # the two stocks
        self.assertAlmostEqual(without["direct_value_eur"], 1300.0)  # the ETF counts as a direct position when it is not decomposed
        self.assertEqual(without["etfs"], [])
        self.assertAlmostEqual(without["total_market_value_eur"], with_lt["total_market_value_eur"])

    def test_nothing_is_counted_twice(self) -> None:
        self.write_local({"BETA": ["ETF"]})  # a direct local entry of the ETF must not add its value a second time
        result = self.overview()
        parts = self.entries_of(result, "ETF")
        self.assertTrue(parts and all(p["origin"] == so.ORIGIN_ETF for p in parts))
        self.assertAlmostEqual(sum(p["market_value_eur"] for p in parts), 400.0)
        self.assertAlmostEqual(self.group(result, "BETA")["direct_value_eur"], 0.0)
        self.assertAlmostEqual(sum(g["market_value_eur"] for g in result["sectors"]), 1300.0)
        self.assertEqual(sum(1 for p in parts if p["origin"] == so.ORIGIN_DIRECT), 0)

    def test_every_entry_says_where_it_comes_from(self) -> None:
        result = self.overview()
        origins = {(p["symbol"], p["origin"]) for g in result["sectors"] for p in g["positions"]}
        self.assertEqual(origins, {("POS", "direct"), ("WATCH", "direct"), ("ETF", "etf_lookthrough")})
        etf_part = next(p for p in self.entries_of(result, "ETF") if p["category"] == "Financials")
        self.assertEqual((etf_part["etf_weight_pct"], round(etf_part["etf_value_eur"])), (40.0, 400))
        self.assertEqual(etf_part["etf_as_of"], MARKET_END.isoformat())

    def test_a_contribution_carries_the_daily_change_of_the_etf(self) -> None:
        self.conn.executemany("INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id) VALUES (3, ?, ?, ?, 1)",
                              [(MARKET_END.isoformat(), 110.0, 110.0), ((MARKET_END - timedelta(days=1)).isoformat(), 100.0, 100.0)])
        result = self.overview()
        changes = {p["day_change_pct"] for p in self.entries_of(result, "ETF")}
        self.assertEqual(len(changes), 1)
        self.assertAlmostEqual(changes.pop(), 10.0)


class UnmappedAndMissingTests(LookthroughCase):
    def test_categories_that_fit_no_single_group_stay_visible_as_their_own_groups(self) -> None:
        result = self.overview()
        it = self.group(result, kind=so.KIND_ETF_CATEGORY, label="Information Technology")
        cash = self.group(result, kind=so.KIND_ETF_CATEGORY, label="Cash")
        self.assertAlmostEqual(it["market_value_eur"], 100.0)
        self.assertAlmostEqual(cash["market_value_eur"], 20.0)
        for g in (it, cash):
            self.assertIsNone(g["sector"])
            self.assertEqual(g["guard_status"], so.GUARD_NOT_ASSESSED)
            self.assertIsNone(g["limit_pct"])
            self.assertEqual(g["direct_value_eur"], 0.0)
        reasons = {p["category"]: p["category_reason"] for g in (it, cash) for p in g["positions"]}
        self.assertEqual(reasons, {"Information Technology": "spans several groups", "Cash": "not a sector"})

    def test_an_unknown_category_is_unmapped_never_guessed(self) -> None:
        self.write_etfs({ETF_ISIN: {**ETF_ENTRY, "sectors": {"Financials": 90.0, "Space Mining": 10.0}}})
        result = self.overview()
        space = self.group(result, kind=so.KIND_ETF_CATEGORY, label="Space Mining")
        self.assertEqual(space["positions"][0]["category_reason"], "UNKNOWN_CATEGORY")
        self.assertAlmostEqual(space["market_value_eur"], 40.0)

    def test_direct_unassigned_positions_stay_in_not_assigned(self) -> None:
        result = self.overview()
        unassigned = self.group(result, kind=so.KIND_UNASSIGNED)
        self.assertEqual([p["symbol"] for p in unassigned["positions"]], ["WATCH"])
        self.assertEqual(result["sectors"][-1]["kind"], so.KIND_UNASSIGNED)  # listed last

    def test_an_etf_without_allocation_data_stays_one_visible_position(self) -> None:
        self.write_etfs({})
        result = self.overview()
        etf = result["etfs"][0]
        self.assertEqual((etf["status"], etf["categories"]), (lt.NO_DATA, 0))
        unassigned = self.group(result, kind=so.KIND_UNASSIGNED)
        self.assertEqual(sorted(p["symbol"] for p in unassigned["positions"]), ["ETF", "WATCH"])
        self.assertAlmostEqual(unassigned["market_value_eur"], 700.0)
        self.assertEqual(result["lookthrough"]["coverage_pct"], 0.0)
        self.assertTrue(any(w.startswith("LOOKTHROUGH_NO_DATA") for w in result["warnings"]))
        self.assertAlmostEqual(sum(g["market_value_eur"] for g in result["sectors"]), 1300.0)

    def test_an_etf_without_an_isin_cannot_get_an_allocation(self) -> None:
        self.conn.execute("UPDATE security SET isin = NULL WHERE id = 3")
        result = self.overview()
        self.assertEqual(result["etfs"][0]["status"], lt.NO_ISIN)
        self.assertEqual([p["origin"] for p in self.entries_of(result, "ETF")], ["direct"])

    def test_missing_look_through_files_never_fail_the_overview(self) -> None:
        (self.universe / lt.MAPPING_NAME).unlink()
        result = self.overview()
        self.assertTrue(result["ok"])
        self.assertTrue(any(w.startswith("LOOKTHROUGH_UNAVAILABLE") for w in result["warnings"]))
        self.assertEqual(result["etfs"][0]["status"], lt.NO_DATA)
        self.assertAlmostEqual(sum(g["market_value_eur"] for g in result["sectors"]), 1300.0)


class DataQualityTests(LookthroughCase):
    def test_weights_that_do_not_add_up_to_about_100_percent_are_rejected(self) -> None:
        self.write_etfs({ETF_ISIN: {**ETF_ENTRY, "sectors": {"Financials": 40.0, "Health Care": 30.0}}})
        result = self.overview()
        self.assertEqual(result["etfs"][0]["status"], lt.INVALID)
        self.assertIn("not about 100", result["etfs"][0]["detail"])
        self.assertEqual([p["origin"] for p in self.entries_of(result, "ETF")], ["direct"])

    def test_rounding_up_to_the_tolerance_is_normalised_so_no_value_is_created(self) -> None:
        self.write_etfs({ETF_ISIN: {**ETF_ENTRY, "sectors": {"Financials": 50.05, "Health Care": 50.05}}})  # 100.1 %
        result = self.overview()
        self.assertAlmostEqual(sum(p["market_value_eur"] for p in self.entries_of(result, "ETF")), 400.0)
        self.assertAlmostEqual(sum(g["market_value_eur"] for g in result["sectors"]), 1300.0)

    def test_invalid_entries_are_rejected_one_by_one(self) -> None:
        bad = {"A": {**ETF_ENTRY, "sectors": {"Financials": -1.0, "Health Care": 101.0}}, "B": {**ETF_ENTRY, "as_of": "soon"}, "C": {**ETF_ENTRY, "source": ""},
               "D": {**ETF_ENTRY, "sectors": {}}, "E": "text", "F": {**ETF_ENTRY, "sectors": {"Financials": True}}, ETF_ISIN: ETF_ENTRY}
        self.write_etfs(bad)
        layers = lt.read_layers(self.universe)
        self.assertEqual(sorted(layers.invalid), ["A", "B", "C", "D", "E", "F"])
        self.assertEqual(list(layers.allocations), [ETF_ISIN])  # one bad entry does not take the good one down

    def test_an_old_allocation_is_used_but_flagged(self) -> None:
        self.write_etfs({ETF_ISIN: {**ETF_ENTRY, "as_of": "2020-01-01"}})
        result = self.overview()
        self.assertEqual(result["etfs"][0]["status"], lt.STALE)
        self.assertGreater(len(self.entries_of(result, "ETF")), 1)
        self.assertTrue(any(w.startswith("LOOKTHROUGH_STALE") for w in result["warnings"]))

    def test_a_mapping_to_an_unknown_group_is_treated_as_not_mapped(self) -> None:
        self.write_mapping({**MAPPING, "mapping": {"Financials": "NO_SUCH_GROUP", "Health Care": "BETA"}})
        result = self.overview()
        self.assertTrue(any(w.startswith("MAPPING_GROUP_UNKNOWN") for w in result["warnings"]))
        fin = self.group(result, kind=so.KIND_ETF_CATEGORY, label="Financials")
        self.assertEqual(fin["positions"][0]["category_reason"], "GROUP_UNKNOWN")

    def test_the_allocation_weights_of_the_split_are_plausible(self) -> None:
        result = self.overview()
        shares = [p["etf_weight_pct"] for p in self.entries_of(result, "ETF")]
        self.assertAlmostEqual(sum(shares), 100.0)
        self.assertTrue(all(0.0 < s <= 100.0 for s in shares))

    def test_coverage_figures(self) -> None:
        result = self.overview()
        info = result["lookthrough"]
        self.assertEqual((info["etf_count"], round(info["etf_value_eur"]), info["coverage_pct"]), (1, 400, 100.0))
        self.assertAlmostEqual(info["mapped_pct"], 70.0)  # Financials 40 % + Health Care 30 % land in a group
        self.assertAlmostEqual(result["assigned_value_eur"], 600.0 + 160.0 + 120.0)
        self.assertAlmostEqual(result["etf_category_value_eur"], 120.0)
        self.assertAlmostEqual(result["unassigned_value_eur"], 300.0)
        etf = result["etfs"][0]
        self.assertEqual((etf["status"], etf["provider"], etf["as_of"]), (lt.APPLIED, "Synthetic", MARKET_END.isoformat()))


class GuardBasisTests(LookthroughCase):
    def test_the_guard_status_of_the_overview_uses_the_effective_exposure(self) -> None:
        result = self.overview(max_sector_weight=0.5)  # ALPHA: direct 600 / 1300 = 46.2 %, effective 760 / 1300 = 58.5 %
        alpha = self.group(result, "ALPHA")
        self.assertLess(alpha["direct_weight_pct"], 50.0)
        self.assertGreater(alpha["weight_pct"], 50.0)
        self.assertEqual(alpha["guard_status"], so.GUARD_OVER)

    def test_the_risk_guard_itself_still_reads_direct_sectors_only(self) -> None:
        import portfolio_risk as pr

        self.write_local({"BETA": ["ETF"]})
        guard = pr.read_sector_map(self.universe / sa.PUBLIC_NAME)
        self.assertEqual(guard.get("ETF"), "BETA")  # a direct entry, as before; the look-through never writes into the sector maps
        source = (ROOT / "tools" / "trading" / "portfolio_risk.py").read_text(encoding="utf-8")
        self.assertNotIn("etf_lookthrough", source)

    def test_nothing_is_written(self) -> None:
        before = sorted((p.name, p.read_bytes()) for p in self.universe.iterdir())
        self.overview()
        self.assertEqual(sorted((p.name, p.read_bytes()) for p in self.universe.iterdir()), before)


class UnitTests(unittest.TestCase):
    def test_contributions_add_up_and_are_sorted(self) -> None:
        allocation = lt.Allocation("X", "n", "p", "2026-01-01", "s", {"A": 25.0, "B": 50.0, "C": 25.1, "Zero": 0.0})
        mapping, _ = lt.read_mapping_from({"mapping": {"A": "G1"}})
        parts = lt.contributions(allocation, 1000.0, mapping)
        self.assertAlmostEqual(sum(p["value_eur"] for p in parts), 1000.0)
        self.assertEqual([p["category"] for p in parts], ["B", "C", "A"])  # no zero weight entry, largest first
        self.assertEqual({p["category"]: p["group"] for p in parts}, {"A": "G1", "B": None, "C": None})

    def test_category_matching_ignores_case_and_spaces(self) -> None:
        mapping, _ = lt.read_mapping_from({"mapping": {"Health Care": "H"}})
        self.assertEqual(mapping.group_of("  health care ")[0], "H")
        self.assertEqual(mapping.group_of("Health"), (None, "UNKNOWN_CATEGORY"))

    def test_the_shipped_mapping_only_uses_existing_sector_groups_and_the_gics_sectors(self) -> None:
        universe = ROOT / "tools" / "trading" / "universe"
        groups = set(json.loads((universe / "sector_map_v1.json").read_text(encoding="utf-8"))["sectors"])
        payload = json.loads((universe / lt.MAPPING_NAME).read_text(encoding="utf-8"))
        self.assertTrue(set(payload["mapping"].values()) <= groups)
        self.assertEqual(sorted(payload["mapping"]), ["Consumer Staples", "Energy", "Financials", "Health Care", "Materials", "Utilities"])
        gics = {"information technology", "financials", "health care", "consumer discretionary", "consumer staples", "industrials", "energy", "utilities", "materials", "real estate"}
        covered = {k.lower() for k in [*payload["mapping"], *payload["not_mapped"]]}
        self.assertTrue(gics <= covered, gics - covered)  # every GICS sector is decided: mapped, or listed with the reason it is not
        self.assertFalse(set(payload["mapping"]) & set(payload["not_mapped"]))
        layers = lt.read_layers(universe, groups)
        self.assertEqual(layers.warnings, ())

    def test_the_private_allocation_file_is_git_ignored_and_protected_from_the_deploy(self) -> None:
        self.assertIn("tools/trading/universe/*_local.json", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "deploy" / "deploy-manifest.json").read_text(encoding="utf-8"))
        self.assertIn("app/universe/" + lt.ALLOCATION_NAME, manifest["protected"])
        files = [f for g in manifest["groups"] if g["name"] == "app-universe" for f in g["files"]]
        self.assertIn(lt.MAPPING_NAME, files)
        self.assertNotIn(lt.ALLOCATION_NAME, files)


if __name__ == "__main__":
    unittest.main()
