# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Security alias resolution: one security_id, many names; no bare-ticker matching.  Synthetic securities only."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))

import market_intelligence as mi  # noqa: E402
from security_aliases import (  # noqa: E402
    ALIAS,
    COMPANY_NAME,
    EXCHANGE_SUFFIXES,
    ISIN,
    SECURITY_ID,
    SOURCE_IDENTIFIER,
    SYMBOL_STEM,
    TEXT_COMPANY_NAME,
    TEXT_EXCHANGE_TICKER,
    TEXT_ISIN,
    WKN,
    AliasIndex,
    SecurityAlias,
    split_symbol,
    symbol_key,
)

ALFA = SecurityAlias(1, "ALFA", ("ALFA",), "US0000000001", "A0AAAA", "Alpha Semi Inc.", (("YAHOO", "ALFA"), ("SEC", "ALFA")))
GOLF = SecurityAlias(2, "GOLF.DE", ("GOLF", "GOLF.DE"), "DE0000000002", None, "Golf Defense AG", (("YAHOO", "GOLF.DE"),))
SHORT_AI = SecurityAlias(3, "AI", ("AI",), "US0000000003", None, "Artificial Systems Corp", (("YAHOO", "AI"),))
SHORT_ON = SecurityAlias(4, "ON", ("ON",), None, None, "Ontario Devices Inc", (("YAHOO", "ON"),))
SHORT_IT = SecurityAlias(5, "IT", ("IT",), None, None, "Itinerary Holdings", (("YAHOO", "IT"),))
SHORT_CAT = SecurityAlias(6, "CAT", ("CAT",), None, None, "Caterwaul Industries Inc", (("YAHOO", "CAT"),))
BRK = SecurityAlias(7, "BRK-B", ("BRK.B",), None, None, "Berkshire Example Holdings", (("YAHOO", "BRK-B"),))
INDEX = AliasIndex([ALFA, GOLF, SHORT_AI, SHORT_ON, SHORT_IT, SHORT_CAT, BRK])


def text_hits(text: str) -> list[tuple[int, str]]:
    return [(m.security_id, m.matched_by) for m in INDEX.resolve_text(text)]


class ResolutionTests(unittest.TestCase):
    def test_a_canonical_symbol_resolves_to_the_security(self) -> None:
        match = INDEX.resolve(symbol="ALFA")
        self.assertEqual((match.security_id, match.matched_by), (1, ALIAS))
        self.assertEqual(INDEX.resolve(symbol="alfa").security_id, 1)  # case-insensitive

    def test_b_every_source_alias_is_the_same_security(self) -> None:
        for kwargs in ({"symbol": "GOLF"}, {"symbol": "GOLF.DE"}, {"source": "Yahoo Finance", "source_symbol": "GOLF.DE"}, {"security_id": 2}):
            with self.subTest(kwargs):
                self.assertEqual(INDEX.resolve(**kwargs).security_id, 2)
        self.assertEqual(INDEX.resolve(source="SEC EDGAR", source_symbol="ALFA").matched_by, SOURCE_IDENTIFIER)
        self.assertEqual(INDEX.resolve(symbol="BRK-B").security_id, INDEX.resolve(symbol="BRK.B").security_id)  # class-share notation

    def test_c_isin_and_wkn_resolve_to_the_same_security(self) -> None:
        self.assertEqual((INDEX.resolve(isin="de0000000002").security_id, INDEX.resolve(isin="DE0000000002").matched_by), (2, ISIN))
        self.assertEqual((INDEX.resolve(wkn="a0aaaa").security_id, INDEX.resolve(wkn="A0AAAA").matched_by), (1, WKN))

    def test_d_many_aliases_one_security_id(self) -> None:
        ids = {
            INDEX.resolve(symbol="GOLF").security_id, INDEX.resolve(symbol="GOLF.DE").security_id, INDEX.resolve(isin="DE0000000002").security_id,
            INDEX.resolve(source="Yahoo Finance", source_symbol="GOLF.DE").security_id, INDEX.resolve(name="Golf Defense").security_id, INDEX.resolve(security_id=2).security_id,
            *INDEX.security_ids_for_query("golf"), *INDEX.security_ids_for_query("DE0000000002"),
        }
        self.assertEqual(ids, {2})
        self.assertEqual(INDEX.key_for(2), "GOLF.DE")  # the report key: the Yahoo symbol, else the public symbol
        self.assertTrue({"GOLF", "GOLF.DE", "DE0000000002"} <= set(INDEX.aliases_of(2)))

    def test_resolution_order(self) -> None:
        # a security_id wins over a conflicting symbol; an ISIN wins over a conflicting symbol
        self.assertEqual(INDEX.resolve(security_id=1, symbol="GOLF").matched_by, SECURITY_ID)
        self.assertEqual(INDEX.resolve(isin="US0000000001", symbol="GOLF").security_id, 1)
        self.assertEqual(INDEX.resolve(wkn="A0AAAA", symbol="GOLF").security_id, 1)

    def test_f_unknown_and_ambiguous_aliases_never_guess(self) -> None:
        self.assertIsNone(INDEX.resolve(symbol="ZZZZ"))
        self.assertIsNone(INDEX.resolve(isin="US9999999999"))
        self.assertEqual(INDEX.security_ids_for_query("ZZZZ"), set())
        twin_a, twin_b = SecurityAlias(10, "TWIN", ("TWIN",), None, None, "Twin A Corp"), SecurityAlias(11, "TWIN2", ("TWIN",), None, None, "Twin B Corp")
        ambiguous = AliasIndex([twin_a, twin_b])
        self.assertIsNone(ambiguous.resolve(symbol="TWIN"))  # two securities share the alias: no match

    def test_stem_fallback_is_opt_in_and_marked(self) -> None:
        self.assertIsNone(INDEX.resolve(symbol="GOLF.F"))
        match = INDEX.resolve(symbol="GOLF.F", allow_stem=True)
        self.assertEqual((match.security_id, match.matched_by), (2, SYMBOL_STEM))

    def test_name_is_the_last_fallback_and_exact(self) -> None:
        self.assertEqual(INDEX.resolve(name="Alpha Semi, Inc.").matched_by, COMPANY_NAME)
        self.assertIsNone(INDEX.resolve(name="Alpha"))  # not a prefix / fuzzy match

    def test_symbol_normalization(self) -> None:
        self.assertEqual(split_symbol("SAP.DE"), ("SAP", "DE"))
        self.assertEqual(split_symbol("BRK-B"), ("BRK.B", None))
        self.assertEqual(split_symbol("BRK.B"), ("BRK.B", None))  # B is not a listing suffix
        self.assertEqual(symbol_key(" sap.de "), "SAP.DE")
        self.assertEqual(EXCHANGE_SUFFIXES, frozenset(mi._SUFFIX_REGION))  # one list of listing suffixes


class FalsePositiveTests(unittest.TestCase):
    def test_e_short_symbols_never_match_ordinary_words(self) -> None:
        for sentence in (
            "AI is changing how every company works and IT matters",
            "Turn ON the lights; the CAT sat ON the mat; AI AI AI",
            "Stocks to watch: AI, ON, IT and CAT rally after the open",
            "Why AI stocks (and ON Semiconductor-like names) jumped",
        ):
            with self.subTest(sentence):
                self.assertEqual(text_hits(sentence), [])

    def test_explicit_exchange_qualified_ticker_does_match(self) -> None:
        self.assertEqual(text_hits("Artificial Systems (NYSE: AI) announces results"), [(3, TEXT_EXCHANGE_TICKER)])
        self.assertEqual(text_hits("Caterwaul Industries Inc. (NASDAQ: CAT) reports"), [(6, TEXT_EXCHANGE_TICKER)])
        self.assertEqual(text_hits("Golf Defense (XETRA: GOLF) wins order"), [(2, TEXT_EXCHANGE_TICKER)])

    def test_exchange_must_fit_the_listing(self) -> None:
        self.assertEqual(text_hits("Foo Corp (NASDAQ: GOLF) reports"), [])  # GOLF is listed on Xetra here, not on a US exchange
        self.assertEqual(text_hits("Foo Corp (XETRA: ALFA) reports"), [])
        self.assertEqual(text_hits("Foo Corp (TSX: ALFA) reports"), [])  # an exchange without a known listing suffix cannot be mapped
        self.assertEqual(text_hits("Foo Corp (NASDAQ: zzzz) reports"), [])  # unknown ticker

    def test_isin_and_full_company_name_phrase(self) -> None:
        self.assertEqual(text_hits("Notice about DE0000000002 for holders"), [(2, TEXT_ISIN)])
        self.assertEqual(text_hits("Alpha Semi announces a new product"), [(1, TEXT_COMPANY_NAME)])
        self.assertEqual(text_hits("alpha semi is not capitalised like a company name"), [])
        self.assertEqual(text_hits("A semi truck and an alpha wolf"), [])
        self.assertEqual(text_hits("Itinerary Holdings ... "), [(5, TEXT_COMPANY_NAME)])  # the full name, not the word IT

    def test_single_generic_or_short_name_words_do_not_identify_a_company(self) -> None:
        generic = AliasIndex([SecurityAlias(20, "NETW", ("NETW",), None, None, "Networks Inc"), SecurityAlias(21, "SHRT", ("SHRT",), None, None, "Acme Corp")])
        self.assertEqual(generic.resolve_text("Networks everywhere; Acme sells things"), [])

    def test_text_never_resolves_two_securities_for_one_name_phrase(self) -> None:
        clash = AliasIndex([SecurityAlias(30, "CLA", ("CLA",), None, None, "Clash Holdings Inc"), SecurityAlias(31, "CLB", ("CLB",), None, None, "Clash Holdings Corp")])
        self.assertEqual(clash.resolve_text("Clash Holdings announces"), [])


class LoaderTests(unittest.TestCase):
    def test_loads_the_security_master_with_source_symbols(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, isin TEXT, wkn TEXT, name TEXT NOT NULL);
            CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE source_symbols (id INTEGER PRIMARY KEY, security_id INTEGER, source_id INTEGER, symbol TEXT);
            INSERT INTO data_sources VALUES (1, 'Yahoo Finance'), (2, 'SEC EDGAR');
            INSERT INTO security VALUES (1, 'GOLF', 'DE0000000002', NULL, 'Golf Defense AG'), (2, 'ALFA', NULL, 'A0AAAA', 'Alpha Semi Inc.');
            INSERT INTO source_symbols(security_id, source_id, symbol) VALUES (1, 1, 'GOLF.DE'), (2, 2, 'ALFA');
            """
        )
        index = AliasIndex.from_connection(conn)
        self.assertEqual((len(index), index.key_for(1), index.key_for(2)), (2, "GOLF.DE", "ALFA"))
        self.assertEqual(index.resolve(source="Yahoo Finance", source_symbol="GOLF.DE").security_id, 1)
        self.assertEqual(index.resolve(source="SEC EDGAR", source_symbol="alfa").security_id, 2)
        self.assertEqual(index.resolve(symbol="GOLF").security_id, 1)

    def test_minimal_or_missing_tables_degrade_without_errors(self) -> None:
        minimal = sqlite3.connect(":memory:")
        minimal.executescript("CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT NOT NULL); INSERT INTO security VALUES (1, 'ALFA', 'Alpha Semi');")
        self.assertEqual(AliasIndex.from_connection(minimal).resolve(symbol="ALFA").security_id, 1)
        self.assertEqual(len(AliasIndex.from_connection(sqlite3.connect(":memory:"))), 0)

    def test_the_report_table_rebuilds_an_equivalent_query_index(self) -> None:
        table = INDEX.report_table([2, 1])
        rebuilt = AliasIndex.from_table(table)
        for query in ("golf", "GOLF.DE", "DE0000000002", "alfa", "A0AAAA"):
            self.assertEqual(rebuilt.security_ids_for_query(query), INDEX.security_ids_for_query(query), query)
        self.assertEqual(list(table), ["1", "2"])

    def test_loading_is_read_only(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT NOT NULL); INSERT INTO security VALUES (1, 'ALFA', 'Alpha');")
        before = conn.total_changes
        AliasIndex.from_connection(conn)
        self.assertEqual(conn.total_changes, before)


if __name__ == "__main__":
    unittest.main()
