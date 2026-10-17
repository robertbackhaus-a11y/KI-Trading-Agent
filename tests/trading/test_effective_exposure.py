# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The effective single-name exposure (``get_effective_exposure``): direct position + the company's share in every held ETF, exact matching, no double counting, unmatched and
missing data visible, synthetic ETFs by index composition, reconciliation, the TSMC-style ADR control case; synthetic data only."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import effective_exposure as ee  # noqa: E402
import etf_holdings as eh  # noqa: E402
from _fixtures import MARKET_END, make_connection  # noqa: E402

AS_OF = MARKET_END.isoformat()
ETF1, ETF2 = "IE00TESTETF1", "IE00TESTETF2"


def holding(name, weight, ticker="", isin="", asset="Equity", exchange="", sector="", location=""):
    return {"name": name, "ticker": ticker, "isin": isin, "weight": weight, "asset_class": asset, "exchange": exchange, "sector": sector, "location": location}


def etf_entry(holdings, kind="physical", as_of=AS_OF, name="Synthetic ETF"):
    return {"name": name, "provider": "Synthetic", "as_of": as_of, "source": "https://example.invalid/holdings", "kind": kind, "holdings": holdings}


class ExposureCase(unittest.TestCase):
    """Securities: 1 POS (stock, ISIN US_POS, NASDAQ), 2 WATCH (stock, ISIN US_WCH, NYSE), 3 ETF (etf, ETF1), 4 ETF2 (etf).  Values: POS 600, WATCH 300, ETF1 400, ETF2 200."""

    def setUp(self) -> None:
        self.conn = make_connection()
        self.addCleanup(self.conn.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.universe = Path(tmp.name)
        c = self.conn
        c.execute("ALTER TABLE security ADD COLUMN isin TEXT")
        c.execute("ALTER TABLE security ADD COLUMN exchange TEXT")
        c.execute("UPDATE security SET isin = 'US0000POS001', exchange = 'NASDAQ' WHERE id = 1")
        c.execute("UPDATE security SET isin = 'US0000WCH001', exchange = 'NYSE' WHERE id = 2")
        c.execute("UPDATE security SET isin = ?, exchange = 'XETRA' WHERE id = 3", (ETF1,))
        c.execute("INSERT INTO security(id, symbol, name, asset_type, isin, exchange) VALUES (4, 'ETF2', 'Second ETF', 'etf', ?, 'LSE')", (ETF2,))
        c.execute("UPDATE positions SET shares = 10, avg_cost = 50, currency = 'EUR' WHERE security_id = 1")
        c.execute("UPDATE market_snapshot SET price = 60, currency = 'EUR' WHERE security_id = 1")
        stamp = AS_OF + "T00:00:00+00:00"
        for sid, shares, price in ((2, 3, 100.0), (3, 1, 400.0), (4, 1, 200.0)):
            c.execute("INSERT INTO positions(security_id, shares, avg_cost, remaining_cost_basis, currency, invested_amount, realized_gain, transaction_count) VALUES (?, ?, 10, 10, 'EUR', 10, 0, 1)", (sid, shares))
            c.execute("INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (?, ?, ?, 'EUR')", (sid, stamp, price))
        self.write({ETF1: etf_entry([holding("Pos Corp", 10.0, isin="US0000POS001"), holding("Other Inc", 60.0, isin="US0000OTH001"), holding("USD CASH", 5.0, asset="Cash"), holding("Third", 25.0, isin="US0000THR001")]),
                    ETF2: etf_entry([holding("Pos Corp", 5.0, isin="US0000POS001"), holding("Other Inc", 95.0, isin="US0000OTH001")])})

    def write(self, etfs, aliases=None, groups=None) -> None:
        payload = {"map_id": "etf_holdings_local", "version": 1, "etfs": etfs}
        if aliases is not None:
            payload["aliases"] = aliases
        if groups is not None:
            payload["issuer_groups"] = groups
        (self.universe / eh.FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")

    def run_ee(self, **kwargs):
        return ee.build_effective_exposure(self.conn, as_of=AS_OF, universe_dir=self.universe, **{"top": 50, **kwargs})

    @staticmethod
    def by_name(result, name):
        return next(r for r in result["top"] if r["name"] == name)


class SumTests(ExposureCase):
    def test_the_sums_reconcile_with_the_portfolio_value(self) -> None:
        result = self.run_ee()
        self.assertAlmostEqual(result["total_market_value_eur"], 1500.0)
        self.assertTrue(result["checks"]["reconciles"])
        parts = result["checks"]["companies_eur"] + result["checks"]["other_eur"] + result["checks"]["rounding_drift_eur"] + result["checks"]["unresolved_eur"]
        self.assertAlmostEqual(parts, 1500.0)
        self.assertAlmostEqual(sum(r["effective_value_eur"] for r in result["top"]), result["checks"]["companies_eur"])
        self.assertAlmostEqual(sum(r["effective_weight_pct"] for r in result["top"]) + result["coverage"]["other_pct_of_total"], 100.0)
        for r in result["top"]:
            self.assertAlmostEqual(r["direct_value_eur"] + r["etf_value_eur"], r["effective_value_eur"])
            self.assertAlmostEqual(r["direct_weight_pct"] + r["etf_weight_pct"], r["effective_weight_pct"])

    def test_a_direct_position_alone(self) -> None:
        wch = self.by_name(self.run_ee(), "Watch Corp")
        self.assertEqual((wch["direct_value_eur"], wch["etf_value_eur"], wch["effective_value_eur"], wch["contributors"]), (300.0, 0.0, 300.0, []))
        self.assertAlmostEqual(wch["effective_weight_pct"], 20.0)

    def test_a_company_only_in_etfs(self) -> None:
        third = self.by_name(self.run_ee(), "Third")
        self.assertEqual((third["direct_value_eur"], third["matched"]), (0.0, False))
        self.assertAlmostEqual(third["etf_value_eur"], 100.0)  # 25 % of 400
        self.assertEqual([(c["etf"], round(c["value_eur"])) for c in third["contributors"]], [("ETF", 100)])

    def test_direct_and_etf_share_of_the_same_company_is_counted_once(self) -> None:
        result = self.run_ee()
        pos = [r for r in result["top"] if r["security_id"] == 1]
        self.assertEqual(len(pos), 1)  # one entry although it is held directly and in both ETFs
        pos = pos[0]
        self.assertAlmostEqual(pos["direct_value_eur"], 600.0)
        self.assertAlmostEqual(pos["etf_value_eur"], 400 * 0.10 + 200 * 0.05)
        self.assertAlmostEqual(pos["effective_value_eur"], 650.0)
        self.assertEqual({c["etf"] for c in pos["contributors"]}, {"ETF", "ETF2"})
        self.assertEqual(pos["etf_count"], 2)
        self.assertEqual(result["overlap_company_count"], 1)

    def test_the_same_company_in_several_etfs_is_aggregated_once(self) -> None:
        other = [r for r in self.run_ee()["top"] if r["name"] == "Other Inc"]
        self.assertEqual(len(other), 1)
        self.assertAlmostEqual(other[0]["etf_value_eur"], 400 * 0.60 + 200 * 0.95)  # 240 + 190
        self.assertEqual(len(other[0]["contributors"]), 2)

    def test_only_etf_positions_and_no_direct_stock(self) -> None:
        self.conn.execute("DELETE FROM positions WHERE security_id IN (1, 2)")
        result = self.run_ee()
        self.assertTrue(result["checks"]["reconciles"])
        self.assertEqual(result["direct_company_count"], 0)
        self.assertAlmostEqual(sum(r["effective_value_eur"] for r in result["top"]) + result["checks"]["other_eur"], 600.0)

    def test_the_cash_row_is_other_and_never_spread_over_companies(self) -> None:
        result = self.run_ee()
        self.assertAlmostEqual(result["checks"]["other_eur"], 400 * 0.05)  # the 5 % cash of the first ETF
        etf = next(e for e in result["etfs"] if e["symbol"] == "ETF")
        self.assertAlmostEqual(etf["other_weight_pct"], 5.0)
        self.assertAlmostEqual(etf["equity_weight_pct"], 95.0)
        self.assertEqual(etf["equity_count"], 3)

    def test_the_part_a_physical_fund_does_not_explain_is_other_too(self) -> None:
        self.write({ETF1: etf_entry([holding("Pos Corp", 10.0, isin="US0000POS001"), holding("Other Inc", 87.5, isin="US0000OTH001")]), ETF2: etf_entry([holding("Other Inc", 100.0, isin="US0000OTH001")])})
        result = self.run_ee()
        etf = next(e for e in result["etfs"] if e["symbol"] == "ETF")
        self.assertAlmostEqual(etf["other_weight_pct"], 2.5)  # 100 - 97.5
        self.assertTrue(result["checks"]["reconciles"])

    def test_top_is_capped_so_that_the_answer_stays_below_the_mcp_size_limit(self) -> None:
        self.assertEqual(ee.MAX_TOP, 40)
        with mock.patch.object(ee, "MAX_TOP", 2):
            result = self.run_ee(top=500)
        self.assertEqual((len(result["top"]), result["top_count"]), (2, 2))
        self.assertEqual(len(self.run_ee(top=0)["top"]), 1)  # at least one row

    def test_top_limits_the_list_but_not_the_checks(self) -> None:
        result = self.run_ee(top=3)
        self.assertEqual((len(result["top"]), result["top_count"]), (3, 3))
        self.assertGreater(result["company_count"], 3)
        self.assertTrue(result["checks"]["reconciles"])
        self.assertEqual([r["effective_value_eur"] for r in result["top"]], sorted((r["effective_value_eur"] for r in result["top"]), reverse=True))


class ControlCaseTests(ExposureCase):
    def test_an_adr_and_its_ordinary_share_are_one_company_through_a_reviewed_alias(self) -> None:
        # total 10 000: the held ADR 620 (6.2 %), a filler stock, and an ETF of 800 that holds the ordinary share with 13.99 % -> 111.92 = 1.12 %
        c = self.conn
        c.execute("UPDATE security SET symbol = 'TSM', name = 'TSMC (ADR)', isin = 'US8740391003', exchange = 'NYSE' WHERE id = 1")
        c.execute("UPDATE market_snapshot SET price = 62 WHERE security_id = 1")  # 10 shares x 62 = 620
        c.execute("UPDATE market_snapshot SET price = 8580 / 3.0 WHERE security_id = 2")  # 3 shares -> 8580
        c.execute("UPDATE market_snapshot SET price = 800 WHERE security_id = 3")
        c.execute("DELETE FROM positions WHERE security_id = 4")
        rest = 100.0 - 13.99
        self.write({ETF1: etf_entry([holding("TAIWAN SEMICONDUCTOR MANUFACTURING", 13.99, ticker="2330", exchange="Taiwan Stock Exchange"), holding("Rest", rest, isin="US0000RST001")])},
                   aliases=[{"ticker": "2330", "exchange": "Taiwan Stock Exchange", "isin": "US8740391003", "reason": "the held security is the ADR of the issuer of this ordinary share"}])
        result = self.run_ee()
        self.assertAlmostEqual(result["total_market_value_eur"], 10000.0)
        tsmc = [r for r in result["top"] if r["security_id"] == 1]
        self.assertEqual(len(tsmc), 1)
        tsmc = tsmc[0]
        self.assertAlmostEqual(tsmc["direct_weight_pct"], 6.2)
        self.assertAlmostEqual(tsmc["etf_weight_pct"], 1.1192, places=3)
        self.assertAlmostEqual(tsmc["effective_weight_pct"], 7.3192, places=3)  # about 7.3 %
        self.assertEqual(tsmc["match_bases"], ["alias"])
        self.assertEqual(tsmc["name"], "TSMC (ADR)")
        self.assertTrue(result["checks"]["reconciles"])

    def test_without_the_alias_the_ordinary_share_stays_a_visible_company_of_its_own(self) -> None:
        c = self.conn
        c.execute("UPDATE security SET symbol = 'TSM', name = 'TSMC (ADR)', isin = 'US8740391003' WHERE id = 1")
        self.write({ETF1: etf_entry([holding("TAIWAN SEMICONDUCTOR MANUFACTURING", 20.0, ticker="2330", exchange="Taiwan Stock Exchange"), holding("Rest", 80.0, isin="US0000RST001")])})
        result = self.run_ee()
        own = self.by_name(result, "TAIWAN SEMICONDUCTOR MANUFACTURING")
        self.assertEqual((own["matched"], own["match_bases"]), (False, ["standalone"]))  # never merged by name similarity
        self.assertEqual(self.by_name(result, "TSMC (ADR)")["etf_value_eur"], 0.0)

    def test_an_alias_to_an_unknown_security_is_reported_and_ignored(self) -> None:
        self.write({ETF1: etf_entry([holding("X", 100.0, ticker="9999", exchange="Somewhere")])}, aliases=[{"ticker": "9999", "exchange": "Somewhere", "isin": "US0000NONE001", "reason": "test"}])
        result = self.run_ee()
        self.assertTrue(any(w.startswith("ALIAS_UNUSED") for w in result["warnings"]))
        self.assertEqual(self.by_name(result, "X")["match_bases"], ["standalone"])


class MatchingTests(ExposureCase):
    def test_ticker_and_exchange_must_both_be_equal(self) -> None:
        c = self.conn
        c.execute("UPDATE security SET symbol = 'ASML', name = 'ASML Holding', isin = 'NL0010273215', exchange = 'EURONEXT_AMSTERDAM' WHERE id = 2")
        self.write({ETF1: etf_entry([holding("ASML HOLDING", 30.0, ticker="ASML", exchange="Euronext Amsterdam"), holding("Rest", 70.0, isin="US0000RST001")]),
                    ETF2: etf_entry([holding("ASML ADR", 10.0, ticker="ASML", exchange="NASDAQ"), holding("Rest", 90.0, isin="US0000RST001")])})
        result = self.run_ee()
        asml = [r for r in result["top"] if r["security_id"] == 2][0]
        self.assertEqual([(c_["etf"], c_["match"]) for c_ in asml["contributors"]], [("ETF", "ticker_exchange")])  # the same ticker on another exchange is not merged
        self.assertAlmostEqual(asml["etf_value_eur"], 120.0)
        self.assertEqual(self.by_name(result, "ASML ADR")["match_bases"], ["standalone"])

    def test_an_isin_match_wins_and_uses_the_database_name(self) -> None:
        result = self.run_ee()
        pos = [r for r in result["top"] if r["security_id"] == 1][0]
        self.assertEqual((pos["name"], pos["match_bases"]), ("Position Corp", ["isin"]))

    def test_an_ambiguous_listing_stays_unmatched_and_is_counted(self) -> None:
        c = self.conn
        c.execute("INSERT INTO security(id, symbol, name, asset_type, isin, exchange) VALUES (5, 'DUP', 'Dup One', 'stock', 'US0000DUP001', 'NYSE'), (6, 'DUP', 'Dup Two', 'stock', 'US0000DUP002', 'NYSE')")
        self.write({ETF1: etf_entry([holding("DUP CORP", 40.0, ticker="DUP", exchange="NYSE"), holding("Rest", 60.0, isin="US0000RST001")]), ETF2: etf_entry([holding("Rest", 100.0, isin="US0000RST001")])})
        result = self.run_ee()
        self.assertEqual(result["coverage"]["ambiguous_unmatched_holdings"], 1)
        self.assertEqual(self.by_name(result, "DUP CORP")["match_bases"], ["ambiguous"])
        self.assertEqual(self.by_name(result, "DUP CORP")["category"], "ambiguous_unmatched")
        self.assertAlmostEqual(result["buckets"]["ambiguous_unmatched"]["ambiguous_value_eur"], 400 * 0.4)

    def test_unmatched_holdings_are_counted_and_visible(self) -> None:
        result = self.run_ee()
        coverage = result["coverage"]
        etf = next(e for e in result["etfs"] if e["symbol"] == "ETF")
        self.assertEqual((etf["direct_matched_count"], etf["etf_only_count"], etf["ambiguous_unmatched_count"]), (1, 2, 0))
        self.assertAlmostEqual(etf["direct_matched_weight_pct"], 10.0)
        self.assertAlmostEqual(etf["etf_only_weight_pct"], 85.0)
        self.assertEqual(coverage["equity_holdings"], coverage["direct_matched_holdings"] + coverage["etf_only_holdings"] + coverage["ambiguous_unmatched_holdings"])

    def test_share_classes_of_one_issuer_form_one_company_through_a_reviewed_group(self) -> None:
        self.write({ETF1: etf_entry([holding("Issuer CL A", 30.0, isin="US0000ISA001"), holding("Issuer CL C", 20.0, isin="US0000ISC001"), holding("Rest", 50.0, isin="US0000RST001")]),
                    ETF2: etf_entry([holding("Rest", 100.0, isin="US0000RST001")])},
                   groups=[{"name": "Issuer Inc.", "isins": ["US0000ISA001", "US0000ISC001"], "reason": "share classes A and C"}])
        result = self.run_ee()
        issuer = [r for r in result["top"] if r["name"] == "Issuer Inc."]
        self.assertEqual(len(issuer), 1)
        self.assertAlmostEqual(issuer[0]["etf_value_eur"], 400 * 0.5)
        self.assertEqual([(c["etf"], c["weight_in_etf_pct"]) for c in issuer[0]["contributors"]], [("ETF", 50.0)])  # one contribution per ETF: both classes add up inside it


class SyntheticAndDataTests(ExposureCase):
    def test_a_synthetic_etf_uses_its_index_composition_with_the_published_weights(self) -> None:
        index = [holding("Pos Corp", 50.0, isin="US0000POS001"), holding("Big Inc", 52.43, isin="US0000BIG001")]  # published index weights add up to 102.43
        self.write({ETF1: etf_entry(index, kind="synthetic_index"), ETF2: etf_entry([holding("Other Inc", 100.0, isin="US0000OTH001")])})
        result = self.run_ee()
        etf = next(e for e in result["etfs"] if e["symbol"] == "ETF")
        self.assertAlmostEqual(etf["weights_drift_pct"], 2.43)
        self.assertEqual(etf["kind"], "synthetic_index")
        # the published weights are NOT scaled: the names that matter keep their published share ...
        self.assertAlmostEqual(self.by_name(result, "Big Inc")["etf_value_eur"], 400 * 52.43 / 100.0)
        etf_part = sum(c["value_eur"] for r in result["top"] for c in r["contributors"] if c["etf"] == "ETF")
        self.assertAlmostEqual(etf_part, 400.0 * 1.0243)
        # ... and the excess is a visible, negative rounding drift that makes the sums reconcile
        self.assertAlmostEqual(etf["rounding_drift_eur"], -400 * 0.0243)
        self.assertAlmostEqual(result["buckets"]["rounding_drift"]["value_eur"], -400 * 0.0243)
        self.assertEqual(result["checks"]["other_eur"], 0.0)  # it is not "other"
        self.assertTrue(result["checks"]["reconciles"])

    def test_a_rounded_index_list_is_not_normalised_because_that_would_bias_the_large_names(self) -> None:
        # 10 large names at their exact (0.1-resolution) weights, 180 mid names published at 0.1 although their true weight is 0.087: the list adds up to more than 100 %
        large = [7.9, 7.1, 5.8, 3.9, 3.1, 2.6, 2.4, 1.9, 1.6, 1.5]
        mid_true, mid_published = 0.087, 0.1
        rest = 100.0 - sum(large) - 180 * mid_true  # the true remainder, published exactly
        index = ([holding(f"Large {i}", w, isin=f"US00LARGE{i:03d}") for i, w in enumerate(large)] + [holding(f"Mid {i}", mid_published, isin=f"US00MID{i:04d}") for i in range(180)]
                 + [holding("Rest", rest, isin="US0000REST01")])
        published = sum(h["weight"] for h in index)
        self.assertGreater(published, 100.0)  # the effect: more than 100 % although every large weight is right
        self.write({ETF1: etf_entry(index, kind="synthetic_index"), ETF2: etf_entry([holding("Other Inc", 100.0, isin="US0000OTH001")])})
        result = self.run_ee()
        etf = next(e for e in result["etfs"] if e["symbol"] == "ETF")
        self.assertAlmostEqual(etf["weights_drift_pct"], published - 100.0)
        largest = self.by_name(result, "Large 0")["etf_value_eur"]
        self.assertAlmostEqual(largest, 400 * 7.9 / 100.0)  # exactly the published weight
        scaled = 400 * 7.9 / published  # what a normalisation to 100 % would have made of it
        self.assertLess(scaled, largest)
        self.assertAlmostEqual((largest - scaled) / largest, 1 - 100.0 / published)  # an error of the size of the drift, pushed into the name that matters most
        self.assertTrue(result["checks"]["reconciles"])

    def test_the_rounding_drift_is_reconciliation_only(self) -> None:
        large = [holding("Pos Corp", 40.0, isin="US0000POS001"), holding("Big Inc", 30.0, isin="US0000BIG001"), holding("Mid Inc", 20.0, isin="US0000MID001")]
        exact = large + [holding("Rest", 10.0, isin="US0000RST001")]  # adds up to 100 %: no drift
        inflated = large + [holding("Rest", 10.0, isin="US0000RST001"), holding("Tiny A", 1.2, isin="US0000TNA001"), holding("Tiny B", 1.2, isin="US0000TNB001")]  # 102.4 %: drift
        results = []
        for index in (exact, inflated):
            self.write({ETF1: etf_entry(index, kind="synthetic_index"), ETF2: etf_entry([holding("Other Inc", 100.0, isin="US0000OTH001")])})
            results.append(self.run_ee())
        clean, drifted = results
        self.assertEqual(clean["buckets"]["rounding_drift"]["value_eur"], 0.0)
        drift = drifted["buckets"]["rounding_drift"]["value_eur"]
        self.assertAlmostEqual(drift, -400 * 0.024)
        # no company carries the drift: every company that exists in both runs has exactly the same values and the ranking of those companies is unchanged
        for name in ("Position Corp", "Big Inc", "Mid Inc", "Rest", "Other Inc"):
            for field in ("direct_value_eur", "etf_value_eur", "effective_value_eur", "effective_weight_pct"):
                self.assertAlmostEqual(self.by_name(clean, name)[field], self.by_name(drifted, name)[field], msg=f"{name}.{field}")
        order = lambda result: [r["name"] for r in result["top"] if r["name"] in ("Position Corp", "Big Inc", "Mid Inc", "Rest", "Other Inc")]  # noqa: E731
        self.assertEqual(order(clean), order(drifted))
        # the drift is neither a company, nor "other", nor part of a contribution; it only makes the sums reconcile
        self.assertTrue(all("drift" not in json.dumps(r["contributors"]) for r in drifted["top"]))
        self.assertEqual(drifted["checks"]["other_eur"], clean["checks"]["other_eur"])
        self.assertAlmostEqual(drifted["checks"]["companies_eur"] + drifted["checks"]["other_eur"] + drifted["checks"]["rounding_drift_eur"] + drifted["checks"]["unresolved_eur"],
                               drifted["total_market_value_eur"])
        self.assertAlmostEqual(drifted["checks"]["companies_eur"] - clean["checks"]["companies_eur"], -drift)  # the two tiny rows are real companies; the drift offsets exactly them
        self.assertTrue(drifted["checks"]["reconciles"])
        # the share of the top list is the share of real companies, not reduced by the drift
        self.assertAlmostEqual(drifted["top_share_pct"], sum(r["effective_value_eur"] for r in drifted["top"]) / drifted["total_market_value_eur"] * 100.0)

    def test_the_buckets_partition_the_invested_value(self) -> None:
        self.write({ETF1: etf_entry([holding("Pos Corp", 10.0, isin="US0000POS001"), holding("Only In Etf", 60.0, isin="US0000OTH001"), holding("USD CASH", 5.0, asset="Cash"),
                                     holding("Dup One", 20.0, ticker="DUP", exchange="NYSE"), holding("Nameless", 5.0)]),
                    ETF2: etf_entry([holding("Only In Etf", 100.0, isin="US0000OTH001")])})
        self.conn.execute("INSERT INTO security(id, symbol, name, asset_type, isin, exchange) VALUES (5, 'DUP', 'A', 'stock', 'US0000DUP001', 'NYSE'), (6, 'DUP', 'B', 'stock', 'US0000DUP002', 'NYSE')")
        result = self.run_ee()
        b = result["buckets"]
        self.assertAlmostEqual(sum(x["value_eur"] for x in b.values()), result["total_market_value_eur"])
        self.assertAlmostEqual(b["direct_matched"]["direct_value_eur"], 900.0)  # POS 600 + WATCH 300
        self.assertAlmostEqual(b["direct_matched"]["etf_value_eur"], 400 * 0.10)  # the ETF share of the directly held company belongs to its bucket
        self.assertAlmostEqual(b["etf_only"]["value_eur"], 400 * 0.60 + 200.0)  # identified by an ISIN, merely not held directly
        self.assertAlmostEqual(b["ambiguous_unmatched"]["ambiguous_value_eur"], 400 * 0.20)
        self.assertAlmostEqual(b["ambiguous_unmatched"]["name_only_value_eur"], 400 * 0.05)
        self.assertAlmostEqual(b["ambiguous_unmatched"]["value_eur"], 400 * 0.25)
        self.assertAlmostEqual(b["other"]["value_eur"], 400 * 0.05)
        self.assertEqual(b["rounding_drift"]["value_eur"], 0.0)
        self.assertTrue(result["checks"]["reconciles"])

    def test_a_company_that_is_not_in_the_security_master_is_etf_only_not_unmatched(self) -> None:
        result = self.run_ee()
        third = self.by_name(result, "Third")  # not a security of the portfolio, identified by its ISIN in the holdings file
        self.assertEqual((third["matched"], third["category"]), (False, "etf_only"))
        self.assertEqual(result["buckets"]["ambiguous_unmatched"]["value_eur"], 0.0)
        for r in result["top"]:
            self.assertEqual(r["category"], "direct_matched" if r["direct_value_eur"] > 0 else "etf_only")

    def test_an_unresolved_etf_counts_as_unattributable_not_as_etf_only(self) -> None:
        self.write({ETF1: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")])})
        result = self.run_ee()
        unclear = result["buckets"]["ambiguous_unmatched"]
        self.assertAlmostEqual(unclear["unresolved_etf_value_eur"], 200.0)
        self.assertAlmostEqual(unclear["value_eur"], 200.0)

    def test_an_etf_without_holdings_stays_one_visible_unresolved_block(self) -> None:
        self.write({ETF1: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")])})
        result = self.run_ee()
        etf2 = next(e for e in result["etfs"] if e["symbol"] == "ETF2")
        self.assertEqual(etf2["status"], eh.NO_DATA)
        self.assertAlmostEqual(result["checks"]["unresolved_eur"], 200.0)
        self.assertAlmostEqual(result["coverage"]["resolved_pct"], 400.0 / 600.0 * 100)
        self.assertTrue(result["checks"]["reconciles"])
        self.assertTrue(any(w.startswith("HOLDINGS_NO_DATA") for w in result["warnings"]))

    def test_a_missing_file_never_fails_the_tool(self) -> None:
        (self.universe / eh.FILE_NAME).unlink()
        result = self.run_ee()
        self.assertTrue(result["ok"] and result["checks"]["reconciles"])
        self.assertAlmostEqual(result["checks"]["unresolved_eur"], 600.0)
        self.assertEqual(result["etf_company_count"], 0)

    def test_an_unreadable_file_is_reported_not_raised(self) -> None:
        (self.universe / eh.FILE_NAME).write_text("{not json", encoding="utf-8")
        result = self.run_ee()
        self.assertTrue(result["ok"])
        self.assertTrue(any(w.startswith("HOLDINGS_UNAVAILABLE") for w in result["warnings"]))

    def test_implausible_entries_are_rejected_one_by_one(self) -> None:
        bad = {"A": etf_entry([holding("X", 50.0)]), "B": {**etf_entry([holding("X", 100.0)]), "as_of": "soon"}, "C": {**etf_entry([holding("X", 100.0)]), "source": ""},
               "D": etf_entry([holding("X", -5.0), holding("Y", 105.0)]), "E": {**etf_entry([holding("X", 100.0)]), "kind": "magic"}, "F": etf_entry([holding("Cash", 100.0, asset="Cash")]),
               ETF1: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")])}
        self.write(bad)
        layers = eh.read_layers(self.universe)
        self.assertEqual(sorted(layers.invalid), ["A", "B", "C", "D", "E", "F"])
        self.assertEqual(list(layers.etfs), [ETF1])

    def test_an_old_holdings_file_is_used_but_flagged(self) -> None:
        old = (MARKET_END - timedelta(days=200)).isoformat()
        self.write({ETF1: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")], as_of=old), ETF2: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")])})
        result = self.run_ee()
        self.assertEqual(next(e for e in result["etfs"] if e["symbol"] == "ETF")["status"], eh.STALE)
        self.assertTrue(any(w.startswith("HOLDINGS_STALE") for w in result["warnings"]))
        self.assertGreater(self.by_name(result, "Position Corp")["etf_value_eur"], 0)

    def test_duplicate_listing_rows_in_one_file_are_one_company(self) -> None:
        self.write({ETF1: etf_entry([holding("Dual", 10.0, ticker="DL", exchange="X"), holding("Dual", 15.0, ticker="DL", exchange="X"), holding("Rest", 75.0, isin="US0000RST001")]),
                    ETF2: etf_entry([holding("Rest", 100.0, isin="US0000RST001")])})
        dual = self.by_name(self.run_ee(), "Dual")
        self.assertAlmostEqual(dual["etf_value_eur"], 400 * 0.25)
        self.assertEqual(len(dual["contributors"]), 1)

    def test_nothing_is_written_and_the_risk_guard_is_untouched(self) -> None:
        before = self.conn.total_changes
        files = sorted((p.name, p.read_bytes()) for p in self.universe.iterdir())
        self.run_ee()
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(sorted((p.name, p.read_bytes()) for p in self.universe.iterdir()), files)
        guard = (ROOT / "tools" / "trading" / "portfolio_risk.py").read_text(encoding="utf-8")
        self.assertNotIn("effective_exposure", guard)
        self.assertNotIn("etf_holdings", guard)
        source = (ROOT / "tools" / "trading" / "effective_exposure.py").read_text(encoding="utf-8")
        for forbidden in ("INSERT", "UPDATE ", "DELETE", "write_text", "commit(", "urlopen"):
            self.assertNotIn(forbidden, source)

    def test_the_tool_is_registered_and_the_private_file_is_protected(self) -> None:
        import importlib.util
        import inspect

        spec = importlib.util.spec_from_file_location("trading_sqlite_effective_test", ROOT / "mcp-tools" / "trading_sqlite.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertEqual(list(inspect.signature(module.Tools.get_effective_exposure).parameters), ["self", "as_of", "top"])
        manifest = json.loads((ROOT / "deploy" / "deploy-manifest.json").read_text(encoding="utf-8"))
        self.assertIn("app/universe/" + eh.FILE_NAME, manifest["protected"])
        deployed = [f for g in manifest["groups"] if g["name"] == "app" for f in g["files"]]
        self.assertTrue({"effective_exposure.py", "etf_holdings.py"} <= set(deployed))
        self.assertIn("tools/trading/universe/*_local.json", (ROOT / ".gitignore").read_text(encoding="utf-8"))  # the holdings file is git-ignored


if __name__ == "__main__":
    unittest.main()
