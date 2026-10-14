"""The single-name guard on the effective exposure (direct position + the company's share in the held ETFs): the basis decision, the ADD / BUY sizing limits, the data quality of the
ETF holdings (stale / missing never pretend the direct figure is complete), the orchestrator summary (TSMC-style ADR + ETF, ETF-only company), no influence of the rounding drift,
no orders; synthetic data only."""

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from datetime import timedelta
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import effective_exposure as ee  # noqa: E402
import etf_holdings as eh  # noqa: E402
import trading_orchestrator as orch  # noqa: E402
import single_name_exposure as sn  # noqa: E402
from add_sizing import evaluate_add_recommendation  # noqa: E402
from analysis_contracts import AvailabilityStatus, DataQuality  # noqa: E402
from entry_sizing import evaluate_entry_recommendation  # noqa: E402
from strategy_config import StrategyConfig  # noqa: E402
from _fixtures import MARKET_END  # noqa: E402
from test_effective_exposure import AS_OF, ETF1, ETF2, ExposureCase, etf_entry, holding  # noqa: E402
from test_swing_add_recommendation import portfolio as add_portfolio, swing_snapshot  # noqa: E402
from test_swing_entry_recommendation import candidate_snapshot, context as entry_context  # noqa: E402

AVAILABLE = DataQuality(AvailabilityStatus.AVAILABLE, as_of="2026-09-23")


def with_etf(ctx, values, status=AvailabilityStatus.AVAILABLE, details=()):
    return replace(ctx, etf_exposure_by_security=tuple(sorted(values.items())), etf_exposure_quality=DataQuality(status, tuple(details)))


class BasisTests(unittest.TestCase):
    """``single_name_exposure.resolve``: which basis the limit may use, and that it says so."""

    def test_usable_holdings_give_the_effective_basis(self) -> None:
        r = sn.resolve(with_etf(entry_context(security=1000), {1: 500.0}), 1, 1000.0)
        self.assertEqual((r.basis, r.direct_value, r.etf_value, r.effective_value, r.notes, r.complete), (sn.EFFECTIVE, 1000.0, 500.0, 1500.0, (), True))

    def test_a_company_not_in_any_etf_has_only_its_direct_position(self) -> None:
        r = sn.resolve(with_etf(entry_context(), {2: 500.0}), 1, 700.0)
        self.assertEqual((r.etf_value, r.effective_value, r.basis), (0.0, 700.0, sn.EFFECTIVE))

    def test_no_etf_held_means_direct_equals_effective(self) -> None:
        r = sn.resolve(with_etf(entry_context(), {}, AvailabilityStatus.NOT_APPLICABLE), 1, 700.0)
        self.assertEqual((r.basis, r.effective_value, r.complete), (sn.DIRECT, 700.0, True))

    def test_not_evaluated_is_said_not_hidden(self) -> None:
        for ctx in (entry_context(), None):
            r = sn.resolve(ctx, 1, 700.0)
            self.assertEqual((r.basis, r.complete, r.notes), (sn.DIRECT_ONLY, False, (sn.NOT_EVALUATED,)))

    def test_stale_missing_or_invalid_holdings_are_direct_only_with_the_reason(self) -> None:
        for status in (AvailabilityStatus.STALE, AvailabilityStatus.PARTIAL, AvailabilityStatus.UNAVAILABLE):
            with self.subTest(status=status):
                r = sn.resolve(with_etf(entry_context(), {1: 999.0}, status, ("ETF_HOLDINGS_STALE:IS3N",)), 1, 700.0)
                self.assertEqual((r.basis, r.complete, r.etf_value, r.effective_value), (sn.DIRECT_ONLY, False, 0.0, 700.0))  # the map is not trusted
                self.assertIn(f"EFFECTIVE_EXPOSURE_{status.name}", r.notes)
                self.assertIn("ETF_HOLDINGS_STALE:IS3N", r.notes)


class SizingTests(unittest.TestCase):
    config = StrategyConfig()

    def entry(self, etf_value, **kwargs):
        return evaluate_entry_recommendation(candidate_snapshot(), with_etf(entry_context(security=0.0), {1: etf_value} if etf_value is not None else {}, **kwargs), self.config)

    def test_an_etf_only_candidate_is_limited_by_its_etf_share(self) -> None:
        free = self.entry(0.0)
        shared = self.entry(19_900.0)  # 19.9 % of the portfolio already in the ETFs
        self.assertTrue(free.eligible)
        self.assertEqual(free.single_name_basis, sn.EFFECTIVE)
        self.assertTrue(shared.eligible)
        self.assertLess(shared.recommended_quantity, free.recommended_quantity)
        self.assertLessEqual(shared.projected_effective_security_weight, 0.20 + 1e-9)
        self.assertEqual(shared.current_etf_security_weight, 0.199)
        self.assertLess(shared.projected_security_weight, shared.projected_effective_security_weight)  # the direct weight stays visible, separately

    def test_the_limit_blocks_with_the_unchanged_reason_code_and_reports_the_figures(self) -> None:
        blocked = self.entry(20_000.0)
        self.assertFalse(blocked.eligible)
        self.assertEqual(blocked.block_reasons, ("ENTRY_SECURITY_WEIGHT_LIMIT",))
        self.assertAlmostEqual(blocked.current_effective_security_weight, 0.20)
        self.assertEqual(blocked.single_name_basis, sn.EFFECTIVE)

    def test_direct_plus_etf_share_of_the_same_company_counts_once_against_the_limit(self) -> None:
        a = evaluate_add_recommendation(swing_snapshot(), with_etf(add_portfolio(security_value=1_000, total=10_000), {1: 1_000.0}), self.config)
        b = evaluate_add_recommendation(swing_snapshot(), add_portfolio(security_value=1_000, total=10_000), self.config)
        self.assertTrue(b.eligible)
        self.assertAlmostEqual(a.current_effective_security_weight, 0.20)  # 10 % direct + 10 % ETF
        self.assertFalse(a.eligible)
        self.assertEqual(a.block_reasons, ("ADD_SECURITY_WEIGHT_LIMIT",))

    def test_stale_or_missing_holdings_keep_the_direct_limit_and_say_the_effective_exposure_is_not_assessed(self) -> None:
        plain = evaluate_entry_recommendation(candidate_snapshot(), entry_context(security=0.0), self.config)
        stale = self.entry(None, status=AvailabilityStatus.STALE, details=("ETF_HOLDINGS_STALE:ETF (as of 2026-01-01)",))
        self.assertEqual(stale.recommended_quantity, plain.recommended_quantity)  # not tightened on invented numbers, not loosened either
        self.assertEqual(stale.single_name_basis, sn.DIRECT_ONLY)
        self.assertIn("EFFECTIVE_EXPOSURE_STALE", stale.single_name_notes)
        self.assertEqual(plain.single_name_basis, sn.DIRECT_ONLY)  # a context without any ETF information is flagged as well
        self.assertEqual(plain.single_name_notes, (sn.NOT_EVALUATED,))

    def test_the_existing_direct_limit_is_unchanged(self) -> None:
        blocked = evaluate_entry_recommendation(candidate_snapshot(), with_etf(entry_context(security=20_000.0), {}, AvailabilityStatus.NOT_APPLICABLE), replace(self.config, sizing=replace(self.config.sizing, max_initial_swing_weight=0.30)))
        self.assertEqual(blocked.block_reasons, ("ENTRY_SECURITY_WEIGHT_LIMIT",))
        add = evaluate_add_recommendation(swing_snapshot(), with_etf(add_portfolio(security_value=2_000, total=10_000), {}, AvailabilityStatus.NOT_APPLICABLE), self.config)
        self.assertEqual(add.block_reasons, ("ADD_SECURITY_WEIGHT_LIMIT",))


class GuardCase(ExposureCase):
    """The portfolio of the base case: POS 600 (direct), WATCH 300, ETF1 400, ETF2 200 -> total 1500; ETF1 holds Pos Corp 10 %, ETF2 5 %."""

    def guard(self, **kwargs):
        return self.run_ee(**kwargs)["guard"]


class GuardInputTests(GuardCase):
    def test_usable_holdings_deliver_the_etf_value_per_company_once(self) -> None:
        guard = self.guard()
        self.assertEqual((guard["usable"], guard["status"], guard["details"]), (True, "available", []))
        self.assertAlmostEqual(guard["etf_value_by_security_id"]["1"], 400 * 0.10 + 200 * 0.05)  # one company, two ETFs, summed once

    def test_the_same_company_in_several_etfs_and_share_classes_is_one_entry(self) -> None:
        self.write({ETF1: etf_entry([holding("Pos Corp", 10.0, isin="US0000POS001"), holding("Pos Corp B", 10.0, ticker="POSB", isin="US0000POS002"), holding("Rest", 80.0, isin="US0000RST001")]),
                    ETF2: etf_entry([holding("Pos Corp", 5.0, isin="US0000POS001"), holding("Rest", 95.0, isin="US0000RST001")])},
                   groups=[{"name": "Pos Corp", "isins": ["US0000POS001", "US0000POS002"], "reason": "two share classes of one issuer"}])
        guard = self.guard()
        self.assertAlmostEqual(guard["etf_value_by_security_id"]["1"], 400 * 0.20 + 200 * 0.05)

    def test_a_stale_etf_makes_the_input_unusable_and_names_it(self) -> None:
        old = (MARKET_END - timedelta(days=200)).isoformat()
        self.write({ETF1: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")], as_of=old), ETF2: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")])})
        guard = self.guard()
        self.assertEqual((guard["usable"], guard["status"], guard["etf_value_by_security_id"]), (False, "stale", {}))
        self.assertEqual(guard["details"], [f"ETF_HOLDINGS_STALE:ETF (as of {old})"])

    def test_a_missing_etf_is_partial_and_a_missing_file_is_unavailable(self) -> None:
        self.write({ETF1: etf_entry([holding("Pos Corp", 10.0, isin="US0000POS001"), holding("Rest", 90.0, isin="US0000RST001")])})  # ETF2 has no holdings
        guard = self.guard()
        self.assertEqual((guard["usable"], guard["status"], guard["etf_value_by_security_id"]), (False, "partial", {}))
        self.assertTrue(guard["details"][0].startswith("ETF_HOLDINGS_NO_DATA:"))
        (self.universe / eh.FILE_NAME).unlink()
        guard = self.guard()
        self.assertEqual((guard["usable"], guard["status"]), (False, "unavailable"))

    def test_no_etf_held_needs_no_holdings(self) -> None:
        self.conn.execute("DELETE FROM positions WHERE security_id IN (3, 4)")
        (self.universe / eh.FILE_NAME).unlink()
        guard = self.guard()
        self.assertEqual((guard["usable"], guard["status"]), (True, "not_applicable"))

    def test_the_rounding_drift_never_enters_the_companies(self) -> None:
        # an index list that sums to 102.43 % (rounded published weights): the excess is reconciliation only
        self.write({ETF1: etf_entry([holding("Pos Corp", 10.0, isin="US0000POS001"), holding("Rest", 92.43, isin="US0000RST001")], kind="synthetic_index"),
                    ETF2: etf_entry([holding("Pos Corp", 5.0, isin="US0000POS001"), holding("Rest", 95.0, isin="US0000RST001")])})
        result = self.run_ee()
        self.assertAlmostEqual(result["guard"]["etf_value_by_security_id"]["1"], 400 * 0.10 + 200 * 0.05)  # published weights times value, not normalised
        self.assertTrue(result["checks"]["reconciles"])
        self.assertNotIn("rounding_drift", {c["category"] for c in result["top"]})


class OrchestratorSummaryTests(GuardCase):
    def summary(self, stale=False):
        import analysis_engine

        ctx = analysis_engine.build_portfolio_context(as_of=AS_OF, connection=self.conn)
        with mock.patch.object(eh, "DEFAULT_UNIVERSE", self.universe):
            return orch._with_etf_exposure(self.conn, ctx, AS_OF, StrategyConfig())

    def test_tsmc_style_adr_plus_etf_is_one_company_at_about_7_3_percent(self) -> None:
        c = self.conn
        c.execute("UPDATE security SET symbol = 'TSM', name = 'TSMC (ADR)', isin = 'US8740391003', exchange = 'NYSE' WHERE id = 1")
        c.execute("UPDATE market_snapshot SET price = 62 WHERE security_id = 1")
        c.execute("UPDATE market_snapshot SET price = 8580 / 3.0 WHERE security_id = 2")
        c.execute("UPDATE market_snapshot SET price = 800 WHERE security_id = 3")
        c.execute("DELETE FROM positions WHERE security_id = 4")
        self.write({ETF1: etf_entry([holding("TAIWAN SEMICONDUCTOR MANUFACTURING", 13.99, ticker="2330", exchange="Taiwan Stock Exchange"), holding("Rest", 86.01, isin="US0000RST001")])},
                   aliases=[{"ticker": "2330", "exchange": "Taiwan Stock Exchange", "isin": "US8740391003", "reason": "ADR of the issuer of this ordinary share"}])
        ctx, summary = self.summary()
        self.assertEqual((summary["basis"], summary["complete"], summary["status"]), ("effective", True, "available"))
        tsmc = next(x for x in summary["largest"] if x["security_id"] == 1)
        self.assertAlmostEqual(tsmc["direct_weight_pct"], 6.2)
        self.assertAlmostEqual(tsmc["etf_weight_pct"], 1.1192, places=3)
        self.assertAlmostEqual(tsmc["effective_weight_pct"], 7.3192, places=3)
        self.assertEqual(sum(1 for x in summary["largest"] if "TSMC" in x["name"] or "SEMICONDUCTOR" in x["name"]), 1)  # once, not twice
        self.assertEqual(summary["limit_pct"], 20.0)
        self.assertNotIn("TSMC (ADR)", summary["breaches"])  # the filler stock of the fixture is the only one above 20 %
        self.assertAlmostEqual(dict(ctx.etf_exposure_by_security)[1] / ctx.total_market_value * 100, 1.1192, places=3)
        self.assertIs(ctx.etf_exposure_quality.status, AvailabilityStatus.AVAILABLE)

    def test_a_company_only_in_etfs_is_visible_as_a_single_name_risk(self) -> None:
        self.write({ETF1: etf_entry([holding("Chip Giant", 30.0, isin="US0000CHP001"), holding("Rest", 70.0, isin="US0000RST001")]),
                    ETF2: etf_entry([holding("Chip Giant", 20.0, isin="US0000CHP001"), holding("Rest", 80.0, isin="US0000RST001")])})
        _, summary = self.summary()
        giant = next(x for x in summary["largest"] if x["name"] == "Chip Giant")
        self.assertEqual(giant["category"], "etf_only")
        self.assertEqual(giant["direct_weight_pct"], 0.0)
        self.assertAlmostEqual(giant["effective_weight_pct"], (400 * 0.30 + 200 * 0.20) / 1500 * 100)
        self.assertEqual(len(giant["etfs"]), 2)  # both ETFs, one company

    def test_a_breach_of_the_unchanged_limit_is_listed(self) -> None:
        self.write({ETF1: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")]), ETF2: etf_entry([holding("Pos Corp", 100.0, isin="US0000POS001")])})
        _, summary = self.summary()  # 600 direct + 400 + 200 in ETFs = 100 % of 1500 -> far above the 20 % limit
        self.assertEqual(len(summary["breaches"]), 1)

    def test_stale_holdings_give_direct_only_and_no_map(self) -> None:
        old = (MARKET_END - timedelta(days=200)).isoformat()
        self.write({ETF1: etf_entry([holding("Pos Corp", 10.0, isin="US0000POS001"), holding("Rest", 90.0, isin="US0000RST001")], as_of=old), ETF2: etf_entry([holding("Pos Corp", 5.0, isin="US0000POS001"), holding("Rest", 95.0, isin="US0000RST001")], as_of=old)})
        ctx, summary = self.summary()
        self.assertEqual((summary["basis"], summary["complete"], summary["status"]), ("direct_only", False, "stale"))
        self.assertTrue(summary["details"])
        self.assertEqual(ctx.etf_exposure_by_security, ())
        self.assertIs(ctx.etf_exposure_quality.status, AvailabilityStatus.STALE)

    def test_a_missing_file_is_unavailable_never_silently_direct(self) -> None:
        (self.universe / eh.FILE_NAME).unlink()
        ctx, summary = self.summary()
        self.assertEqual((summary["basis"], summary["complete"], summary["status"]), ("direct_only", False, "unavailable"))
        self.assertEqual(sn.resolve(ctx, 1, 600.0).basis, sn.DIRECT_ONLY)

    def test_an_error_in_the_calculation_is_reported_not_swallowed_silently(self) -> None:
        import analysis_engine

        ctx = analysis_engine.build_portfolio_context(as_of=AS_OF, connection=self.conn)
        with mock.patch.object(ee, "build_effective_exposure", side_effect=RuntimeError("boom")):
            ctx2, summary = orch._with_etf_exposure(self.conn, ctx, AS_OF, StrategyConfig())
        self.assertEqual((summary["basis"], summary["status"], summary["details"]), ("direct_only", "unavailable", ["EFFECTIVE_EXPOSURE_ERROR:RuntimeError"]))
        self.assertEqual(sn.resolve(ctx2, 1, 1.0).basis, sn.DIRECT_ONLY)

    def test_a_portfolio_without_an_etf_is_not_applicable(self) -> None:
        self.conn.execute("DELETE FROM positions WHERE security_id IN (3, 4)")
        ctx, summary = self.summary()
        self.assertEqual((summary["basis"], summary["status"], summary["largest"]), ("direct", "not_applicable", []))
        self.assertEqual(sn.resolve(ctx, 1, 5.0).basis, sn.DIRECT)


class UntouchedTests(unittest.TestCase):
    def test_the_sector_and_correlation_guard_is_not_part_of_the_change(self) -> None:
        text = (ROOT / "tools" / "trading" / "portfolio_risk.py").read_text(encoding="utf-8")
        for name in ("single_name_exposure", "etf_exposure", "effective_exposure"):
            self.assertNotIn(name, text)
        self.assertEqual(StrategyConfig().sizing.max_security_weight, 0.20)


if __name__ == "__main__":
    unittest.main()
