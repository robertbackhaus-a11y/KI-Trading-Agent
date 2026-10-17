# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The ETF data refresh (``etf_refresh`` / ``Refresh-TradingEtfData.py``): strict provider parsers, validation before writing, atomic replacement, independent files, private
alias / issuer data kept, SPXS never normalised; synthetic provider answers only (no network)."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "trading"
sys.path.insert(0, str(TOOLS))

import etf_holdings as eh  # noqa: E402
import etf_lookthrough as lt  # noqa: E402
import etf_refresh as er  # noqa: E402

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
XUSE, IS3N, SPXS = "IE000R4ZNTN3", "IE00BKM4GZ66", "IE00B3YCGJ38"
XUSE_CSV_URL = "https://www.ishares.com/uk/individual/en/products/340748/ishares-msci-world-ex-usa-ucits-etf/latest-holdings.csv"
XUSE_PAGE_URL = "https://www.ishares.com/uk/individual/en/products/340748/ishares-msci-world-ex-usa-ucits-etf"
IS3N_CSV_URL = "https://www.ishares.com/uk/individual/en/products/264659/ishares-msci-emerging-markets-imi-ucits-etf/latest-holdings.csv"
IS3N_PAGE_URL = "https://www.ishares.com/uk/individual/en/products/264659/ishares-msci-emerging-markets-imi-ucits-etf"
SPXS_INDEX_URL = "https://dng-api.invesco.com/cache/v1/accounts/en_IE/shareclasses/IE00B3YCGJ38/holdings/index?idType=isin"
SPXS_FUND_URL = "https://dng-api.invesco.com/cache/v1/accounts/en_IE/shareclasses/IE00B3YCGJ38?idType=isin"

HEADER = ",".join(f'"{c}"' if "," in c or " " in c else c for c in er.ISHARES_HEADER)
SECTORS = ["Financials", "Industrials", "Information Technology", "Health Care", "Cash and/or Derivatives"]


# ------------------------------------------------------------------------------------------------------------ synthetic provider answers
def names(n: int, prefix: str = "CO") -> list[tuple[str, str]]:
    return [(f"{prefix}{i:03d}", f"{prefix} COMPANY {i:03d}") for i in range(n)]


def ishares_csv(rows: list[tuple[str, str, float]], as_of: str = "13/Oct/2026", extra: str = "", header: str = HEADER) -> bytes:
    lines = [f'Fund Holdings as of,"{as_of}"', "", header]
    for ticker, name, weight in rows:
        lines.append(f'"{ticker}","{name}","Financials","Equity","1,000.00","{weight}","1,000.00","10.00","10.00","Germany","Xetra","EUR"')
    lines.append('"EUR","EUR CASH","Cash and/or Derivatives","Cash","1.00","0.10","1.00","1.00","1.00","Germany","-","EUR"')
    lines.append(extra)
    lines += ["", '"The content contained herein is owned by BlackRock."']
    return "\n".join(lines).encode("utf-8")


def equal_rows(n: int, prefix: str = "CO", total: float = 99.9) -> list[tuple[str, str, float]]:
    return [(t, nm, round(total / n, 4)) for t, nm in names(n, prefix)]


def ishares_page(title_ticker: str, sectors: dict[str, float], as_of: str = "20261013") -> bytes:
    inner = {"asOf": {"value": int(as_of)}, "fund": {"value": list(sectors.values())}, "net": {"value": [0] * len(sectors)}, "type": {"value": list(sectors)}}
    block = json.dumps({"name": "sector", "fullName": "exposureBreakdowns.sector", "dataPointsByNameMap": inner}, separators=(",", ":"))
    return f'<html><head><title>iShares Fund | {title_ticker} | iShares</title></head><body><script>{{"x":1,"sector":{block}}}</script></body></html>'.encode("utf-8")


def sector_values(scale: float = 1.0) -> dict[str, float]:
    return {"Financials": round(30 * scale, 2), "Industrials": round(25 * scale, 2), "Information Technology": round(20 * scale, 2), "Health Care": round(15 * scale, 2),
            "Cash and/or Derivatives": round(10 * scale, 2)}


def invesco_index(n: int = 480, total: float = 102.5, as_of: str = "2026-08-31", isin_prefix: str = "US") -> bytes:
    rows = [{"name": f"IDX {i:03d} ORD", "isin": f"{isin_prefix}{i:09d}0", "cusip": f"{i:09d}", "weight": round(total / n, 2)} for i in range(n)]
    return json.dumps({"effectiveDate": as_of, "holdings": rows}).encode("utf-8")


def invesco_class(name: str = "Invesco S&amp;P 500 UCITS ETF Acc") -> bytes:
    return json.dumps({"shareClassFullName": name}).encode("utf-8")


def stored_holding(ticker: str, name: str, weight: float) -> dict:
    return {"name": name, "ticker": ticker, "weight": weight, "asset_class": "Equity", "sector": "Financials", "location": "Germany", "exchange": "Xetra"}


def stored_payload(n: int = 60) -> dict:
    index_rows = [{"name": f"IDX {i:03d} ORD", "ticker": "", "isin": f"US{i:09d}0", "weight": round(102.0 / 480, 2), "asset_class": "Equity"} for i in range(480)]
    return {
        "map_id": "etf_holdings_local", "version": 1, "description": "private description that must survive",
        "etfs": {
            XUSE: {"name": "iShares MSCI World ex-USA UCITS ETF USD Acc", "provider": "iShares (BlackRock)", "as_of": "2026-10-12", "kind": "physical", "source": "old xuse source",
                   "holdings": [stored_holding(t, nm, w) for t, nm, w in equal_rows(n, "CO", 99.8)]},
            IS3N: {"name": "iShares Core MSCI EM IMI UCITS ETF USD Acc", "provider": "iShares (BlackRock)", "as_of": "2026-10-12", "kind": "physical", "source": "old is3n source",
                   "holdings": [stored_holding(t, nm, w) for t, nm, w in equal_rows(n, "EM", 99.8)]},
            SPXS: {"name": "Invesco S&P 500 UCITS ETF Acc", "provider": "Invesco", "as_of": "2026-08-28", "kind": "synthetic_index", "source": "old spxs source", "note": "old note", "holdings": index_rows},
        },
        "aliases": [{"ticker": "2330", "exchange": "Taiwan Stock Exchange", "isin": "US8740391003", "reason": "reviewed ADR link"}],
        "issuer_groups": [{"name": "Alphabet Inc.", "isins": ["US02079K3059", "US02079K1079"], "reason": "A and C"}],
    }


def stored_lookthrough() -> dict:
    return {"map_id": "etf_lookthrough_local", "version": 1, "description": "private lookthrough description",
            "etfs": {XUSE: {"name": "iShares MSCI World ex-USA UCITS ETF USD Acc", "provider": "iShares (BlackRock)", "as_of": "2026-10-12", "source": "old", "sectors": sector_values(0.99999)},
                     IS3N: {"name": "iShares Core MSCI EM IMI UCITS ETF USD Acc", "provider": "iShares (BlackRock)", "as_of": "2026-10-12", "source": "old", "sectors": sector_values()},
                     SPXS: {"name": "Invesco S&P 500 UCITS ETF Acc", "provider": "Invesco", "as_of": "2026-07-31", "source": "factsheet", "sectors": sector_values()}}}


def good_answers() -> dict[str, bytes]:
    return {
        XUSE_CSV_URL: ishares_csv(equal_rows(60, "CO", 99.8)), IS3N_CSV_URL: ishares_csv(equal_rows(60, "EM", 99.8)),
        XUSE_PAGE_URL: ishares_page("XUSE", sector_values()), IS3N_PAGE_URL: ishares_page("EIMI", sector_values()),
        SPXS_INDEX_URL: invesco_index(), SPXS_FUND_URL: invesco_class(),
    }


class Provider:
    """A fetcher that answers from a dict, records the URLs and raises RefreshError for anything it does not know (like a failing download)."""

    def __init__(self, answers: dict[str, bytes]) -> None:
        self.answers, self.urls = dict(answers), []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        value = self.answers.get(url)
        if value is None:
            raise er.RefreshError("HTTP_ERROR", f"404 for {url}")
        if isinstance(value, Exception):
            raise value
        return value


class RefreshCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.hpath, self.lpath = self.dir / er.HOLDINGS_FILE, self.dir / er.LOOKTHROUGH_FILE
        self.hpath.write_text(json.dumps(stored_payload(), separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        self.lpath.write_text(json.dumps(stored_lookthrough(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def run_refresh(self, answers=None, **kwargs) -> dict:
        provider = Provider(good_answers() if answers is None else answers)
        self.provider = provider
        return er.refresh(self.dir, fetcher=provider, now=NOW, **kwargs)

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def rows(self, report: dict, dataset: str) -> dict[str, dict]:
        return {r["etf"]: r for r in report["etfs"] if r["dataset"] == dataset}

    def stored(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if ".new-" in p.name)


class DryRunTests(RefreshCase):
    def test_a_dry_run_downloads_and_validates_but_writes_nothing(self) -> None:
        before = (self.digest(self.hpath), self.digest(self.lpath))
        report = self.run_refresh()
        self.assertFalse(report["write"])
        self.assertEqual((self.digest(self.hpath), self.digest(self.lpath)), before)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted([er.LOOKTHROUGH_FILE, er.HOLDINGS_FILE]))
        status = {(r["etf"], r["dataset"]): r["status"] for r in report["etfs"]}
        self.assertEqual(status[("XUSE", "holdings")], "READY")
        self.assertEqual(status[("SPXS", "lookthrough")], "NOT_AUTOMATED")
        self.assertEqual([info["written"] for info in report["files"].values()], [False, False])

    def test_the_report_has_everything_asked_for(self) -> None:
        report = self.run_refresh()
        row = self.rows(report, "holdings")["XUSE"]
        self.assertEqual((row["as_of"], row["holdings"], row["provider"]), ("2026-10-13", 61, "iShares (BlackRock)"))  # 60 equities and the cash line
        self.assertEqual(row["source"], XUSE_CSV_URL + " (full holdings file)")
        self.assertAlmostEqual(row["weights_total"], 99.9, places=1)  # 60 equities and the 0.1 % cash line
        changes = row["changes"]
        self.assertEqual((changes["as_of_old"], changes["as_of_new"], changes["old"]["count"], changes["new"]["count"]), ("2026-10-12", "2026-10-13", 60, 61))
        self.assertEqual((changes["added"], changes["removed"]), (1, 0))  # the cash line is new in the published list
        self.assertFalse(row["written"])


class IsharesTests(RefreshCase):
    def test_xuse_holdings_are_written_as_published_and_everything_private_survives(self) -> None:
        answers = good_answers()
        answers[XUSE_CSV_URL] = ishares_csv([("CO000", "CO COMPANY 000", 5.5)] + equal_rows(60, "CO", 94.2)[1:], as_of="13/Oct/2026")
        report = self.run_refresh(answers, write=True, only=["XUSE"], what=[er.HOLDINGS])
        self.assertEqual(self.rows(report, "holdings")["XUSE"]["status"], "WRITTEN")
        data = self.stored(self.hpath)
        entry = data["etfs"][XUSE]
        self.assertEqual((entry["as_of"], entry["kind"], entry["provider"]), ("2026-10-13", "physical", "iShares (BlackRock)"))
        self.assertEqual(entry["source"], XUSE_CSV_URL + " (full holdings file)")
        self.assertEqual(entry["holdings"][0], {"name": "CO COMPANY 000", "ticker": "CO000", "weight": 5.5, "asset_class": "Equity", "sector": "Financials", "location": "Germany", "exchange": "Xetra"})
        self.assertEqual(entry["holdings"][-1]["asset_class"], "Cash")  # the cash line is kept as published
        old = stored_payload()
        self.assertEqual(data["aliases"], old["aliases"])  # reviewed links are not lost
        self.assertEqual(data["issuer_groups"], old["issuer_groups"])
        self.assertEqual(data["description"], old["description"])
        self.assertEqual(data["etfs"][IS3N], old["etfs"][IS3N])  # another ETF is not touched
        self.assertEqual(data["etfs"][SPXS], old["etfs"][SPXS])
        self.assertTrue(self.hpath.read_text(encoding="utf-8").startswith('{"map_id"'))  # the compact layout of the stored file is kept
        self.assertEqual(self.leftovers(), [])
        backup = [p for p in self.dir.iterdir() if ".bak-refresh-" in p.name]
        self.assertEqual(len(backup), 1)
        self.assertEqual(json.loads(backup[0].read_text(encoding="utf-8")), old)  # the previous file, byte for byte what it was
        layers = eh.read_layers(self.dir)
        self.assertEqual(eh.status_of(layers, XUSE, "2026-10-15")[0], eh.APPLIED)

    def test_is3n_holdings_and_the_lookthrough_with_the_london_ticker_of_the_page(self) -> None:
        report = self.run_refresh(write=True, only=["IS3N"])
        self.assertEqual(self.rows(report, "holdings")["IS3N"]["status"], "WRITTEN")
        self.assertEqual(self.rows(report, "lookthrough")["IS3N"]["status"], "WRITTEN")  # the UK page says EIMI, not IS3N
        (self.dir / lt.MAPPING_NAME).write_bytes((TOOLS / "universe" / lt.MAPPING_NAME).read_bytes())
        allocation = lt.read_layers(self.dir).allocations[IS3N]
        self.assertEqual((allocation.as_of, round(allocation.total, 2)), ("2026-10-13", 100.0))
        self.assertIn("exposure breakdown, sector, as of 13 Oct 2026", allocation.source)
        self.assertEqual(self.stored(self.lpath)["etfs"][XUSE], stored_lookthrough()["etfs"][XUSE])  # untouched
        self.assertTrue(self.lpath.read_text(encoding="utf-8").startswith('{\n  "map_id"'))  # the indented layout is kept

    def test_the_parsers_are_strict_about_the_provider_format(self) -> None:
        good = ishares_csv(equal_rows(5))
        as_of, rows = er.parse_ishares_holdings(good)
        self.assertEqual((as_of, len(rows)), ("2026-10-13", 6))
        self.assertEqual(rows[-1]["asset_class"], "Cash")  # the footer after the blank line is not data
        for label, raw in (("a web page", b"<!doctype html><html></html>"), ("changed column", ishares_csv(equal_rows(5), header=HEADER.replace("Weight (%)", "Weight"))),
                           ("no date line", good.replace(b"Fund Holdings as of", b"Holdings as of")), ("bad date", ishares_csv(equal_rows(5), as_of="2026-10-13")),
                           ("extra column", good.replace(b'"EUR"\n', b'"EUR","x"\n', 1)), ("text weight", ishares_csv([("A", "B", "n/a")]))):
            with self.subTest(label):
                with self.assertRaises(er.RefreshError) as ctx:
                    er.parse_ishares_holdings(raw)
                self.assertEqual(ctx.exception.code, "FORMAT_CHANGED")


class SpxsTests(RefreshCase):
    def test_spxs_uses_the_index_list_and_never_normalises_the_weights(self) -> None:
        report = self.run_refresh(write=True, only=["SPXS"], what=[er.HOLDINGS])
        row = self.rows(report, "holdings")["SPXS"]
        self.assertEqual(row["status"], "WRITTEN")
        self.assertTrue(any("NOT_NORMALISED" in w for w in row["warnings"]))
        self.assertTrue(all("/holdings/fund" not in u for u in self.provider.urls))  # never the collateral basket
        self.assertIn(SPXS_INDEX_URL, self.provider.urls)
        entry = self.stored(self.hpath)["etfs"][SPXS]
        published = json.loads(good_answers()[SPXS_INDEX_URL])["holdings"]
        self.assertEqual([h["weight"] for h in entry["holdings"]], [h["weight"] for h in published])  # exactly as published
        total = sum(h["weight"] for h in entry["holdings"])
        self.assertGreater(total, 100.0)
        self.assertAlmostEqual(row["weights_total"], round(total, 4), places=3)
        self.assertEqual(entry["kind"], "synthetic_index")
        self.assertTrue(all(h["isin"] and not h["ticker"] and h["asset_class"] == "Equity" for h in entry["holdings"]))
        self.assertIn("NOT scaled", entry["note"])
        self.assertIn(f"{total:.2f} %", entry["note"])  # the note carries the current figures, not stale ones
        self.assertEqual(entry["as_of"], "2026-08-31")

    def test_the_excess_over_100_percent_stays_a_rounding_drift_not_a_company(self) -> None:
        self.run_refresh(write=True, only=["SPXS"], what=[er.HOLDINGS])
        layers = eh.read_layers(self.dir)
        entry = layers.etfs[SPXS]
        self.assertGreater(entry.total_weight, 100.0)
        self.assertEqual(entry.kind, eh.SYNTHETIC_INDEX)
        self.assertEqual(eh.status_of(layers, SPXS, "2026-10-15")[0], eh.APPLIED)

    def test_the_wrong_fund_behind_the_isin_is_refused(self) -> None:
        answers = good_answers()
        answers[SPXS_FUND_URL] = invesco_class("Invesco NASDAQ-100 UCITS ETF Acc")
        report = self.run_refresh(answers, write=True, only=["SPXS"], what=[er.HOLDINGS])
        self.assertEqual(self.rows(report, "holdings")["SPXS"]["status"], "SKIPPED")
        self.assertIn("NOT_THE_FUND", self.rows(report, "holdings")["SPXS"]["errors"][0])
        self.assertEqual(self.stored(self.hpath)["etfs"][SPXS], stored_payload()["etfs"][SPXS])

    def test_the_sector_allocation_of_spxs_is_not_automated_and_says_how_old_it_is(self) -> None:
        report = self.run_refresh(write=True, what=[er.LOOKTHROUGH])
        row = self.rows(report, "lookthrough")["SPXS"]
        self.assertEqual(row["status"], "NOT_AUTOMATED")
        self.assertIn("76 days old", row["warnings"][0])
        self.assertEqual(self.stored(self.lpath)["etfs"][SPXS], stored_lookthrough()["etfs"][SPXS])
        self.assertTrue(all("invesco" not in u or "lookthrough" not in u for u in self.provider.urls))

    def test_invalid_index_answers(self) -> None:
        for label, raw in (("html", b"<html>"), ("no isin", json.dumps({"effectiveDate": "2026-08-31", "holdings": [{"name": "A", "weight": 1.0}]}).encode()),
                           ("no date", json.dumps({"holdings": []}).encode())):
            with self.subTest(label):
                with self.assertRaises(er.RefreshError):
                    er.parse_invesco_index(raw)
        errors, _ = er.validate_holdings(er.SPECS[2], "2026-08-31", [{"name": "A", "ticker": "", "isin": "", "weight": 100.0, "asset_class": "Equity"}] * 500, None, NOW.date())
        self.assertTrue(any("IDENTIFIER_MISSING" in e for e in errors))
        self.assertTrue(any("DUPLICATE_ROWS" in e for e in errors))


class ValidationTests(RefreshCase):
    def reject(self, answers: dict, code: str, **kwargs) -> dict:
        before = (self.digest(self.hpath), self.digest(self.lpath))
        report = self.run_refresh(answers, write=True, **kwargs)
        row = self.rows(report, "holdings")["XUSE"] if er.HOLDINGS in kwargs.get("what", [er.HOLDINGS]) else self.rows(report, "lookthrough")["XUSE"]
        self.assertIn(row["status"], ("REJECTED", "SKIPPED"), row)
        self.assertTrue(any(code in e for e in row["errors"]), row["errors"])
        self.assertFalse(row["written"])
        self.assertEqual((self.digest(self.hpath), self.digest(self.lpath)), before)  # the old files are unchanged
        self.assertEqual(self.leftovers(), [])
        return report

    def test_a_broken_or_empty_provider_answer_leaves_the_files_untouched(self) -> None:
        for label, value, code in (("empty", er.RefreshError("EMPTY", "empty answer"), "EMPTY"), ("http", er.RefreshError("HTTP_ERROR", "503"), "HTTP_ERROR"),
                                   ("html instead of csv", b"<!doctype html><html></html>", "FORMAT_CHANGED"), ("header only", ("Fund Holdings as of,\"13/Oct/2026\"\n\n" + HEADER + "\n").encode(), "EMPTY")):
            with self.subTest(label):
                self.reject({**good_answers(), XUSE_CSV_URL: value}, code, only=["XUSE"], what=[er.HOLDINGS])

    def test_a_changed_format_is_an_error_not_a_guess(self) -> None:
        self.reject({**good_answers(), XUSE_CSV_URL: ishares_csv(equal_rows(60, "CO", 99.8), header=HEADER.replace("Exchange", "Venue"))}, "FORMAT_CHANGED", only=["XUSE"], what=[er.HOLDINGS])
        self.reject({**good_answers(), XUSE_PAGE_URL: ishares_page("XUSE", sector_values()).replace(b"exposureBreakdowns.sector", b"exposureBreakdowns.sectors")}, "FORMAT_CHANGED",
                    only=["XUSE"], what=[er.LOOKTHROUGH])

    def test_a_collapsed_list_a_different_fund_an_older_date_and_duplicates_are_rejected(self) -> None:
        self.reject({**good_answers(), XUSE_CSV_URL: ishares_csv(equal_rows(20, "CO", 99.8))}, "HOLDINGS_COUNT_DROPPED", only=["XUSE"], what=[er.HOLDINGS])
        self.reject({**good_answers(), XUSE_CSV_URL: ishares_csv(equal_rows(60, "ZZ", 99.8))}, "NOT_THE_FUND", only=["XUSE"], what=[er.HOLDINGS])
        self.reject({**good_answers(), XUSE_CSV_URL: ishares_csv(equal_rows(60, "CO", 99.8), as_of="01/Oct/2026")}, "OLDER_THAN_STORED", only=["XUSE"], what=[er.HOLDINGS])
        self.reject({**good_answers(), XUSE_CSV_URL: ishares_csv(equal_rows(60, "CO", 99.8) + [equal_rows(60, "CO", 99.8)[3]])}, "DUPLICATE_ROWS", only=["XUSE"], what=[er.HOLDINGS])
        self.reject({**good_answers(), XUSE_CSV_URL: ishares_csv(equal_rows(60, "CO", 99.8), as_of="20/Oct/2026")}, "DATE_IN_FUTURE", only=["XUSE"], what=[er.HOLDINGS])

    def test_weights_that_do_not_add_up_are_rejected_by_the_loaders_own_check(self) -> None:
        self.reject({**good_answers(), XUSE_CSV_URL: ishares_csv(equal_rows(60, "CO", 60.0))}, "LOADER_REJECTS", only=["XUSE"], what=[er.HOLDINGS])
        self.reject({**good_answers(), XUSE_PAGE_URL: ishares_page("XUSE", sector_values(0.5))}, "LOADER_REJECTS", only=["XUSE"], what=[er.LOOKTHROUGH])

    def test_identifiers_the_matching_needs_must_not_get_lost(self) -> None:
        lost = [(t if i % 2 else "", nm, w) for i, (t, nm, w) in enumerate(equal_rows(60, "CO", 99.8))]
        self.reject({**good_answers(), XUSE_CSV_URL: ishares_csv(lost)}, "IDENTIFIER_LOST", only=["XUSE"], what=[er.HOLDINGS])

    def test_a_page_of_another_fund_is_not_taken_as_the_sector_allocation(self) -> None:
        self.reject({**good_answers(), XUSE_PAGE_URL: ishares_page("EXSA", sector_values())}, "NOT_THE_FUND", only=["XUSE"], what=[er.LOOKTHROUGH])

    def test_stale_and_fresh_data_are_told_apart(self) -> None:
        fresh = self.run_refresh(only=["XUSE"], what=[er.HOLDINGS])
        self.assertEqual(self.rows(fresh, "holdings")["XUSE"]["warnings"], [])
        answers = {**good_answers(), XUSE_CSV_URL: ishares_csv(equal_rows(60, "CO", 99.8), as_of="13/Apr/2026")}
        self.hpath.write_text(json.dumps({**stored_payload(), "etfs": {**stored_payload()["etfs"], XUSE: {**stored_payload()["etfs"][XUSE], "as_of": "2026-03-01"}}}), encoding="utf-8")
        stale = self.run_refresh(answers, only=["XUSE"], what=[er.HOLDINGS])
        row = self.rows(stale, "holdings")["XUSE"]
        self.assertEqual(row["status"], "READY")  # newer than the stored one, but the provider's data itself is old
        self.assertTrue(any("STALE_SOURCE" in w and "185 days" in w for w in row["warnings"]), row["warnings"])


def noisy_is3n_csv(fx_rows: int = 887, as_of: str = "14/Oct/2026") -> bytes:
    """An IS3N-like list: 60 equities, one equity with a published weight of 0.00, one tiny but not zero, cash lines (one negative, one zero), a future and many FX rows with weight 0."""
    lines = [f'Fund Holdings as of,"{as_of}"', "", HEADER]
    for ticker, name, weight in equal_rows(60, "EM", 99.3):
        lines.append(f'"{ticker}","{name}","Financials","Equity","1,000.00","{weight}","1,000.00","10.00","10.00","Germany","Xetra","EUR"')
    lines.append('"ZERO","ZERO WEIGHT CORP","Financials","Equity","12.00","0.00","12.00","1.00","12.00","Germany","Xetra","EUR"')
    lines.append('"TINY","TINY WEIGHT CORP","Financials","Equity","12.00","0.001","12.00","1.00","12.00","Germany","Xetra","EUR"')
    lines.append('"USD","USD CASH","Cash and/or Derivatives","Cash","1.00","0.53","1.00","1.00","1.00","United States","-","USD"')
    lines.append('"TWD","TWD CASH","Cash and/or Derivatives","Cash","-1.00","-0.08","-1.00","1.00","1.00","Taiwan","-","TWD"')
    lines.append('"EUR","EUR CASH","Cash and/or Derivatives","Cash","0.00","0.00","0.00","1.00","1.00","Germany","-","EUR"')
    lines.append('"MFSZ6","MSCI EM INDEX DEC 26","Cash and/or Derivatives","Futures","0.00","0.00","19,620,720.00","126.00","3,114.40","-","Ice Futures U.S.","USD"')
    for i in range(fx_rows):
        lines.append(f'"FX{i}","USD/CUR{i}","Cash and/or Derivatives","FX","-1.00","0.00","-1.00","-1.00","1.00","United States","-","USD"')
    lines += ["", '"The content contained herein is owned by BlackRock."']
    return "\n".join(lines).encode("utf-8")


class ZeroNoiseTests(RefreshCase):
    def refresh_noisy(self, **kwargs) -> dict:
        return self.run_refresh({**good_answers(), IS3N_CSV_URL: noisy_is3n_csv()}, write=True, only=["IS3N"], what=[er.HOLDINGS], **kwargs)

    def test_only_non_equity_rows_with_a_weight_of_exactly_zero_are_left_out(self) -> None:
        report = self.refresh_noisy()
        row = self.rows(report, "holdings")["IS3N"]
        self.assertEqual(row["status"], "WRITTEN")
        self.assertEqual(row["provider_rows"], 953)  # 62 equities, 3 cash lines, 1 future, 887 FX rows
        self.assertEqual(row["omitted_zero_weight_rows"], {"Cash": 1, "FX": 887, "Futures": 1})
        self.assertEqual(row["holdings"], 64)  # 62 equity rows and the two cash lines that carry a weight
        entry = self.stored(self.hpath)["etfs"][IS3N]
        classes = [h["asset_class"] for h in entry["holdings"]]
        self.assertEqual((classes.count("Equity"), classes.count("Cash"), classes.count("FX"), classes.count("Futures")), (62, 2, 0, 0))
        self.assertIn("ZERO WEIGHT CORP", {h["name"] for h in entry["holdings"]})  # an equity with a rounded weight of 0.00 still names a company: it stays
        self.assertEqual(next(h for h in entry["holdings"] if h["name"] == "TINY WEIGHT CORP")["weight"], 0.001)  # tiny but not zero: kept
        self.assertEqual([h["weight"] for h in entry["holdings"] if h["asset_class"] == "Cash"], [0.53, -0.08])  # a negative cash line is exposure information
        self.assertEqual(row["zero_weight_equity_rows"], 1)

    def test_the_providers_sum_is_documented_and_not_changed(self) -> None:
        report = self.refresh_noisy()
        row = self.rows(report, "holdings")["IS3N"]
        raw_sum = sum(h["weight"] for h in er.parse_ishares_holdings(noisy_is3n_csv())[1])
        entry = self.stored(self.hpath)["etfs"][IS3N]
        self.assertAlmostEqual(raw_sum, sum(h["weight"] for h in entry["holdings"]), places=9)
        self.assertAlmostEqual(row["weights_total"], round(raw_sum, 4), places=4)
        self.assertEqual((entry["provider_rows"], entry["omitted_zero_weight_rows"]), (953, {"Cash": 1, "FX": 887, "Futures": 1}))  # the file says what was left out
        self.assertEqual(row["changes"]["provider_rows"], 953)
        self.assertEqual(eh.read_layers(self.dir).etfs[IS3N].total_weight, entry and sum(h["weight"] for h in entry["holdings"]))

    def test_the_stored_holdings_shrink(self) -> None:
        report = self.refresh_noisy()
        info = report["files"][er.HOLDINGS_FILE]
        self.assertEqual(info["bytes_after"], self.hpath.stat().st_size)
        stored = json.dumps(self.stored(self.hpath)["etfs"][IS3N]["holdings"], separators=(",", ":"))
        raw = json.dumps(er.parse_ishares_holdings(noisy_is3n_csv())[1], separators=(",", ":"))
        self.assertLess(len(stored), len(raw) / 5)  # 64 rows instead of 953

    def test_an_older_file_with_the_zero_rows_does_not_make_the_new_list_look_collapsed(self) -> None:
        payload = stored_payload()
        payload["etfs"][IS3N]["holdings"] += [{"name": f"USD/CUR{i}", "ticker": f"FX{i}", "weight": 0.0, "asset_class": "FX", "sector": "Cash and/or Derivatives", "location": "United States",
                                               "exchange": "-"} for i in range(900)]
        self.hpath.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        report = self.run_refresh({**good_answers(), IS3N_CSV_URL: ishares_csv(equal_rows(60, "EM", 99.8))}, write=True, only=["IS3N"], what=[er.HOLDINGS])
        row = self.rows(report, "holdings")["IS3N"]
        self.assertEqual(row["status"], "WRITTEN", row["errors"])  # 61 rows against 960 stored would look like a collapse without the filter on the stored side
        self.assertEqual(row["changes"]["old"]["count"], 960)  # the stored count is what is in the file; the comparison ignores its zero rows

    def test_the_same_list_again_is_unchanged(self) -> None:
        self.refresh_noisy()
        digest = self.digest(self.hpath)
        report = self.run_refresh({**good_answers(), IS3N_CSV_URL: noisy_is3n_csv()}, write=True, only=["IS3N"], what=[er.HOLDINGS])
        self.assertEqual(self.rows(report, "holdings")["IS3N"]["status"], "UNCHANGED")
        self.assertEqual(self.digest(self.hpath), digest)

    def test_a_list_made_only_of_zero_noise_is_still_an_error(self) -> None:
        rows = [f'"FX{i}","USD/CUR{i}","Cash and/or Derivatives","FX","-1.00","0.00","-1.00","-1.00","1.00","US","-","USD"' for i in range(80)]
        only_noise = "\n".join(['Fund Holdings as of,"14/Oct/2026"', "", HEADER] + rows).encode()
        report = self.run_refresh({**good_answers(), IS3N_CSV_URL: only_noise}, write=True, only=["IS3N"], what=[er.HOLDINGS])
        row = self.rows(report, "holdings")["IS3N"]
        self.assertEqual(row["status"], "REJECTED")
        self.assertFalse(row["written"])

    def test_spxs_and_the_clean_ishares_lists_are_untouched_by_the_filter(self) -> None:
        report = self.run_refresh(write=True, what=[er.HOLDINGS])
        self.assertEqual(self.rows(report, "holdings")["SPXS"]["omitted_zero_weight_rows"], {})
        data = self.stored(self.hpath)
        self.assertNotIn("omitted_zero_weight_rows", data["etfs"][SPXS])
        published = json.loads(good_answers()[SPXS_INDEX_URL])["holdings"]
        self.assertEqual([h["weight"] for h in data["etfs"][SPXS]["holdings"]], [h["weight"] for h in published])  # still exactly as published, not normalised
        self.assertGreater(sum(h["weight"] for h in data["etfs"][SPXS]["holdings"]), 100.0)
        self.assertNotIn("omitted_zero_weight_rows", data["etfs"][XUSE])
        self.assertEqual(data["aliases"], stored_payload()["aliases"])
        self.assertEqual(data["issuer_groups"], stored_payload()["issuer_groups"])


class ExposureUnchangedTests(unittest.TestCase):
    def test_effective_exposure_is_identical_with_and_without_the_zero_rows(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_effective_exposure import ETF1, ETF2, ExposureCase, etf_entry, holding

        class Case(ExposureCase):
            def runTest(self) -> None:  # driven by the test below
                pass

        case = Case()
        case.setUp()
        try:
            rows = [holding("Pos Corp", 10.0, isin="US0000POS001"), holding("Other Inc", 60.0, isin="US0000OTH001"), holding("USD CASH", 5.0, asset="Cash"), holding("Third", 25.0, isin="US0000THR001")]
            noise = [holding(f"USD/CUR{i}", 0.0, ticker=f"FX{i}", asset="FX") for i in range(50)] + [holding("EUR CASH", 0.0, asset="Cash"), holding("INDEX FUT", 0.0, asset="Futures")]
            second = etf_entry([holding("Pos Corp", 5.0, isin="US0000POS001"), holding("Other Inc", 95.0, isin="US0000OTH001")])
            case.write({ETF1: etf_entry(rows + noise), ETF2: second})
            noisy = case.run_ee()
            kept, omitted = er.split_zero_noise(rows + noise)
            self.assertEqual(sum(omitted.values()), 52)
            case.write({ETF1: etf_entry(kept), ETF2: second})
            clean = case.run_ee()
            for key in ("top", "buckets", "guard", "company_count", "top_share_pct", "total_market_value_eur", "warnings"):
                self.assertEqual(noisy[key], clean[key], key)
            for field in ("etf_value_eur", "resolved_etf_value_eur", "other_value_eur", "rounding_drift_eur"):
                self.assertEqual(noisy["coverage"][field], clean["coverage"][field], field)
            self.assertTrue(noisy["checks"]["reconciles"] and clean["checks"]["reconciles"])
            n_etf, c_etf = (next(e for e in r["etfs"] if e["symbol"] == "ETF") for r in (noisy, clean))
            self.assertEqual(n_etf["holdings_count"] - c_etf["holdings_count"], 52)  # the only figure that moves: the number of listed rows
            self.assertEqual((n_etf["other_weight_pct"], n_etf["weights_total_pct"]), (c_etf["other_weight_pct"], c_etf["weights_total_pct"]))
        finally:
            case.tearDown()


class AtomicTests(RefreshCase):
    def test_an_error_during_the_replacement_leaves_the_old_file_and_no_debris(self) -> None:
        before = self.digest(self.hpath)
        with mock.patch.object(er.os, "replace", side_effect=OSError("disk full")):
            report = self.run_refresh(write=True, only=["XUSE"], what=[er.HOLDINGS])
        info = report["files"][er.HOLDINGS_FILE]
        self.assertFalse(info["written"])
        self.assertIn("WRITE_FAILED", info["error"])
        self.assertEqual(self.rows(report, "holdings")["XUSE"]["status"], "NOT_WRITTEN")
        self.assertEqual(self.digest(self.hpath), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted([er.LOOKTHROUGH_FILE, er.HOLDINGS_FILE]))  # no candidate, no backup left over

    def test_a_candidate_the_loader_cannot_read_back_is_never_moved_into_place(self) -> None:
        before = self.digest(self.hpath)
        with mock.patch.object(er, "_verify_holdings", side_effect=er.RefreshError("VERIFY_FAILED", "not readable")):
            report = self.run_refresh(write=True, only=["XUSE"], what=[er.HOLDINGS])
        self.assertFalse(report["files"][er.HOLDINGS_FILE]["written"])
        self.assertEqual(self.digest(self.hpath), before)
        self.assertEqual(self.leftovers(), [])

    def test_the_verification_catches_lost_aliases(self) -> None:
        payload = stored_payload()
        candidate = self.dir / "candidate.json"
        candidate.write_text(json.dumps({**payload, "aliases": []}), encoding="utf-8")
        with self.assertRaises(er.RefreshError):
            er._verify_holdings(candidate, set(), payload)

    def test_holdings_and_lookthrough_are_independent(self) -> None:
        answers = good_answers()
        answers[XUSE_PAGE_URL] = er.RefreshError("HTTP_ERROR", "503")
        answers[IS3N_PAGE_URL] = er.RefreshError("HTTP_ERROR", "503")
        lt_before = self.digest(self.lpath)
        report = self.run_refresh(answers, write=True)
        self.assertTrue(report["files"][er.HOLDINGS_FILE]["written"])
        self.assertFalse(report["files"][er.LOOKTHROUGH_FILE]["written"])
        self.assertEqual(self.digest(self.lpath), lt_before)  # no sector data arrived: the stored one is not overwritten
        self.assertEqual(self.stored(self.hpath)["etfs"][XUSE]["as_of"], "2026-10-13")
        answers = good_answers()
        answers[XUSE_CSV_URL] = er.RefreshError("HTTP_ERROR", "503")
        answers[IS3N_CSV_URL] = er.RefreshError("HTTP_ERROR", "503")
        answers[SPXS_INDEX_URL] = er.RefreshError("HTTP_ERROR", "503")
        h_before = self.digest(self.hpath)
        report = self.run_refresh(answers, write=True)
        self.assertFalse(report["files"][er.HOLDINGS_FILE]["written"])
        self.assertEqual(self.digest(self.hpath), h_before)
        self.assertTrue(report["files"][er.LOOKTHROUGH_FILE]["written"])

    def test_one_failing_etf_does_not_block_the_others(self) -> None:
        answers = {**good_answers(), IS3N_CSV_URL: ishares_csv(equal_rows(5, "EM", 99.8))}
        report = self.run_refresh(answers, write=True, what=[er.HOLDINGS])
        rows = self.rows(report, "holdings")
        self.assertEqual((rows["XUSE"]["status"], rows["IS3N"]["status"], rows["SPXS"]["status"]), ("WRITTEN", "REJECTED", "WRITTEN"))
        data = self.stored(self.hpath)
        self.assertEqual(data["etfs"][IS3N], stored_payload()["etfs"][IS3N])  # the failing one keeps its stored entry

    def test_the_same_data_again_changes_nothing(self) -> None:
        self.run_refresh(write=True)
        digests = (self.digest(self.hpath), self.digest(self.lpath))
        names_before = sorted(p.name for p in self.dir.iterdir())
        report = self.run_refresh(write=True)
        self.assertEqual({r["status"] for r in report["etfs"] if r["dataset"] == "holdings"}, {"UNCHANGED"})
        self.assertEqual((self.digest(self.hpath), self.digest(self.lpath)), digests)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), names_before)  # no second backup

    def test_without_a_stored_file_the_first_refresh_creates_it(self) -> None:
        self.hpath.unlink()
        report = self.run_refresh(write=True, only=["XUSE"], what=[er.HOLDINGS])
        self.assertEqual(self.rows(report, "holdings")["XUSE"]["status"], "WRITTEN")
        self.assertEqual(list(self.stored(self.hpath)["etfs"]), [XUSE])


class CliTests(RefreshCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        spec = importlib.util.spec_from_file_location("refresh_cli_test", TOOLS / "Refresh-TradingEtfData.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        provider = Provider(good_answers())
        out = io.StringIO()
        with mock.patch.object(er, "fetch", lambda url, timeout=0: provider(url)), contextlib.redirect_stdout(out):
            code = cli.main(["--universe-dir", str(self.dir), *argv])
        return code, out.getvalue()

    def test_the_dry_run_prints_the_report_and_writes_nothing(self) -> None:
        before = self.digest(self.hpath)
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("dry run - nothing is written", text)
        self.assertIn("XUSE  holdings    READY", text)
        self.assertIn("sum of the published weights", text)
        self.assertRegex(text, r"etf_holdings_local.json: [\d,]+ -> [\d,]+ bytes; written=no")
        self.assertEqual(self.digest(self.hpath), before)

    def test_write_with_json(self) -> None:
        code, text = self.run_cli("--write", "--json", "--only", "XUSE", "--what", "holdings")
        report = json.loads(text)
        self.assertEqual((code, report["write"], report["files"][er.HOLDINGS_FILE]["written"]), (0, True, True))
        self.assertEqual(self.stored(self.hpath)["etfs"][XUSE]["as_of"], "2026-10-13")


if __name__ == "__main__":
    unittest.main()
