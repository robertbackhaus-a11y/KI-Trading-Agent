from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import (  # noqa: E402
    AvailabilityStatus,
    DataQuality,
    PortfolioContext,
)
from analysis_engine import build_analysis_snapshot, build_portfolio_context  # noqa: E402
from capital_state import resolve_capital_state  # noqa: E402
from decision_engine import decide  # noqa: E402
from portfolio_context import evaluate_sizing_readiness  # noqa: E402
from strategy_config import CapitalStateConfig, SizingPolicyConfig, StrategyConfig  # noqa: E402
from _fixtures import MARKET_END, make_connection  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "trading" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("capital_state_migration", "Migrate-TradingCapitalState.py")
manager = _load("manage_capital_state", "Manage-TradingCapitalState.py")


def _create_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT);
        INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0');
        """
    )
    migration.apply_migration(conn)
    return conn


def _context(*, cash: float | None, cash_status: AvailabilityStatus) -> PortfolioContext:
    quality = DataQuality(AvailabilityStatus.AVAILABLE, as_of="2026-09-23")
    cash_quality = DataQuality(cash_status, as_of="2026-09-23")
    return PortfolioContext(
        evaluation_as_of="2026-09-23",
        base_currency="EUR",
        total_market_value=100.0,
        valuation_quality=quality,
        swing_market_value=35.0,
        swing_weight_pct=35.0,
        long_term_market_value=65.0,
        long_term_weight_pct=65.0,
        cash_available=cash,
        cash_quality=cash_quality,
        buying_power=None,
        buying_power_quality=DataQuality(AvailabilityStatus.UNAVAILABLE),
        capital_state_as_of=None,
        capital_state_source=None,
        current_security_id=None,
        current_security_market_value=None,
        current_security_market_value_eur=None,
        current_security_weight_pct=None,
        allocation_quality=quality,
    )


class CapitalStateSchemaAndResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = _create_db(Path(self.temp_dir.name) / "trading.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _set(self, **overrides: object) -> dict:
        values: dict[str, object] = {
            "as_of": "2026-09-23",
            "currency": "EUR",
            "cash_available": 15000.0,
            "buying_power": None,
            "source": "manual",
            "quality": "available",
            "notes": None,
            "write": True,
        }
        values.update(overrides)
        return manager.set_capital_state(self.conn, **values)

    def test_migration_dry_run_write_and_feature_marker(self) -> None:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0')")
            before = migration.inspect_migration(conn)
            self.assertFalse(before["table_exists"])
            self.assertIsNone(before["feature_version"])
            after = migration.apply_migration(conn)["after"]
            self.assertTrue(after["table_exists"])
            self.assertEqual(after["feature_version"], "1")
        finally:
            conn.close()

    def test_empty_state_is_unknown_and_future_state_is_excluded(self) -> None:
        empty = resolve_capital_state(self.conn, "2026-09-23")
        self.assertIsNone(empty.cash_available)
        self.assertEqual(empty.cash_quality.status, AvailabilityStatus.UNAVAILABLE)
        self._set(as_of="2026-09-24", cash_available=999.0)
        future = resolve_capital_state(self.conn, "2026-09-23")
        self.assertIsNone(future.cash_available)
        self.assertIsNone(future.as_of)

    def test_cash_only_buying_power_only_both_and_null_values(self) -> None:
        cash_only = self._set(cash_available=15000.0, buying_power=None)
        self.assertEqual(cash_only["inserted"]["cash_available"], 15000.0)
        resolved = resolve_capital_state(self.conn, "2026-09-23")
        self.assertEqual(resolved.cash_available, 15000.0)
        self.assertEqual(resolved.cash_quality.status, AvailabilityStatus.AVAILABLE)
        self.assertIsNone(resolved.buying_power)
        self.assertEqual(resolved.buying_power_quality.status, AvailabilityStatus.UNAVAILABLE)

        buying_only = self._set(as_of="2026-09-24", cash_available=None, buying_power=22000.0)
        self.assertEqual(buying_only["inserted"]["buying_power"], 22000.0)
        resolved = resolve_capital_state(self.conn, "2026-09-24")
        self.assertIsNone(resolved.cash_available)
        self.assertEqual(resolved.buying_power, 22000.0)

        self._set(as_of="2026-09-25", cash_available=17000.0, buying_power=24000.0)
        both = resolve_capital_state(self.conn, "2026-09-25")
        self.assertEqual(both.cash_available, 17000.0)
        self.assertEqual(both.buying_power, 24000.0)

        self._set(as_of="2026-09-26", cash_available=None, buying_power=None)
        nulls = resolve_capital_state(self.conn, "2026-09-26")
        self.assertIsNone(nulls.cash_available)
        self.assertIsNone(nulls.buying_power)
        self.assertEqual(nulls.cash_quality.status, AvailabilityStatus.UNAVAILABLE)

    def test_latest_eligible_state_and_optional_freshness(self) -> None:
        self._set(as_of="2026-09-20", cash_available=100.0)
        self._set(as_of="2026-09-22", cash_available=200.0)
        self._set(as_of="2026-09-24", cash_available=300.0)
        latest = resolve_capital_state(self.conn, "2026-09-23")
        self.assertEqual(latest.cash_available, 200.0)
        self.assertEqual(latest.as_of, "2026-09-22")
        no_threshold = resolve_capital_state(self.conn, "2026-10-10")
        self.assertEqual(no_threshold.cash_quality.status, AvailabilityStatus.AVAILABLE)
        stale = resolve_capital_state(
            self.conn,
            "2026-10-10",
            CapitalStateConfig(freshness_max_age_days=5),
        )
        self.assertEqual(stale.cash_quality.status, AvailabilityStatus.STALE)

    def test_set_dry_run_and_validation(self) -> None:
        dry_run = self._set(write=False)
        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM portfolio_capital_state").fetchone()[0],
            0,
        )
        self.assertEqual(manager.validate_capital_states(self.conn), [])
        with self.assertRaisesRegex(manager.CapitalStateValidationError, "currency must be EUR"):
            self._set(currency="USD")


class CapitalStatePortfolioAndSizingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        self.conn.execute("INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0')")
        self.conn.commit()
        migration.apply_migration(self.conn)
        self.as_of = MARKET_END.isoformat()

    def tearDown(self) -> None:
        self.conn.close()

    def test_portfolio_context_preserves_known_capital_source_and_as_of(self) -> None:
        manager.set_capital_state(
            self.conn,
            as_of=self.as_of,
            currency="EUR",
            cash_available=15000.0,
            buying_power=22000.0,
            source="manual",
            write=True,
        )
        context = build_portfolio_context(1, connection=self.conn)
        self.assertEqual(context.cash_available, 15000.0)
        self.assertEqual(context.cash_quality.status, AvailabilityStatus.AVAILABLE)
        self.assertEqual(context.buying_power, 22000.0)
        self.assertEqual(context.buying_power_quality.status, AvailabilityStatus.AVAILABLE)
        self.assertEqual(context.capital_state_as_of, self.as_of)
        self.assertEqual(context.capital_state_source, "manual")

    def test_sizing_config_defaults_and_readiness_are_explicit(self) -> None:
        policy = SizingPolicyConfig()
        self.assertEqual(policy.max_security_weight, 0.20)
        self.assertEqual(policy.max_initial_swing_weight, 0.10)
        self.assertEqual(policy.max_add_pct_of_original, 0.25)
        self.assertEqual(policy.minimum_cash_reserve, 10_000.0)
        self.assertEqual(policy.max_add_count, 1)
        self.assertEqual(policy.whole_share_policy, "floor")

        missing_cash = evaluate_sizing_readiness(
            _context(cash=None, cash_status=AvailabilityStatus.UNAVAILABLE), policy
        )
        self.assertFalse(missing_cash.can_compute_add_sizing)
        self.assertIn("available cash is unavailable", missing_cash.missing_requirements)
        self.assertNotIn("max security weight is undefined", missing_cash.missing_requirements)

        ready = evaluate_sizing_readiness(
            _context(cash=15000.0, cash_status=AvailabilityStatus.AVAILABLE),
            SizingPolicyConfig(
                max_security_weight=0.10,
                max_add_pct_of_original=0.25,
                minimum_cash_reserve=1000.0,
                whole_share_policy="floor",
            ),
        )
        self.assertTrue(ready.can_compute_add_sizing)
        self.assertEqual(ready.missing_requirements, ())

    def test_capital_context_and_readiness_do_not_create_or_change_actions(self) -> None:
        before = decide(build_analysis_snapshot(1, connection=self.conn), StrategyConfig())
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        context = build_portfolio_context(1, connection=self.conn)
        evaluate_sizing_readiness(context, SizingPolicyConfig())
        self.conn.set_trace_callback(None)
        after = decide(build_analysis_snapshot(1, connection=self.conn), StrategyConfig())
        writes = [
            statement for statement in statements
            if statement.lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP")
            )
        ]
        self.assertEqual(writes, [])
        self.assertEqual(after, before)
        self.assertNotIn(after.action.value, {"BUY", "ADD"})


if __name__ == "__main__":
    unittest.main()
