"""Regression tests for the safe, incremental Parqet importer."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))

from parqet_import import (  # noqa: E402
    CLASS_CONFLICT,
    CLASS_DUPLICATE,
    CLASS_INVALID,
    CLASS_NEW,
    CLASS_NEW_HISTORICAL,
    CLASS_UNKNOWN_SECURITY,
    ParqetImportError,
    apply_import_plan,
    build_import_plan,
    economic_fingerprint,
)


HEADERS = "datetime;date;time;type;holding;identifier;wkn;shares;price;amount;fee;tax;realizedgains;currency;broker;assettype;notes\n"


def row(*, dt: str = "2026-09-24T10:00:00.000Z", kind: str = "BUY", holding: str = "Test", isin: str = "US0000000001", wkn: str = "WKN", shares: str = "2", price: str = "10,50", amount: str = "21,00", fee: str = "1,00", tax: str = "0,00", gain: str = "", currency: str = "EUR", broker: str = "ing", notes: str = "note") -> str:
    return f"{dt};;;{kind};{holding};{isin};{wkn};{shares};{price};{amount};{fee};{tax};{gain};{currency};{broker};stock;{notes}\n"


class ParqetImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "trading.db"
        self.csv_path = Path(self.directory.name) / "input.csv"
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE security(id INTEGER PRIMARY KEY, symbol TEXT, isin TEXT, wkn TEXT, name TEXT NOT NULL);
            CREATE TABLE source_symbols(id INTEGER PRIMARY KEY, security_id INTEGER NOT NULL, source_id INTEGER NOT NULL, symbol TEXT NOT NULL);
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
            INSERT INTO security(id, symbol, isin, name) VALUES (1, 'TEST', 'US0000000001', 'Test');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.directory.cleanup()

    def write_csv(self, *rows: str, bom: bool = False) -> None:
        self.csv_path.write_text(HEADERS + "".join(rows), encoding="utf-8-sig" if bom else "utf-8")

    def insert_existing(self, *, amount: float = 21.0, external_id: str | None = None, dt: str = "2026-09-24T10:00:00.000Z") -> None:
        self.conn.execute("INSERT INTO transactions(security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker, external_id) VALUES (1, 'BUY', ?, 2, 10.5, ?, 1, 0, 'EUR', 'ing', ?)", (dt, amount, external_id))

    def test_parser_semicolon_decimal_comma_and_utf8_bom(self) -> None:
        self.csv_path.write_text(
            HEADERS.replace("holding;", "holdingname;") + row(gain="4,25"),
            encoding="utf-8-sig",
        )
        plan = build_import_plan(self.conn, self.csv_path)
        record = plan.records[0]
        self.assertEqual(record.classification, CLASS_NEW)
        self.assertEqual(record.price, 10.5)
        self.assertEqual(record.realized_gain, 4.25)
        self.assertTrue(json.dumps(plan.primitive()))

    def test_missing_headers_and_malformed_numeric_are_rejected_or_invalid(self) -> None:
        self.csv_path.write_text("type;identifier\nBUY;US0000000001\n", encoding="utf-8")
        with self.assertRaisesRegex(ParqetImportError, "missing required columns"):
            build_import_plan(self.conn, self.csv_path)
        self.write_csv(row(price="not-a-number"))
        self.assertEqual(build_import_plan(self.conn, self.csv_path).records[0].classification, CLASS_INVALID)

    def test_known_exact_isin_and_unresolved_security(self) -> None:
        self.write_csv(row(), row(isin="US9999999999", holding="No Match"))
        plan = build_import_plan(self.conn, self.csv_path)
        self.assertEqual([r.classification for r in plan.records], [CLASS_NEW, CLASS_UNKNOWN_SECURITY])
        self.assertEqual(plan.records[0].security_resolution, "EXACT_ISIN")

    def test_safe_source_wkn_and_exact_normalized_name_fallbacks(self) -> None:
        self.conn.executescript("""
            INSERT INTO security(id, symbol, isin, name) VALUES (2, 'ALIAS', 'US0000000002', 'Alias Corp');
            INSERT INTO source_symbols(id, security_id, source_id, symbol) VALUES (1, 2, 1, 'ALIAS-STABLE');
            INSERT INTO security(id, symbol, isin, name) VALUES (3, 'HYNIX', 'KR7000660001', 'SK hynix Inc.');
            UPDATE security SET name='SK hynix Inc.', symbol='000660' WHERE id=3;
            INSERT INTO security(id, symbol, isin, name) VALUES (4, 'FLEX', NULL, 'Flex Ltd.');
        """)
        self.conn.execute("UPDATE security SET wkn='A1JWRE' WHERE id=3")
        self.write_csv(
            row(isin="ALIAS-STABLE"),
            row(isin="US78392B1070", wkn="A1JWRE", holding="SK Hynix"),
            row(isin="SG9999000020", wkn="890331", holding="Flex Ltd"),
        )
        records = build_import_plan(self.conn, self.csv_path).records
        self.assertEqual([record.security_id for record in records], [2, 3, 4])
        self.assertEqual([record.security_resolution for record in records], ["EXACT_SOURCE_SYMBOL", "EXACT_WKN", "EXACT_NORMALIZED_HOLDING_NAME"])

    def test_ambiguous_fallback_stays_unknown_and_does_not_create_security(self) -> None:
        self.conn.executemany("INSERT INTO security(id, symbol, isin, name, wkn) VALUES (?, ?, NULL, ?, 'DUP')", [(2, 'A', 'One'), (3, 'B', 'Two')])
        before = self.conn.execute("SELECT COUNT(*) FROM security").fetchone()[0]
        self.write_csv(row(isin="US9999999999", wkn="DUP", holding="No Match"))
        record = build_import_plan(self.conn, self.csv_path).records[0]
        self.assertEqual(record.classification, CLASS_UNKNOWN_SECURITY)
        self.assertIn("AMBIGUOUS_SECURITY", record.classification_reason)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM security").fetchone()[0], before)

    def test_duplicate_new_historical_and_conflict_classification(self) -> None:
        self.insert_existing()
        self.write_csv(row(), row(dt="2026-09-01T10:00:00.000Z"), row(amount="22,00"))
        plan = build_import_plan(self.conn, self.csv_path)
        self.assertEqual([r.classification for r in plan.records], [CLASS_DUPLICATE, CLASS_NEW_HISTORICAL, CLASS_CONFLICT])

    def test_external_id_conflict_is_detected(self) -> None:
        fingerprint = economic_fingerprint(security_id=1, transaction_type="BUY", transaction_date="2026-09-24T10:00:00.000Z", shares=2, price=10.5, amount=21, fees=1, taxes=0, currency="EUR", broker="ing")
        self.insert_existing(amount=22, external_id=f"parqet:{fingerprint}")
        self.write_csv(row())
        self.assertEqual(build_import_plan(self.conn, self.csv_path).records[0].classification, CLASS_CONFLICT)

    def test_write_is_idempotent_rebuilds_position_and_preserves_realized_gain_notes(self) -> None:
        self.write_csv(row(gain="2,50"))
        preview = build_import_plan(self.conn, self.csv_path)
        result = apply_import_plan(self.conn, preview, expected_plan_token=preview.plan_token, create_backup=True)
        self.assertTrue(result["written"])
        self.assertTrue(Path(result["backup_path"]).is_file())
        position = self.conn.execute("SELECT shares, avg_cost, remaining_cost_basis, invested_amount FROM positions WHERE security_id=1").fetchone()
        self.assertEqual(tuple(position), (2.0, 11.0, 22.0, 22.0))
        notes = json.loads(self.conn.execute("SELECT notes FROM transactions").fetchone()[0])
        self.assertEqual(notes["parqet_realizedgains"], 2.5)
        repeat = build_import_plan(self.conn, self.csv_path)
        self.assertEqual(repeat.counts["new"], 0)
        self.assertEqual(repeat.counts["duplicate"], 1)
        self.assertTrue(json.dumps(result, sort_keys=True))

    def test_write_rolls_back_when_global_duplicate_audit_fails(self) -> None:
        self.insert_existing(dt="2026-09-20T10:00:00.000Z")
        self.insert_existing(dt="2026-09-20T10:00:00.000Z")
        self.write_csv(row(dt="2026-09-24T10:00:00.000Z"))
        plan = build_import_plan(self.conn, self.csv_path)
        with self.assertRaisesRegex(ParqetImportError, "duplicate audit"):
            apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)

    def test_historical_requires_opt_in_and_plan_token_prevents_drift(self) -> None:
        self.insert_existing(dt="2026-09-24T10:00:00.000Z")
        self.write_csv(row(dt="2026-09-01T10:00:00.000Z"))
        plan = build_import_plan(self.conn, self.csv_path)
        self.assertEqual(plan.records[0].classification, CLASS_NEW_HISTORICAL)
        no_opt_in = apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        self.assertFalse(no_opt_in["written"])
        with self.assertRaisesRegex(ParqetImportError, "stale"):
            apply_import_plan(self.conn, plan, expected_plan_token="not-the-plan", include_historical=True)
        fresh = build_import_plan(self.conn, self.csv_path)
        written = apply_import_plan(self.conn, fresh, expected_plan_token=fresh.plan_token, include_historical=True)
        self.assertEqual(len(written["inserted_transaction_ids"]), 1)

    def test_transfer_broker_validation_and_historical_transfer(self) -> None:
        self.insert_existing(dt="2026-09-24T10:00:00.000Z")
        self.write_csv(
            row(kind="TransferIn", broker="", dt="2021-02-02T08:00:00.000Z"),
            row(kind="TransferOut", broker="", dt="2021-02-03T08:00:00.000Z"),
            row(kind="BUY", broker="", dt="2026-09-25T08:00:00.000Z"),
        )
        records = build_import_plan(self.conn, self.csv_path).records
        self.assertEqual(records[0].classification, CLASS_NEW_HISTORICAL)
        self.assertIsNone(records[0].broker)
        self.assertEqual(records[1].classification, CLASS_NEW_HISTORICAL)
        self.assertIsNone(records[1].broker)
        self.assertEqual(records[2].classification, CLASS_INVALID)
        self.assertIn("missing broker", records[2].classification_reason)

    def test_db_drift_after_preview_rejects_apply(self) -> None:
        self.write_csv(row())
        plan = build_import_plan(self.conn, self.csv_path)
        self.insert_existing(dt="2026-09-23T10:00:00.000Z")
        with self.assertRaisesRegex(ParqetImportError, "stale"):
            apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)

    def test_lifecycle_reports_pre_and_post_campaign_without_writes(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO metadata(key, value) VALUES ('swing_campaign_schema_version', '1');
            CREATE TABLE swing_campaign(id INTEGER PRIMARY KEY, security_id INTEGER, strategy_assignment_id INTEGER,
                opened_at TEXT, original_quantity REAL, reference_avg_cost REAL, reference_currency TEXT, status TEXT, closed_at TEXT);
            CREATE TABLE swing_campaign_event(id INTEGER PRIMARY KEY, campaign_id INTEGER, event_type TEXT, event_at TEXT,
                quantity REAL, price REAL, currency TEXT, transaction_id INTEGER, source TEXT);
            INSERT INTO swing_campaign VALUES (1, 1, 1, '2026-09-20', 2, 10, 'EUR', 'open', NULL);
            INSERT INTO swing_campaign_event VALUES (1, 1, 'baseline', '2026-09-20', 2, 10, 'EUR', NULL, 'manual');
            """
        )
        self.insert_existing(dt="2026-09-19T10:00:00.000Z")
        self.write_csv(row(dt="2026-09-21T10:00:00.000Z"))
        plan = build_import_plan(self.conn, self.csv_path)
        outcome = apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        lifecycle = outcome["lifecycle"][0]
        self.assertIn("POST_CAMPAIGN_LIFECYCLE_UNCLASSIFIED", lifecycle["flags"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM swing_campaign_event").fetchone()[0], 1)

    def test_lifecycle_reports_pre_campaign_baseline_impact_without_rewrite(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO metadata(key, value) VALUES ('swing_campaign_schema_version', '1');
            CREATE TABLE swing_campaign(id INTEGER PRIMARY KEY, security_id INTEGER, strategy_assignment_id INTEGER,
                opened_at TEXT, original_quantity REAL, reference_avg_cost REAL, reference_currency TEXT, status TEXT, closed_at TEXT);
            CREATE TABLE swing_campaign_event(id INTEGER PRIMARY KEY, campaign_id INTEGER, event_type TEXT, event_at TEXT,
                quantity REAL, price REAL, currency TEXT, transaction_id INTEGER, source TEXT);
            INSERT INTO swing_campaign VALUES (1, 1, 1, '2026-09-23', 2, 10, 'EUR', 'open', NULL);
            INSERT INTO swing_campaign_event VALUES (1, 1, 'baseline', '2026-09-23', 2, 10, 'EUR', NULL, 'manual');
            """
        )
        self.insert_existing(dt="2026-09-20T10:00:00.000Z")
        self.write_csv(row(dt="2026-09-21T10:00:00.000Z"))
        plan = build_import_plan(self.conn, self.csv_path)
        outcome = apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        self.assertIn("PRE_CAMPAIGN_BASELINE_IMPACT", outcome["lifecycle"][0]["flags"])
        self.assertEqual(self.conn.execute("SELECT original_quantity FROM swing_campaign").fetchone()[0], 2.0)


if __name__ == "__main__":
    unittest.main()
