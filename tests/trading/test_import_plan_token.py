# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The plan token of the transaction import: a check may only be applied to the database state it described.

The token used to cover only COUNT(*), MAX(id) and MAX(transaction_date) of ``transactions`` besides the classified rows, so a changed quantity, price, fee or
tax of an existing transaction of a touched security (or its master data, a split, a strategy assignment) left the token unchanged and an old check could
be applied to a different state.  Real database schema (Reset-TradingDb); synthetic data only.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))

import import_workflow as workflow  # noqa: E402
import transaction_import as ti  # noqa: E402
from test_import_workflow import A1, A2, B1, ISIN_A, FlowCase, prow  # noqa: E402

OLD = prow("2026-02-02T10:00:00.000Z", "Buy", isin=ISIN_A, name="Alpha Corp", wkn="AAA111", shares="4", price="9", amount="36")


class PlanTokenCase(FlowCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_flow(A1, B1, create_securities=True)  # Alpha and Beta exist, Alpha holds 10 shares from A1
        self.alpha = int(self.security(ISIN_A)["id"])
        self.a1 = int(self.conn.execute("SELECT id FROM transactions WHERE security_id = ?", (self.alpha,)).fetchone()[0])

    def writer(self) -> sqlite3.Connection:
        """A second connection, as another process (scheduler, sql client) would have."""
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        self.addCleanup(conn.close)
        return conn

    def assert_refused(self, checked: dict, **kwargs) -> None:
        before = self.dump_hash()
        with self.assertRaisesRegex(ti.TransactionImportError, "stale"):
            workflow.run(self.db_path, self.csv_path, fmt="parqet", expected_plan_token=checked["plan_token"], **kwargs)
        self.assertEqual(self.dump_hash(), before)  # nothing was written, no backup row, no partial state


class ExistingTransactionChangeTests(PlanTokenCase):
    """The second file only adds A2; A1 is an existing transaction of the same security that the file does not repeat."""

    def change_a1(self, column: str, value: float) -> None:
        self.writer().execute(f"UPDATE transactions SET {column} = ? WHERE id = ?", (value, self.a1))

    def test_changed_quantity_of_an_existing_transaction_makes_the_check_stale(self) -> None:
        checked = self.check(A2)
        self.change_a1("shares", 11)
        self.assert_refused(checked)

    def test_changed_price_makes_the_check_stale(self) -> None:
        checked = self.check(A2)
        self.change_a1("price", 10.5)
        self.assert_refused(checked)

    def test_changed_fees_or_taxes_make_the_check_stale(self) -> None:
        for column in ("fees", "taxes", "amount"):
            with self.subTest(column=column):
                checked = self.check(A2)
                self.change_a1(column, 1.25)
                self.assert_refused(checked)
                self.change_a1(column, 0.0 if column != "amount" else 100.0)  # back to the checked state for the next case

    def test_aggregates_alone_did_not_see_the_change(self) -> None:
        """Documents the defect: COUNT, MAX(id) and MAX(transaction_date) are equal before and after the edit."""
        aggregates = "SELECT COUNT(*), MAX(id), MAX(transaction_date) FROM transactions"
        before = self.conn.execute(aggregates).fetchone()
        self.change_a1("shares", 11)
        self.assertEqual(tuple(self.conn.execute(aggregates).fetchone()), tuple(before))


class MasterDataChangeTests(PlanTokenCase):
    def test_changed_master_data_of_a_touched_security_makes_the_check_stale(self) -> None:
        for column, value in (("currency", "USD"), ("asset_type", "etf"), ("name", "Alpha Corporation"), ("active", 0)):
            with self.subTest(column=column):
                checked = self.check(A2)
                original = self.conn.execute(f"SELECT {column} FROM security WHERE id = ?", (self.alpha,)).fetchone()[0]
                self.writer().execute(f"UPDATE security SET {column} = ? WHERE id = ?", (value, self.alpha))
                self.assert_refused(checked)
                self.writer().execute(f"UPDATE security SET {column} = ? WHERE id = ?", (original, self.alpha))

    def test_a_new_split_makes_the_check_stale(self) -> None:
        self.conn.executescript(Path(ti.__file__).with_name("Migrate-TradingCorporateActions.py").read_text(encoding="utf-8").split('MIGRATION_SQL = """', 1)[1].split('"""', 1)[0])
        checked = self.check(A2)
        self.writer().execute("INSERT INTO corporate_action(security_id, action_type, effective_date, ratio_numerator, ratio_denominator, source) VALUES (?, 'STOCK_SPLIT', '2026-03-05', 2, 1, 'test')",
                              (self.alpha,))
        self.assert_refused(checked)

    def test_a_new_strategy_assignment_makes_the_check_stale(self) -> None:
        checked = self.check(A2)
        self.writer().execute("INSERT INTO strategy_assignment(security_id, strategy_type, effective_from, source) VALUES (?, 'long_term', '2026-03-02', 'test')", (self.alpha,))
        self.assert_refused(checked)

    def test_a_new_source_symbol_makes_the_check_stale(self) -> None:
        checked = self.check(A2)
        writer = self.writer()
        source = writer.execute("INSERT INTO data_sources(name, source_type, active) VALUES ('Yahoo', 'market', 1)").lastrowid
        writer.execute("INSERT INTO source_symbols(security_id, source_id, symbol) VALUES (?, ?, 'ALP')", (self.alpha, source))
        self.assert_refused(checked)


class UnrelatedChangeTests(PlanTokenCase):
    def test_unchanged_state_is_imported_exactly_as_checked(self) -> None:
        checked = self.check(A2)
        result = workflow.run(self.db_path, self.csv_path, fmt="parqet", expected_plan_token=checked["plan_token"])
        self.assertTrue(result["written"])
        self.assertEqual(result["plan_token"], checked["plan_token"])
        self.assertEqual(result["diff"]["position_changes"], checked["diff"]["position_changes"])
        self.assertEqual(self.position(ISIN_A)["shares"], 15.0)

    def test_market_data_of_a_touched_security_does_not_invalidate_the_check(self) -> None:
        """Prices are collected daily and are not part of an import; they must not make every check stale."""
        checked = self.check(A2)
        self.writer().execute("INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (?, '2026-03-10T00:00:00Z', 12.0, 'EUR')", (self.alpha,))
        result = workflow.run(self.db_path, self.csv_path, fmt="parqet", expected_plan_token=checked["plan_token"])
        self.assertTrue(result["written"])

    def test_the_token_is_deterministic(self) -> None:
        self.write_parqet(A2)
        first = ti.build_import_plan(self.conn, self.csv_path, adapter="parqet")
        second = ti.build_import_plan(self.conn, self.csv_path, adapter="parqet")
        self.assertEqual(first.plan_token, second.plan_token)
        self.assertEqual(first.db_state_token, second.db_state_token)


class TokenAndRaceTests(PlanTokenCase):
    def test_invalid_or_foreign_tokens_are_refused(self) -> None:
        self.check(A2)
        for token in ("0" * 64, "not-a-token", self.check(A1)["plan_token"]):
            with self.subTest(token=token[:12]):
                self.write_parqet(A2)
                with self.assertRaisesRegex(ti.TransactionImportError, "stale"):
                    workflow.run(self.db_path, self.csv_path, fmt="parqet", expected_plan_token=token)
        self.assertEqual(self.counts()["transactions"], 2)

    def test_a_check_without_historical_rows_cannot_import_them(self) -> None:
        checked = self.check(OLD)  # predates A1: NEW_HISTORICAL, the check skips it
        self.assertEqual(checked["diff"]["new_transactions"]["count"], 0)
        self.assert_refused(checked, include_historical=True)
        rechecked = self.check(OLD, include_historical=True)
        result = workflow.run(self.db_path, self.csv_path, fmt="parqet", include_historical=True, expected_plan_token=rechecked["plan_token"])
        self.assertEqual(len(result["inserted_transaction_ids"]), 1)

    def test_a_change_between_the_last_plan_and_the_write_lock_is_refused(self) -> None:
        """apply_import_plan rebuilds the plan under BEGIN IMMEDIATE; a writer that slips in after the unlocked rebuild is caught there."""
        checked = self.check(A2)
        real = ti.build_import_plan
        calls: list[int] = []

        def racing(conn, *args, **kwargs):
            plan = real(conn, *args, **kwargs)
            calls.append(1)
            if len(calls) == 2:  # workflow.run plans once, apply_import_plan plans again before it takes the lock
                self.writer().execute("UPDATE transactions SET shares = 11 WHERE id = ?", (self.a1,))
            return plan

        before = self.counts()
        with mock.patch.object(ti, "build_import_plan", racing), self.assertRaisesRegex(ti.TransactionImportError, "stale"):
            workflow.run(self.db_path, self.csv_path, fmt="parqet", expected_plan_token=checked["plan_token"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.counts(), before)

    def test_a_change_during_the_check_is_reported_not_mixed(self) -> None:
        self.write_parqet(A2)
        real = ti.build_import_plan
        calls: list[int] = []

        def racing(conn, *args, **kwargs):
            plan = real(conn, *args, **kwargs)
            calls.append(1)
            if len(calls) == 1:  # after the plan of the real database, before the snapshot of the simulation
                self.writer().execute("UPDATE transactions SET price = 10.5 WHERE id = ?", (self.a1,))
            return plan

        rw = sqlite3.connect(self.db_path, isolation_level=None)
        rw.row_factory = sqlite3.Row
        self.addCleanup(rw.close)
        with mock.patch.object(ti, "build_import_plan", racing), self.assertRaisesRegex(ti.TransactionImportError, "changed during the check"):
            ti.preview_import(rw, self.csv_path, adapter="parqet")

    def test_an_error_inside_the_write_transaction_leaves_nothing_behind(self) -> None:
        checked = self.check(A2, A3_ROW)
        before = self.dump_hash()
        with mock.patch.object(ti, "_duplicate_audit", side_effect=sqlite3.OperationalError("disk I/O error")), self.assertRaises(sqlite3.OperationalError):
            workflow.run(self.db_path, self.csv_path, fmt="parqet", expected_plan_token=checked["plan_token"])
        self.assertEqual(self.dump_hash(), before)
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        result = workflow.run(self.db_path, self.csv_path, fmt="parqet", expected_plan_token=checked["plan_token"])  # the same check is still valid afterwards
        self.assertEqual(len(result["inserted_transaction_ids"]), 2)


A3_ROW = prow("2026-03-16T10:00:00.000Z", "Sell", isin=ISIN_A, name="Alpha Corp", wkn="AAA111", shares="3", price="15", amount="45")


if __name__ == "__main__":
    unittest.main()
