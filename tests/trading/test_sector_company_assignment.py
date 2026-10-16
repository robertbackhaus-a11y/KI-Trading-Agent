"""The company-based sector assignment of the sector overview: a company inside an ETF counts in the project sector its own security has (same matching as the effective
exposure: ISIN, reviewed alias, ticker + exchange, never fuzzy); everything else stays in its provider category (mapped one-to-one or NOT_ASSESSED); the total never changes."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import effective_exposure as ee  # noqa: E402
import etf_holdings as eh  # noqa: E402
import etf_lookthrough as lt  # noqa: E402
import sector_overview as so  # noqa: E402
from _fixtures import MARKET_END  # noqa: E402
from test_sector_overview import OverviewCase  # noqa: E402

AS_OF = MARKET_END.isoformat()
ETF1, ETF2 = "IE00TESTETF1", "IE00TESTETF2"
MAPPING = {"map_id": "etf_sector_mapping_v1", "version": 1, "mapping": {"Financials": "ALPHA", "Health Care": "BETA"},
           "not_mapped": {"Information Technology": "spans several groups", "Cash": "not a sector"}}


def holding(name, weight, ticker="", isin="", exchange="", sector="", asset="Equity"):
    return {"name": name, "ticker": ticker, "isin": isin, "weight": weight, "asset_class": asset, "exchange": exchange, "sector": sector, "location": ""}


def etf_entry(holdings):
    return {"name": "Synthetic ETF", "provider": "Synthetic", "as_of": AS_OF, "source": "https://example.invalid/holdings", "kind": "physical", "holdings": holdings}


def allocation(sectors):
    return {"name": "Synthetic ETF", "provider": "Synthetic", "as_of": AS_OF, "source": "https://example.invalid/factsheet", "sectors": sectors}


class CompanyCase(OverviewCase):
    """Stocks: POS 600 (ALPHA, ISIN POS), WATCH 300 (no sector).  ETF1 400, ETF2 200.  Known to the database but not held: ALI (BETA, by alias), TKR (ALPHA, by ticker + exchange),
    NOSEC (an ISIN, no sector).

    ETF1 holds Pos 10 % (ISIN), Alias 5 % (alias), Tkr 5 % (ticker + exchange), NoSec 10 %, Unknown 20 % (not in the database), a look-alike of Pos 5 % (name only)
    ETF2 holds Pos 20 %.  Allocations: ETF1 Financials 40 / Health Care 20 / IT 35 / Cash 5, ETF2 Financials 60 / IT 40."""

    def setUp(self) -> None:
        super().setUp()
        c = self.conn
        c.execute("ALTER TABLE security ADD COLUMN isin TEXT")
        c.execute("ALTER TABLE security ADD COLUMN exchange TEXT")
        c.execute("UPDATE security SET isin = 'US0000POS001', exchange = 'NASDAQ' WHERE id = 1")
        c.execute("UPDATE security SET isin = ?, exchange = 'XETRA' WHERE id = 3", (ETF1,))
        c.execute("UPDATE market_snapshot SET price = 400 WHERE security_id = 3")
        for sid, symbol, name, kind, isin, exchange in ((4, "ETF2", "Second ETF", "etf", ETF2, "LSE"), (5, "ALI", "Alias Corp", "stock", "US0000ALI001", "NYSE"),
                                                         (6, "TKR", "Tkr Corp", "stock", "US0000TKR001", "XETRA"), (7, "NOSEC", "NoSec Corp", "stock", "US0000NOS001", "NYSE")):
            c.execute("INSERT INTO security(id, symbol, name, asset_type, isin, exchange) VALUES (?, ?, ?, ?, ?, ?)", (sid, symbol, name, kind, isin, exchange))
        c.execute("UPDATE security SET name = 'Position Corp' WHERE id = 1")
        c.execute("INSERT INTO positions(security_id, shares, avg_cost, remaining_cost_basis, currency, invested_amount, realized_gain, transaction_count) VALUES (4, 1, 10, 10, 'EUR', 10, 0, 1)")
        c.execute("INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (4, ?, 200, 'EUR')", (AS_OF + "T00:00:00+00:00",))
        self.write_public({"map_id": "sector_map_v1", "version": 1, "sectors": {"ALPHA": ["POS", "TKR"], "BETA": ["OTHER", "ALI"]}})
        (self.universe / lt.MAPPING_NAME).write_text(json.dumps(MAPPING), encoding="utf-8")
        self.write_allocations({ETF1: allocation({"Financials": 40.0, "Health Care": 20.0, "Information Technology": 35.0, "Cash": 5.0}),
                                ETF2: allocation({"Financials": 60.0, "Information Technology": 40.0})})
        self.write_holdings(self.default_holdings())

    @staticmethod
    def default_holdings():
        return {
            ETF1: etf_entry([holding("Pos Corp", 10.0, isin="US0000POS001", sector="Financials"),
                             holding("Alias Corp ADR", 5.0, ticker="ALI.L", exchange="London Stock Exchange", sector="Health Care"),
                             holding("Tkr Corp", 5.0, ticker="TKR", exchange="XETRA", sector="Financials"),
                             holding("NoSec Corp", 10.0, isin="US0000NOS001", sector="Information Technology"),
                             holding("Unknown Inc", 20.0, isin="US0000UNK001", sector="Information Technology"),
                             holding("POSITION CORPORATION", 5.0, sector="Financials"),
                             holding("Filler One", 45.0, isin="US0000FL1001", sector="Information Technology")]),  # the weights of a physical fund add up to about 100 %
            ETF2: etf_entry([holding("Pos Corp", 20.0, isin="US0000POS001", sector="Financials"), holding("Filler Two", 80.0, isin="US0000FL2001", sector="Information Technology")]),
        }

    def write_allocations(self, etfs) -> None:
        (self.universe / lt.ALLOCATION_NAME).write_text(json.dumps({"map_id": "etf_lookthrough_local", "version": 1, "etfs": etfs}), encoding="utf-8")

    def write_holdings(self, etfs, aliases=None) -> None:
        aliases = [{"ticker": "ALI.L", "exchange": "London Stock Exchange", "isin": "US0000ALI001", "reason": "the ADR of this ordinary share"}] if aliases is None else aliases
        (self.universe / eh.FILE_NAME).write_text(json.dumps({"map_id": "etf_holdings_local", "version": 1, "etfs": etfs, "aliases": aliases}), encoding="utf-8")

    def sector_group(self, result, sector):
        return next(g for g in result["sectors"] if g["sector"] == sector)

    def company_entries(self, result, sector):
        return [p for p in self.sector_group(result, sector)["positions"] if p["origin"] == so.ORIGIN_COMPANY]


class AssignmentTests(CompanyCase):
    def test_a_company_in_an_etf_lands_in_the_sector_of_its_security(self) -> None:
        result = self.overview()
        alpha = self.sector_group(result, "ALPHA")
        direct = [p for p in alpha["positions"] if p["origin"] == so.ORIGIN_DIRECT]
        self.assertEqual([(p["symbol"], round(p["market_value_eur"])) for p in direct], [("POS", 600)])  # the direct position: unchanged
        names = {c["name"] for p in self.company_entries(result, "ALPHA") for c in p["companies"]}
        self.assertIn("Position Corp", names)  # the same company, the same sector, now through the ETF too

    def test_the_match_by_isin_alias_and_ticker_with_exchange(self) -> None:
        result = self.overview()
        alpha: dict[str, float] = {}
        for p in self.company_entries(result, "ALPHA"):
            for c in p["companies"]:
                alpha[c["name"]] = alpha.get(c["name"], 0.0) + c["value_eur"]  # one entry per ETF: the company adds up across them
        beta = {c["name"]: c["value_eur"] for p in self.company_entries(result, "BETA") for c in p["companies"]}
        self.assertAlmostEqual(alpha["Position Corp"], 40.0 + 40.0)  # ISIN: 10 % of ETF1 (400) + 20 % of ETF2 (200)
        self.assertAlmostEqual(alpha["Tkr Corp"], 20.0)  # ticker + exchange: 5 % of ETF1
        self.assertAlmostEqual(beta["Alias Corp"], 20.0)  # reviewed alias: 5 % of ETF1, a company that is only in an ETF

    def test_a_company_without_a_sector_or_without_a_security_stays_not_assessed(self) -> None:
        result = self.overview()
        assigned_names = {c["name"] for g in result["sectors"] for p in g["positions"] for c in p.get("companies", [])}
        self.assertFalse({"NoSec Corp", "Unknown Inc"} & assigned_names)
        it = next(g for g in result["sectors"] if g["label"] == "Information Technology")
        self.assertEqual((it["kind"], it["guard_status"]), (so.KIND_ETF_CATEGORY, so.GUARD_NOT_ASSESSED))
        self.assertAlmostEqual(it["market_value_eur"], 140.0 + 80.0)  # ETF1 35 % of 400 + ETF2 40 % of 200: the companies without a sector stay in it

    def test_there_is_no_fuzzy_matching(self) -> None:
        result = self.overview()
        names = {c["name"] for g in result["sectors"] for p in g["positions"] for c in p.get("companies", [])}
        self.assertNotIn("POSITION CORPORATION", names)
        self.assertEqual(sum(p["company_count"] for p in self.company_entries(result, "ALPHA") if p["symbol"] == "ETF"), 2)  # Pos and Tkr, not the look-alike
        effective = ee.build_effective_exposure(self.conn, as_of=AS_OF, universe_dir=self.universe, top=50)
        look_alike = next(r for r in effective["top"] if r["name"] == "POSITION CORPORATION")
        self.assertFalse(look_alike["matched"])

    def test_several_etfs_with_the_same_company_add_up_in_one_sector(self) -> None:
        result = self.overview()
        entries = self.company_entries(result, "ALPHA")
        self.assertEqual(sorted(p["symbol"] for p in entries), ["ETF", "ETF2"])  # one entry per ETF and sector
        pos = [c for p in entries for c in p["companies"] if c["name"] == "Position Corp"]
        self.assertAlmostEqual(sum(c["value_eur"] for c in pos), 80.0)
        effective = ee.build_effective_exposure(self.conn, as_of=AS_OF, universe_dir=self.universe, top=50)
        self.assertAlmostEqual(next(r for r in effective["top"] if r["security_id"] == 1)["etf_value_eur"], 80.0)  # the same figure as the single-name exposure

    def test_the_value_of_a_company_is_taken_out_of_its_provider_category(self) -> None:
        result = self.overview()
        alpha = self.sector_group(result, "ALPHA")
        lookthrough = {p["symbol"]: p for p in alpha["positions"] if p["origin"] == so.ORIGIN_ETF}
        self.assertAlmostEqual(lookthrough["ETF"]["market_value_eur"], 160.0 - 40.0 - 20.0)  # Financials of ETF1 without Pos and Tkr
        self.assertAlmostEqual(lookthrough["ETF2"]["market_value_eur"], 120.0 - 40.0)
        self.assertAlmostEqual(alpha["market_value_eur"], 600.0 + (60.0 + 100.0) + (40.0 + 80.0))
        self.assertAlmostEqual(self.sector_group(result, "BETA")["market_value_eur"], 20.0 + 60.0)

    def test_nothing_is_counted_twice_and_the_parts_add_up(self) -> None:
        result = self.overview()
        self.assertAlmostEqual(result["total_market_value_eur"], 1500.0)
        self.assertAlmostEqual(sum(g["market_value_eur"] for g in result["sectors"]), 1500.0)
        self.assertTrue(result["checks"]["reconciles"])
        self.assertAlmostEqual(result["assigned_value_eur"] + result["not_assessed_value_eur"], 1500.0)  # sector groups + NOT_ASSESSED (provider categories and not assigned)
        self.assertAlmostEqual(result["not_assessed_value_eur"], 300.0 + 140.0 + 20.0 + 80.0)  # WATCH, ETF1 IT + Cash, ETF2 IT
        for etf in result["etfs"]:
            parts = sum(p["market_value_eur"] for g in result["sectors"] for p in g["positions"] if p["security_id"] == etf["security_id"])
            self.assertAlmostEqual(parts, etf["market_value_eur"])
        self.assertAlmostEqual(result["etf_company_value_eur"], 60.0 + 20.0 + 40.0)
        self.assertAlmostEqual(sum(p["weight_pct"] for g in result["sectors"] for p in g["positions"]), 100.0)

    def test_the_direct_sector_values_do_not_change(self) -> None:
        with_companies, plain = self.overview(), self.overview(lookthrough=False)
        for g in plain["sectors"]:
            if g["sector"] is None:
                continue
            self.assertAlmostEqual(next(x for x in with_companies["sectors"] if x["sector"] == g["sector"])["direct_value_eur"], g["direct_value_eur"])
        self.assertEqual(plain["etf_company_value_eur"], 0.0)

    def test_the_etf_report_says_how_much_was_assigned_by_company(self) -> None:
        etf = next(e for e in self.overview()["etfs"] if e["symbol"] == "ETF")
        self.assertEqual((round(etf["company_value_eur"]), etf["company_count"]), (80, 3))
        self.assertAlmostEqual(etf["estimated_value_eur"], 0.0)  # the provider names the category of every holding
        self.assertAlmostEqual(etf["mapped_value_eur"], 80.0 + 100.0 + 60.0)  # companies + the Financials and Health Care rest


class EstimateTests(CompanyCase):
    def test_a_holding_without_a_provider_sector_is_taken_from_the_fitting_group_and_reported(self) -> None:
        holdings = self.default_holdings()
        holdings[ETF2] = etf_entry([holding("Pos Corp", 20.0, isin="US0000POS001"), holding("Filler Two", 80.0, isin="US0000FL2001")])  # no GICS sector
        self.write_holdings(holdings)
        result = self.overview()
        etf2 = next(e for e in result["etfs"] if e["symbol"] == "ETF2")
        self.assertAlmostEqual(etf2["estimated_value_eur"], 40.0)
        self.assertTrue(any(w.startswith("COMPANY_SPLIT_ESTIMATED: ETF2") for w in result["warnings"]))
        self.assertAlmostEqual(self.sector_group(result, "ALPHA")["market_value_eur"], 880.0)  # taken from the category that maps to the company's own group: the sum is the same
        self.assertTrue(result["checks"]["reconciles"])

    def test_holdings_that_cannot_be_used_are_reported_and_the_etf_stays_split_by_category(self) -> None:
        holdings = self.default_holdings()
        holdings[ETF2] = etf_entry([holding("Pos Corp", 20.0, isin="US0000POS001")])  # a physical fund whose weights add up to 20 %: invalid
        self.write_holdings(holdings)
        result = self.overview()
        self.assertTrue(any(w.startswith("COMPANY_SPLIT_UNAVAILABLE_") and "ETF2" in w for w in result["warnings"]), result["warnings"])
        etf2 = next(e for e in result["etfs"] if e["symbol"] == "ETF2")
        self.assertEqual(etf2["company_value_eur"], 0.0)
        self.assertTrue(result["checks"]["reconciles"])

    def test_without_usable_holdings_the_etf_is_split_by_provider_category_only(self) -> None:
        (self.universe / eh.FILE_NAME).unlink()
        result = self.overview()
        self.assertEqual(result["etf_company_value_eur"], 0.0)
        self.assertAlmostEqual(self.sector_group(result, "ALPHA")["market_value_eur"], 600.0 + 160.0 + 120.0)
        self.assertTrue(result["checks"]["reconciles"])


if __name__ == "__main__":
    unittest.main()
