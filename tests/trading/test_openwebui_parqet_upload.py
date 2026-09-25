"""Tests for the OpenWebUI-authorized Parqet upload boundary and tool flow."""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TRADING = ROOT / "tools" / "trading"
sys.path.insert(0, str(TRADING))

from openwebui_upload_resolver import (  # noqa: E402
    AuthorizedUpload,
    UploadResolutionError,
    require_attached_upload,
    validate_csv_upload_path,
)
import parqet_import  # noqa: E402


FILE_ID = "11111111-1111-4111-8111-111111111111"
HEADERS = "datetime;date;time;type;holding;identifier;wkn;shares;price;amount;fee;tax;realizedgains;currency;broker;assettype;notes\n"


def csv_row(*, dt: str = "2026-09-24T10:00:00.000Z", amount: str = "21,00") -> str:
    return f"{dt};;;BUY;Test;US0000000001;TEST;2;10,50;{amount};1,00;0,00;;EUR;broker;stock;note\n"


def load_tool_module():
    path = ROOT / "openwebui-tools" / "trading_sqlite.py"
    spec = importlib.util.spec_from_file_location("test_trading_sqlite", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UploadResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.csv = self.uploads / "parqet.csv"
        self.csv.write_text(HEADERS + csv_row(), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_authorized_upload(self) -> None:
        self.assertEqual(
            require_attached_upload(FILE_ID, [{"type": "file", "id": FILE_ID}]),
            FILE_ID,
        )
        self.assertEqual(
            validate_csv_upload_path(path=self.csv, upload_dir=self.uploads, filename="parqet.csv", content_type="text/csv"),
            self.csv.resolve(),
        )

    def test_raw_path_path_traversal_and_missing_attachment_are_rejected(self) -> None:
        for raw in (r"C:\KI-Stack\data\trading\trading.db", "../parqet.csv"):
            with self.assertRaises(UploadResolutionError) as caught:
                require_attached_upload(raw, [{"type": "file", "id": FILE_ID}])
            self.assertEqual(caught.exception.code, "INVALID_UPLOAD_REFERENCE")
        with self.assertRaisesRegex(UploadResolutionError, "not attached"):
            require_attached_upload(FILE_ID, [])

    def test_wrong_type_and_out_of_scope_path_are_rejected(self) -> None:
        outside = self.root / "outside.csv"
        outside.write_text(HEADERS + csv_row(), encoding="utf-8")
        with self.assertRaises(UploadResolutionError) as caught:
            validate_csv_upload_path(path=outside, upload_dir=self.uploads, filename="outside.csv", content_type="text/csv")
        self.assertEqual(caught.exception.code, "UPLOAD_OUT_OF_SCOPE")
        with self.assertRaises(UploadResolutionError) as caught:
            validate_csv_upload_path(path=self.csv, upload_dir=self.uploads, filename="parqet.pdf", content_type="application/pdf")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_FILE_TYPE")


class WebUIParqetToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "trading.db"
        self.csv = self.root / "parqet.csv"
        self.csv.write_text(HEADERS + csv_row(), encoding="utf-8")
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript(
            """
            CREATE TABLE security(id INTEGER PRIMARY KEY, symbol TEXT, isin TEXT, wkn TEXT, name TEXT NOT NULL);
            CREATE TABLE source_symbols(id INTEGER PRIMARY KEY, security_id INTEGER, source_id INTEGER, symbol TEXT);
            CREATE TABLE transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, security_id INTEGER NOT NULL, transaction_type TEXT NOT NULL, transaction_date TEXT NOT NULL, shares REAL, price REAL, amount REAL, fees REAL DEFAULT 0, taxes REAL DEFAULT 0, currency TEXT, broker TEXT, external_id TEXT, notes TEXT);
            CREATE UNIQUE INDEX idx_external ON transactions(external_id) WHERE external_id IS NOT NULL;
            CREATE TABLE positions(security_id INTEGER PRIMARY KEY, shares REAL NOT NULL DEFAULT 0, avg_cost REAL, remaining_cost_basis REAL, currency TEXT, invested_amount REAL, realized_gain REAL DEFAULT 0, first_transaction_at TEXT, last_transaction_at TEXT, transaction_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE imports(id INTEGER PRIMARY KEY, import_type TEXT NOT NULL, source TEXT, file_name TEXT, started_at TEXT, completed_at TEXT, records_total INTEGER, records_imported INTEGER, records_failed INTEGER, status TEXT, notes TEXT);
            INSERT INTO security(id, symbol, isin, name) VALUES (1, 'TEST', 'US0000000001', 'Test');
            """
        )
        self.conn.commit()
        module = load_tool_module()
        self.tool = module.Tools()
        self.tool.valves.database_path = str(self.db)

        async def resolve(*args, **kwargs):
            return AuthorizedUpload(FILE_ID, "parqet.csv", "text/csv", self.csv)

        resolver = types.SimpleNamespace(resolve_openwebui_upload=resolve)

        def load(name: str):
            return resolver if name == "openwebui_upload_resolver" else parqet_import

        self.tool._load_runtime_trading_module = load

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def preview(self) -> dict:
        return asyncio.run(self.tool.preview_parqet_import(FILE_ID, __files__=[{"id": FILE_ID}], __user__={"id": "user"}))

    def apply(self, token: str, include_historical: bool = False) -> dict:
        return asyncio.run(self.tool.apply_parqet_import(FILE_ID, token, include_historical, __files__=[{"id": FILE_ID}], __user__={"id": "user"}))

    def test_preview_is_structured_and_does_not_write(self) -> None:
        result = self.preview()
        self.assertTrue(result["ok"])
        self.assertIn("plan_token", result)
        self.assertEqual(result["new"], 1)
        self.assertEqual(len(result["non_duplicate_rows"]), 1)
        self.assertNotIn("source_file", result)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

    def test_unknown_preview_token_is_stale_and_cannot_write(self) -> None:
        result = self.apply("not-a-preview-token")
        self.assertEqual(result["error"], "PLAN_STALE")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

    def test_apply_writes_after_preview_and_rebuilds_position_with_backup(self) -> None:
        preview = self.preview()
        result = self.apply(preview["plan_token"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["written"])
        self.assertIn("backup_file", result)
        position = self.conn.execute("SELECT shares, avg_cost FROM positions WHERE security_id=1").fetchone()
        self.assertEqual(position, (2.0, 11.0))

    def test_stale_file_and_database_state_are_rejected(self) -> None:
        token = self.preview()["plan_token"]
        self.csv.write_text(HEADERS + csv_row(amount="22,00"), encoding="utf-8")
        self.assertEqual(self.apply(token)["error"], "FILE_CHANGED")

        self.csv.write_text(HEADERS + csv_row(), encoding="utf-8")
        token = self.preview()["plan_token"]
        self.conn.execute("INSERT INTO transactions(security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker) VALUES (1, 'BUY', '2026-09-23T10:00:00.000Z', 1, 1, 1, 0, 0, 'EUR', 'broker')")
        self.conn.commit()
        self.assertEqual(self.apply(token)["error"], "DB_CHANGED")

    def test_historical_requires_explicit_opt_in_and_conflicts_do_not_write(self) -> None:
        self.conn.execute("INSERT INTO transactions(security_id, transaction_type, transaction_date, shares, price, amount, fees, taxes, currency, broker) VALUES (1, 'BUY', '2026-09-24T12:00:00.000Z', 1, 1, 1, 0, 0, 'EUR', 'broker')")
        self.conn.commit()
        self.csv.write_text(HEADERS + csv_row(dt="2026-09-01T10:00:00.000Z"), encoding="utf-8")
        preview = self.preview()
        self.assertEqual(preview["new_historical"], 1)
        self.assertFalse(self.apply(preview["plan_token"])["written"])
        preview = self.preview()
        self.assertTrue(self.apply(preview["plan_token"], include_historical=True)["written"])

        self.csv.write_text(HEADERS + csv_row(dt="2026-09-24T12:00:00.000Z", amount="2,00"), encoding="utf-8")
        preview = self.preview()
        self.assertEqual(preview["conflict"], 1)
        self.assertFalse(self.apply(preview["plan_token"])["written"])
