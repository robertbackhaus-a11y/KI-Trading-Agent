"""FX chain: ECB multi-currency backfill, pence normalization, promotion FX reason code, scheduler job."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import AvailabilityStatus  # noqa: E402
from fx_resolver import normalize_quote_unit, resolve_fx_rate  # noqa: E402
from swing_promotion import DATA_INSUFFICIENT, PROMOTE, evaluate_swing_promotion  # noqa: E402
from _fixtures import MARKET_END, MARKET_START, make_connection  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "trading" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("fx_migration_multi", "Migrate-TradingFXRates.py")
ecb = _load("ecb_backfill_multi", "Backfill-TradingFXRatesECB.py")

AS_OF = "2026-09-18"
RATES = {"USD": 1.2, "GBP": 0.86, "AUD": 1.6, "KRW": 1600.0}


def _migrated_connection(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0');
        CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, isin TEXT, wkn TEXT, name TEXT NOT NULL, exchange TEXT, currency TEXT);
        CREATE TABLE positions (security_id INTEGER PRIMARY KEY, shares REAL, avg_cost REAL, remaining_cost_basis REAL, currency TEXT, realized_gain REAL, last_transaction_at TEXT);
        CREATE TABLE market_snapshot (security_id INTEGER PRIMARY KEY, price REAL, previous_close REAL, market_cap REAL, currency TEXT, as_of_at TEXT);
        """
    )
    migration.apply_migration(conn)
    return conn


def _insert_rates(conn: sqlite3.Connection, rate_date: str = AS_OF, currencies=tuple(RATES)) -> None:
    for quote in currencies:
        conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', ?, ?, 'ECB', ?)",
            (rate_date, quote, RATES[quote], rate_date + "T16:00:00+00:00"),
        )


class ResolverCurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _migrated_connection()
        _insert_rates(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_eur_is_unchanged(self) -> None:
        res = resolve_fx_rate(self.conn, "EUR", "EUR", AS_OF)
        self.assertEqual(res.convert(10), 10.0)
        self.assertEqual(res.quality.status, AvailabilityStatus.NOT_APPLICABLE)
        self.assertEqual(res.price_factor, 1.0)

    def test_each_ecb_currency_converts_with_its_rate(self) -> None:
        for quote, rate in RATES.items():
            with self.subTest(quote=quote):
                res = resolve_fx_rate(self.conn, quote, "EUR", AS_OF)
                self.assertEqual(res.quality.status, AvailabilityStatus.AVAILABLE)
                self.assertAlmostEqual(res.convert(rate * 10) or 0.0, 10.0)
                self.assertEqual(res.rate, rate)
                self.assertEqual(res.source, "ECB")
                self.assertEqual(res.price_factor, 1.0)

    def test_pence_normalizes_to_pounds_by_factor_one_hundredth(self) -> None:
        self.assertEqual(normalize_quote_unit("GBp"), ("GBP", 0.01))
        self.assertEqual(normalize_quote_unit("GBP"), ("GBP", 1.0))
        res = resolve_fx_rate(self.conn, "GBp", "GBP", AS_OF)
        self.assertEqual(res.price_factor, 0.01)
        self.assertAlmostEqual(res.convert(650) or 0.0, 6.5)
        self.assertEqual(res.quality.status, AvailabilityStatus.AVAILABLE)

    def test_pence_full_chain_to_eur(self) -> None:
        res = resolve_fx_rate(self.conn, "GBp", "EUR", AS_OF)
        self.assertEqual(res.from_currency, "GBp")
        self.assertEqual(res.quality.status, AvailabilityStatus.AVAILABLE)
        self.assertAlmostEqual(res.convert(650) or 0.0, 6.5 / 0.86)
        pounds = resolve_fx_rate(self.conn, "GBP", "EUR", AS_OF)
        self.assertAlmostEqual((res.convert(650) or 0.0) * 100, pounds.convert(650) or 0.0)

    def test_pounds_are_never_scaled_and_unknown_variants_stay_invalid(self) -> None:
        self.assertAlmostEqual((resolve_fx_rate(self.conn, "GBP", "EUR", AS_OF).convert(86) or 0.0), 100.0)
        for variant in ("GBX", "gbp", "gbP", "", None):
            with self.subTest(variant=variant):
                res = resolve_fx_rate(self.conn, variant, "EUR", AS_OF)
                self.assertEqual(res.quality.status, AvailabilityStatus.UNAVAILABLE)
                self.assertIsNone(res.convert(100))

    def test_missing_rate_is_unavailable_including_pence(self) -> None:
        conn = _migrated_connection()
        _insert_rates(conn, currencies=("USD",))
        try:
            for quote in ("GBP", "AUD", "KRW", "GBp"):
                with self.subTest(quote=quote):
                    res = resolve_fx_rate(conn, quote, "EUR", AS_OF)
                    self.assertEqual(res.quality.status, AvailabilityStatus.UNAVAILABLE)
                    self.assertIsNone(res.convert(100))
            self.assertAlmostEqual(resolve_fx_rate(conn, "USD", "EUR", AS_OF).convert(120) or 0.0, 100.0)
        finally:
            conn.close()

    def test_stale_pence_rate_is_withheld(self) -> None:
        res = resolve_fx_rate(self.conn, "GBp", "EUR", "2026-09-30")
        self.assertEqual(res.quality.status, AvailabilityStatus.STALE)
        self.assertIsNone(res.convert(650))

    def test_no_triangulation_outside_eur_pairs(self) -> None:
        res = resolve_fx_rate(self.conn, "GBp", "USD", AS_OF)
        self.assertEqual(res.quality.status, AvailabilityStatus.UNAVAILABLE)


class PromotionFxReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.today = date.today().isoformat()
        self.conn.execute("ALTER TABLE watchlist ADD COLUMN priority INTEGER")
        self.conn.execute("ALTER TABLE watchlist ADD COLUMN entry_reason TEXT")
        self.conn.execute("ALTER TABLE watchlist ADD COLUMN updated_at TEXT")
        self.conn.execute("INSERT INTO security(id, symbol, name, asset_type) VALUES (4, 'READY', 'Ready Corp', 'stock')")
        self.conn.execute("INSERT INTO watchlist(security_id, status, priority, entry_reason) VALUES (4, 'WATCH', 7, 'test')")
        rows = [(4, (MARKET_START + timedelta(days=i)).isoformat(), 100.0 + i, 100.0 + i, "EUR", 1) for i in range(260)]
        self.conn.executemany("INSERT INTO market_data(security_id, trade_date, close, adjusted_close, currency, source_id) VALUES (?, ?, ?, ?, ?, ?)", rows)
        self._snapshot(359, "EUR")

    def tearDown(self) -> None:
        self.conn.close()

    def _snapshot(self, price, currency) -> None:
        self.conn.execute("DELETE FROM market_snapshot WHERE security_id=4")
        self.conn.execute(
            "INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (4, ?, ?, ?)",
            (MARKET_END.isoformat() + "T00:00:00+00:00", price, currency),
        )

    def _rate(self, quote: str, rate: float) -> None:
        self.conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', ?, ?, 'ECB', ?)",
            (self.today, quote, rate, self.today + "T16:00:00+00:00"),
        )

    def test_eur_candidate_is_unchanged(self) -> None:
        decision = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(decision.recommendation, PROMOTE)
        self.assertEqual(decision.reasons, ("PROMOTION_READY",))
        self.assertEqual(decision.current_price_eur, 359)

    def test_missing_fx_rate_reports_fx_reason_not_market_data(self) -> None:
        for quote in ("USD", "AUD", "KRW", "GBp"):
            with self.subTest(quote=quote):
                self._snapshot(359, quote)
                decision = evaluate_swing_promotion(self.conn, 4)
                self.assertEqual(decision.recommendation, DATA_INSUFFICIENT)
                self.assertEqual(decision.reasons, ("PROMOTION_FX_RATE_MISSING",))
                self.assertIsNone(decision.current_price_eur)
                self.assertEqual(decision.current_price_currency, quote)

    def test_missing_quote_or_quote_currency_is_still_market_data_missing(self) -> None:
        self.conn.execute("DELETE FROM market_snapshot WHERE security_id=4")
        gone = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(gone.recommendation, DATA_INSUFFICIENT)
        self.assertEqual(gone.reasons, ("PROMOTION_MARKET_DATA_MISSING",))
        self._snapshot(359, None)
        no_currency = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(no_currency.reasons, ("PROMOTION_MARKET_DATA_MISSING",))

    def test_existing_usd_case_is_unchanged_once_a_rate_exists(self) -> None:
        self._snapshot(360, "USD")
        self._rate("USD", 1.2)
        decision = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(decision.recommendation, PROMOTE)
        self.assertAlmostEqual(decision.current_price_eur, 300.0)

    def test_other_currencies_and_pence_reach_a_normal_decision(self) -> None:
        cases = {"AUD": (320.0, 200.0), "KRW": (320000.0, 200.0), "GBP": (172.0, 200.0), "GBp": (17200.0, 200.0)}
        for quote, (price, eur) in cases.items():
            with self.subTest(quote=quote):
                self.conn.execute("DELETE FROM fx_rates")
                self._rate(quote if quote != "GBp" else "GBP", RATES[quote if quote != "GBp" else "GBP"])
                self._snapshot(price, quote)
                decision = evaluate_swing_promotion(self.conn, 4)
                self.assertEqual(decision.reasons, ("PROMOTION_READY",))
                self.assertAlmostEqual(decision.current_price_eur, eur)

    def test_stale_fx_rate_is_an_fx_reason(self) -> None:
        self.conn.execute(
            "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at) VALUES (?, 'EUR', 'USD', 1.2, 'ECB', 'x')",
            ((MARKET_END - timedelta(days=30)).isoformat(),),
        )
        self._snapshot(360, "USD")
        decision = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(decision.reasons, ("PROMOTION_FX_RATE_MISSING",))


class MultiCurrencyBackfillTests(unittest.TestCase):
    def _payload(self, quote: str, rate: float, day: str = AS_OF) -> str:
        return f"FREQ,CURRENCY,CURRENCY_DENOM,TIME_PERIOD,OBS_VALUE\nD,{quote},EUR,{day},{rate}\n"

    def test_every_supported_currency_builds_an_official_eur_url_and_parses(self) -> None:
        for quote, rate in RATES.items():
            with self.subTest(quote=quote):
                self.assertIn(f"D.{quote}.EUR.SP00.A", ecb.build_ecb_url(quote, latest=True))
                rows = ecb.parse_ecb_csv(self._payload(quote, rate), quote)
                self.assertEqual([(r["base_currency"], r["quote_currency"], r["rate"]) for r in rows], [("EUR", quote, rate)])

    def test_default_covers_the_four_currencies_and_parsing_is_strict(self) -> None:
        self.assertEqual(ecb.DEFAULT_QUOTE_CURRENCIES, ("USD", "GBP", "AUD", "KRW"))
        self.assertEqual(ecb.parse_quote_currencies("USD, gbp,AUD,KRW,usd"), ["USD", "GBP", "AUD", "KRW"])
        for bad in ("", " , ", "EUR", "GBp1", "US"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ecb.parse_quote_currencies(bad)

    def test_upsert_of_several_currencies_is_idempotent(self) -> None:
        conn = _migrated_connection()
        try:
            rows = [
                {"rate_date": AS_OF, "base_currency": "EUR", "quote_currency": q, "rate": r, "source": "ECB"}
                for q, r in RATES.items()
            ]
            self.assertEqual(ecb.upsert_rates(conn, rows, fetched_at="2026-09-18T16:00:00+00:00"), 4)
            self.assertEqual(ecb.upsert_rates(conn, rows, fetched_at="2026-09-19T16:00:00+00:00"), 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0], 4)
        finally:
            conn.close()

    def _run_main(self, db_path: Path, argv: list[str], fake_fetch) -> tuple[int, dict]:
        out = io.StringIO()
        code = 0
        with mock.patch.object(ecb, "fetch_ecb_rates", fake_fetch), mock.patch.object(sys, "argv", ["prog", "--db-path", str(db_path), *argv]), contextlib.redirect_stdout(out):
            try:
                ecb.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, json.loads(out.getvalue())

    def _fake_fetch(self, failing=()):
        def fetch(quote, **kwargs):
            if quote in failing:
                raise OSError("network down")
            url = ecb.build_ecb_url(quote, latest=True)
            return url, ecb.parse_ecb_csv(self._payload(quote, RATES[quote]), quote)

        return fetch

    def test_one_run_loads_all_default_currencies_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = _migrated_connection(str(db))
            conn.close()
            code, report = self._run_main(db, ["--write"], self._fake_fetch())
            self.assertEqual(code, 0)
            self.assertEqual([c["quote_currency"] for c in report["currencies"]], ["USD", "GBP", "AUD", "KRW"])
            self.assertEqual((report["row_count"], report["written"]), (4, 4))
            self.assertNotIn("errors", report)
            code, report = self._run_main(db, ["--write"], self._fake_fetch())
            self.assertEqual((code, report["written"]), (0, 4))
            check = sqlite3.connect(db)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0], 4)
            finally:
                check.close()

    def test_explicit_single_currency_still_works_and_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            _migrated_connection(str(db)).close()
            code, report = self._run_main(db, ["--quote-currency", "USD"], self._fake_fetch())
            self.assertEqual((code, report["row_count"], report.get("dry_run")), (0, 1, True))
            check = sqlite3.connect(db)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0], 0)
            finally:
                check.close()

    def test_one_failing_currency_keeps_the_others_and_fails_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            _migrated_connection(str(db)).close()
            code, report = self._run_main(db, ["--write"], self._fake_fetch(failing=("AUD",)))
            self.assertEqual(code, 1)
            self.assertEqual(report["written"], 3)
            self.assertEqual(list(report["errors"]), ["AUD"])
            check = sqlite3.connect(db)
            try:
                stored = {r[0] for r in check.execute("SELECT quote_currency FROM fx_rates")}
            finally:
                check.close()
            self.assertEqual(stored, {"USD", "GBP", "KRW"})


class SchedulerConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (ROOT / "scheduler" / "Manage-TradingTasks.ps1").read_text(encoding="utf-8-sig")
        self.jobs = re.findall(r"@\{ Name = '([^']+)';\s+Script = '([^']+)';\s+Args = '([^']*)'", self.text)

    def test_single_fx_task_loads_all_four_currencies(self) -> None:
        fx = [job for job in self.jobs if job[1] == "Backfill-TradingFXRatesECB.py"]
        self.assertEqual(len(fx), 1)
        name, _script, args = fx[0]
        self.assertEqual(name, "FXRates-Backfill")
        self.assertIn("--write", args)
        match = re.search(r"--quote-currency\s+(\S+)", args)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).split(","), list(ecb.DEFAULT_QUOTE_CURRENCIES))

    def test_task_set_is_the_documented_one(self) -> None:
        self.assertEqual(
            [job[0] for job in self.jobs],
            ["MarketData-Backfill", "FXRates-Backfill", "EventsNews-Backfill", "Fundamentals-SEC", "Candidate-Discovery", "Market-Intelligence"],
        )

    def test_discovery_and_market_intelligence_run_after_the_evening_backfills(self) -> None:
        def times(name: str) -> list[str]:
            line = next(line for line in self.text.splitlines() if f"Name = '{name}'" in line)
            match = re.search(r"At = (@\([^)]*\)|'[^']*')", line)
            self.assertIsNotNone(match, name)
            return re.findall(r"\d{2}:\d{2}", match.group(1))

        self.assertEqual(times("EventsNews-Backfill"), ["17:25"])
        self.assertEqual(times("Candidate-Discovery"), ["17:35"])
        self.assertEqual(times("Market-Intelligence"), ["08:15", "17:45"])
        for name in ("Candidate-Discovery", "Market-Intelligence"):
            line = next(line for line in self.text.splitlines() if f"Name = '{name}'" in line)
            self.assertIn("Logon = $false", line)  # fixed times only, no extra run at logon


if __name__ == "__main__":
    unittest.main()
