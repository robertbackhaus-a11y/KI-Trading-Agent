"""Provider-neutral transaction import: canonical CSV, Parqet adapter, strategy-aware initialization, Swing campaigns, dry run.

Real database schema (Reset-TradingDb) with all constraints and triggers; synthetic data only.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "trading"
sys.path.insert(0, str(TOOLS))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reset = _load("reset_for_import_tests", "Reset-TradingDb.py")
cli = _load("import_trading_transactions", "Import-TradingTransactions.py")
parqet_cli = _load("import_parqet_transactions", "Import-ParqetTransactions.py")

import parqet_import  # noqa: E402
from swing_lifecycle import derive_open_lifecycle  # noqa: E402
from transaction_import import (  # noqa: E402
    CLASS_CONFLICT,
    CLASS_DUPLICATE,
    CLASS_INVALID,
    CLASS_NEW,
    CLASS_NEW_HISTORICAL,
    CLASS_UNKNOWN_SECURITY,
    ImportOptions,
    TransactionImportError,
    apply_import_plan,
    build_import_plan,
    parse_key_value_options,
    preview_import,
)

HEAD = "transaction_date,transaction_type,isin,symbol,wkn,name,shares,price,amount,fees,taxes,currency,broker,external_id,realized_gain,asset_type,notes\n"
EXA = dict(isin="US0000000001", symbol="EXA", name="Example Corp A")
EXB = dict(isin="US0000000002", symbol="EXB", name="Example Corp B")
EXD = dict(isin="US0000000004", symbol="EXD", name="Example Corp D")
TABLES = ("security", "transactions", "positions", "strategy_assignment", "swing_campaign", "swing_campaign_event", "imports")
PARQET_HEAD = "datetime;date;time;type;holding;identifier;wkn;shares;price;amount;fee;tax;realizedgains;currency;broker;assettype;notes\n"


def line(when, kind, *, isin=EXA["isin"], symbol=EXA["symbol"], name=EXA["name"], shares="10", price="10", amount="", fees="0", taxes="0",
         currency="EUR", broker="demo", external_id="", gain="", wkn="", asset_type="", notes="") -> str:
    return f"{when},{kind},{isin},{symbol},{wkn},{name},{shares},{price},{amount},{fees},{taxes},{currency},{broker},{external_id},{gain},{asset_type},{notes}\n"


class ImportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "trading.db"
        with contextlib.redirect_stdout(io.StringIO()):
            reset.create_database(self.db_path)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.csv_path = Path(self.directory.name) / "import.csv"

    # helpers --------------------------------------------------------------------------------------------------------------------------
    def write_csv(self, *rows: str, head: str = HEAD, bom: bool = False) -> Path:
        self.csv_path.write_text(head + "".join(rows), encoding="utf-8-sig" if bom else "utf-8")
        return self.csv_path

    def plan(self, *rows: str, **option_kwargs):
        self.write_csv(*rows)
        return build_import_plan(self.conn, self.csv_path, adapter="canonical", options=ImportOptions(**option_kwargs))

    def run_import(self, *rows: str, include_historical=False, **option_kwargs) -> dict:
        plan = self.plan(*rows, **option_kwargs)
        return apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token, include_historical=include_historical)

    def counts(self) -> dict:
        return {table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}

    def dump_hash(self) -> str:
        digest = hashlib.sha256()
        for statement in self.conn.iterdump():
            digest.update(statement.encode("utf-8"))
        return digest.hexdigest()

    def assignment(self, symbol: str):
        return self.conn.execute(
            "SELECT sa.* FROM strategy_assignment sa JOIN security s ON s.id = sa.security_id WHERE s.symbol = ?", (symbol,)).fetchall()

    def lifecycle(self, symbol: str):
        security_id = self.conn.execute("SELECT id FROM security WHERE symbol = ?", (symbol,)).fetchone()[0]
        shares = self.conn.execute("SELECT shares FROM positions WHERE security_id = ?", (security_id,)).fetchone()[0]
        return derive_open_lifecycle(self.conn, security_id, current_quantity=float(shares))

    def codes(self, result: dict) -> list[str]:
        return [item["code"] for item in result["open_items"]]


SWING_OPENING = (line("2026-03-02T10:00:00Z", "BUY", shares="40", price="10", amount="400", fees="2"),)


# --------------------------------------------------------------------------------------------------------------------------- A, B
class CanonicalParsingTests(ImportCase):
    def test_a_valid_canonical_rows_are_normalized(self) -> None:
        plan = self.plan(
            line("2026-03-02", "BUY", shares="3", price="10.5", fees="1.25"),
            line("2026-03-03T09:15:00+01:00", "SELL", shares="1", price="11"),
            line("2026-03-04", "DIVIDEND", shares="", price="", amount="4.2", taxes="1.1"),
            line("2026-03-05", "TRANSFERIN", shares="5", price="", amount="50"),
            line("2026-03-06", "TRANSFEROUT", shares="1", price="", amount=""),
            line("2026-03-07", "COST", shares="", price="", amount="2.5"),
            create_securities=True,
        )
        records = plan.records
        self.assertEqual([r.classification for r in records], [CLASS_NEW] * 6)
        self.assertEqual([r.transaction_type for r in records], ["BUY", "SELL", "DIVIDEND", "TRANSFERIN", "TRANSFEROUT", "COST"])
        buy, sell, dividend, transfer_in, transfer_out, cost = records
        self.assertEqual(buy.transaction_date, "2026-03-02T00:00:00.000Z")
        self.assertEqual(sell.transaction_date, "2026-03-03T08:15:00.000Z")  # normalized to UTC
        self.assertAlmostEqual(buy.amount, 31.5)  # derived: shares x price
        self.assertEqual((buy.fees, buy.taxes, dividend.taxes), (1.25, 0.0, 1.1))
        self.assertEqual((transfer_in.amount, transfer_in.price), (50.0, 10.0))  # price derived from the carried-in cost basis
        self.assertEqual((transfer_out.amount, transfer_out.price), (0.0, 0.0))
        self.assertEqual((dividend.shares, cost.amount), (0.0, 2.5))
        self.assertTrue(all(r.external_id is None and r.new_security["symbol"] == "EXA" for r in records))

    def test_a_semicolon_delimiter_bom_and_case_insensitive_headers(self) -> None:
        text = (HEAD.replace(",", ";").upper() + line("2026-03-02", "buy", shares="2", price="5").replace(",", ";"))
        self.csv_path.write_text(text, encoding="utf-8-sig")
        record = build_import_plan(self.conn, self.csv_path, adapter="canonical", options=ImportOptions(create_securities=True)).records[0]
        self.assertEqual((record.classification, record.transaction_type, record.amount), (CLASS_NEW, "BUY", 10.0))

    def test_b_malformed_files_are_rejected(self) -> None:
        for head, message in (
            (HEAD.strip() + ",fee\n", "unknown columns: fee"),
            ("transaction_date,isin,currency\n", "missing required columns: transaction_type"),
            ("transaction_type,isin,currency\n", "missing required columns: transaction_date"),
            ("transaction_date,transaction_type,currency\n", "isin/symbol"),
        ):
            self.csv_path.write_text(head, encoding="utf-8")
            with self.subTest(head=head), self.assertRaisesRegex(TransactionImportError, message):
                build_import_plan(self.conn, self.csv_path, adapter="canonical")
        self.csv_path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(TransactionImportError, "no header"):
            build_import_plan(self.conn, self.csv_path, adapter="canonical")
        with self.assertRaises(FileNotFoundError):
            build_import_plan(self.conn, Path(self.directory.name) / "missing.csv", adapter="canonical")

    def test_b_malformed_rows_are_invalid_with_a_reason(self) -> None:
        cases = {
            "1,5 decimal comma": (line("2026-03-02", "BUY", price='"1,5"'), "invalid price"),
            "thousands separator": (line("2026-03-02", "BUY", price='"1,000.50"'), "invalid price"),
            "negative shares": (line("2026-03-02", "BUY", shares="-1"), "invalid shares"),
            "german date": (line("02.03.2026", "BUY"), "invalid transaction_date"),
            "missing date": (line("", "BUY"), "missing datetime"),
            "unsupported TAX": (line("2026-03-02", "TAX", amount="1"), "unsupported transaction type"),
            "unsupported SPLIT": (line("2026-03-02", "SPLIT", shares="2"), "unsupported transaction type"),
            "unsupported INTEREST": (line("2026-03-02", "INTEREST", amount="1"), "unsupported transaction type"),
            "FEE points to COST": (line("2026-03-02", "FEE", amount="1"), "use COST"),
            "bad isin": (line("2026-03-02", "BUY", isin="12"), "invalid isin"),
            "no identifier": (line("2026-03-02", "BUY", isin="", symbol=""), "missing isin and symbol"),
            "bad currency": (line("2026-03-02", "BUY", currency="EURO"), "currency"),
            "BUY without price": (line("2026-03-02", "BUY", price=""), "requires price"),
            "BUY without shares": (line("2026-03-02", "BUY", shares=""), "requires shares"),
            "TRANSFERIN without amount": (line("2026-03-02", "TRANSFERIN", price="", amount=""), "requires amount"),
            "DIVIDEND without amount": (line("2026-03-02", "DIVIDEND", shares="", price="", amount=""), "requires amount"),
        }
        for label, (row, message) in cases.items():
            with self.subTest(label):
                record = self.plan(row, create_securities=True).records[0]
                self.assertEqual(record.classification, CLASS_INVALID)
                self.assertIn(message, record.classification_reason)
        self.assertEqual(self.counts()["transactions"], 0)

    def test_security_creation_needs_the_flag_and_a_name(self) -> None:
        self.assertEqual(self.plan(line("2026-03-02", "BUY")).records[0].classification, CLASS_UNKNOWN_SECURITY)
        no_name = self.plan(line("2026-03-02", "BUY", name=""), create_securities=True).records[0]
        self.assertEqual(no_name.classification, CLASS_UNKNOWN_SECURITY)
        self.assertIn("a name is required", no_name.classification_reason)

    def test_cli_option_parsing(self) -> None:
        self.assertEqual(parse_key_value_options(["exa=Swing", "EXB=long-term"], kind="strategy"), (("EXA", "swing"), ("EXB", "long_term")))
        self.assertEqual(parse_key_value_options(["EXA=2026-03-02"], kind="date"), (("EXA", "2026-03-02"),))
        for bad, kind in ((["EXA=tactical"], "strategy"), (["EXA"], "strategy"), (["EXA=02.03.2026"], "date"), (["EXA=swing", "EXA=long_term"], "strategy")):
            with self.subTest(bad), self.assertRaises(TransactionImportError):
                parse_key_value_options(bad, kind=kind)


# --------------------------------------------------------------------------------------------------------------------------- C, D, K
class IdempotencyHistoricalConflictTests(ImportCase):
    ROWS = (
        line("2026-03-02T10:00:00Z", "BUY", shares="40", price="10", amount="400"),
        line("2026-03-09T10:00:00Z", "BUY", shares="10", price="11", amount="110"),
        line("2026-03-10T10:00:00Z", "SELL", shares="5", price="12", amount="60"),
    )

    def test_c_same_file_twice_adds_nothing(self) -> None:
        options = dict(create_securities=True, strategies=(("EXA", "swing"),))
        first = self.run_import(*self.ROWS, **options)
        self.assertEqual((first["summary"]["inserted"], first["summary"]["new_securities"], first["summary"]["campaigns_created"]), (3, 1, 1))
        state = (self.counts(), self.dump_hash())
        second = self.run_import(*self.ROWS, **options)
        self.assertEqual(second["summary"]["inserted"], 0)
        self.assertEqual(second["summary"]["duplicates"], 3)
        self.assertEqual((second["summary"]["new_securities"], second["summary"]["new_positions"], second["summary"]["strategy_assignments_created"],
                          second["summary"]["campaigns_created"], second["summary"]["campaigns_reconciled"]), (0, 0, 0, 0, 0))
        self.assertFalse(second["would_write"])
        self.assertEqual((self.counts(), self.dump_hash()), state)  # not even an imports row

    def test_c_equal_trades_are_duplicates_across_formats(self) -> None:
        self.run_import(*self.ROWS, create_securities=True)
        parqet_text = PARQET_HEAD + "2026-03-02T10:00:00.000Z;;;BUY;Example Corp A;US0000000001;;40;10,00;400,00;0,00;0,00;;EUR;demo;stock;\n"
        parqet_path = Path(self.directory.name) / "parqet.csv"
        parqet_path.write_text(parqet_text, encoding="utf-8")
        plan = parqet_import.build_import_plan(self.conn, parqet_path)
        self.assertEqual(plan.records[0].classification, CLASS_DUPLICATE)

    def test_c_the_same_trade_with_a_changed_value_is_a_conflict_not_a_duplicate(self) -> None:
        self.run_import(*self.ROWS, create_securities=True)
        changed = self.plan(line("2026-03-02T10:00:00Z", "BUY", shares="40", price="10", amount="401")).records[0]
        self.assertEqual(changed.classification, CLASS_CONFLICT)
        self.assertIn("same security/type/datetime", changed.classification_reason)

    def test_k_conflicts_are_reported_and_block_only_themselves(self) -> None:
        self.run_import(*self.ROWS, create_securities=True)
        result = self.run_import(
            line("2026-03-02T10:00:00Z", "BUY", shares="40", price="10", amount="999"),  # conflict
            line("2026-03-11T10:00:00Z", "BUY", shares="1", price="10", amount="10"),  # fine
        )
        self.assertEqual((result["summary"]["conflicts"], result["summary"]["inserted"]), (1, 1))
        self.assertEqual(result["blocked_row_numbers"], [2])
        self.assertIn("ROWS_BLOCKED", self.codes(result))
        self.assertEqual(result["summary"]["validation_status"], "INCOMPLETE")

    def test_k_repeats_inside_one_file_and_external_id_clashes(self) -> None:
        twice = self.plan(*self.ROWS, self.ROWS[0], create_securities=True)
        self.assertEqual([r.classification for r in twice.records], [CLASS_NEW, CLASS_NEW, CLASS_NEW, CLASS_CONFLICT])
        self.assertIn("more than once in this file", twice.records[3].classification_reason)
        shared = self.plan(
            line("2026-03-02", "BUY", external_id="ID-1", shares="1", price="10"),
            line("2026-03-03", "BUY", external_id="ID-1", shares="2", price="10"), create_securities=True)
        self.assertEqual([r.classification for r in shared.records], [CLASS_NEW, CLASS_CONFLICT])
        self.run_import(line("2026-03-02", "BUY", external_id="ID-1", shares="1", price="10"), create_securities=True)
        clash = self.plan(line("2026-03-05", "BUY", external_id="ID-1", shares="9", price="10")).records[0]
        self.assertEqual(clash.classification, CLASS_CONFLICT)
        self.assertIn("external_id matches", clash.classification_reason)

    def test_d_historical_rows_are_skipped_until_included(self) -> None:
        self.run_import(*self.ROWS, create_securities=True)
        older = line("2026-01-05T10:00:00Z", "BUY", shares="3", price="9", amount="27")
        skipped = self.run_import(older)
        self.assertEqual((skipped["summary"]["historical_skipped"], skipped["summary"]["historical_inserted"], skipped["summary"]["inserted"]), (1, 0, 0))
        self.assertIn("HISTORICAL_ROWS_NOT_IMPORTED", self.codes(skipped))
        self.assertEqual(self.plan(older).records[0].classification, CLASS_NEW_HISTORICAL)
        included = self.run_import(older, include_historical=True)
        self.assertEqual((included["summary"]["historical_skipped"], included["summary"]["historical_inserted"], included["summary"]["inserted"]), (0, 1, 1))
        self.assertEqual(self.conn.execute("SELECT shares FROM positions").fetchone()[0], 40 + 10 - 5 + 3)


# --------------------------------------------------------------------------------------------------------------------------- E, F, G, H
class StrategyAndCampaignTests(ImportCase):
    def test_e_new_position_without_strategy_is_reported_not_guessed(self) -> None:
        result = self.run_import(*SWING_OPENING, create_securities=True)
        self.assertEqual(result["summary"]["new_positions"], 1)
        self.assertEqual(result["summary"]["strategy_required"], 1)
        self.assertIn("STRATEGY_ASSIGNMENT_REQUIRED", self.codes(result))
        self.assertEqual(self.assignment("EXA"), [])
        self.assertEqual(self.counts()["swing_campaign"], 0)
        self.assertEqual(self.counts()["transactions"], 1)  # the transactions themselves are valid and stay imported
        self.assertEqual(result["summary"]["validation_status"], "INCOMPLETE")

    def test_f_new_swing_position_is_fully_initialized(self) -> None:
        result = self.run_import(
            *SWING_OPENING,
            line("2026-03-09T10:00:00Z", "BUY", shares="10", price="11", amount="110"),
            line("2026-03-10T10:00:00Z", "SELL", shares="5", price="12", amount="60"),
            create_securities=True, strategies=(("EXA", "swing"),),
        )
        summary = result["summary"]
        self.assertEqual((summary["strategy_assignments_created"], summary["campaigns_created"], summary["campaigns_reconciled"],
                          summary["campaign_initialization_required"], summary["strategy_required"]), (1, 1, 1, 0, 0))
        assignment = self.assignment("EXA")[0]
        self.assertEqual((assignment["strategy_type"], assignment["effective_from"], assignment["effective_to"]), ("swing", "2026-03-02", None))
        campaign = self.conn.execute("SELECT * FROM swing_campaign").fetchone()
        self.assertEqual((campaign["opened_at"], campaign["original_quantity"], campaign["status"], campaign["strategy_assignment_id"]), ("2026-03-02", 40.0, "open", assignment["id"]))
        self.assertEqual((campaign["reference_currency"], round(campaign["reference_avg_cost"], 6)), ("EUR", 10.05))  # (400 + 2 fees) / 40
        events = self.conn.execute("SELECT * FROM swing_campaign_event ORDER BY id").fetchall()
        self.assertEqual([e["event_type"] for e in events], ["baseline", "add", "manual_reduction"])
        opener = self.conn.execute("SELECT id FROM transactions ORDER BY transaction_date, id LIMIT 1").fetchone()[0]
        self.assertEqual(events[0]["transaction_id"], opener)  # the baseline is linked to its opening BUY and never counted twice
        self.assertEqual([e["quantity"] for e in events], [40.0, 10.0, 5.0])
        lifecycle = self.lifecycle("EXA")
        self.assertEqual((lifecycle.event_derived_quantity, lifecycle.reconciliation_delta), (45.0, 0.0))
        self.assertEqual(result["campaigns_created"][0]["start_basis"], "opening BUY")
        self.assertNotIn("CAMPAIGN_INITIALIZATION_REQUIRED", self.codes(result))
        # a second run neither duplicates the assignment, the campaign nor an event
        before = self.counts()
        again = self.run_import(
            *SWING_OPENING, line("2026-03-09T10:00:00Z", "BUY", shares="10", price="11", amount="110"),
            line("2026-03-10T10:00:00Z", "SELL", shares="5", price="12", amount="60"), strategies=(("EXA", "swing"),))
        self.assertEqual(self.counts(), before)
        self.assertEqual((again["summary"]["strategy_assignments_created"], again["summary"]["campaigns_created"], again["summary"]["campaigns_reconciled"]), (0, 0, 0))

    def test_f_strategy_can_follow_an_earlier_import_of_the_same_file(self) -> None:
        self.run_import(*SWING_OPENING, create_securities=True)
        result = self.run_import(*SWING_OPENING, strategies=(("EXA", "swing"),))
        self.assertEqual((result["summary"]["inserted"], result["summary"]["strategy_assignments_created"], result["summary"]["campaigns_created"]), (0, 1, 1))
        self.assertEqual(self.lifecycle("EXA").reconciliation_delta, 0.0)

    def test_f_an_opening_trade_that_is_not_in_the_file_needs_an_explicit_start(self) -> None:
        self.run_import(*SWING_OPENING, create_securities=True)
        later = line("2026-03-09T10:00:00Z", "BUY", shares="10", price="11", amount="110")
        needed = self.run_import(later, strategies=(("EXA", "swing"),))
        self.assertIn("CAMPAIGN_INITIALIZATION_REQUIRED", self.codes(needed))
        item = next(i for i in needed["open_items"] if i["code"] == "CAMPAIGN_INITIALIZATION_REQUIRED")
        self.assertEqual(item["detail"], "OPENING_TRANSACTION_NOT_IN_THIS_FILE")
        self.assertEqual((len(self.assignment("EXA")), self.counts()["swing_campaign"]), (1, 0))  # the strategy is set, the campaign is not guessed
        explicit = self.run_import(later, campaign_opened_at=(("EXA", "2026-03-05"),))
        self.assertEqual(explicit["summary"]["campaigns_created"], 1)
        campaign = self.conn.execute("SELECT * FROM swing_campaign").fetchone()
        self.assertEqual((campaign["opened_at"], campaign["original_quantity"]), ("2026-03-05", 40.0))  # quantity held before that day
        self.assertIsNone(self.conn.execute("SELECT transaction_id FROM swing_campaign_event WHERE event_type='baseline'").fetchone()[0])
        self.assertEqual(self.lifecycle("EXA").reconciliation_delta, 0.0)  # the 03-09 BUY was reconciled as an add event

    def test_g_long_term_creates_an_assignment_and_no_campaign(self) -> None:
        result = self.run_import(
            line("2026-03-02", "BUY", **EXB, shares="100", price="5", amount="500"),
            create_securities=True, strategies=(("EXB", "long_term"),),
        )
        self.assertEqual((result["summary"]["strategy_assignments_created"], result["summary"]["campaigns_created"]), (1, 0))
        self.assertEqual(self.assignment("EXB")[0]["strategy_type"], "long_term")
        self.assertEqual((self.counts()["swing_campaign"], self.counts()["swing_campaign_event"]), (0, 0))
        self.assertNotIn("CAMPAIGN_INITIALIZATION_REQUIRED", self.codes(result))
        self.assertIn("MARKET_DATA_MISSING", self.codes(result))  # the only thing left is market data, not strategy or campaign

    def test_h_existing_swing_position_is_reconciled_not_recreated(self) -> None:
        self.run_import(*SWING_OPENING, create_securities=True, strategies=(("EXA", "swing"),))
        campaign_id = self.conn.execute("SELECT id FROM swing_campaign").fetchone()[0]
        result = self.run_import(
            line("2026-04-01T10:00:00Z", "BUY", shares="8", price="12", amount="96"),
            line("2026-04-02T10:00:00Z", "SELL", shares="3", price="13", amount="39"),
            strategies=(("EXA", "swing"),),
        )
        self.assertEqual((result["summary"]["inserted"], result["summary"]["campaigns_created"], result["summary"]["campaigns_reconciled"]), (2, 0, 1))
        self.assertEqual(self.conn.execute("SELECT id FROM swing_campaign").fetchall()[0][0], campaign_id)
        self.assertEqual([e["event_type"] for e in self.conn.execute("SELECT event_type FROM swing_campaign_event ORDER BY id")], ["baseline", "add", "manual_reduction"])
        self.assertEqual(self.lifecycle("EXA").reconciliation_delta, 0.0)

    def test_a_conflicting_strategy_never_overwrites_an_assignment(self) -> None:
        self.run_import(*SWING_OPENING, create_securities=True, strategies=(("EXA", "long_term"),))
        result = self.run_import(*SWING_OPENING, strategies=(("EXA", "swing"),))
        self.assertIn("STRATEGY_CONFLICT", self.codes(result))
        self.assertEqual([a["strategy_type"] for a in self.assignment("EXA")], ["long_term"])

    def test_unknown_option_keys_abort_without_writing(self) -> None:
        before = (self.counts(), self.dump_hash())
        with self.assertRaisesRegex(TransactionImportError, "NOPE"):
            self.run_import(*SWING_OPENING, create_securities=True, strategies=(("NOPE", "swing"),))
        self.assertEqual((self.counts(), self.dump_hash()), before)

    def test_effective_from_is_the_position_start_and_never_overlaps_history(self) -> None:
        # the position was flat in between: the held position starts at the second purchase
        self.run_import(
            line("2025-01-10", "BUY", **EXB, shares="10", price="5", amount="50"), line("2025-02-10", "SELL", **EXB, shares="10", price="6", amount="60"),
            line("2026-03-02", "BUY", **EXB, shares="5", price="7", amount="35"), create_securities=True, strategies=(("EXB", "long_term"),))
        self.assertEqual(self.assignment("EXB")[0]["effective_from"], "2026-03-02")
        # an old closed assignment that overlaps the position start is not worked around
        self.run_import(line("2026-05-01", "BUY", **EXD, shares="1", price="1", amount="1"), create_securities=True)
        security_id = self.conn.execute("SELECT id FROM security WHERE symbol='EXD'").fetchone()[0]
        self.conn.execute("INSERT INTO strategy_assignment(security_id, strategy_type, effective_from, effective_to) VALUES (?, 'swing', '2026-04-01', '2026-06-30')", (security_id,))
        # active assignment ended before today -> none active; the new one would overlap the old one
        result = self.run_import(line("2026-05-01", "BUY", **EXD, shares="1", price="1", amount="1"), strategies=(("EXD", "long_term"),))
        self.assertIn("STRATEGY_EFFECTIVE_FROM_UNCLEAR", self.codes(result))


# --------------------------------------------------------------------------------------------------------------------------- I
class TransferInTests(ImportCase):
    TRANSFER = line("2026-02-02", "TRANSFERIN", **EXD, shares="20", price="", amount="2000")

    def test_i_a_transfer_in_is_never_a_campaign_start_by_itself(self) -> None:
        result = self.run_import(self.TRANSFER, create_securities=True, strategies=(("EXD", "swing"),))
        self.assertEqual((result["summary"]["strategy_assignments_created"], result["summary"]["campaigns_created"]), (1, 0))
        item = next(i for i in result["open_items"] if i["code"] == "CAMPAIGN_INITIALIZATION_REQUIRED")
        self.assertEqual(item["detail"], "TRANSFERIN_IS_NOT_A_CAMPAIGN_START")
        self.assertEqual(self.assignment("EXD")[0]["effective_from"], "2026-02-02")  # the position exists since the transfer
        self.assertEqual(self.counts()["swing_campaign"], 0)

    def test_i_an_explicit_later_start_defines_the_campaign(self) -> None:
        self.run_import(self.TRANSFER, create_securities=True, strategies=(("EXD", "swing"),))
        result = self.run_import(self.TRANSFER, campaign_opened_at=(("EXD", "2026-03-15"),))
        self.assertEqual(result["summary"]["campaigns_created"], 1)
        campaign = self.conn.execute("SELECT * FROM swing_campaign").fetchone()
        self.assertEqual((campaign["opened_at"], campaign["original_quantity"], campaign["reference_avg_cost"], campaign["reference_currency"]), ("2026-03-15", 20.0, 100.0, "EUR"))
        self.assertEqual(self.lifecycle("EXD").reconciliation_delta, 0.0)

    def test_i_an_explicit_start_on_the_transfer_day_links_the_baseline_to_it(self) -> None:
        result = self.run_import(self.TRANSFER, create_securities=True, strategies=(("EXD", "swing"),), campaign_opened_at=(("EXD", "2026-02-02"),))
        self.assertEqual(result["summary"]["campaigns_created"], 1)
        baseline = self.conn.execute("SELECT transaction_id FROM swing_campaign_event WHERE event_type='baseline'").fetchone()[0]
        self.assertEqual(baseline, self.conn.execute("SELECT id FROM transactions").fetchone()[0])
        self.assertEqual(self.lifecycle("EXD").reconciliation_delta, 0.0)

    def test_i_explicit_starts_that_cannot_be_right_are_refused(self) -> None:
        for when, reason in (("2026-01-01", "CAMPAIGN_START_BEFORE_POSITION_START"), ((date.today() + timedelta(days=3)).isoformat(), "CAMPAIGN_START_IN_THE_FUTURE")):
            with self.subTest(when):
                result = self.run_import(self.TRANSFER, create_securities=True, strategies=(("EXD", "swing"),), campaign_opened_at=(("EXD", when),))
                self.assertEqual(next(i for i in result["open_items"] if i["code"] == "CAMPAIGN_INITIALIZATION_REQUIRED")["detail"], reason)
                self.assertEqual(self.counts()["swing_campaign"], 0)

    def test_i_later_transfers_need_manual_review_and_do_not_leave_a_half_campaign(self) -> None:
        result = self.run_import(
            line("2026-03-02", "BUY", shares="40", price="10", amount="400"), line("2026-03-09", "TRANSFEROUT", shares="5", price="", amount=""),
            create_securities=True, strategies=(("EXA", "swing"),))
        item = next(i for i in result["open_items"] if i["code"] == "CAMPAIGN_INITIALIZATION_REQUIRED")
        self.assertIn("TRANSFER_REQUIRES_MANUAL_REVIEW", item["detail"])
        self.assertEqual((self.counts()["swing_campaign"], self.counts()["swing_campaign_event"]), (0, 0))
        self.assertEqual(self.counts()["transactions"], 2)


# --------------------------------------------------------------------------------------------------------------------------- J
class DryRunTests(ImportCase):
    OPTIONS = dict(create_securities=True, strategies=(("EXA", "swing"),))

    def test_j_preview_shows_the_full_end_state_and_leaves_the_database_untouched(self) -> None:
        rows = (*SWING_OPENING, line("2026-03-09T10:00:00Z", "BUY", shares="10", price="11", amount="110"))
        self.write_csv(*rows)
        readonly = sqlite3.connect(f"file:///{self.db_path.as_posix()}?mode=ro", uri=True)
        readonly.row_factory = sqlite3.Row
        self.addCleanup(readonly.close)
        before = (self.counts(), self.dump_hash(), sorted(p.name for p in Path(self.directory.name).iterdir()))
        preview = preview_import(readonly, self.csv_path, adapter="canonical", options=ImportOptions(**self.OPTIONS))
        self.assertEqual((self.counts(), self.dump_hash(), sorted(p.name for p in Path(self.directory.name).iterdir())), before)  # no row, no file
        planned = preview["planned"]
        self.assertTrue(preview["dry_run"] and planned["dry_run"] and not planned["written"] and planned["would_write"])
        self.assertEqual((planned["summary"]["inserted"], planned["summary"]["new_securities"], planned["summary"]["strategy_assignments_created"],
                          planned["summary"]["campaigns_created"], planned["summary"]["campaigns_reconciled"]), (2, 1, 1, 1, 1))
        self.assertEqual(preview["new"], 2)
        self.assertEqual(planned["created_securities"][0]["symbol"], "EXA")
        self.assertEqual(planned["campaigns_created"][0]["opened_at"], "2026-03-02")
        self.assertIn("MARKET_DATA_MISSING", [i["code"] for i in planned["open_items"]])
        # a write with the same arguments produces exactly the previewed end state
        written = self.run_import(*rows, **self.OPTIONS)
        for key in ("summary", "created_securities", "open_items"):
            self.assertEqual(written[key], planned[key], key)

    def test_j_preview_reports_missing_strategies_and_blockers(self) -> None:
        self.write_csv(*SWING_OPENING)
        readonly = sqlite3.connect(f"file:///{self.db_path.as_posix()}?mode=ro", uri=True)
        readonly.row_factory = sqlite3.Row
        self.addCleanup(readonly.close)
        planned = preview_import(readonly, self.csv_path, adapter="canonical", options=ImportOptions(create_securities=True))["planned"]
        self.assertEqual(planned["summary"]["strategy_required"], 1)
        self.assertIn("STRATEGY_ASSIGNMENT_REQUIRED", [i["code"] for i in planned["open_items"]])
        self.assertEqual(self.counts()["transactions"], 0)

    def test_j_cli_defaults_to_the_dry_run_and_requires_a_format(self) -> None:
        self.write_csv(*SWING_OPENING)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--format", "canonical", "--csv", str(self.csv_path), "--db-path", str(self.db_path), "--create-securities", "--strategy", "EXA=swing", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["planned"]["summary"]["campaigns_created"], 1)
        self.assertEqual(self.counts()["transactions"], 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--csv", str(self.csv_path), "--db-path", str(self.db_path)])

    def test_j_cli_write_makes_a_backup_and_reports_errors(self) -> None:
        self.write_csv(*SWING_OPENING)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--format", "canonical", "--csv", str(self.csv_path), "--db-path", str(self.db_path), "--create-securities",
                             "--strategy", "EXA=swing", "--write", "--json"])
        result = json.loads(out.getvalue())
        self.assertEqual((code, result["written"], result["summary"]["campaigns_created"]), (0, True, 1))
        self.assertTrue(Path(result["backup_path"]).is_file())
        self.assertEqual(self.counts()["swing_campaign"], 1)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["--format", "canonical", "--csv", str(self.csv_path), "--db-path", str(self.db_path), "--strategy", "NOPE=swing"])
        self.assertEqual(code, 2)
        self.assertIn("NOPE", err.getvalue())


# --------------------------------------------------------------------------------------------------------------------------- L
class ExampleCsvTests(ImportCase):
    EXAMPLE = ROOT / "examples" / "trading-import-example.csv"

    def test_l_the_example_csv_imports_completely(self) -> None:
        strategies = (("EXA", "swing"), ("EXB", "long_term"), ("EXC", "long_term"), ("EXD", "long_term"))
        plan = build_import_plan(self.conn, self.EXAMPLE, adapter="canonical", options=ImportOptions(create_securities=True, strategies=strategies))
        self.assertEqual(plan.counts["new"], 9)
        self.assertEqual({r.transaction_type for r in plan.records}, {"BUY", "SELL", "TRANSFERIN", "DIVIDEND", "COST"})
        self.assertEqual({r.currency for r in plan.records}, {"EUR", "USD"})
        result = apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        summary = result["summary"]
        self.assertEqual((summary["inserted"], summary["new_securities"], summary["new_positions"], summary["strategy_assignments_created"],
                          summary["campaigns_created"], summary["strategy_required"], summary["campaign_initialization_required"]), (9, 4, 4, 4, 1, 0, 0))
        shares = dict(self.conn.execute("SELECT s.symbol, p.shares FROM positions p JOIN security s ON s.id = p.security_id").fetchall())
        self.assertEqual(shares, {"EXA": 45.0, "EXB": 150.0, "EXC": 10.0, "EXD": 20.0})
        self.assertEqual(self.conn.execute("SELECT p.currency FROM positions p JOIN security s ON s.id = p.security_id WHERE s.symbol='EXC'").fetchone()[0], "USD")
        self.assertEqual(self.lifecycle("EXA").reconciliation_delta, 0.0)
        # idempotent
        again = build_import_plan(self.conn, self.EXAMPLE, adapter="canonical", options=ImportOptions(strategies=strategies))
        self.assertEqual(again.counts["duplicate"], 9)

    def test_l_the_example_contains_no_real_data(self) -> None:
        text = self.EXAMPLE.read_text(encoding="utf-8")
        self.assertTrue(all(name in text for name in ("Example Corp A", "Example Corp B", "Example Corp C", "Example Corp D")))
        self.assertNotRegex(text, r"(?i)parqet|jan|[a-z0-9._%+-]+@[a-z0-9.-]+")
        isins = {row.split(",")[2] for row in text.splitlines()[1:]}
        self.assertTrue(all(isin.startswith("US00000000") for isin in isins))


# --------------------------------------------------------------------------------------------------------------------------- M
class ParqetCompatibilityTests(ImportCase):
    def parqet_file(self, *rows: str) -> Path:
        path = Path(self.directory.name) / "parqet.csv"
        path.write_text(PARQET_HEAD + "".join(rows), encoding="utf-8")
        return path

    @staticmethod
    def parqet_row(when, kind, shares, price, amount, *, isin="US0000000001", name="Example Corp A", fee="0,00") -> str:
        return f"{when};;;{kind};{name};{isin};;{shares};{price};{amount};{fee};0,00;;EUR;demo;stock;\n"

    def test_m_parqet_still_imports_with_its_conventions(self) -> None:
        self.conn.execute("INSERT INTO security(symbol, isin, name) VALUES ('EXA', 'US0000000001', 'Example Corp A')")
        path = self.parqet_file(self.parqet_row("2026-03-02T10:00:00.000Z", "Kauf", "40", "10,00", "400,00", fee="2,00"),
                                self.parqet_row("2026-03-09T10:00:00.000Z", "BUY", "10", "11,00", "110,00"))
        plan = parqet_import.build_import_plan(self.conn, path)
        self.assertEqual((plan.adapter_name, [r.classification for r in plan.records]), ("parqet", [CLASS_NEW, CLASS_NEW]))
        self.assertTrue(plan.records[0].external_id.startswith("parqet:"))
        result = apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        self.assertEqual(result["summary"]["inserted"], 2)
        row = self.conn.execute("SELECT * FROM imports").fetchone()
        self.assertEqual((row["import_type"], row["source"]), ("PARQET_CSV_INCREMENTAL", "Parqet"))
        self.assertEqual(json.loads(self.conn.execute("SELECT notes FROM transactions").fetchone()[0])["import_source"], "parqet_incremental")
        # unknown securities are still refused (nothing is created implicitly)
        unknown = parqet_import.build_import_plan(self.conn, self.parqet_file(self.parqet_row("2026-03-10T10:00:00.000Z", "BUY", "1", "1,00", "1,00", isin="US0000000009", name="Nobody")))
        self.assertEqual(unknown.records[0].classification, CLASS_UNKNOWN_SECURITY)

    def test_m_parqet_campaign_reconciliation_keeps_its_event_source(self) -> None:
        self.conn.execute("INSERT INTO security(symbol, isin, name) VALUES ('EXA', 'US0000000001', 'Example Corp A')")
        path = self.parqet_file(self.parqet_row("2026-03-02T10:00:00.000Z", "BUY", "40", "10,00", "400,00"))
        plan = parqet_import.build_import_plan(self.conn, path, options=ImportOptions(strategies=(("EXA", "swing"),)))
        apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        later = self.parqet_file(self.parqet_row("2026-03-02T10:00:00.000Z", "BUY", "40", "10,00", "400,00"),
                                 self.parqet_row("2026-03-09T10:00:00.000Z", "SELL", "5", "12,00", "60,00"))
        plan = parqet_import.build_import_plan(self.conn, later)
        apply_import_plan(self.conn, plan, expected_plan_token=plan.plan_token)
        sources = [r[0] for r in self.conn.execute("SELECT source FROM swing_campaign_event WHERE event_type != 'baseline'")]
        self.assertEqual(sources, ["parqet_reconciliation"])

    def test_m_the_parqet_entry_point_defaults_to_the_parqet_format(self) -> None:
        self.conn.execute("INSERT INTO security(symbol, isin, name) VALUES ('EXA', 'US0000000001', 'Example Corp A')")
        path = self.parqet_file(self.parqet_row("2026-03-02T10:00:00.000Z", "BUY", "40", "10,00", "400,00"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = parqet_cli.main(["--csv", str(path), "--db-path", str(self.db_path), "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual((code, payload["format"], payload["new"]), (0, "parqet", 1))
        self.assertEqual(self.counts()["transactions"], 0)  # preview by default
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(parqet_cli.main(["--csv", str(path), "--db-path", str(self.db_path), "--write"]), 0)
        self.assertEqual(self.counts()["transactions"], 1)


# --------------------------------------------------------------------------------------------------------------------------- N
class OrchestratorReadyTests(ImportCase):
    def add_market_data(self) -> None:
        source_id = self.conn.execute("SELECT id FROM data_sources WHERE name = 'Yahoo Finance'").fetchone()[0]
        start = date.today() - timedelta(days=259)
        for security_id, base in ((row[0], 10.0 + row[0]) for row in self.conn.execute("SELECT id FROM security ORDER BY id").fetchall()):
            rows = [(security_id, (start + timedelta(days=i)).isoformat(), base + i * 0.05, base + i * 0.05, source_id) for i in range(260)]
            self.conn.executemany("INSERT INTO market_data(security_id, trade_date, close, adjusted_close, source_id) VALUES (?, ?, ?, ?, ?)", rows)
            self.conn.execute("INSERT INTO market_snapshot(security_id, as_of_at, price, currency, source_id) VALUES (?, ?, ?, 'EUR', ?)",
                              (security_id, date.today().isoformat() + "T00:00:00+00:00", rows[-1][2], source_id))

    def test_n_a_complete_import_is_ready_for_the_orchestrator(self) -> None:
        from trading_orchestrator import run_trading_orchestrator

        result = self.run_import(
            *SWING_OPENING, line("2026-03-02", "BUY", **EXB, shares="100", price="5", amount="500"),
            create_securities=True, strategies=(("EXA", "swing"), ("EXB", "long_term")),
        )
        self.assertEqual(self.codes(result), ["MARKET_DATA_MISSING", "MARKET_DATA_MISSING"])  # the single remaining step: market data
        self.add_market_data()
        again = self.run_import(*SWING_OPENING, line("2026-03-02", "BUY", **EXB, shares="100", price="5", amount="500"),
                                strategies=(("EXA", "swing"), ("EXB", "long_term")))
        self.assertEqual((again["open_items"], again["summary"]["validation_status"]), ([], "COMPLETE"))
        outcome = run_trading_orchestrator(self.conn).primitive()
        text = json.dumps(outcome, default=str)
        self.assertNotIn("SWING_CAMPAIGN_UNAVAILABLE", text)
        self.assertEqual(outcome["blocking_summary"], {"global_blocking": 0, "security_blocking": 0})
        by_symbol = {item["symbol"]: item for item in outcome["existing_position_results"]}
        self.assertEqual(set(by_symbol), {"EXA", "EXB"})
        self.assertEqual((by_symbol["EXA"]["strategy"], by_symbol["EXA"]["campaign_status"], by_symbol["EXA"]["campaign_reconciliation_delta"]), ("swing", "open", 0.0))
        self.assertEqual((by_symbol["EXB"]["strategy"], by_symbol["EXB"]["campaign_id"]), ("long_term", None))
        self.assertTrue(all(item["decision"]["action"] for item in by_symbol.values()))

    def test_n_without_strategy_the_orchestrator_would_block_and_the_import_says_so(self) -> None:
        result = self.run_import(*SWING_OPENING, create_securities=True)
        self.assertIn("STRATEGY_ASSIGNMENT_REQUIRED", self.codes(result))

# --------------------------------------------------------------------------------------------------------------------------- result semantics
class ResultSemanticsTests(ImportCase):
    BASE = (
        line("2026-03-02T10:00:00Z", "BUY", shares="40", price="10", amount="400"),
        line("2026-03-09T10:00:00Z", "BUY", shares="10", price="11", amount="110"),
    )

    def test_failed_counts_only_rows_that_could_not_be_processed(self) -> None:
        self.run_import(*self.BASE, line("2026-03-02", "BUY", **EXB, shares="5", price="5", amount="25"),
                        create_securities=True, strategies=(("EXA", "swing"),))  # EXB: held, no strategy -> strategy_required
        result = self.run_import(
            self.BASE[0],  # duplicate
            line("2026-03-09T10:00:00Z", "BUY", shares="10", price="11", amount="999"),  # conflict
            line("2026-01-05T10:00:00Z", "BUY", shares="3", price="9", amount="27"),  # historical, skipped
            line("2026-03-12T10:00:00Z", "BUY", shares="1", price="10", amount="10"),  # inserted
            strategies=(("EXA", "swing"),),
        )
        summary = result["summary"]
        self.assertEqual((summary["duplicates"], summary["conflicts"], summary["historical_skipped"], summary["inserted"]), (1, 1, 1, 1))
        self.assertEqual(summary["failed"], 0)  # none of the above is a processing error
        self.assertEqual(summary["validation_status"], "INCOMPLETE")
        self.assertEqual(self.conn.execute("SELECT records_failed FROM imports ORDER BY id DESC LIMIT 1").fetchone()[0], 0)
        # strategy / campaign items are open items, never failures
        held = self.run_import(line("2026-03-02", "BUY", **EXB, shares="5", price="5", amount="25"))
        self.assertEqual((held["summary"]["failed"], held["summary"]["strategy_required"]), (0, 1))
        transfer = self.run_import(line("2026-02-02", "TRANSFERIN", **EXD, shares="20", price="", amount="2000"), create_securities=True,
                                   strategies=(("EXD", "swing"),), include_historical=True)
        self.assertEqual((transfer["summary"]["failed"], transfer["summary"]["campaign_initialization_required"]), (0, 1))
        # failures: an invalid row and a row whose security is unknown and not created
        broken = self.run_import(
            line("2026-03-13T10:00:00Z", "BUY", price="not-a-number"),
            line("2026-03-13T10:00:00Z", "BUY", isin="US0000000099", symbol="ZZZ", name="Nobody"),
            line("2026-03-14T10:00:00Z", "BUY", shares="1", price="10", amount="10"),
        )
        self.assertEqual((broken["summary"]["failed"], broken["summary"]["inserted"], broken["summary"]["conflicts"]), (2, 1, 0))
        self.assertEqual(self.conn.execute("SELECT records_failed FROM imports ORDER BY id DESC LIMIT 1").fetchone()[0], 2)

    def assert_incomplete_but_imported(self, result: dict, code: str, detail: str, *, transactions: int) -> dict:
        self.assertEqual(result["summary"]["validation_status"], "INCOMPLETE")  # never a false COMPLETE
        item = next((i for i in result["open_items"] if i["code"] == code), None)
        self.assertIsNotNone(item, f"{code} missing from {result['open_items']}")
        self.assertIn(detail, json.dumps(item["detail"]))
        self.assertTrue(item["symbol"] and item["detail"])
        self.assertEqual(self.counts()["transactions"], transactions)  # the transactions stay imported
        return item

    def test_transactions_stay_and_the_reason_is_precise_when_strategy_or_campaign_cannot_be_completed(self) -> None:
        # 1) no strategy given
        result = self.run_import(*self.BASE, create_securities=True)
        self.assert_incomplete_but_imported(result, "STRATEGY_ASSIGNMENT_REQUIRED", "--strategy", transactions=2)
        self.assertEqual(self.assignment("EXA"), [])
        # 2) swing, but the opening transaction is a transfer-in
        result = self.run_import(line("2026-03-15", "TRANSFERIN", **EXD, shares="20", price="", amount="2000"), create_securities=True, strategies=(("EXD", "swing"),))
        self.assert_incomplete_but_imported(result, "CAMPAIGN_INITIALIZATION_REQUIRED", "TRANSFERIN_IS_NOT_A_CAMPAIGN_START", transactions=3)
        self.assertEqual(len(self.assignment("EXD")), 1)  # the part that is clear is done
        # 3) an existing campaign meets a transfer-out: manual review, nothing invented
        self.run_import(*self.BASE, strategies=(("EXA", "swing"),))
        result = self.run_import(line("2026-03-20T10:00:00Z", "TRANSFEROUT", shares="5", price="", amount=""), strategies=(("EXA", "swing"),))
        item = self.assert_incomplete_but_imported(result, "CAMPAIGN_RECONCILIATION_REQUIRED", "TRANSFER_REQUIRES_MANUAL_REVIEW", transactions=4)
        self.assertEqual(item["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual([e["event_type"] for e in self.conn.execute("SELECT event_type FROM swing_campaign_event ORDER BY id")], ["baseline", "add"])

    def test_complete_only_when_nothing_is_open(self) -> None:
        result = self.run_import(*self.BASE, create_securities=True, strategies=(("EXA", "swing"),))
        self.assertEqual([i["code"] for i in result["open_items"]], ["MARKET_DATA_MISSING"])
        self.assertEqual(result["summary"]["validation_status"], "INCOMPLETE")
        OrchestratorReadyTests.add_market_data(self)
        again = self.run_import(*self.BASE, strategies=(("EXA", "swing"),))
        self.assertEqual((again["open_items"], again["summary"]["validation_status"]), ([], "COMPLETE"))


if __name__ == "__main__":
    unittest.main()
