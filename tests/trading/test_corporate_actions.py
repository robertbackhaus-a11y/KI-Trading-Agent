"""Regression tests for corporate-action (stock split) support (Phase 4B.3)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))

from corporate_actions import (  # noqa: E402
    CorporateAction,
    CorporateActionError,
    corporate_action_schema_available,
    cumulative_split_factor,
    insert_stock_split,
    load_corporate_actions,
    validate_new_split,
)
from parqet_import import _rebuild_position, rebuild_position  # noqa: E402


def make_action(*, effective_date: str, ratio_numerator: float, ratio_denominator: float = 1.0, id_: int | None = None) -> CorporateAction:
    return CorporateAction(
        id=id_,
        security_id=1,
        action_type="STOCK_SPLIT",
        effective_date=effective_date,
        ratio_numerator=ratio_numerator,
        ratio_denominator=ratio_denominator,
        source="test",
        source_reference=None,
        notes=None,
    )


class CumulativeSplitFactorTests(unittest.TestCase):
    def test_no_actions_is_neutral(self) -> None:
        self.assertEqual(cumulative_split_factor([], "2025-01-01", "2026-01-01"), 1.0)

    def test_single_split_applies_only_when_transaction_predates_it(self) -> None:
        actions = [make_action(effective_date="2025-12-15", ratio_numerator=100)]
        self.assertEqual(cumulative_split_factor(actions, "2025-10-27", "2026-01-01"), 100.0)
        self.assertEqual(cumulative_split_factor(actions, "2025-12-16", "2026-01-01"), 1.0)

    def test_as_of_before_effective_date_keeps_pre_split_basis(self) -> None:
        actions = [make_action(effective_date="2025-12-15", ratio_numerator=100)]
        self.assertEqual(cumulative_split_factor(actions, "2025-10-27", "2025-12-01"), 1.0)

    def test_as_of_on_effective_date_applies_split(self) -> None:
        actions = [make_action(effective_date="2025-12-15", ratio_numerator=100)]
        self.assertEqual(cumulative_split_factor(actions, "2025-10-27", "2025-12-15"), 100.0)

    def test_multiple_splits_combine_multiplicatively_and_order_independently(self) -> None:
        actions_forward = [
            make_action(effective_date="2024-01-01", ratio_numerator=2),
            make_action(effective_date="2025-01-01", ratio_numerator=3),
        ]
        actions_reversed = list(reversed(actions_forward))
        self.assertEqual(cumulative_split_factor(actions_forward, "2023-01-01", "2026-01-01"), 6.0)
        self.assertEqual(cumulative_split_factor(actions_reversed, "2023-01-01", "2026-01-01"), 6.0)

    def test_transaction_between_two_splits_only_gets_the_later_one(self) -> None:
        actions = [
            make_action(effective_date="2024-01-01", ratio_numerator=2),
            make_action(effective_date="2025-01-01", ratio_numerator=3),
        ]
        self.assertEqual(cumulative_split_factor(actions, "2024-06-01", "2026-01-01"), 3.0)

    def test_default_as_of_is_today_not_none(self) -> None:
        actions = [make_action(effective_date="2020-01-01", ratio_numerator=2)]
        self.assertEqual(cumulative_split_factor(actions, "2019-01-01"), 2.0)


class SchemaBackedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "trading.db"
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE security(id INTEGER PRIMARY KEY, symbol TEXT, isin TEXT, wkn TEXT, name TEXT NOT NULL);
            CREATE TABLE transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, security_id INTEGER NOT NULL, transaction_type TEXT NOT NULL,
                transaction_date TEXT NOT NULL, shares REAL, price REAL, amount REAL, fees REAL DEFAULT 0,
                taxes REAL DEFAULT 0, currency TEXT, broker TEXT, external_id TEXT, notes TEXT,
                FOREIGN KEY(security_id) REFERENCES security(id));
            CREATE TABLE positions(
                security_id INTEGER PRIMARY KEY, shares REAL NOT NULL DEFAULT 0, avg_cost REAL,
                remaining_cost_basis REAL, currency TEXT, invested_amount REAL, realized_gain REAL DEFAULT 0,
                first_transaction_at TEXT, last_transaction_at TEXT, transaction_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE corporate_action (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                security_id         INTEGER NOT NULL,
                action_type         TEXT NOT NULL CHECK (action_type IN ('STOCK_SPLIT')),
                effective_date      TEXT NOT NULL,
                ratio_numerator     REAL NOT NULL CHECK (ratio_numerator > 0),
                ratio_denominator   REAL NOT NULL CHECK (ratio_denominator > 0),
                source              TEXT NOT NULL CHECK (length(trim(source)) > 0),
                source_reference    TEXT,
                notes               TEXT,
                created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (security_id) REFERENCES security(id) ON DELETE CASCADE,
                UNIQUE (security_id, action_type, effective_date, ratio_numerator, ratio_denominator)
            );
            INSERT INTO security(id, symbol, isin, name) VALUES (1, 'TEST', 'US0000000001', 'Test');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.directory.cleanup()

    def enable_feature(self) -> None:
        self.conn.execute(
            "INSERT INTO metadata(key, value) VALUES ('corporate_action_schema_version', '1')"
        )

    def insert_transaction(self, *, kind: str = "BUY", dt: str, shares: float, price: float, amount: float, fees: float = 0.0, taxes: float = 0.0) -> None:
        self.conn.execute(
            """INSERT INTO transactions(security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, 'EUR', 'ing')""",
            (kind, dt, shares, price, amount, fees, taxes),
        )

    # -- schema availability / load -----------------------------------------

    def test_schema_unavailable_returns_empty_list_and_false(self) -> None:
        self.assertFalse(corporate_action_schema_available(self.conn))
        self.assertEqual(load_corporate_actions(self.conn, 1), [])

    def test_load_returns_actions_ordered_by_effective_date(self) -> None:
        self.enable_feature()
        insert_stock_split(self.conn, security_id=1, effective_date="2025-01-01", ratio_numerator=3, ratio_denominator=1, source="test")
        insert_stock_split(self.conn, security_id=1, effective_date="2020-01-01", ratio_numerator=2, ratio_denominator=1, source="test")
        actions = load_corporate_actions(self.conn, 1)
        self.assertEqual([a.effective_date for a in actions], ["2020-01-01", "2025-01-01"])

    # -- validation / insertion ----------------------------------------------

    def test_validate_new_split_rejects_unknown_security(self) -> None:
        self.enable_feature()
        problems = validate_new_split(self.conn, security_id=999, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1)
        self.assertTrue(any("not found" in p for p in problems))

    def test_validate_new_split_rejects_bad_date_and_bad_ratio(self) -> None:
        self.enable_feature()
        self.assertTrue(validate_new_split(self.conn, security_id=1, effective_date="not-a-date", ratio_numerator=2, ratio_denominator=1))
        self.assertTrue(validate_new_split(self.conn, security_id=1, effective_date="2025-01-01", ratio_numerator=0, ratio_denominator=1))
        self.assertTrue(validate_new_split(self.conn, security_id=1, effective_date="2025-01-01", ratio_numerator=1, ratio_denominator=1))

    def test_insert_stock_split_requires_schema(self) -> None:
        with self.assertRaises(CorporateActionError):
            insert_stock_split(self.conn, security_id=1, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1, source="test")

    def test_insert_stock_split_requires_nonempty_source(self) -> None:
        self.enable_feature()
        with self.assertRaises(CorporateActionError):
            insert_stock_split(self.conn, security_id=1, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1, source="   ")

    def test_duplicate_insert_is_rejected_idempotency_guard(self) -> None:
        self.enable_feature()
        insert_stock_split(self.conn, security_id=1, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1, source="test")
        with self.assertRaisesRegex(CorporateActionError, "already exists"):
            insert_stock_split(self.conn, security_id=1, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1, source="test")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM corporate_action").fetchone()[0], 1)

    # -- position rebuild integration ----------------------------------------

    def test_rebuild_position_unaffected_security_matches_pre_split_behavior(self) -> None:
        self.enable_feature()
        self.insert_transaction(dt="2025-01-01", shares=10, price=10, amount=100, fees=1)
        _rebuild_position(self.conn, 1)
        position = self.conn.execute("SELECT shares, avg_cost, remaining_cost_basis, invested_amount FROM positions WHERE security_id=1").fetchone()
        self.assertEqual(tuple(position), (10.0, 10.1, 101.0, 101.0))

    def test_rebuild_position_applies_split_to_buy_before_effective_date(self) -> None:
        self.enable_feature()
        self.insert_transaction(dt="2025-10-27", shares=90, price=1173.04, amount=105573.60, fees=0)
        insert_stock_split(self.conn, security_id=1, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1, source="test")
        rebuild_position(self.conn, 1, as_of="2026-01-01")
        position = self.conn.execute("SELECT shares, avg_cost, remaining_cost_basis, invested_amount FROM positions WHERE security_id=1").fetchone()
        self.assertEqual(position["shares"], 9000.0)
        self.assertAlmostEqual(position["remaining_cost_basis"], 105573.60)
        self.assertAlmostEqual(position["invested_amount"], 105573.60)
        self.assertAlmostEqual(position["avg_cost"], 105573.60 / 9000.0)

    def test_rebuild_position_as_of_before_split_keeps_pre_split_shares(self) -> None:
        self.enable_feature()
        self.insert_transaction(dt="2025-10-27", shares=90, price=1173.04, amount=105573.60, fees=0)
        insert_stock_split(self.conn, security_id=1, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1, source="test")
        rebuild_position(self.conn, 1, as_of="2025-11-01")
        position = self.conn.execute("SELECT shares FROM positions WHERE security_id=1").fetchone()
        self.assertEqual(position["shares"], 90.0)

    def test_rebuild_position_sell_after_split_reduces_adjusted_shares(self) -> None:
        self.enable_feature()
        self.insert_transaction(dt="2025-10-27", shares=90, price=1173.04, amount=105573.60, fees=0)
        insert_stock_split(self.conn, security_id=1, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1, source="test")
        self.insert_transaction(kind="SELL", dt="2026-02-01", shares=500, price=12.0, amount=6000.0, fees=1)
        rebuild_position(self.conn, 1, as_of="2026-03-01")
        position = self.conn.execute("SELECT shares FROM positions WHERE security_id=1").fetchone()
        self.assertEqual(position["shares"], 8500.0)

    def test_rebuild_position_does_not_fabricate_realized_gain_from_split(self) -> None:
        self.enable_feature()
        self.insert_transaction(dt="2025-10-27", shares=90, price=1173.04, amount=105573.60, fees=0)
        _rebuild_position(self.conn, 1)
        before_realized = self.conn.execute("SELECT realized_gain FROM positions WHERE security_id=1").fetchone()[0]
        insert_stock_split(self.conn, security_id=1, effective_date="2025-12-15", ratio_numerator=100, ratio_denominator=1, source="test")
        rebuild_position(self.conn, 1, as_of="2026-01-01")
        after_realized = self.conn.execute("SELECT realized_gain FROM positions WHERE security_id=1").fetchone()[0]
        self.assertEqual(before_realized, after_realized)

    def test_rebuild_position_cumulative_splits_multiply(self) -> None:
        self.enable_feature()
        self.insert_transaction(dt="2020-01-01", shares=10, price=100, amount=1000, fees=0)
        insert_stock_split(self.conn, security_id=1, effective_date="2021-01-01", ratio_numerator=2, ratio_denominator=1, source="test")
        insert_stock_split(self.conn, security_id=1, effective_date="2022-01-01", ratio_numerator=3, ratio_denominator=1, source="test")
        rebuild_position(self.conn, 1, as_of="2023-01-01")
        position = self.conn.execute("SELECT shares, remaining_cost_basis FROM positions WHERE security_id=1").fetchone()
        self.assertEqual(position["shares"], 60.0)
        self.assertAlmostEqual(position["remaining_cost_basis"], 1000.0)


if __name__ == "__main__":
    unittest.main()
