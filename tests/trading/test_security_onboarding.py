"""onboard_security: controlled creation of one new security (A-Y); synthetic securities and the real schema only."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import security_aliases as sa  # noqa: E402
import security_onboarding as so  # noqa: E402
import test_watchlist_admin as twa  # noqa: E402


def make_isin(body: str) -> str:
    """A syntactically valid synthetic ISIN: ``body`` is the 11-character prefix, the check digit is appended."""
    for digit in "0123456789":
        if so.isin_checksum_ok(body + digit):
            return body + digit
    raise AssertionError(body)


NEW_ISIN = make_isin("ZZ000000NEW")
VALID_A, VALID_B = make_isin("ZZ00000ALPH"), make_isin("ZZ00000BETA")  # the fixture ISINs of the watchlist tests have no valid check digit
MINIMAL = dict(symbol="NEWCO", name="Newco Systems Inc", asset_type="stock")
ALL_FIELDS = dict(MINIMAL, isin=NEW_ISIN, wkn="N3WC0A", exchange="TESTX", currency="EUR", country="DE",
                  provider_symbols={"Yahoo Finance": "NEWCO.DE", "FMP": "NEWCO"}, add_to_watchlist=True, watchlist_priority=5)
GROWING = ("security", "source_symbols", "watchlist", "sqlite_sequence")  # the only tables an onboarding may change


class OnboardingCase(twa.WatchlistDbCase):
    def setUp(self) -> None:
        super().setUp()
        with self.connect() as conn:
            conn.execute("UPDATE security SET isin = ? WHERE id = 1", (VALID_A,))
            conn.execute("UPDATE security SET isin = ? WHERE id = 2", (VALID_B,))
            conn.commit()

    def onboard(self, dry_run: bool = True, **fields) -> dict:
        return asyncio.run(self.tools.onboard_security(dry_run=dry_run, **fields))

    def rows(self, table: str) -> int:
        with self.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def security(self, symbol: str):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM security WHERE symbol = ?", (symbol,)).fetchone()

    def untouched(self, before: dict[str, str]) -> None:
        after = self.fingerprint(exclude=GROWING)
        self.assertEqual(after, {k: v for k, v in before.items() if k not in GROWING})


class DryRunTests(OnboardingCase):
    def test_a_a_valid_new_security_is_ready(self) -> None:
        result = self.onboard(**ALL_FIELDS)
        self.assertEqual((result["ok"], result["result"], result["changed"], result["conflicts"]), (True, "READY", False, []))
        self.assertEqual(result["planned"]["security"]["symbol"], "NEWCO")
        self.assertEqual(result["planned"]["watchlist"], {"status": "WATCH", "priority": 5})
        self.assertEqual(result["planned"]["sector"], None)
        self.assertEqual([p["source"] for p in result["planned"]["provider_symbols"]], ["FMP", "Yahoo Finance"])

    def test_b_a_dry_run_writes_nothing(self) -> None:
        before, mtime = self.fingerprint(exclude=()), self.db.stat().st_mtime_ns
        for fields in (MINIMAL, ALL_FIELDS, dict(MINIMAL, symbol="AAA"), dict(MINIMAL, symbol="x y")):
            self.onboard(**fields)
        self.assertEqual(self.fingerprint(exclude=()), before)
        self.assertEqual(self.db.stat().st_mtime_ns, mtime)

    def test_the_default_is_a_dry_run(self) -> None:
        self.assertEqual(asyncio.run(self.tools.onboard_security(**MINIMAL))["result"], "READY")
        self.assertIsNone(self.security("NEWCO"))

    def test_the_input_is_normalized_and_nothing_is_added(self) -> None:
        result = self.onboard(symbol="  newco ", name="  Newco Systems Inc ", asset_type="STOCK", isin=NEW_ISIN.lower(), wkn="n3wc0a", currency="eur")
        planned = result["planned"]["security"]
        self.assertEqual((planned["symbol"], planned["name"], planned["asset_type"], planned["isin"], planned["wkn"], planned["currency"]),
                         ("NEWCO", "Newco Systems Inc", "stock", NEW_ISIN, "N3WC0A", "EUR"))
        self.assertEqual(self.onboard(symbol="NEWCO", name="Newco Systems Inc", asset_type="stock")["planned"]["security"]["exchange"], None)  # no exchange is invented
        self.assertNotIn(".", planned["symbol"])  # SAP never becomes SAP.DE


class CreateTests(OnboardingCase):
    def test_c_a_minimal_security_is_created(self) -> None:
        before = self.fingerprint(exclude=())
        result = self.onboard(dry_run=False, **MINIMAL)
        self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "CREATED", True))
        row = self.security("NEWCO")
        self.assertEqual((row["id"], row["name"], row["asset_type"], row["active"]), (result["security_id"], "Newco Systems Inc", "stock", 1))
        self.assertEqual(row["id"], 123457)  # the AUTOINCREMENT id, never asked from the caller
        for column in ("isin", "wkn", "exchange", "currency", "country", "industry"):
            self.assertIsNone(row[column], column)  # nothing is guessed
        self.assertEqual((self.rows("source_symbols"), self.rows("watchlist")), (1, 2))
        self.untouched(before)

    def test_d_e_isin_and_wkn_are_stored(self) -> None:
        self.onboard(dry_run=False, **dict(MINIMAL, isin=NEW_ISIN, wkn="N3WC0A"))
        row = self.security("NEWCO")
        self.assertEqual((row["isin"], row["wkn"]), (NEW_ISIN, "N3WC0A"))

    def test_f_g_provider_symbols_are_source_symbols(self) -> None:
        result = self.onboard(dry_run=False, **dict(MINIMAL, provider_symbols={"yahoo": "NEWCO.DE", "FMP": "NEWCO"}))
        with self.connect() as conn:
            stored = conn.execute("SELECT d.name, s.symbol, s.exchange, s.currency FROM source_symbols s JOIN data_sources d ON d.id = s.source_id WHERE s.security_id = ? ORDER BY d.name",
                                  (result["security_id"],)).fetchall()
        self.assertEqual([tuple(r) for r in stored], [("FMP", "NEWCO", None, None), ("Yahoo Finance", "NEWCO.DE", None, None)])

    def test_h_there_is_no_sector_and_none_is_assigned(self) -> None:
        self.assertNotIn("sector", self.tools_schema())
        self.onboard(dry_run=False, **ALL_FIELDS)
        self.assertNotIn("sector", self.security("NEWCO").keys())  # the column no longer exists; the sector comes from the sector map only

    def tools_schema(self) -> set[str]:
        import inspect

        return set(inspect.signature(self.tools.onboard_security).parameters)

    def test_i_the_watchlist_entry_is_created_in_the_same_transaction(self) -> None:
        result = self.onboard(dry_run=False, **ALL_FIELDS)
        self.assertEqual(self.watchlist()[result["security_id"]], ("WATCH", 5))
        self.assertEqual(result["watchlist"], {"status": "WATCH", "priority": 5})

    def test_without_the_option_there_is_no_watchlist_entry(self) -> None:
        result = self.onboard(dry_run=False, **MINIMAL)
        self.assertNotIn(result["security_id"], self.watchlist())

    def test_s_to_v_no_position_strategy_campaign_or_order(self) -> None:
        before = self.fingerprint(exclude=())
        self.onboard(dry_run=False, **ALL_FIELDS)
        after = self.fingerprint(exclude=())
        changed = {name for name in after if after[name] != before.get(name)}
        self.assertEqual(changed - {"sqlite_sequence"}, {"security", "source_symbols", "watchlist"})
        for table in ("positions", "strategy_assignment", "swing_campaign", "swing_campaign_event", "transactions", "market_data", "fundamentals", "news", "events", "candidate_promotion"):
            self.assertEqual(after[table], before[table], table)

    def test_x_the_existing_resolver_finds_the_new_security(self) -> None:
        result = self.onboard(dry_run=False, **ALL_FIELDS)
        sid = result["security_id"]
        with self.connect() as conn:
            index = sa.AliasIndex.from_connection(conn)
            for identifier in ("NEWCO", "NEWCO.DE", NEW_ISIN, "N3WC0A", "Newco Systems Inc"):
                resolution = twa.wa.resolve_identifier(conn, identifier)
                self.assertEqual((resolution.outcome, resolution.security_id), ("FOUND", sid), identifier)
            self.assertEqual(index.key_for(sid), "NEWCO.DE")  # the Yahoo symbol is the listing key
        rows = self.run_tool("get_watchlist", table=True)["rows"]
        row = next(r for r in rows if r["security_id"] == sid)
        self.assertEqual((row["symbol"], row["quality"]), ("NEWCO", "MARKET_DATA_MISSING"))  # no market data yet, and none is invented
        self.assertNotIn("score", row)


class ExistingAndConflictTests(OnboardingCase):
    def codes(self, result: dict) -> list[str]:
        return [c["code"] for c in result["conflicts"]]

    def test_j_w_a_repeated_call_never_creates_a_duplicate(self) -> None:
        first = self.onboard(dry_run=False, **ALL_FIELDS)
        before = self.fingerprint(exclude=())
        for fields in (ALL_FIELDS, MINIMAL, dict(MINIMAL, isin=NEW_ISIN)):
            again = self.onboard(dry_run=False, **fields)
            self.assertEqual((again["result"], again["changed"], again["existing"]["security_id"]), ("ALREADY_EXISTS", False, first["security_id"]))
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_an_existing_security_is_reported_with_its_identity(self) -> None:
        result = self.onboard(symbol="AAA", name="Alpha Corp", asset_type="stock", isin=VALID_A)
        self.assertEqual(result["result"], "ALREADY_EXISTS")
        self.assertEqual({k: result["existing"][k] for k in ("security_id", "symbol", "name")}, {"security_id": 1, "symbol": "AAA", "name": "Alpha Corp"})
        self.assertEqual(result["on_watchlist"], False)
        self.assertEqual(self.onboard(symbol="GGG", name="Gamma AG", asset_type="stock")["on_watchlist"], True)

    def test_k_a_symbol_of_another_security_is_a_conflict(self) -> None:
        for symbol, match in (("AAA", "EXACT"), ("BBB.DE", "EXACT"), ("AAA.DE", "STEM")):  # an exact public / provider symbol, and the same stem with a listing suffix
            result = self.onboard(symbol=symbol, name="Different Company Ltd", asset_type="stock")
            self.assertEqual(result["result"], "SYMBOL_CONFLICT", symbol)
            self.assertEqual(result["conflicts"][0]["match"], match, symbol)
            self.assertIn("security_id", result["conflicts"][0])
        self.assertEqual(self.onboard(symbol="DUP", name="Third Corp", asset_type="stock")["conflicts"].__len__(), 2)  # both owners are listed

    def test_l_an_isin_of_another_security_is_a_conflict(self) -> None:
        result = self.onboard(symbol="NEWCO", name="Newco Systems Inc", asset_type="stock", isin=VALID_B)
        self.assertEqual(result["result"], "ISIN_CONFLICT")
        self.assertEqual((result["conflicts"][0]["security_id"], result["conflicts"][0]["symbol"]), (2, "BBB"))
        self.assertEqual(self.security("NEWCO"), None)

    def test_m_a_wkn_of_another_security_is_a_conflict(self) -> None:
        result = self.onboard(symbol="NEWCO", name="Newco Systems Inc", asset_type="stock", wkn=twa.WKN_B)
        self.assertEqual((result["result"], result["conflicts"][0]["security_id"]), ("WKN_CONFLICT", 2))

    def test_n_a_provider_symbol_of_another_security_is_an_alias_conflict(self) -> None:
        result = self.onboard(**dict(MINIMAL, provider_symbols={"Yahoo Finance": "BBB.DE"}))
        self.assertEqual(result["result"], "ALIAS_CONFLICT")
        conflict = result["conflicts"][0]
        self.assertEqual((conflict["field"], conflict["value"], conflict["security_id"], conflict["symbol"]), ("provider_symbols.Yahoo Finance", "BBB.DE", 2, "BBB"))

    def test_all_conflicts_are_reported_not_just_the_first(self) -> None:
        result = self.onboard(symbol="AAA", name="Other Name Ltd", asset_type="stock", isin=VALID_B, wkn=twa.WKN_B, provider_symbols={"Yahoo Finance": "BBB.DE"})
        self.assertEqual(sorted(set(self.codes(result))), ["ALIAS_CONFLICT", "ISIN_CONFLICT", "SYMBOL_CONFLICT", "WKN_CONFLICT"])
        self.assertEqual(result["result"], "ISIN_CONFLICT")  # the most specific one leads

    def test_a_name_alone_is_only_a_hint(self) -> None:
        result = self.onboard(symbol="NEWCO", name="Alpha Corp", asset_type="stock")
        self.assertEqual((result["result"], self.codes(result)), ("READY", []))
        self.assertEqual([(w["code"], w["security_id"]) for w in result["warnings"]], [("NAME_MATCH", 1)])

    def test_y_an_inactive_existing_security_is_reported_and_not_touched(self) -> None:
        before = self.fingerprint(exclude=())
        result = self.onboard(dry_run=False, symbol="OLD", name="Old Corp", asset_type="stock")
        self.assertEqual((result["result"], result["existing"]["active"]), ("ALREADY_EXISTS", False))
        self.assertIn("EXISTING_SECURITY_INACTIVE", [w["code"] for w in result["warnings"]])
        self.assertEqual(self.fingerprint(exclude=()), before)  # no reactivation: editing is a separate workflow


class InvalidInputTests(OnboardingCase):
    def reasons(self, **fields) -> set[tuple[str, str]]:
        result = self.onboard(**fields)
        self.assertEqual(result["result"], "INVALID_INPUT", fields)
        self.assertEqual((result["ok"], result["changed"]), (True, False))
        return {(e["field"], e["reason"]) for e in result["errors"]}

    def test_o_invalid_input(self) -> None:
        cases = [
            (dict(MINIMAL, symbol=""), ("symbol", "REQUIRED")), (dict(MINIMAL, symbol="a b"), ("symbol", "BAD_SYMBOL")), (dict(MINIMAL, symbol="X" * 21), ("symbol", "BAD_SYMBOL")),
            (dict(MINIMAL, name="  "), ("name", "REQUIRED")), (dict(MINIMAL, name="N" * 201), ("name", "TOO_LONG")), (dict(MINIMAL, name="a\x00b"), ("name", "CONTROL_CHARACTERS")),
            (dict(MINIMAL, asset_type=""), ("asset_type", "REQUIRED")), (dict(MINIMAL, asset_type="bond"), ("asset_type", "UNKNOWN_ASSET_TYPE")),
            (dict(MINIMAL, isin="ZZ123"), ("isin", "BAD_ISIN")), (dict(MINIMAL, isin="ZZ0000000010"), ("isin", "BAD_ISIN_CHECKSUM")),
            (dict(MINIMAL, wkn="123"), ("wkn", "BAD_WKN")), (dict(MINIMAL, currency="EURO"), ("currency", "BAD_CURRENCY")), (dict(MINIMAL, country="DEU"), ("country", "BAD_COUNTRY")),
            (dict(MINIMAL, provider_symbols={"Bloomberg": "X"}), ("provider_symbols", "UNKNOWN_PROVIDER")),
            (dict(MINIMAL, provider_symbols={"SEC EDGAR": "0000000001"}), ("provider_symbols", "UNKNOWN_PROVIDER")),  # CIKs are never guessed
            (dict(MINIMAL, provider_symbols={"Yahoo Finance": "bad symbol"}), ("provider_symbols.Yahoo Finance", "BAD_PROVIDER_SYMBOL")),
            (dict(MINIMAL, provider_symbols="NEWCO"), ("provider_symbols", "MAPPING_EXPECTED")),
            (dict(MINIMAL, add_to_watchlist=True, watchlist_priority="abc"), ("watchlist_priority", "BAD_PRIORITY")),
            (dict(MINIMAL, watchlist_priority=3), ("watchlist_priority", "PRIORITY_WITHOUT_WATCHLIST")),
            (dict(MINIMAL, add_to_watchlist="yes"), ("add_to_watchlist", "BOOLEAN_EXPECTED")),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertIn(expected, self.reasons(**fields))
        self.assertEqual(self.rows("security"), len(twa.SECURITIES))

    def test_every_error_is_listed_and_nothing_is_written_on_create(self) -> None:
        before = self.fingerprint(exclude=())
        result = self.onboard(dry_run=False, symbol="", name="", asset_type="x", isin="bad")
        self.assertEqual(sorted(e["field"] for e in result["errors"]), ["asset_type", "isin", "name", "symbol"])
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_p_there_is_no_sector_to_be_unknown(self) -> None:
        self.assertNotIn("sector", self.tools_params())

    def tools_params(self) -> set[str]:
        import inspect

        return set(inspect.signature(self.tools.onboard_security).parameters)

    def test_the_isin_check_digit_matches_known_isins(self) -> None:
        for isin in ("US0378331005", "DE000BAY0017", "GB0002634946", "US5949181045"):  # public reference ISINs, only used to test the check digit
            self.assertTrue(so.isin_checksum_ok(isin), isin)
        self.assertFalse(so.isin_checksum_ok("US0378331006"))


class AtomicityTests(OnboardingCase):
    def break_table(self, table: str) -> None:
        with self.connect() as conn:
            conn.execute(f"CREATE TRIGGER trg_test_block_{table} BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END")
            conn.commit()

    def test_q_an_alias_failure_rolls_everything_back(self) -> None:
        self.break_table("source_symbols")
        before = self.fingerprint(exclude=())
        result = self.onboard(dry_run=False, **ALL_FIELDS)
        self.assertEqual((result["ok"], result["result"], result["changed"]), (False, "ERROR", False))
        self.assertEqual(self.fingerprint(exclude=()), before)
        self.assertIsNone(self.security("NEWCO"))

    def test_r_a_watchlist_failure_rolls_the_security_back(self) -> None:
        self.break_table("watchlist")
        before = self.fingerprint(exclude=())
        result = self.onboard(dry_run=False, **ALL_FIELDS)
        self.assertEqual((result["ok"], result["result"], result["changed"]), (False, "ERROR", False))
        self.assertEqual(self.fingerprint(exclude=()), before)  # no security and no provider symbol remain
        self.assertEqual(self.rows("source_symbols"), 1)

    def test_a_missing_data_source_rolls_back(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE data_sources SET active = 0 WHERE name = 'FMP'")
            conn.commit()
        before = self.fingerprint(exclude=())
        result = self.onboard(dry_run=False, **ALL_FIELDS)
        self.assertEqual((result["result"], result["changed"]), ("ERROR", False))
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_the_checks_run_again_under_the_write_lock(self) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO security(symbol, name) VALUES ('NEWCO', 'Somebody Else Ltd')")  # appears between the dry run and the create
            conn.commit()
        result = self.onboard(dry_run=False, **MINIMAL)
        self.assertEqual((result["result"], result["changed"]), ("SYMBOL_CONFLICT", False))
        self.assertEqual(self.rows("security"), len(twa.SECURITIES) + 1)


class ToolSurfaceTests(unittest.TestCase):
    def test_the_signature_defaults_to_a_dry_run_and_has_no_sql_or_id_fields(self) -> None:
        import inspect

        params = inspect.signature(twa.tools_class().onboard_security).parameters
        self.assertIs(params["dry_run"].default, True)
        self.assertIs(params["add_to_watchlist"].default, False)
        self.assertEqual(sorted(p for p in params if p != "self"), sorted(["symbol", "name", "asset_type", "isin", "wkn", "exchange", "currency", "country", "provider_symbols",
                                                                         "add_to_watchlist", "watchlist_priority", "dry_run"]))
        for forbidden in ("security_id", "sql", "query", "sector", "cik", "active"):
            self.assertNotIn(forbidden, params)

    def test_the_module_writes_only_security_source_symbols_and_watchlist(self) -> None:
        source = (ROOT / "tools" / "trading" / "security_onboarding.py").read_text(encoding="utf-8")
        import re

        written = set(re.findall(r"(?:INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)", source))
        self.assertEqual(written, {"security", "source_symbols", "watchlist"})


if __name__ == "__main__":
    unittest.main()
