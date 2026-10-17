# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The one-flow import (``import_workflow``): analyse, compare, show the difference, import, reconcile; master data, idempotence, rollback, derived positions.

Real database schema (Reset-TradingDb); synthetic data only.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import io
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))

import import_workflow as workflow  # noqa: E402
import transaction_import as ti  # noqa: E402
from test_transaction_import import PARQET_HEAD, ImportCase, cli  # noqa: E402

ISIN_A, ISIN_B, ISIN_C = "US1111111111", "DE2222222222", "FR3333333333"


def prow(when, kind, *, isin, name, wkn="", shares="10", price="10", amount="", fee="0", tax="0", currency="EUR", broker="ing", asset="Security") -> str:
    amount = amount or f"{float(shares) * float(price):.2f}".replace(".", ",")
    return f"{when};{when[:10]};12:00:00;{kind};{name};{isin};{wkn};{shares};{price};{amount};{fee};{tax};;{currency};{broker};{asset};\n".replace("10.0", "10")


A1 = prow("2026-03-02T10:00:00.000Z", "Buy", isin=ISIN_A, name="Alpha Corp", wkn="AAA111", shares="10", price="10", amount="100")
A2 = prow("2026-03-09T10:00:00.000Z", "Buy", isin=ISIN_A, name="Alpha Corp", wkn="AAA111", shares="5", price="12", amount="60")
A3 = prow("2026-03-16T10:00:00.000Z", "Sell", isin=ISIN_A, name="Alpha Corp", wkn="AAA111", shares="3", price="15", amount="45")
B1 = prow("2026-03-03T10:00:00.000Z", "Buy", isin=ISIN_B, name="Beta AG", wkn="BBB222", shares="4", price="50", amount="200")


class FlowCase(ImportCase):
    def write_parqet(self, *rows: str) -> Path:
        return self.write_csv(*rows, head=PARQET_HEAD)

    def check(self, *rows: str, **kwargs) -> dict:
        self.write_parqet(*rows)
        return workflow.check(self.db_path, self.csv_path, fmt="parqet", **kwargs)

    def run_flow(self, *rows: str, **kwargs) -> dict:
        self.write_parqet(*rows)
        return workflow.run(self.db_path, self.csv_path, fmt="parqet", **kwargs)

    def security(self, isin: str):
        return self.conn.execute("SELECT * FROM security WHERE isin = ?", (isin,)).fetchone()

    def position(self, isin: str):
        return self.conn.execute("SELECT p.* FROM positions p JOIN security s ON s.id = p.security_id WHERE s.isin = ?", (isin,)).fetchone()

    def add_security(self, isin, name, *, symbol=None, wkn=None) -> int:
        return self.conn.execute("INSERT INTO security(symbol, isin, wkn, name, asset_type, active) VALUES (?, ?, ?, ?, 'stock', 1)", (symbol, isin, wkn, name)).lastrowid


class FreshImportTests(FlowCase):
    def test_a_fresh_import_creates_everything_from_the_file_and_reconciles(self) -> None:
        before = self.dump_hash()
        checked = self.check(A1, A2, B1, create_securities=True)
        self.assertEqual(self.dump_hash(), before)  # the check writes nothing
        diff = checked["diff"]
        self.assertEqual(sorted(s["isin"] for s in diff["new_securities"]), sorted([ISIN_A, ISIN_B]))
        self.assertEqual(diff["new_transactions"], {"count": 3, "by_type": {"BUY": 3}})
        self.assertEqual({c["symbol"]: (c["kind"], c["shares_after"]) for c in diff["position_changes"]}, {"Alpha Corp": ("new", 15.0), "Beta AG": ("new", 4.0)})
        self.assertEqual(checked["closing"]["status"], "MATCH")  # the simulated end state matches the file
        result = self.run_flow(A1, A2, B1, create_securities=True)
        self.assertTrue(result["written"])
        self.assertEqual(self.counts()["security"], 2)
        self.assertEqual(self.counts()["transactions"], 3)
        alpha = self.security(ISIN_A)
        self.assertEqual((alpha["name"], alpha["wkn"], alpha["symbol"], alpha["asset_type"]), ("Alpha Corp", "AAA111", None, "stock"))  # nothing invented: no ticker
        self.assertTrue(all(c["asset_type_defaulted"] for c in result["created_securities"]))  # Parqet says "Security": the default is declared, not hidden
        position = self.position(ISIN_A)
        self.assertEqual((position["shares"], position["remaining_cost_basis"], position["invested_amount"]), (15.0, 160.0, 160.0))
        closing = result["closing"]
        self.assertEqual(closing["status"], "MATCH")
        self.assertEqual((closing["counts"]["securities"], closing["counts"]["transactions"], closing["counts"]["positions_held"]), (2, 3, 2))
        self.assertEqual(closing["integrity_check"], ["ok"])
        self.assertEqual(closing["foreign_key_check"], [])
        self.assertTrue(Path(result["backup_path"]).is_file())

    def test_the_same_file_twice_is_idempotent(self) -> None:
        self.run_flow(A1, A2, B1, create_securities=True)
        after_first = self.dump_hash()
        imports = self.counts()["imports"]
        again = self.run_flow(A1, A2, B1, create_securities=True)
        self.assertFalse(again["written"])
        self.assertEqual(self.dump_hash(), after_first)
        self.assertEqual(self.counts()["imports"], imports)
        self.assertEqual(again["diff"]["new_transactions"]["count"], 0)
        self.assertEqual(again["diff"]["existing_transactions"], 3)
        self.assertIsNone(again["backup_path"])
        self.assertEqual(again["closing"]["status"], "MATCH")

    def test_without_create_securities_a_new_security_is_unresolved_not_invented(self) -> None:
        checked = self.check(A1, B1)
        self.assertEqual(checked["diff"]["new_transactions"]["count"], 0)
        self.assertEqual(checked["diff"]["problem_row_count"], 2)
        self.assertEqual(self.counts()["security"], 0)
        self.assertEqual(checked["closing"]["status"], "DIFFERENCES")
        self.assertEqual(checked["closing"]["unresolved_count"], 2)

    def test_invalid_master_data_blocks_the_creation_instead_of_repairing_it(self) -> None:
        result = self.check(prow("2026-03-02T10:00:00.000Z", "Buy", isin="XX", name="Broken", shares="1", price="1", amount="1"), create_securities=True)
        self.assertEqual(result["diff"]["new_securities"], [])
        self.assertEqual(self.counts()["security"], 0)


class ExistingDataTests(FlowCase):
    def test_an_existing_security_is_reused_and_never_duplicated(self) -> None:
        security_id = self.add_security(ISIN_A, "Alpha Corp", symbol="ALP", wkn="AAA111")
        result = self.run_flow(A1, A2, create_securities=True)
        self.assertEqual(self.counts()["security"], 1)
        self.assertEqual({r[0] for r in self.conn.execute("SELECT DISTINCT security_id FROM transactions")}, {security_id})
        self.assertEqual(result["diff"]["new_securities"], [])
        self.assertEqual(self.security(ISIN_A)["symbol"], "ALP")  # existing master data is not touched

    def test_a_missing_isin_is_supplemented_when_the_wkn_identifies_the_security(self) -> None:
        security_id = self.add_security(None, "Alpha Corp", symbol="ALP", wkn="AAA111")
        checked = self.check(A1)
        self.assertEqual([(u["field"], u["to"]) for u in checked["diff"]["masterdata_updates"]], [("isin", ISIN_A)])
        self.assertIsNone(self.conn.execute("SELECT isin FROM security WHERE id = ?", (security_id,)).fetchone()[0])  # not before the write
        result = self.run_flow(A1)
        self.assertEqual(self.conn.execute("SELECT isin FROM security WHERE id = ?", (security_id,)).fetchone()[0], ISIN_A)
        self.assertEqual(len(result["masterdata_updated"]), 1)
        self.assertEqual(self.counts()["security"], 1)
        again = self.run_flow(A1)
        self.assertEqual(again["diff"]["masterdata_updates"], [])  # supplemented once
        self.assertFalse(again["written"])

    def test_a_different_database_value_is_a_conflict_and_is_not_overwritten(self) -> None:
        security_id = self.add_security("US9999999999", "Alpha Corp", symbol="ALP", wkn="AAA111")  # same WKN, another ISIN (an ADR / GDR row)
        result = self.run_flow(A1)
        self.assertEqual(self.conn.execute("SELECT isin FROM security WHERE id = ?", (security_id,)).fetchone()[0], "US9999999999")
        self.assertEqual([(c["field"], c["database"], c["file"]) for c in result["diff"]["masterdata_conflicts"]], [("isin", "US9999999999", ISIN_A)])
        self.assertIn(ti.MASTERDATA_CONFLICT, [i["code"] for i in result["open_items"]])
        self.assertEqual(self.counts()["transactions"], 1)  # the row itself is valid: it resolved through the WKN

    def test_a_wkn_that_differs_is_reported_not_changed(self) -> None:
        self.add_security(ISIN_A, "Alpha Corp", symbol="ALP", wkn="OLD000")
        result = self.run_flow(A1)
        self.assertEqual(self.security(ISIN_A)["wkn"], "OLD000")
        self.assertEqual([(c["field"], c["database"], c["file"]) for c in result["diff"]["masterdata_conflicts"]], [("wkn", "OLD000", "AAA111")])

    def test_a_row_found_only_by_name_with_another_isin_is_a_conflict_never_merged(self) -> None:
        self.add_security("US9999999999", "Alpha Corp", symbol="ALP")
        result = self.run_flow(prow("2026-03-02T10:00:00.000Z", "Buy", isin=ISIN_A, name="Alpha Corp", shares="1", price="1", amount="1"), create_securities=True)
        self.assertEqual(result["diff"]["new_transactions"]["count"], 0)
        self.assertEqual(self.counts()["transactions"], 0)
        self.assertEqual(result["diff"]["problem_rows"][0]["classification"], ti.CLASS_CONFLICT)

    def test_a_duplicate_transaction_is_not_imported_again_and_a_repeated_row_is_a_conflict(self) -> None:
        self.run_flow(A1, create_securities=True)
        result = self.run_flow(A1, A2, A2, create_securities=True)
        self.assertEqual(result["diff"]["new_transactions"]["count"], 1)
        self.assertEqual(result["diff"]["existing_transactions"], 1)
        self.assertEqual(result["diff"]["problem_row_count"], 1)  # the second identical A2 row of the file
        self.assertEqual(self.counts()["transactions"], 2)


class PositionTests(FlowCase):
    def test_new_buys_and_sells_recompute_the_position_through_the_existing_rebuild(self) -> None:
        self.run_flow(A1, create_securities=True)
        self.assertEqual(self.position(ISIN_A)["shares"], 10.0)
        result = self.run_flow(A1, A2, A3, create_securities=True)
        changes = {c["symbol"]: c for c in result["diff"]["position_changes"]}
        self.assertEqual((changes["Alpha Corp"]["shares_before"], changes["Alpha Corp"]["shares_after"], changes["Alpha Corp"]["kind"]), (10.0, 12.0, "changed"))
        position = self.position(ISIN_A)
        self.assertEqual(position["shares"], 12.0)
        self.assertAlmostEqual(position["avg_cost"], 160.0 / 15.0)
        self.assertAlmostEqual(position["remaining_cost_basis"], 160.0 - 3 * 160.0 / 15.0)
        self.assertEqual(position["transaction_count"], 3)
        self.assertEqual(result["closing"]["positions_vs_transactions"]["drift_count"], 0)
        self.assertEqual(result["closing"]["status"], "MATCH")

    def test_only_the_touched_securities_are_rebuilt(self) -> None:
        self.run_flow(A1, B1, create_securities=True)
        self.conn.execute("UPDATE positions SET updated_at = '2000-01-01 00:00:00' WHERE security_id = (SELECT id FROM security WHERE isin = ?)", (ISIN_B,))
        self.run_flow(A1, A2, B1, create_securities=True)
        self.assertEqual(self.position(ISIN_B)["updated_at"], "2000-01-01 00:00:00")

    def test_the_closing_reconciliation_finds_a_position_that_differs_from_its_transactions(self) -> None:
        # the import rebuilds the positions it touches; a position it does not touch stays as it is, and the reconciliation reports it
        self.run_flow(A1, B1, create_securities=True)
        self.conn.execute("UPDATE positions SET shares = 99 WHERE security_id = (SELECT id FROM security WHERE isin = ?)", (ISIN_B,))
        closing = self.check(A1, B1)["closing"]
        self.assertGreaterEqual(closing["positions_vs_transactions"]["drift_count"], 1)
        self.assertEqual(closing["status"], "DIFFERENCES")

    def test_a_file_that_sells_more_than_it_buys_is_reported_as_a_difference_with_the_reason(self) -> None:
        sell = prow("2026-03-05T10:00:00.000Z", "Sell", isin=ISIN_A, name="Alpha Corp", wkn="AAA111", shares="20", price="10", amount="200")
        result = self.run_flow(A1, sell, create_securities=True)
        self.assertEqual(self.position(ISIN_A)["shares"], 0.0)
        difference = result["closing"]["file_vs_database"]["share_differences"][0]
        self.assertEqual((difference["file_net_shares"], difference["database_shares"]), (-10.0, 0.0))
        self.assertIn("sells more", difference["note"])
        self.assertEqual(result["closing"]["status"], "DIFFERENCES")

    def test_nothing_but_the_import_rules_changes_watchlist_strategy_and_campaigns(self) -> None:
        self.add_security(ISIN_B, "Beta AG", symbol="BET")
        sid = self.security(ISIN_B)["id"]
        self.conn.execute("INSERT INTO watchlist(security_id, status) VALUES (?, 'watching')", (sid,))
        before = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("watchlist", "strategy_assignment", "swing_campaign", "swing_campaign_event")}
        watch_row = tuple(self.conn.execute("SELECT * FROM watchlist").fetchone())
        result = self.run_flow(A1, B1, create_securities=True)
        after = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        self.assertEqual(before, after)
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM watchlist").fetchone()), watch_row)
        self.assertEqual(sorted(i["code"] for i in result["open_items"] if i["code"] == ti.STRATEGY_ASSIGNMENT_REQUIRED), [ti.STRATEGY_ASSIGNMENT_REQUIRED] * 2)  # reported, never guessed


class RollbackTests(FlowCase):
    def test_an_error_leaves_no_half_finished_state(self) -> None:
        self.run_flow(A1, create_securities=True)
        before = self.dump_hash()
        real = ti._rebuild_position
        calls = []

        def failing(conn, security_id, *args, **kwargs):
            calls.append(security_id)
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk full")
            return real(conn, security_id, *args, **kwargs)

        with mock.patch.object(ti, "_rebuild_position", failing):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_flow(A1, A2, B1, create_securities=True)  # a new security, its transactions, a changed position - all of it must go
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.dump_hash(), before)
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_a_stale_check_is_refused(self) -> None:
        checked = self.check(A1, create_securities=True)
        self.write_parqet(A1, A2)
        with self.assertRaises(ti.TransactionImportError):
            workflow.run(self.db_path, self.csv_path, fmt="parqet", create_securities=True, expected_plan_token=checked["plan_token"])
        self.assertEqual(self.counts()["transactions"], 0)


def load_tools():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("trading_sqlite_import_test", root / "mcp-tools" / "trading_sqlite.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.Tools._load_runtime_trading_module = classmethod(lambda cls, name: importlib.import_module(name))  # the tests use the repo copies, not the deployed ones
    return module.Tools


class McpToolTests(FlowCase):
    def setUp(self) -> None:
        super().setUp()
        self.tools = load_tools()()
        self.tools.valves.database_path = str(self.db_path)

    def call(self, name: str, **arguments) -> dict:
        return asyncio.run(getattr(self.tools, name)(**arguments))

    def test_check_then_import_with_the_token(self) -> None:
        self.write_parqet(A1, B1)
        checked = self.call("check_transaction_import", file_path=str(self.csv_path), create_securities=True)
        self.assertTrue(checked["ok"])
        self.assertEqual((checked["mode"], checked["written"], checked["would_write"]), ("check", False, True))
        self.assertEqual(checked["diff"]["new_transactions"]["count"], 2)
        self.assertNotIn("rows", checked)  # the answer stays small: the row-by-row plan is not part of it
        self.assertEqual(self.counts()["transactions"], 0)
        imported = self.call("import_transactions", file_path=str(self.csv_path), plan_token=checked["plan_token"], create_securities=True)
        self.assertTrue(imported["ok"] and imported["written"])
        self.assertEqual(self.counts()["transactions"], 2)
        self.assertEqual(imported["closing"]["status"], "MATCH")
        self.assertTrue(Path(imported["backup_path"]).is_file())

    def test_a_wrong_token_or_a_missing_token_writes_nothing(self) -> None:
        self.write_parqet(A1)
        wrong = self.call("import_transactions", file_path=str(self.csv_path), plan_token="0" * 64, create_securities=True)
        self.assertFalse(wrong["ok"])
        self.assertIn("stale", wrong["message"])
        missing = self.call("import_transactions", file_path=str(self.csv_path), plan_token="", create_securities=True)
        self.assertFalse(missing["ok"])
        self.assertEqual(self.counts()["transactions"], 0)

    def test_only_an_existing_csv_file_is_read(self) -> None:
        other = Path(self.directory.name) / "notes.txt"
        other.write_text("x", encoding="utf-8")
        self.assertFalse(self.call("check_transaction_import", file_path=str(other))["ok"])
        self.assertFalse(self.call("check_transaction_import", file_path=str(Path(self.directory.name) / "missing.csv"))["ok"])
        self.assertFalse(self.call("check_transaction_import", file_path=str(self.db_path))["ok"])
        self.write_parqet(A1)
        self.assertFalse(self.call("check_transaction_import", file_path=str(self.csv_path), format="excel")["ok"])


class IdentifierAliasTests(FlowCase):
    """An ADR / GDR row: the file's ISIN differs from the master's ISIN, the WKN is the same.  The alias is explicit, evidenced, stored as a source symbol and never a guess."""

    def setUp(self) -> None:
        super().setUp()
        self.sid = self.add_security("KR0000000019", "Hynix Test", symbol="000660", wkn="AAA111")
        self.rows = (A1,)  # ISIN_A with the WKN AAA111

    def alias_rows(self):
        rows = self.conn.execute("SELECT s.symbol, d.name FROM source_symbols s JOIN data_sources d ON d.id = s.source_id WHERE s.security_id = ? AND d.name = 'Parqet'", (self.sid,))
        return [tuple(r) for r in rows]

    def test_without_the_alias_the_conflict_names_the_way_out(self) -> None:
        result = self.check(*self.rows)
        conflict = result["diff"]["masterdata_conflicts"][0]
        self.assertEqual((conflict["field"], conflict["database"], conflict["file"]), ("isin", "KR0000000019", ISIN_A))
        self.assertIn(f"--accept-identifier-alias {ISIN_A}=000660", conflict["reason"])  # same WKN: the hint appears
        self.assertEqual(self.alias_rows(), [])

    def test_the_accepted_alias_is_recorded_and_the_conflict_is_gone_for_good(self) -> None:
        checked = self.check(*self.rows, accept_aliases=[f"{ISIN_A}=000660"])
        self.assertEqual([(a["identifier"], a["status"]) for a in checked["diff"]["identifier_aliases"]], [(ISIN_A, "NEW")])
        self.assertEqual(checked["diff"]["masterdata_conflicts"], [])
        self.assertEqual(self.alias_rows(), [])  # the check wrote nothing
        result = self.run_flow(*self.rows, accept_aliases=[f"{ISIN_A}=000660"])
        self.assertTrue(result["written"])
        self.assertEqual(self.alias_rows(), [(ISIN_A, "Parqet")])
        self.assertEqual(self.security_isin(), "KR0000000019")  # the master ISIN is never overwritten
        again = self.check(*self.rows)  # no option any more: the resolver finds the stored identifier
        self.assertEqual(again["diff"]["masterdata_conflicts"], [])
        self.assertEqual(again["diff"]["existing_transactions"], 1)
        self.assertEqual(again["closing"]["status"], "MATCH")
        self.assertEqual(self.counts()["security"], 1)  # and no duplicate security was ever created

    def security_isin(self):
        return self.conn.execute("SELECT isin FROM security WHERE id = ?", (self.sid,)).fetchone()[0]

    def test_a_row_that_arrives_later_is_resolved_through_the_stored_alias(self) -> None:
        self.run_flow(*self.rows, accept_aliases=[f"{ISIN_A}=000660"])
        self.conn.execute("UPDATE security SET wkn = NULL WHERE id = ?", (self.sid,))  # even without the WKN route the identifier resolves
        result = self.run_flow(A1, A2, create_securities=True)
        self.assertEqual(result["diff"]["new_transactions"]["count"], 1)
        self.assertEqual(self.counts()["security"], 1)
        self.assertEqual(self.conn.execute("SELECT shares FROM positions WHERE security_id = ?", (self.sid,)).fetchone()[0], 15.0)

    def test_an_alias_without_evidence_is_refused_and_nothing_is_written(self) -> None:
        before = self.dump_hash()
        for wkn_db, spec, text in (("OTHER9", f"{ISIN_A}=000660", "WKN differs"), (None, f"{ISIN_A}=000660", "evidence missing"), ("AAA111", f"{ISIN_A}=NOSUCH", "no security"),
                                   ("AAA111", f"{ISIN_B}=000660", "does not occur in the file")):
            self.conn.execute("UPDATE security SET wkn = ? WHERE id = ?", (wkn_db, self.sid))
            before = self.dump_hash()
            checked = self.check(*self.rows, accept_aliases=[spec])
            entry = checked["diff"]["identifier_aliases"][0]
            self.assertEqual(entry["status"], "REFUSED", spec)
            self.assertIn(text, entry["reason"], spec)
            with self.assertRaises(ti.TransactionImportError):
                self.run_flow(*self.rows, accept_aliases=[spec])
            self.assertEqual(self.dump_hash(), before, spec)  # the whole write was refused

    def test_an_isin_that_belongs_to_another_security_cannot_be_an_alias(self) -> None:
        self.add_security(ISIN_A, "Real Owner", symbol="OWN")
        entry = self.check(*self.rows, accept_aliases=[f"{ISIN_A}=000660"])["diff"]["identifier_aliases"][0]
        self.assertEqual(entry["status"], "REFUSED")
        self.assertIn("is the ISIN of security", entry["reason"])

    def test_a_security_has_one_identifier_per_source(self) -> None:
        self.run_flow(*self.rows, accept_aliases=[f"{ISIN_A}=000660"])
        other = prow("2026-03-02T10:00:00.000Z", "Buy", isin=ISIN_B, name="Hynix Test", wkn="AAA111", shares="1", price="1", amount="1")
        entry = self.check(other, accept_aliases=[f"{ISIN_B}=000660"])["diff"]["identifier_aliases"][0]
        self.assertEqual(entry["status"], "REFUSED")
        self.assertIn("already has", entry["reason"])


class DecisionTests(FlowCase):
    """What the Import page decides on the spot: a strategy for a newly held position, an alias for an identifier conflict - through the existing options, with a token per decision."""

    def assignments(self):
        return {r[0]: r[1] for r in self.conn.execute("SELECT s.isin, a.strategy_type FROM strategy_assignment a JOIN security s ON s.id = a.security_id")}

    def campaigns(self):
        return self.conn.execute("SELECT COUNT(*) FROM swing_campaign").fetchone()[0]

    def test_a_newly_held_position_without_a_strategy_is_offered_for_a_decision(self) -> None:
        needed = self.check(A1, B1, create_securities=True)["diff"]["strategy_needed"]
        self.assertEqual(sorted(n["key"] for n in needed), sorted([ISIN_A, ISIN_B]))  # no ticker: the ISIN is the key the option takes
        self.assertEqual({n["name"] for n in needed}, {"Alpha Corp", "Beta AG"})
        self.assertTrue(all(n["shares"] > 0 for n in needed))

    def test_the_chosen_strategy_is_applied_through_the_existing_assignment_logic(self) -> None:
        none = self.check(A1, B1, create_securities=True)
        chosen = self.check(A1, B1, create_securities=True, strategies=[f"{ISIN_A}=long_term"])
        self.assertNotEqual(none["plan_token"], chosen["plan_token"])  # every decision gives a new plan
        self.assertEqual([n["key"] for n in chosen["diff"]["strategy_needed"]], [ISIN_B])
        self.assertEqual(self.assignments(), {})  # the check writes nothing
        result = self.run_flow(A1, B1, create_securities=True, strategies=[f"{ISIN_A}=long_term"], expected_plan_token=chosen["plan_token"])
        self.assertEqual(self.assignments(), {ISIN_A: "long_term"})  # "none" for the other one stays: no assignment is guessed
        self.assertEqual([n["key"] for n in result["diff"]["strategy_needed"]], [ISIN_B])
        self.assertEqual(self.campaigns(), 0)  # long-term: no campaign

    def test_swing_without_a_campaign_start_starts_no_campaign(self) -> None:
        by_default = self.check(A1, create_securities=True, strategies=[f"{ISIN_A}=swing"])
        self.assertEqual(len(by_default["planned"]["campaigns_created"]), 1)  # the existing rule: a derivable start is started
        checked = self.check(A1, create_securities=True, strategies=[f"{ISIN_A}=swing"], start_campaigns=False)
        self.assertNotEqual(by_default["plan_token"], checked["plan_token"])
        self.assertEqual(checked["planned"]["campaigns_created"], [])
        item = next(i for i in checked["planned"]["open_items"] if i["code"] == ti.CAMPAIGN_INITIALIZATION_REQUIRED)
        self.assertIn("CAMPAIGN_START_NOT_REQUESTED", item["detail"])
        self.run_flow(A1, create_securities=True, strategies=[f"{ISIN_A}=swing"], start_campaigns=False)
        self.assertEqual(self.assignments(), {ISIN_A: "swing"})
        self.assertEqual(self.campaigns(), 0)

    def test_an_explicit_campaign_date_is_still_honoured(self) -> None:
        self.run_flow(A1, create_securities=True, strategies=[f"{ISIN_A}=swing"], start_campaigns=False)
        self.run_flow(A1, campaign_opened_at=[f"{ISIN_A}=2026-03-02"], start_campaigns=False)
        self.assertEqual(self.campaigns(), 1)

    def test_the_same_decisions_twice_are_idempotent(self) -> None:
        decisions = dict(create_securities=True, strategies=[f"{ISIN_A}=long_term"], start_campaigns=False)
        self.run_flow(A1, B1, **decisions)
        after = self.dump_hash()
        again = self.run_flow(A1, B1, **decisions)
        self.assertFalse(again["written"])
        self.assertEqual(self.dump_hash(), after)

    def test_an_alias_conflict_offers_a_candidate_with_the_evidence(self) -> None:
        sid = self.add_security("KR0000000019", "Hynix Test", symbol="000660", wkn="AAA111")
        diff = self.check(A1)["diff"]
        self.assertEqual(diff["alias_candidates"], [{"identifier": ISIN_A, "security": "000660", "same_wkn": True, "security_id": sid, "name": "Hynix Test", "wkn": "AAA111",
                                                      "database_isin": "KR0000000019"}])
        accepted = self.check(A1, accept_aliases=[f"{ISIN_A}=000660"])["diff"]
        self.assertEqual(accepted["alias_candidates"], [])  # decided: no longer open
        self.assertEqual(accepted["identifier_aliases"][0]["status"], "NEW")

    def test_without_the_same_wkn_the_candidate_says_so_and_the_backend_refuses(self) -> None:
        self.add_security("KR0000000019", "Hynix Test", symbol="000660", wkn="OTHER9")
        self.conn.execute("UPDATE security SET isin = 'KR0000000019' WHERE symbol = '000660'")
        diff = self.check(A1)["diff"]
        self.assertEqual(diff["alias_candidates"], [])  # resolved by name only -> a CONFLICT row, not a master data conflict


class CliTests(FlowCase):
    def test_the_cli_prints_the_difference_and_the_reconciliation(self) -> None:
        self.write_parqet(A1, B1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--format", "parqet", "--csv", str(self.csv_path), "--db-path", str(self.db_path), "--create-securities"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Difference to the database:", text)
        self.assertIn("Reconciliation file <-> database: MATCH", text)
        self.assertEqual(self.counts()["transactions"], 0)  # the default is the dry run
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--format", "parqet", "--csv", str(self.csv_path), "--db-path", str(self.db_path), "--create-securities", "--write"]), 0)
        self.assertEqual(self.counts()["transactions"], 2)
        self.assertIn("integrity_check ['ok'], foreign_key_check ok", out.getvalue())


if __name__ == "__main__":
    unittest.main()
