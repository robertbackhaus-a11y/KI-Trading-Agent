# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Watchlist tools (get_watchlist, add / remove / set priority): the real schema, synthetic securities, atomic and narrow writes (A-R)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRADING = ROOT / "tools" / "trading"
for path in (TRADING,):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import security_aliases as sa  # noqa: E402
import watchlist_admin as wa  # noqa: E402
import watchlist_view as wv  # noqa: E402

_spec = importlib.util.spec_from_file_location("init_trading_db_for_watchlist_tests", TRADING / "Initialize-TradingDatabase.py")
init_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(init_db)

ISIN_A, ISIN_B = "ZZ0000000011", "ZZ0000000029"
WKN_B = "B0TEST"
SECURITIES = [  # id, symbol, name, isin, wkn, active
    (1, "AAA", "Alpha Corp", ISIN_A, "A0TEST", 1),
    (2, "BBB", "Beta Holdings Inc", ISIN_B, WKN_B, 1),
    (3, "GGG", "Gamma AG", None, None, 1),  # held, swing, already on the watchlist
    (4, "DUP", "Duplicate One Corp", None, None, 1),  # the same public symbol twice: ambiguous
    (5, "DUP", "Duplicate Two Corp", None, None, 1),
    (6, "OLD", "Old Corp", None, None, 0),  # inactive
    (7, "CCC", "Charlie Systems Corp", None, "123456", 1),  # a numeric WKN ...
    (123456, "NUM", "Numeric Id Corp", None, None, 1),  # ... that is also the security_id of another security
]


def tools_class():
    spec = importlib.util.spec_from_file_location("trading_sqlite_watchlist_test", ROOT / "mcp-tools" / "trading_sqlite.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    def loader(cls, name):  # the deployed helpers live in C:\tools\trading\app; the tests use the repo copies
        return importlib.import_module(name)

    module.Tools._load_runtime_trading_module = classmethod(loader)
    return module.Tools


class WatchlistDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "trading.db"
        with contextlib.redirect_stdout(io.StringIO()):
            init_db.create_database(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executemany("INSERT INTO security(id, symbol, name, isin, wkn, active) VALUES (?, ?, ?, ?, ?, ?)", SECURITIES)
        yahoo = conn.execute("SELECT id FROM data_sources WHERE name = 'Yahoo Finance'").fetchone()[0]
        conn.execute("INSERT INTO source_symbols(security_id, source_id, symbol) VALUES (2, ?, 'BBB.DE')", (yahoo,))
        conn.execute("INSERT INTO positions(security_id, shares, avg_cost, currency) VALUES (3, 10, 20.0, 'EUR')")
        conn.execute("INSERT INTO strategy_assignment(security_id, strategy_type, effective_from, source) VALUES (3, 'swing', '2020-01-01', 'test')")
        conn.execute("INSERT INTO swing_campaign(security_id, strategy_assignment_id, opened_at, original_quantity, reference_avg_cost, reference_currency, status, source) "
                     "VALUES (3, 1, '2026-01-02', 10, 20.0, 'EUR', 'open', 'test')")
        conn.execute("INSERT INTO watchlist(security_id, status, priority) VALUES (3, 'WATCH', 10)")
        conn.execute("INSERT INTO watchlist(security_id, status) VALUES (4, 'WATCH')")
        start = date.today() - timedelta(days=60)
        conn.executemany(
            "INSERT INTO market_data(security_id, trade_date, close, adjusted_close, currency, source_id) VALUES (3, ?, ?, ?, 'EUR', ?)",
            [((start + timedelta(days=i)).isoformat(), 20.0 + i * 0.1, 20.0 + i * 0.1, yahoo) for i in range(61)],
        )
        conn.commit()
        conn.close()
        self.tools = tools_class()()
        self.tools.valves.database_path = str(self.db)

    # -- helpers --------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def connect(self):
        """A connection that is really closed afterwards (an open handle blocks the temp folder cleanup on Windows)."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def run_tool(self, name: str, *args, **kwargs) -> dict:
        return asyncio.run(getattr(self.tools, name)(*args, **kwargs))

    def watchlist(self) -> dict[int, tuple]:
        with self.connect() as conn:
            return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT security_id, status, priority FROM watchlist")}

    def fingerprint(self, exclude=("watchlist",)) -> dict[str, str]:
        out = {}
        with self.connect() as conn:
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
                if name in exclude:
                    continue
                digest = hashlib.sha256()
                for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
                    digest.update(repr(tuple(row)).encode("utf-8", "replace"))
                out[name] = digest.hexdigest()
        return out


class ResolutionTests(WatchlistDbCase):
    def resolve(self, identifier):
        with self.connect() as conn:
            return wa.resolve_identifier(conn, identifier)

    def test_each_identifier_kind_resolves_in_the_documented_order(self) -> None:
        for identifier, expected in ((1, (1, sa.SECURITY_ID)), ("1", (1, sa.SECURITY_ID)), (ISIN_B, (2, sa.ISIN)), (ISIN_B.lower(), (2, sa.ISIN)), (WKN_B, (2, sa.WKN)),
                                     ("bbb", (2, sa.ALIAS)), ("BBB.DE", (2, sa.ALIAS)), ("Beta Holdings Inc", (2, sa.COMPANY_NAME)), ("BETA HOLDINGS, INC.", (2, sa.COMPANY_NAME))):
            with self.subTest(identifier=identifier):
                result = self.resolve(identifier)
                self.assertEqual((result.outcome, result.security_id, result.matched_by), ("FOUND", expected[0], expected[1]))

    def test_the_symbol_stem_is_a_controlled_fallback(self) -> None:
        result = self.resolve("AAA.DE")  # AAA is known without the listing suffix
        self.assertEqual((result.outcome, result.security_id, result.matched_by), ("FOUND", 1, sa.SYMBOL_STEM))

    def test_there_is_no_fuzzy_search(self) -> None:
        for identifier in ("Alph", "Alpha", "Beta", "beta holdings", "Gam", "AA", "AAAA", "Corp", "xyz"):  # a partial company name never matches
            with self.subTest(identifier=identifier):
                self.assertEqual(self.resolve(identifier).outcome, "NOT_FOUND")

    def test_several_hits_are_ambiguous_and_never_the_first(self) -> None:
        result = self.resolve("DUP")
        self.assertEqual((result.outcome, result.security_id, result.candidates), ("AMBIGUOUS", None, (4, 5)))

    def test_a_number_that_is_a_security_id_and_a_wkn_is_ambiguous(self) -> None:
        result = self.resolve("123456")
        self.assertEqual((result.outcome, result.candidates), ("AMBIGUOUS", (7, 123456)))

    def test_invalid_identifiers(self) -> None:
        for identifier in (None, "", "   ", True, 1.5, [], {}, "A" * 81, "bad\nid", "x\x00y"):
            with self.subTest(identifier=repr(identifier)[:20]):
                self.assertEqual(self.resolve(identifier).outcome, "INVALID")

    def test_alias_index_candidates_tell_unknown_from_ambiguous(self) -> None:
        with self.connect() as conn:
            index = sa.AliasIndex.from_connection(conn)
        self.assertEqual(index.candidates(sa.ALIAS, "DUP"), frozenset({4, 5}))
        self.assertEqual(index.candidates(sa.ALIAS, "NOPE"), frozenset())
        self.assertIsNone(index.resolve(symbol="DUP"))  # resolve() itself stays silent about the ambiguity (unchanged)
        self.assertEqual(index.candidates(sa.SECURITY_ID, "abc"), frozenset())
        with self.assertRaises(ValueError):
            index.candidates("NOPE", "x")


class AddTests(WatchlistDbCase):
    def add(self, identifier, **kw):
        return self.run_tool("add_watchlist_candidate", identifier, **kw)

    # B
    def test_b_add_via_security_id(self) -> None:
        result = self.add("1")
        self.assertEqual((result["ok"], result["result"], result["changed"], result["security_id"], result["matched_by"]), (True, "ADDED", True, 1, "SECURITY_ID"))
        self.assertEqual(self.watchlist()[1], ("WATCH", None))

    # C
    def test_c_add_via_symbol(self) -> None:
        self.assertEqual((self.add("aaa")["result"], self.add("aaa")["result"]), ("ADDED", "ALREADY_EXISTS"))

    # D
    def test_d_add_via_isin(self) -> None:
        result = self.add(ISIN_B)
        self.assertEqual((result["result"], result["security_id"], result["isin"], result["matched_by"]), ("ADDED", 2, ISIN_B, "ISIN"))

    # E
    def test_e_add_via_wkn(self) -> None:
        result = self.add(WKN_B)
        self.assertEqual((result["result"], result["security_id"], result["matched_by"]), ("ADDED", 2, "WKN"))

    # F
    def test_f_add_via_alias(self) -> None:
        result = self.add("BBB.DE")
        self.assertEqual((result["result"], result["security_id"], result["matched_by"]), ("ADDED", 2, "ALIAS"))
        self.assertEqual(self.add("Beta Holdings Inc")["result"], "ALREADY_EXISTS")  # the exact company name finds the same security

    # G
    def test_g_a_duplicate_is_reported_and_changes_nothing(self) -> None:
        before = self.watchlist()
        result = self.add("GGG", priority=99)
        self.assertEqual((result["ok"], result["result"], result["changed"], result["priority"]), (True, "ALREADY_EXISTS", False, 10))
        self.assertEqual(self.watchlist(), before)  # also the priority is not overwritten by an add

    # H
    def test_h_an_unknown_security_is_not_created(self) -> None:
        with self.connect() as conn:
            before = conn.execute("SELECT COUNT(*) FROM security").fetchone()[0], conn.execute("SELECT COUNT(*) FROM source_symbols").fetchone()[0]
        for identifier in ("ZZ9999999999", "NOSUCH", "999999", "Unknown Corp"):
            result = self.add(identifier)
            self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "SECURITY_NOT_FOUND", False), identifier)
            self.assertEqual(result["message"], "Wertpapier ist noch nicht im Security Master.")
            self.assertIn("separater Workflow", result["hint"])
        with self.connect() as conn:
            self.assertEqual((conn.execute("SELECT COUNT(*) FROM security").fetchone()[0], conn.execute("SELECT COUNT(*) FROM source_symbols").fetchone()[0]), before)

    # I
    def test_i_an_ambiguous_identifier_lists_the_candidates_and_writes_nothing(self) -> None:
        before = self.watchlist()
        result = self.add("DUP")
        self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "AMBIGUOUS_IDENTIFIER", False))
        self.assertEqual([c["security_id"] for c in result["candidates"]], [4, 5])
        self.assertEqual(self.watchlist(), before)

    def test_an_inactive_security_is_not_added_and_the_input_is_validated(self) -> None:
        result = self.add("OLD")
        self.assertEqual((result["result"], result["reason"], result["changed"]), ("SECURITY_NOT_FOUND", "SECURITY_INACTIVE", False))
        self.assertEqual(self.add("")["result"], "INVALID_IDENTIFIER")
        self.assertEqual(self.add(None)["result"], "INVALID_IDENTIFIER")
        for priority in (1.5, "x", True, [1], 2**63, float("nan"), float("inf")):
            self.assertEqual(self.add("AAA", priority=priority)["result"], "INVALID_PRIORITY", priority)
        self.assertNotIn(1, self.watchlist())

    def test_add_with_a_priority_and_the_dry_run(self) -> None:
        dry = self.add("AAA", priority=40, dry_run=True)
        self.assertEqual((dry["result"], dry["changed"], dry["security_id"], dry["isin"]), ("WOULD_ADD", False, 1, ISIN_A))
        self.assertNotIn(1, self.watchlist())
        self.assertEqual(self.add("AAA", priority=40)["priority"], 40)
        self.assertEqual(self.watchlist()[1], ("WATCH", 40))
        self.assertEqual(self.add("AAA", dry_run=True)["result"], "ALREADY_EXISTS")
        self.assertEqual(self.add("NOSUCH", dry_run=True)["result"], "SECURITY_NOT_FOUND")

    def test_a_security_without_market_data_can_be_added(self) -> None:
        self.assertEqual(self.add("AAA")["result"], "ADDED")  # AAA has no price history at all
        rows = {r["security_id"]: r for r in self.run_tool("get_watchlist", table=True)["rows"]}
        self.assertEqual(rows[1]["quality"], "MARKET_DATA_MISSING")
        self.assertNotIn("score", rows[1])

    def test_sql_in_an_identifier_is_just_text(self) -> None:
        before = self.fingerprint(exclude=())
        for identifier in ("1; DROP TABLE watchlist", "x' OR '1'='1", "AAA'); DELETE FROM security;--"):
            self.assertEqual(self.add(identifier)["result"], "SECURITY_NOT_FOUND", identifier)
        self.assertEqual(self.fingerprint(exclude=()), before)


class RemoveAndPriorityTests(WatchlistDbCase):
    # J
    def test_j_remove_an_existing_entry(self) -> None:
        result = self.run_tool("remove_watchlist_candidate", 4)
        self.assertEqual((result["ok"], result["result"], result["changed"], result["security_id"], result["still_held"]), (True, "REMOVED", True, 4, False))
        self.assertNotIn(4, self.watchlist())
        with self.connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM security WHERE id = 4").fetchone())  # the security itself stays

    # K
    def test_k_remove_a_missing_entry(self) -> None:
        for security_id in (1, 999):
            result = self.run_tool("remove_watchlist_candidate", security_id)
            self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "NOT_FOUND", False))
        for bad in (0, -1, True, 1.5, "x", None):
            self.assertEqual(self.run_tool("remove_watchlist_candidate", bad)["result"], "INVALID_IDENTIFIER", bad)

    def test_removing_a_held_security_only_removes_the_watchlist_row(self) -> None:
        before = self.fingerprint()
        result = self.run_tool("remove_watchlist_candidate", 3)
        self.assertEqual((result["result"], result["still_held"]), ("REMOVED", True))
        self.assertIn("Position bleibt unverändert", result["message"])
        self.assertEqual(self.fingerprint(), before)

    def test_a_removal_reports_whether_notes_went_with_the_row(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE watchlist SET thesis = 'a thesis' WHERE security_id = 3")
            conn.commit()
        flagged = {r["security_id"]: r.get("annotated") for r in self.run_tool("get_watchlist", table=True)["rows"]}
        self.assertEqual(flagged, {3: True, 4: None})  # the flag is shown only where something would be lost
        detail = self.run_tool("get_watchlist", symbol="GGG", detail=True)["rows"][0]
        self.assertEqual((detail["thesis"], detail["notes"], detail["entry_reason"], detail["target_entry"]), ("a thesis", None, None, None))
        with_notes = self.run_tool("remove_watchlist_candidate", 3)
        self.assertEqual((with_notes["result"], with_notes["had_notes"]), ("REMOVED", True))
        self.assertIn("Notizen", with_notes["message"])
        plain = self.run_tool("remove_watchlist_candidate", 4)
        self.assertEqual((plain["result"], plain["had_notes"]), ("REMOVED", False))
        self.assertNotIn("Notizen", plain["message"])

    def test_there_is_no_invented_business_range_only_whole_numbers_or_null(self) -> None:
        self.assertEqual((wa.PRIORITY_MIN, wa.PRIORITY_MAX), (-(2**63), 2**63 - 1))  # the storage range of an SQLite INTEGER, nothing more
        for priority, stored in ((-5, -5), (0, 0), (101, 101), (1000, 1000), (7.0, 7), ("12", 12), (2**62, 2**62), (-(2**62), -(2**62))):
            result = self.run_tool("set_watchlist_priority", 3, priority)
            self.assertEqual((result["result"], result["priority"]), ("PRIORITY_SET", stored), priority)
            self.assertEqual(self.watchlist()[3][1], stored)
        self.assertEqual(self.run_tool("add_watchlist_candidate", "AAA", priority=250)["priority"], 250)
        self.assertEqual(self.watchlist()[1], ("WATCH", 250))

    # L
    def test_l_set_a_valid_priority(self) -> None:
        result = self.run_tool("set_watchlist_priority", 3, 75)
        self.assertEqual((result["ok"], result["result"], result["changed"], result["priority"], result["previous_priority"]), (True, "PRIORITY_SET", True, 75, 10))
        self.assertEqual(self.watchlist()[3], ("WATCH", 75))
        self.assertEqual(self.run_tool("set_watchlist_priority", 3, 75)["changed"], False)  # idempotent
        self.assertEqual(self.run_tool("set_watchlist_priority", 3, 0)["priority"], 0)
        cleared = self.run_tool("set_watchlist_priority", 3, None)
        self.assertEqual((cleared["result"], cleared["priority"]), ("PRIORITY_SET", None))
        self.assertEqual(self.watchlist()[3], ("WATCH", None))

    # M
    def test_m_an_invalid_priority_changes_nothing(self) -> None:
        before = self.watchlist()
        for priority in (1.5, "abc", "1.5", True, [1], {}, 2**63, -(2**63) - 1, float("nan"), float("-inf")):
            result = self.run_tool("set_watchlist_priority", 3, priority)
            self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "INVALID_PRIORITY", False), priority)
        self.assertEqual(self.run_tool("set_watchlist_priority", 99, 5)["result"], "NOT_FOUND")
        self.assertEqual(self.run_tool("set_watchlist_priority", "x", 5)["result"], "INVALID_IDENTIFIER")
        self.assertEqual(self.watchlist(), before)

    def test_only_priority_and_updated_at_of_that_row_change(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE watchlist SET updated_at = '2020-01-01 00:00:00', notes = 'n', thesis = 't', entry_reason = 'e', target_entry = 12.5 WHERE security_id = 3")
            conn.commit()
            before = tuple(conn.execute("SELECT * FROM watchlist WHERE security_id = 3").fetchone())
        self.run_tool("set_watchlist_priority", 3, 60)
        with self.connect() as conn:
            after = tuple(conn.execute("SELECT * FROM watchlist WHERE security_id = 3").fetchone())
        with self.connect() as conn:
            names = [c[1] for c in conn.execute("PRAGMA table_info(watchlist)")]
        changed = [n for n, a, b in zip(names, before, after) if a != b]
        self.assertEqual(sorted(changed), ["priority", "updated_at"])


class IsolationTests(WatchlistDbCase):
    def exercise(self) -> None:
        self.run_tool("add_watchlist_candidate", "AAA", priority=5)
        self.run_tool("add_watchlist_candidate", ISIN_B)
        self.run_tool("set_watchlist_priority", 1, 33)
        self.run_tool("remove_watchlist_candidate", 4)
        self.run_tool("remove_watchlist_candidate", 3)
        self.run_tool("add_watchlist_candidate", "DUP")
        self.run_tool("add_watchlist_candidate", "NOSUCH")

    # N, O, P, Q
    def test_n_o_p_q_writes_change_only_the_watchlist(self) -> None:
        before = self.fingerprint()
        self.exercise()
        self.assertEqual(self.fingerprint(), before)  # every table except watchlist is byte-identical: positions, strategies, campaigns, securities, market data
        for table in ("positions", "strategy_assignment", "swing_campaign", "security", "source_symbols", "market_data", "transactions", "metadata"):
            self.assertIn(table, before)
        self.assertEqual(set(self.watchlist()), {1, 2, 5} - {5} | {1, 2})  # 3 and 4 removed, 1 and 2 added; the ambiguous add wrote nothing

    def test_the_schema_is_untouched(self) -> None:
        with self.connect() as conn:
            before = conn.execute("SELECT group_concat(sql, '') FROM sqlite_master").fetchone()[0]
        self.exercise()
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT group_concat(sql, '') FROM sqlite_master").fetchone()[0], before)

    # R
    def test_r_an_error_rolls_back_completely(self) -> None:
        with self.connect() as conn:
            conn.execute("CREATE TRIGGER trg_test_block_insert BEFORE INSERT ON watchlist BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END")
            conn.execute("CREATE TRIGGER trg_test_block_update BEFORE UPDATE ON watchlist BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END")
            conn.execute("CREATE TRIGGER trg_test_block_delete BEFORE DELETE ON watchlist BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END")
            conn.commit()
        before, tables = self.watchlist(), self.fingerprint(exclude=())
        for result in (self.run_tool("add_watchlist_candidate", "AAA"), self.run_tool("remove_watchlist_candidate", 3), self.run_tool("set_watchlist_priority", 3, 50)):
            self.assertEqual((result["ok"], result["result"], result["changed"]), (False, "ERROR", False))
            self.assertIn("blocked for the test", result["message"])
        self.assertEqual((self.watchlist(), self.fingerprint(exclude=())), (before, tables))
        with self.connect() as conn:
            self.assertFalse(conn.in_transaction)

    def test_a_locked_database_is_an_error_not_a_half_write(self) -> None:
        blocker = sqlite3.connect(self.db, timeout=0.1)
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.close)
        self.tools.valves.timeout_seconds = 0.1
        admin_conn = sqlite3.connect(self.db, timeout=0.1)
        self.addCleanup(admin_conn.close)
        result = wa.add_watchlist_candidate(admin_conn, "AAA")
        self.assertEqual((result["ok"], result["result"], result["changed"]), (False, "ERROR", False))
        blocker.rollback()
        self.assertNotIn(1, self.watchlist())
        self.assertEqual(wa.add_watchlist_candidate(admin_conn, "AAA")["result"], "ADDED")  # the connection is usable again after the failed attempt

    def test_a_missing_database_is_an_error_result(self) -> None:
        self.tools.valves.database_path = str(Path(self.tmp.name) / "missing.db")
        result = self.run_tool("add_watchlist_candidate", "AAA")
        self.assertEqual((result["ok"], result["result"], result["changed"]), (False, "ERROR", False))


class ReadTests(WatchlistDbCase):
    # A
    def test_a_get_watchlist_returns_every_candidate(self) -> None:
        self.run_tool("add_watchlist_candidate", "AAA")
        self.run_tool("add_watchlist_candidate", ISIN_B, priority=80)
        result = self.run_tool("get_watchlist", table=True)
        self.assertEqual((result["ok"], result["read_only"], result["matching"], result["returned"], result["truncated"]), (True, True, 4, 4, False))
        self.assertEqual(result["counts"]["total"], 4)
        self.assertEqual(result["counts"]["by_status"], {"WATCH": 4})
        rows = {r["security_id"]: r for r in result["rows"]}
        self.assertEqual(set(rows), {1, 2, 3, 4})
        gamma = rows[3]
        self.assertEqual((gamma["symbol"], gamma["name"], gamma["strategy"], gamma["held"], gamma["priority"], gamma["status"], gamma["news_status"]),
                         ("GGG", "Gamma AG", "swing", True, 10, "WATCH", "NEWS_UNAVAILABLE"))
        self.assertEqual(gamma["quality"], "LIMITED")  # 61 price points (OK needs 200)
        self.assertEqual(gamma["rank"], 1)  # the only scored entry
        self.assertIsNotNone(gamma["score"])
        self.assertEqual(rows[2]["isin"], ISIN_B)
        self.assertEqual((rows[2]["quality"], rows[2].get("rank"), rows[2]["priority"]), ("MARKET_DATA_MISSING", None, 80))
        self.assertEqual([r["security_id"] for r in result["rows"]][0], 3)  # ranked first, then by priority
        self.assertEqual([r["security_id"] for r in result["rows"]][1], 2)  # unranked: higher priority first
        warning_codes = {w["code"] for w in result["warnings"]}
        self.assertIn("MARKET_INTELLIGENCE_REPORT_UNAVAILABLE", warning_codes)
        self.assertEqual(result["status"], "PARTIAL")

    def test_get_watchlist_does_not_write(self) -> None:
        before = self.fingerprint(exclude=())
        mtime = self.db.stat().st_mtime_ns
        self.run_tool("get_watchlist", detail=True)
        self.run_tool("get_watchlist")
        self.assertEqual(self.fingerprint(exclude=()), before)
        self.assertEqual(self.db.stat().st_mtime_ns, mtime)

    def test_filters(self) -> None:
        self.run_tool("add_watchlist_candidate", "AAA")
        self.assertEqual([r["security_id"] for r in self.run_tool("get_watchlist", strategy="swing", table=True)["rows"]], [3])
        self.assertEqual(sorted(r["security_id"] for r in self.run_tool("get_watchlist", strategy="NONE", table=True)["rows"]), [1, 4])
        self.assertEqual([r["security_id"] for r in self.run_tool("get_watchlist", symbol="ggg, nope", table=True)["rows"]], [3])
        self.assertEqual([r["security_id"] for r in self.run_tool("get_watchlist", symbol=ISIN_A, table=True)["rows"]], [1])
        self.assertEqual(self.run_tool("get_watchlist", status="REMOVED")["matching"], 0)
        limited = self.run_tool("get_watchlist", limit=1)
        self.assertEqual((limited["returned"], limited["matching"], limited["truncated"]), (1, 3, True))

    def test_detail_adds_the_diagnosis_fields(self) -> None:
        row = self.run_tool("get_watchlist", symbol="GGG", detail=True)["rows"][0]
        for key in ("wkn", "yahoo_symbol", "data_points", "assessment_reasons", "aliases", "news", "updated_at"):
            self.assertIn(key, row)
        self.assertIn("sector_source", row)  # the sector now comes from the sector map (no database column)
        compact = self.run_tool("get_watchlist", symbol="GGG")["rows"][0]
        self.assertNotIn("aliases", compact)

    # compact (chat) answer
    def test_the_default_answer_is_the_compact_chat_shape(self) -> None:
        self.run_tool("add_watchlist_candidate", "AAA")
        result = self.run_tool("get_watchlist")
        self.assertNotIn("counts", result)
        summary = result["summary"]
        self.assertEqual((summary["total"], summary["held"], summary["statuses"], summary["strategies"]), (3, 1, {"WATCH": 3}, {"NONE": 2, "swing": 1}))
        self.assertEqual(summary["same_for_all"], {"status": "WATCH", "news_status": "NEWS_UNAVAILABLE"})
        allowed = set(wv._COMPACT_KEYS)
        for row in result["rows"]:
            self.assertLessEqual(set(row), allowed)
            for private in ("security_id", "isin", "added_at", "as_of", "promotion", "annotated", "notes", "thesis", "aliases"):
                self.assertNotIn(private, row)
            self.assertNotIn("status", row)  # identical for every entry: stated once in the summary
        self.assertEqual([r["symbol"] for r in result["rows"]][0], "GGG")
        self.assertEqual(result["rows"][0]["held"], True)
        self.assertNotIn("held", result["rows"][1])  # only ever true
        self.assertEqual((result["matching"], result["returned"], result["truncated"]), (3, 3, False))
        json.loads(json.dumps(result))

    def test_a_differing_status_stays_in_the_rows(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE watchlist SET status = 'PAUSED' WHERE security_id = 4")
            conn.commit()
        result = self.run_tool("get_watchlist")
        self.assertNotIn("status", result["summary"]["same_for_all"])
        self.assertEqual(sorted(r["status"] for r in result["rows"]), ["PAUSED", "WATCH"])

    def test_table_and_detail_keep_the_previous_shapes(self) -> None:
        table = self.run_tool("get_watchlist", table=True)
        self.assertIn("counts", table)
        self.assertNotIn("summary", table)
        self.assertEqual(set(table["rows"][0]) & {"security_id", "status", "added_at", "news_status"}, {"security_id", "status", "added_at", "news_status"})
        self.assertNotIn("aliases", table["rows"][0])
        detail = self.run_tool("get_watchlist", table=True, detail=True)  # detail wins over table
        self.assertIn("aliases", detail["rows"][0])
        self.assertIn("counts", detail)

    def test_the_summary_describes_the_whole_selection_when_rows_are_limited(self) -> None:
        result = self.run_tool("get_watchlist", limit=1)
        self.assertEqual((result["returned"], result["matching"], result["truncated"], result["summary"]["total"]), (1, 2, True, 2))

    def test_an_oversized_answer_is_trimmed_to_the_budget(self) -> None:
        rows = [{"security_id": i, "symbol": f"S{i}", "name": "N" * 60, "isin": "ZZ0000000011", "strategy": "swing", "held": False, "priority": 1, "status": "WATCH", "rank": i,
                 "score": 1.0, "quality": "OK", "as_of": "2026-01-01", "assessment": "WATCH", "entry_status": "WAIT_FOR_TRIGGER", "promotion": "KEEP_WATCHING",
                 "news_status": "NEWS_CLEAR", "added_at": "2026-01-01 00:00:00", "aliases": [], "news": {"counts": {}, "events": []}} for i in range(1, 400)]
        view = {"status": "AVAILABLE", "counts": {"total": 399}, "order": "x", "rows": rows, "warnings": []}
        out = wv.query_watchlist(view, limit=500, table=True)
        self.assertLessEqual(len(wv._text(out)), wv.RESPONSE_CHAR_BUDGET)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["rows"][0]["security_id"], 1)  # the best-ranked rows are kept
        self.assertEqual(out["returned"], len(out["rows"]))
        self.assertLessEqual(out["filter"]["limit"], wv.MAX_LIMIT)


class ViewJoinTests(unittest.TestCase):
    ROWS = [
        {"security_id": 1, "status": "WATCH", "priority": None, "symbol": "AAA", "name": "Alpha", "isin": None, "held": False, "strategy": None, "added_at": "x"},
        {"security_id": 2, "status": "WATCH", "priority": 50, "symbol": "BBB", "name": "Beta", "isin": None, "held": True, "strategy": "swing", "added_at": "x"},
        {"security_id": 3, "status": "PAUSED", "priority": None, "symbol": "CCC", "name": "Gamma", "isin": None, "held": False, "strategy": "long_term", "added_at": "x"},
    ]

    def test_the_join_uses_the_existing_results_and_degrades_without_them(self) -> None:
        ranking = [{"security_id": 2, "score": 71.234, "quality": "OK", "as_of": "2026-10-01", "data_points": 250, "current_price": 5.0},
                   {"security_id": 1, "score": None, "quality": "INSUFFICIENT", "as_of": None, "data_points": 0, "current_price": None}]
        decisions = [{"security_id": 2, "decision_status": "BUY", "decision_reasons": ["R"], "portfolio_constraints": []}]
        engine = {"portfolio_action_plan": {"entry_candidates": [{"security_id": 2, "entry_status": "ENTRY_READY", "rank": 1, "entry_score": 9.9}]},
                  "promotion_results": [{"security_id": 2, "recommendation": "PROMOTE"}]}
        view = wv.build_watchlist(rows=self.ROWS, ranking=ranking, decisions=decisions, engine=engine, mi=None)
        by_id = {r["security_id"]: r for r in view["rows"]}
        self.assertEqual((by_id[2]["rank"], by_id[2]["score"], by_id[2]["quality"], by_id[2]["assessment"], by_id[2]["entry_status"], by_id[2]["promotion"]),
                         (1, 71.23, "OK", "BUY", "ENTRY_READY", "PROMOTE"))
        self.assertEqual((by_id[1]["rank"], by_id[1]["score"], by_id[1]["quality"]), (None, None, "MARKET_DATA_MISSING"))
        self.assertEqual((by_id[3]["quality"], by_id[3]["rank"], by_id[3]["assessment"]), (None, None, None))  # a non-WATCH status is not ranked
        self.assertEqual({r["news_status"] for r in view["rows"]}, {"NEWS_UNAVAILABLE"})
        self.assertEqual([r["security_id"] for r in view["rows"]], [2, 1, 3])
        self.assertEqual(view["counts"]["by_strategy"], {"NONE": 1, "long_term": 1, "swing": 1})
        self.assertEqual(view["counts"]["held"], 1)
        bare = wv.build_watchlist(rows=self.ROWS, ranking=None, decisions=None, engine=None, mi=None)
        self.assertEqual({w["code"] for w in bare["warnings"]}, {"RANKING_UNAVAILABLE", "DECISIONS_UNAVAILABLE", "ENGINE_UNAVAILABLE", "MARKET_INTELLIGENCE_REPORT_UNAVAILABLE"})
        self.assertEqual(bare["status"], "PARTIAL")

    def test_the_inputs_are_not_modified(self) -> None:
        import copy

        rows = copy.deepcopy(self.ROWS)
        wv.build_watchlist(rows=rows, ranking=[], decisions=[], engine={}, mi=None)
        self.assertEqual(rows, self.ROWS)


if __name__ == "__main__":
    unittest.main()
