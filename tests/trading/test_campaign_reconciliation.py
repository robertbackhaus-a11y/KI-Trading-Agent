"""Tests for the generic post-campaign transaction <-> Swing campaign reconciliation.

Uses the real Phase-3B.1 lifecycle schema (constraints, triggers, unique
transaction link) from Migrate-TradingSwingLifecycle.py.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "trading"
sys.path.insert(0, str(TOOLS))

from parqet_import import (  # noqa: E402
    apply_campaign_reconciliation,
    apply_import_plan,
    build_import_plan,
    rebuild_position,
    reconcile_campaign_transactions,
)
from swing_lifecycle import derive_open_lifecycle  # noqa: E402

_spec = importlib.util.spec_from_file_location("migrate_swing_lifecycle", TOOLS / "Migrate-TradingSwingLifecycle.py")
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

HEADERS = "datetime;date;time;type;holding;identifier;wkn;shares;price;amount;fee;tax;realizedgains;currency;broker;assettype;notes\n"
ALFA, BRAV, FOXT, GOLF = 1, 2, 3, 4
ISIN = {ALFA: "US0000000001", BRAV: "US0000000002", FOXT: "US0000000003", GOLF: "US0000000004"}


def csv_row(kind: str, dt: str, shares: str, price: str, amount: str, isin: str = ISIN[ALFA]) -> str:
    return f"{dt};;;{kind};Test;{isin};WKN;{shares};{price};{amount};0,00;0,00;;EUR;ing;stock;note\n"


class CampaignReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "trading.db"
        self.csv_path = Path(self.directory.name) / "input.csv"
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
            CREATE TABLE security(id INTEGER PRIMARY KEY, symbol TEXT, isin TEXT, wkn TEXT, name TEXT NOT NULL);
            CREATE TABLE transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, security_id INTEGER NOT NULL, transaction_type TEXT NOT NULL,
                transaction_date TEXT NOT NULL, shares REAL, price REAL, amount REAL, fees REAL DEFAULT 0,
                taxes REAL DEFAULT 0, currency TEXT, broker TEXT, external_id TEXT, notes TEXT,
                FOREIGN KEY(security_id) REFERENCES security(id));
            CREATE UNIQUE INDEX idx_external ON transactions(external_id) WHERE external_id IS NOT NULL;
            CREATE TABLE positions(
                security_id INTEGER PRIMARY KEY, shares REAL NOT NULL DEFAULT 0, avg_cost REAL,
                remaining_cost_basis REAL, currency TEXT, invested_amount REAL, realized_gain REAL DEFAULT 0,
                first_transaction_at TEXT, last_transaction_at TEXT, transaction_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE imports(id INTEGER PRIMARY KEY, import_type TEXT NOT NULL, source TEXT, file_name TEXT,
                started_at TEXT, completed_at TEXT, records_total INTEGER, records_imported INTEGER,
                records_failed INTEGER, status TEXT, notes TEXT);
            CREATE TABLE strategy_assignment(id INTEGER PRIMARY KEY AUTOINCREMENT, security_id INTEGER NOT NULL, strategy_type TEXT NOT NULL);
            """
        )
        for security_id, symbol in ((ALFA, "ALFA"), (BRAV, "BRAV"), (FOXT, "FOXT"), (GOLF, "GOLF")):
            self.conn.execute("INSERT INTO security(id, symbol, isin, name) VALUES (?, ?, ?, ?)", (security_id, symbol, ISIN[security_id], f"Name {symbol}"))
        self.conn.executescript(_migration.MIGRATION_SQL)
        self.conn.execute("INSERT INTO metadata(key, value) VALUES ('swing_campaign_schema_version', '1')")
        self._external = 0

    def tearDown(self) -> None:
        self.conn.close()
        self.directory.cleanup()

    def add_tx(self, kind: str, dt: str, shares: float, *, security_id: int = ALFA, price: float = 100.0) -> int:
        self._external += 1
        cursor = self.conn.execute(
            """INSERT INTO transactions(security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker, external_id)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, 'EUR', 'ing', ?)""",
            (security_id, kind, dt, shares, price, shares * price, f"ext-{self._external}"),
        )
        rebuild_position(self.conn, security_id)
        return int(cursor.lastrowid)

    def open_campaign(self, security_id: int, opened_at: str, quantity: float, *, status: str = "open") -> int:
        assignment = self.conn.execute("INSERT INTO strategy_assignment(security_id, strategy_type) VALUES (?, 'swing')", (security_id,)).lastrowid
        closed_at = opened_at if status == "closed" else None
        campaign_id = self.conn.execute(
            """INSERT INTO swing_campaign(security_id, strategy_assignment_id, opened_at, original_quantity, reference_avg_cost,
                                          reference_currency, status, closed_at, source)
               VALUES (?, ?, ?, ?, 100, 'EUR', ?, ?, 'manual')""",
            (security_id, assignment, opened_at, quantity, status, closed_at),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO swing_campaign_event(campaign_id, event_type, event_at, quantity, price, currency, source) VALUES (?, 'baseline', ?, ?, 100, 'EUR', 'manual')",
            (campaign_id, opened_at, quantity),
        )
        return int(campaign_id)

    def events(self, campaign_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM swing_campaign_event WHERE campaign_id = ? ORDER BY id", (campaign_id,)).fetchall()

    def lifecycle(self, security_id: int):
        position = self.conn.execute("SELECT shares FROM positions WHERE security_id = ?", (security_id,)).fetchone()
        return derive_open_lifecycle(self.conn, security_id, current_quantity=float(position[0]) if position else None)

    # 1 + 8: ALFA case (trade already imported earlier -> standalone reconciliation)
    def test_alfa_case_sell_after_campaign_start(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 120)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 120)
        transaction_id = self.add_tx("SELL", "2024-03-22T10:59:39.000Z", 30, price=150.0)
        before = self.lifecycle(ALFA)
        self.assertEqual((before.event_derived_quantity, before.reconciliation_delta), (120.0, -30.0))

        result = apply_campaign_reconciliation(self.conn)

        self.assertTrue(result["written"])
        events = self.events(campaign_id)
        self.assertEqual([e["event_type"] for e in events], ["baseline", "manual_reduction"])
        sell = events[1]
        self.assertEqual((sell["quantity"], sell["price"], sell["currency"], sell["transaction_id"]), (30.0, 150.0, "EUR", transaction_id))
        self.assertEqual(sell["source"], "parqet_reconciliation")
        after = self.lifecycle(ALFA)
        self.assertEqual(after.original_quantity, 120.0)
        self.assertEqual((after.event_derived_quantity, after.reconciliation_delta), (90.0, 0.0))
        self.assertEqual((after.tp1_status, after.tp2_status), ("not_recorded", "not_recorded"))
        self.assertEqual(self.conn.execute("SELECT shares FROM positions WHERE security_id = ?", (ALFA,)).fetchone()[0], 90.0)
        self.assertEqual(result["campaigns"][0]["delta_after_reconciliation"], 0.0)

    # 2: repeating the same import never duplicates the event
    def test_reimport_of_same_csv_creates_no_duplicate_event(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 120)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 120)
        self.csv_path.write_text(HEADERS + csv_row("SELL", "2024-03-22T10:59:39.000Z", "30", "150,00", "4500,00"), encoding="utf-8")

        plan = build_import_plan(self.conn, self.csv_path)
        first = apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        self.assertEqual(len(self.events(campaign_id)), 2)
        item = first["lifecycle"][0]
        self.assertEqual(item["status"], "RECONCILED")
        self.assertEqual((item["position_quantity"], item["campaign_expected_quantity"], item["delta_after_reconciliation"]), (90.0, 90.0, 0.0))
        self.assertEqual(item["reconciled_transactions"][0]["event_type"], "manual_reduction")
        self.assertEqual(item["tp1_status"], "not_recorded")
        self.assertNotIn("POST_CAMPAIGN_LIFECYCLE_UNCLASSIFIED", item["flags"])

        plan_again = build_import_plan(self.conn, self.csv_path)
        second = apply_import_plan(self.conn, plan_again, expected_plan_token=plan_again.plan_token)
        self.assertFalse(second["written"])
        again = apply_campaign_reconciliation(self.conn)
        self.assertFalse(again["written"])
        self.assertEqual(again["campaigns"][0]["skipped_already_processed"], 1)
        self.assertEqual(len(self.events(campaign_id)), 2)

    # 3 + 9: BRAV case (SELL before opened_at belongs to the baseline)
    def test_bravo_case_sell_before_campaign_start_is_ignored(self) -> None:
        self.add_tx("BUY", "2024-02-01T10:00:00.000Z", 60, security_id=BRAV)
        self.add_tx("SELL", "2024-03-17T16:17:12.000Z", 40, security_id=BRAV)
        campaign_id = self.open_campaign(BRAV, "2024-03-22", 20)
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual(report["status"], "NOTHING_TO_RECONCILE")
        self.assertEqual((report["skipped_pre_campaign"], report["delta_after_reconciliation"]), (2, 0.0))
        self.assertEqual([e["event_type"] for e in self.events(campaign_id)], ["baseline"])
        self.assertEqual(self.lifecycle(BRAV).reconciliation_delta, 0.0)

    # 4 + 10: FOXT case (BUY before opened_at belongs to the baseline)
    def test_foxtrot_case_buy_before_campaign_start_is_ignored(self) -> None:
        self.add_tx("BUY", "2024-02-01T10:00:00.000Z", 50, security_id=FOXT)
        self.add_tx("BUY", "2024-03-17T16:32:00.000Z", 15, security_id=FOXT)
        campaign_id = self.open_campaign(FOXT, "2024-03-22", 65)
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual(report["status"], "NOTHING_TO_RECONCILE")
        self.assertEqual(report["skipped_pre_campaign"], 2)
        self.assertEqual([e["event_type"] for e in self.events(campaign_id)], ["baseline"])
        self.assertEqual(self.lifecycle(FOXT).reconciliation_delta, 0.0)

    # 5: BUY after campaign start -> existing ADD semantics
    def test_buy_after_campaign_start_becomes_linked_add_event(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 65, security_id=FOXT)
        campaign_id = self.open_campaign(FOXT, "2024-03-22", 65)
        transaction_id = self.add_tx("BUY", "2024-03-23T09:00:00.000Z", 4, security_id=FOXT, price=180.0)
        apply_campaign_reconciliation(self.conn)
        events = self.events(campaign_id)
        self.assertEqual([e["event_type"] for e in events], ["baseline", "add"])
        self.assertEqual((events[1]["quantity"], events[1]["price"], events[1]["transaction_id"]), (4.0, 180.0, transaction_id))
        context = self.lifecycle(FOXT)
        self.assertEqual((context.original_quantity, context.event_derived_quantity, context.reconciliation_delta), (65.0, 69.0, 0.0))
        self.assertEqual((context.add_event_count, context.total_add_quantity), (1, 4.0))

    # 6: several open campaigns -> never assigned automatically
    def test_multiple_open_campaigns_are_ambiguous_and_never_written(self) -> None:
        self.conn.execute("DROP INDEX idx_swing_campaign_one_open_per_security")
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 100)
        first = self.open_campaign(ALFA, "2024-03-22", 100)
        second = self.open_campaign(ALFA, "2024-03-23", 100)
        self.add_tx("SELL", "2024-03-24T10:00:00.000Z", 10)
        reports = reconcile_campaign_transactions(self.conn, write=True)
        self.assertEqual(reports[0]["status"], "AMBIGUOUS")
        self.assertEqual(reports[0]["ambiguous"], ["MULTIPLE_OPEN_CAMPAIGNS"])
        self.assertEqual((len(self.events(first)), len(self.events(second))), (1, 1))
        self.assertFalse(apply_campaign_reconciliation(self.conn)["written"])

    # 7 + GOLF: no open campaign -> nothing invented
    def test_transaction_without_open_campaign_creates_no_event(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 10, security_id=GOLF)
        self.add_tx("SELL", "2024-03-25T10:00:00.000Z", 4, security_id=GOLF)
        closed = self.open_campaign(ALFA, "2024-03-10", 10, status="closed")
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 10)
        self.add_tx("SELL", "2024-03-25T10:00:00.000Z", 3)
        self.assertEqual(reconcile_campaign_transactions(self.conn, write=True), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM swing_campaign_event").fetchone()[0], 1)
        self.assertEqual(len(self.events(closed)), 1)

    def test_full_exit_reconciles_to_zero_without_closing_campaign(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 120)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 120)
        self.add_tx("SELL", "2024-03-23T10:00:00.000Z", 120)
        result = apply_campaign_reconciliation(self.conn)
        report = result["campaigns"][0]
        self.assertEqual((report["position_quantity"], report["campaign_expected_quantity"], report["delta_after_reconciliation"]), (0.0, 0.0, 0.0))
        self.assertIn("FULL_EXIT_CAMPAIGN_STILL_OPEN", report["flags"])
        self.assertEqual(self.conn.execute("SELECT status FROM swing_campaign WHERE id = ?", (campaign_id,)).fetchone()[0], "open")

    def test_existing_unlinked_manual_event_blocks_double_counting(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 120)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 120)
        self.conn.execute("INSERT INTO swing_campaign_event(campaign_id, event_type, event_at, quantity, source) VALUES (?, 'manual_reduction', '2024-03-22', 30, 'manual')", (campaign_id,))
        self.add_tx("SELL", "2024-03-22T10:59:39.000Z", 30)
        result = apply_campaign_reconciliation(self.conn)
        self.assertFalse(result["written"])
        report = result["campaigns"][0]
        self.assertEqual(report["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertIn("PROJECTED_DELTA_NONZERO", {i["reason"] for i in report["manual_review_required"]})
        self.assertEqual(len(self.events(campaign_id)), 2)

    def test_transfer_after_campaign_start_requires_manual_review(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 100)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 100)
        self.add_tx("TRANSFEROUT", "2024-03-23T10:00:00.000Z", 10)
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual(report["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(report["manual_review_required"][0]["reason"], "TRANSFER_REQUIRES_MANUAL_REVIEW")
        self.assertEqual(len(self.events(campaign_id)), 1)

    def test_split_after_post_campaign_trade_requires_manual_review(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 100)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 100)
        self.add_tx("SELL", "2024-03-23T10:00:00.000Z", 10)
        self.conn.executescript(
            """
            CREATE TABLE corporate_action(id INTEGER PRIMARY KEY, security_id INTEGER, action_type TEXT, effective_date TEXT,
                ratio_numerator REAL, ratio_denominator REAL, source TEXT, source_reference TEXT, notes TEXT);
            INSERT INTO metadata(key, value) VALUES ('corporate_action_schema_version', '1');
            INSERT INTO corporate_action(security_id, action_type, effective_date, ratio_numerator, ratio_denominator)
                VALUES (1, 'STOCK_SPLIT', '2024-04-01', 2, 1);
            """
        )
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual(report["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(report["manual_review_required"][0]["reason"], "CORPORATE_ACTION_AFTER_TRANSACTION")
        self.assertEqual(len(self.events(campaign_id)), 1)

    def test_oversell_requires_manual_review(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 100)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 40)  # baseline smaller than the position
        self.add_tx("SELL", "2024-03-23T10:00:00.000Z", 60)
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual(report["manual_review_required"][0]["reason"], "REDUCTION_EXCEEDS_CAMPAIGN_QUANTITY")
        self.assertEqual(len(self.events(campaign_id)), 1)

    # Same-day rule: opened_at has date resolution, broker trades have exact timestamps.
    def test_same_day_single_sell_proven_by_quantity_is_reconciled(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 120)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 120)
        transaction_id = self.add_tx("SELL", "2024-03-22T10:59:39.000Z", 30)  # 120 - 30 == 90 == position
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual((report["status"], report["applied"], report["manual_review_required"]), ("RECONCILED", True, []))
        self.assertEqual([(e["event_type"], e["transaction_id"]) for e in self.events(campaign_id)], [("baseline", None), ("manual_reduction", transaction_id)])
        self.assertEqual(self.lifecycle(ALFA).reconciliation_delta, 0.0)

    def test_same_day_single_buy_proven_by_quantity_is_reconciled(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 50, security_id=FOXT)
        campaign_id = self.open_campaign(FOXT, "2024-03-22", 50)
        self.add_tx("BUY", "2024-03-22T15:00:00.000Z", 15, security_id=FOXT)  # 50 + 15 == 65 == position
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual((report["status"], report["applied"]), ("RECONCILED", True))
        self.assertEqual([e["event_type"] for e in self.events(campaign_id)], ["baseline", "add"])

    def test_multiple_same_day_trades_require_manual_review_even_if_quantities_add_up(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 120)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 120)
        first = self.add_tx("SELL", "2024-03-22T09:30:00.000Z", 20)
        second = self.add_tx("SELL", "2024-03-22T10:59:39.000Z", 10)  # 120 - 20 - 10 == 90, still ambiguous
        result = apply_campaign_reconciliation(self.conn)
        self.assertFalse(result["written"])
        report = result["campaigns"][0]
        self.assertEqual(report["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual({(i["transaction_id"], i["reason"]) for i in report["manual_review_required"]},
                         {(first, "SAME_DAY_TRADES_AMBIGUOUS"), (second, "SAME_DAY_TRADES_AMBIGUOUS")})
        self.assertEqual(len(self.events(campaign_id)), 1)

    def test_same_day_buy_and_sell_netting_to_zero_is_ambiguous(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 100)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 100)
        self.add_tx("BUY", "2024-03-22T08:00:00.000Z", 10)
        self.add_tx("SELL", "2024-03-22T09:00:00.000Z", 10)  # delta would be 0 either way
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual(report["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual({i["reason"] for i in report["manual_review_required"]}, {"SAME_DAY_TRADES_AMBIGUOUS"})
        self.assertEqual(len(self.events(campaign_id)), 1)

    def test_same_day_buy_already_contained_in_baseline_requires_manual_review(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 50, security_id=FOXT)
        self.add_tx("BUY", "2024-03-22T08:00:00.000Z", 15, security_id=FOXT)  # before the baseline was taken
        campaign_id = self.open_campaign(FOXT, "2024-03-22", 65)  # baseline == position, BUY already inside
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual(report["status"], "MANUAL_REVIEW_REQUIRED")
        reasons = {i["reason"] for i in report["manual_review_required"]}
        self.assertEqual(reasons, {"SAME_DAY_TRADE_NOT_PROVEN", "PROJECTED_DELTA_NONZERO"})
        self.assertEqual(len(self.events(campaign_id)), 1)
        self.assertEqual(self.lifecycle(FOXT).reconciliation_delta, 0.0)

    def test_same_day_sell_already_contained_in_baseline_requires_manual_review(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 120)
        self.add_tx("SELL", "2024-03-22T08:00:00.000Z", 30)  # before the baseline was taken
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 90)  # baseline == position, SELL already inside
        report = reconcile_campaign_transactions(self.conn, write=True)[0]
        self.assertEqual(report["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertIn("SAME_DAY_TRADE_NOT_PROVEN", {i["reason"] for i in report["manual_review_required"]})
        self.assertEqual(len(self.events(campaign_id)), 1)

    def test_dry_run_on_read_only_connection_writes_nothing(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 120)
        campaign_id = self.open_campaign(ALFA, "2024-03-22", 120)
        self.add_tx("SELL", "2024-03-22T10:59:39.000Z", 30)
        read_only = sqlite3.connect(f"file:///{self.db_path.as_posix()}?mode=ro", uri=True)
        read_only.row_factory = sqlite3.Row
        read_only.execute("PRAGMA query_only = ON")
        try:
            report = reconcile_campaign_transactions(read_only, write=False)[0]
        finally:
            read_only.close()
        self.assertEqual((report["status"], report["applied"]), ("RECONCILED", False))
        self.assertEqual(len(self.events(campaign_id)), 1)

    def test_unresolved_post_campaign_delta_keeps_unclassified_flag_in_import_report(self) -> None:
        self.add_tx("BUY", "2024-03-01T10:00:00.000Z", 100)
        self.open_campaign(ALFA, "2024-03-22", 100)
        self.add_tx("TRANSFEROUT", "2024-03-23T10:00:00.000Z", 10)
        self.csv_path.write_text(HEADERS + csv_row("BUY", "2024-03-25T10:00:00.000Z", "5", "10,00", "50,00"), encoding="utf-8")
        plan = build_import_plan(self.conn, self.csv_path)
        item = apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)["lifecycle"][0]
        self.assertEqual(item["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertIn("POST_CAMPAIGN_LIFECYCLE_UNCLASSIFIED", item["flags"])
        self.assertTrue(item["lifecycle_reconciliation_required"])


if __name__ == "__main__":
    unittest.main()
