"""Discovery report (CLI writes) and MCP reader (get_candidate_discovery reads): atomic, validated, read-only, no network."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import inspect
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discovery_report as dr  # noqa: E402
import test_candidate_discovery as tcd  # noqa: E402  (module import: its tests are not collected twice)
from candidate_discovery import discover_watchlist_candidates  # noqa: E402

NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = load_module("discover_cli_report", ROOT / "tools" / "trading" / "Discover-TradingCandidates.py")
tools_module = load_module("trading_sqlite_report_test", ROOT / "mcp-tools" / "trading_sqlite.py")
Tools = tools_module.Tools
# the reader loads the deployed helper by name; point the cache at the repo module
Tools._runtime_module_cache["discovery_report"] = dr

UNIVERSE = [
    {"symbol": "UPA", "name": "Up A", "market": "US", "currency": "USD"},
    {"symbol": "UPB", "name": "Up B", "market": "US", "currency": "USD"},
    {"symbol": "PULL", "name": "Pull", "market": "US", "currency": "USD"},
    {"symbol": "DOWN", "name": "Down", "market": "US", "currency": "USD"},
    {"symbol": "THIN", "name": "Thin", "market": "US", "currency": "USD"},
    {"symbol": "HELD", "name": "Held", "market": "US", "currency": "USD"},
    {"symbol": "WATCHED", "name": "Watched", "market": "US", "currency": "USD"},
    {"symbol": "SWISS", "name": "Swiss", "market": "CH", "currency": "CHF"},
]


def histories():
    return {
        "UPA": tcd.history(daily=0.003), "UPB": tcd.history(daily=0.0015), "PULL": tcd.history(tail_drop=0.85),
        "DOWN": tcd.history(daily=-0.003, start=300.0), "THIN": tcd.history(volume=100.0),
    }


class Workspace:
    """Temp dir with a production-shaped DB file, a universe file and a report directory."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "trading.db"
        prod = tcd.make_prod()
        disk = sqlite3.connect(self.db)
        prod.backup(disk)
        disk.close()
        prod.close()
        self.universe = self.dir / "universe.json"
        self.universe.write_text(json.dumps({"universe_id": "test_universe", "version": 7, "entries": UNIVERSE}), encoding="utf-8")
        self.reports = self.dir / "candidate-discovery"

    def run_cli(self, *extra, fetch=None, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db-path", str(self.db), "--universe", str(self.universe), "--as-of", tcd.TODAY, "--output-dir", str(self.reports), *extra],
                            fetch_history=fetch or tcd.Fetcher(histories()), now=now)
        return code, out.getvalue(), err.getvalue()

    def db_fingerprint(self):
        conn = sqlite3.connect(f"file:///{self.db.as_posix()}?mode=ro", uri=True)
        try:
            return tcd.fingerprint(conn)
        finally:
            conn.close()

    def close(self):
        self.tmp.cleanup()


class CliReportTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.close()

    def test_successful_run_writes_latest_and_a_history_report(self):
        code, out, _ = self.ws.run_cli()
        self.assertEqual(code, 0)
        files = sorted(p.name for p in self.ws.reports.iterdir())
        self.assertEqual(files, ["candidate-discovery-20261007-180000.json", "latest.json"])
        self.assertIn("Report:", out)
        latest = json.loads((self.ws.reports / "latest.json").read_text(encoding="utf-8"))
        history = json.loads((self.ws.reports / "candidate-discovery-20261007-180000.json").read_text(encoding="utf-8"))
        self.assertEqual(latest, history)
        self.assertEqual(dr.validate_report(latest), [])

    def test_no_history_option_writes_only_latest(self):
        self.ws.run_cli("--no-history")
        self.assertEqual([p.name for p in self.ws.reports.iterdir()], ["latest.json"])

    def test_report_schema_and_content(self):
        self.ws.run_cli()
        report = json.loads((self.ws.reports / "latest.json").read_text(encoding="utf-8"))
        meta = report["metadata"]
        for key in dr.METADATA_KEYS:
            self.assertIn(key, meta)
        self.assertEqual((meta["schema_version"], meta["universe_name"], meta["universe_size"], meta["deterministic"]), (1, "test_universe", 8, True))
        self.assertEqual(meta["analyzed_count"], len(report["results"]))
        self.assertEqual(meta["excluded_count"], len(report["excluded"]))
        self.assertEqual(sum(meta[k] for k in dr.COUNT_BY_STATUS.values()), meta["analyzed_count"])
        self.assertEqual((meta["source"]["fetched_count"], meta["source"]["cache_hit_count"], meta["source"]["cache_used"]), (5, 0, False))
        self.assertTrue(meta["read_only"] and not meta["watchlist_written"] and not meta["orders_created"])
        for row in report["results"]:
            for key in dr.RESULT_FIELDS:
                self.assertIn(key, row)
            self.assertNotIn("rows", row)
        self.assertEqual({e["symbol"]: e["reason_codes"] for e in report["excluded"]}, {"HELD": ["ALREADY_IN_PORTFOLIO"], "WATCHED": ["ALREADY_ON_WATCHLIST"], "SWISS": ["UNSUPPORTED_CURRENCY"]})
        self.assertEqual({e["symbol"]: e["existing_security_id"] for e in report["excluded"]}["HELD"], 1)
        self.assertNotIn("prices", json.dumps(report))
        self.assertLess(len(json.dumps(report)), 30_000)  # no raw time series

    def test_report_results_equal_the_unchanged_discovery_result(self):
        self.ws.run_cli()
        report = json.loads((self.ws.reports / "latest.json").read_text(encoding="utf-8"))
        conn = tcd.make_prod()
        direct = discover_watchlist_candidates(conn, [tcd.entry(e["symbol"], name=e["name"], market=e["market"], currency=e["currency"]) for e in UNIVERSE],
                                               tcd.Fetcher(histories()), as_of=tcd.TODAY)
        conn.close()
        self.assertEqual([(r["symbol"], r["status"], r["discovery_score"], r["reason_codes"], r["rank"]) for r in report["results"]],
                         [(c["symbol"], c["status"], c["discovery_score"], c["reason_codes"], c["rank"]) for c in direct["candidates"]])
        self.assertEqual([e["symbol"] for e in report["excluded"]], [e["symbol"] for e in direct["excluded_candidates"]])

    def test_results_are_deterministically_ordered_and_reproducible(self):
        self.ws.run_cli("--no-history")
        first = (self.ws.reports / "latest.json").read_text(encoding="utf-8")
        self.ws.run_cli("--no-history", now=NOW + timedelta(hours=1))
        second = json.loads((self.ws.reports / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(first)["results"], second["results"])
        self.assertEqual(json.loads(first)["metadata"]["results_sha256"], second["metadata"]["results_sha256"])
        self.assertNotEqual(json.loads(first)["metadata"]["generated_at"], second["metadata"]["generated_at"])
        statuses = [r["status"] for r in second["results"]]
        self.assertEqual(statuses, sorted(statuses, key=dr.STATUSES.index))

    def test_cli_does_not_touch_the_database(self):
        before = self.ws.db_fingerprint()
        self.ws.run_cli()
        self.assertEqual(before, self.ws.db_fingerprint())

    def test_failed_run_never_destroys_the_previous_report(self):
        self.ws.run_cli()
        good = (self.ws.reports / "latest.json").read_bytes()
        for label, fetch in (("provider down", lambda s: None), ("provider raises", lambda s: (_ for _ in ()).throw(RuntimeError("down")))):
            with self.subTest(label):
                code, _, err = self.ws.run_cli(fetch=fetch, now=NOW + timedelta(days=1))
                self.assertEqual(code, 2)
                self.assertIn("previous report was left untouched", err)
                self.assertEqual((self.ws.reports / "latest.json").read_bytes(), good)
                self.assertEqual(len(list(self.ws.reports.glob("candidate-discovery-*.json"))), 1)

    def test_error_during_the_atomic_replace_keeps_the_old_report_and_leaves_no_temp_file(self):
        self.ws.run_cli()
        good = (self.ws.reports / "latest.json").read_bytes()
        with mock.patch.object(dr.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.ws.run_cli(now=NOW + timedelta(days=1))
        self.assertEqual((self.ws.reports / "latest.json").read_bytes(), good)
        self.assertEqual([p.name for p in self.ws.reports.iterdir() if p.name.endswith(".tmp")], [])

    def test_invalid_report_is_refused_before_replacing(self):
        self.ws.run_cli()
        good = (self.ws.reports / "latest.json").read_bytes()
        report = json.loads(good)
        report["metadata"]["analyzed_count"] += 1
        with self.assertRaises(dr.ReportError):
            dr.write_report(report, self.ws.reports)
        self.assertEqual((self.ws.reports / "latest.json").read_bytes(), good)

    def test_atomic_replace_swaps_the_whole_file(self):
        self.ws.run_cli("--no-history")
        a = json.loads((self.ws.reports / "latest.json").read_text(encoding="utf-8"))
        report = copy.deepcopy(a)
        report["metadata"]["generated_at"] = "2026-10-08T06:00:00+00:00"
        dr.write_report(report, self.ws.reports, history=False)
        b = json.loads((self.ws.reports / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(b["metadata"]["generated_at"], "2026-10-08T06:00:00+00:00")
        self.assertEqual(dr.validate_report(b), [])


class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace()
        cls.ws.run_cli()
        cls.report = json.loads((cls.ws.reports / "latest.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.ws.close()

    def tools(self, workspace=None):
        ws = workspace or self.ws
        instance = Tools()
        instance.valves.database_path = str(ws.db)
        return instance

    def call(self, **kw):
        with mock.patch.object(dr, "datetime", wraps=datetime) as fake:
            fake.now.return_value = NOW
            return asyncio.run(self.tools().get_candidate_discovery(**kw))

    def test_reader_returns_the_report_with_freshness_and_counts(self):
        res = self.call()
        self.assertEqual((res["ok"], res["status"]), (True, "AVAILABLE"))
        self.assertEqual(res["freshness"]["generated_at"], self.report["metadata"]["generated_at"])
        self.assertEqual(res["counts"]["universe_size"], 8)
        self.assertEqual(res["freshness"]["age_hours"], 0.0)
        self.assertFalse(res["report_stale"])

    def test_no_report_means_unavailable_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Tools()
            instance.valves.database_path = str(Path(tmp) / "trading.db")
            res = asyncio.run(instance.get_candidate_discovery())
        self.assertEqual((res["ok"], res["status"], res["reason"]), (True, "UNAVAILABLE", "NO_DISCOVERY_REPORT"))
        self.assertIn("never starts", res["hint"])

    def test_corrupt_or_tampered_report_is_unavailable_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "candidate-discovery"
            directory.mkdir()
            instance = Tools()
            instance.valves.database_path = str(Path(tmp) / "trading.db")
            (directory / "latest.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(asyncio.run(instance.get_candidate_discovery())["reason"], "REPORT_INVALID")
            tampered = copy.deepcopy(self.report)
            tampered["results"][0]["discovery_score"] = 999.0
            (directory / "latest.json").write_text(json.dumps(tampered), encoding="utf-8")
            res = asyncio.run(instance.get_candidate_discovery())
            self.assertEqual((res["status"], res["reason"]), ("UNAVAILABLE", "REPORT_INVALID"))
            self.assertTrue(any("results_sha256" in p for p in res["problems"]))

    def test_old_report_is_not_hidden_but_flagged_with_age_and_dates(self):
        old = copy.deepcopy(self.report)
        old["metadata"]["generated_at"] = "2026-09-20T06:00:00+00:00"
        old["metadata"]["evaluation_as_of"] = "2026-09-20"
        res = dr.query_report(old, now=NOW)
        self.assertTrue(res["report_stale"])
        self.assertEqual(res["freshness"]["status"], "REPORT_STALE")
        self.assertEqual((res["freshness"]["evaluation_age_days"], res["freshness"]["max_age_days"]), (17, 5))
        self.assertGreater(res["freshness"]["age_hours"], 400)
        self.assertTrue(res["notes"][0].startswith("REPORT_STALE"))
        self.assertTrue(res["results"])  # the content is still returned
        self.assertEqual(dr.query_report(old, now=NOW, max_age_days=30)["freshness"]["status"], "FRESH")

    def test_status_filter_and_aliases(self):
        ready = self.call(status="DISCOVERY_READY")
        self.assertTrue(ready["results"])
        self.assertTrue(all(r["status"] == "DISCOVERY_READY" for r in ready["results"]))
        self.assertEqual(self.call(status="ready")["matching"], ready["matching"])
        rejected = self.call(status="REJECTED")
        self.assertTrue(all(r["status"] == "DISCOVERY_REJECTED" for r in rejected["results"]))
        self.assertEqual(self.call(status="ALL")["matching"], len(self.report["results"]))
        bad = self.call(status="BUY")
        self.assertEqual((bad["ok"], bad["error"]), (False, "INVALID_STATUS"))

    def test_excluded_list_shows_the_pre_filter_reasons(self):
        res = self.call(status="EXCLUDED")
        self.assertEqual({r["symbol"]: r["reason_codes"] for r in res["results"]}, {"HELD": ["ALREADY_IN_PORTFOLIO"], "WATCHED": ["ALREADY_ON_WATCHLIST"], "SWISS": ["UNSUPPORTED_CURRENCY"]})

    def test_limit_truncation_and_caps(self):
        res = self.call(limit=2)
        self.assertEqual((res["returned"], res["truncated"], res["filter"]["limit_applied"]), (2, True, 2))
        self.assertEqual(self.call(limit=0)["filter"]["limit_applied"], 1)
        self.assertEqual(self.call(limit=10_000)["filter"]["limit_applied"], dr.MAX_LIMIT_COMPACT)
        self.assertEqual(self.call(limit=10_000, detail=True)["filter"]["limit_applied"], dr.MAX_LIMIT_DETAIL)

    def test_compact_and_detail_fields(self):
        compact = self.call(limit=3)["results"][0]
        self.assertEqual(sorted(k for k in compact if k != "list"), sorted(dr.COMPACT_FIELDS))
        detail = self.call(limit=3, detail=True)
        self.assertEqual(sorted(k for k in detail["results"][0] if k != "list"), sorted(dr.RESULT_FIELDS))
        self.assertIn("methodology", detail)
        self.assertNotIn("methodology", self.call(limit=3))

    def test_single_symbol_explanation(self):
        res = self.call(symbol="upa", detail=True)
        self.assertEqual([r["symbol"] for r in res["results"]], ["UPA"])
        self.assertEqual(res["results"][0]["status"], "DISCOVERY_READY")
        self.assertIn("TREND_INTACT", res["results"][0]["reason_codes"])
        self.assertIn("methodology", res)
        held = self.call(symbol="HELD")
        self.assertEqual(held["results"][0]["reason_codes"], ["ALREADY_IN_PORTFOLIO"])
        self.assertEqual(self.call(symbol="NOPE")["returned"], 0)

    def test_sorting_is_deterministic_and_matches_the_stored_order(self):
        a, b = self.call(), self.call()
        self.assertEqual(json.dumps(a["results"]), json.dumps(b["results"]))
        self.assertEqual([r["rank"] for r in a["results"]], list(range(1, len(a["results"]) + 1)))
        self.assertEqual([r["symbol"] for r in a["results"]], [r["symbol"] for r in self.report["results"]])

    def test_rendered_german_output_and_disclaimers(self):
        text = self.call(limit=5)["rendered_de"]
        for fragment in ("| Rank | Symbol | Name | Discovery-Score | Status | Grund |", "kein neuer Lauf", "weder PROMOTE noch Kauf", "keine automatische Watchlist-Aufnahme"):
            self.assertIn(fragment, text)

    def test_reader_makes_no_external_request_and_never_touches_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "candidate-discovery"
            directory.mkdir()
            (directory / "latest.json").write_text(json.dumps(self.report), encoding="utf-8")
            instance = Tools()
            instance.valves.database_path = str(Path(tmp) / "does-not-exist.db")  # a database access would fail
            with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network access")), \
                 mock.patch("urllib.request.urlopen", side_effect=AssertionError("urlopen")), \
                 mock.patch.object(Tools, "_readonly_import_connection", side_effect=AssertionError("db access")), \
                 mock.patch.object(Tools, "_connect", side_effect=AssertionError("db access")), \
                 mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db access")):
                # the synchronous core carries all logic (the asyncio loop itself uses sockets on Windows)
                res = instance._get_candidate_discovery_sync("READY", 5, True, None)
            self.assertEqual(res["status"], "AVAILABLE")
            # the async tool (same core) while the temporary report directory still exists
            self.assertEqual(asyncio.run(instance.get_candidate_discovery(status="READY", limit=5, detail=True))["status"], "AVAILABLE")

    def test_reader_does_not_change_the_report_the_database_or_the_watchlist(self):
        report_bytes = (self.ws.reports / "latest.json").read_bytes()
        before = self.ws.db_fingerprint()
        self.call(status="ALL", limit=100)
        self.assertEqual((self.ws.reports / "latest.json").read_bytes(), report_bytes)
        self.assertEqual(before, self.ws.db_fingerprint())
        self.assertEqual([p.name for p in self.ws.reports.iterdir() if p.name.endswith(".tmp")], [])

    def test_no_buy_no_order_in_the_reader_output(self):
        text = json.dumps(self.call(status="ALL", limit=100, detail=True)["results"])
        for forbidden in ('"BUY"', "ENTRY_READY", '"PROMOTE"', "order"):
            self.assertNotIn(forbidden, text)

    def test_reader_module_has_no_network_or_engine_imports(self):
        source = Path(dr.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib", "http.client", "requests", "sqlite3", "candidate_discovery", "analysis_engine", "yahoo"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)


class ToolSurfaceTests(unittest.TestCase):
    EXISTING = sorted(["sql_execute", "database_tables", "database_schema", "table_info", "database_status", "rank_watchlist", "run_trading_orchestrator",
                       "evaluate_swing_candidate", "evaluate_swing_candidates", "approve_swing_promotion", "suggest_strategy_assignments", "evaluate_watchlist_candidates"])

    def public(self):
        return sorted(n for n, m in inspect.getmembers(Tools()) if not n.startswith("_") and callable(m) and (inspect.ismethod(m) or inspect.isfunction(m)))

    def test_fifteen_tools_and_the_twelve_original_ones_are_unchanged(self):
        names = self.public()
        self.assertEqual(len(names), 15)
        self.assertEqual(sorted(set(names) - {"get_candidate_discovery", "get_market_intelligence", "get_opportunity_view"}), self.EXISTING)
        self.assertIn("get_candidate_discovery", names)

    def test_the_new_tool_has_no_write_or_run_parameters(self):
        params = list(inspect.signature(Tools().get_candidate_discovery).parameters)
        self.assertEqual(params, ["status", "limit", "detail", "symbol"])
        self.assertTrue(inspect.iscoroutinefunction(Tools().get_candidate_discovery))

    def test_existing_tool_signatures_are_unchanged(self):
        self.assertEqual(list(inspect.signature(Tools().run_trading_orchestrator).parameters), ["as_of"])
        self.assertEqual(list(inspect.signature(Tools().evaluate_swing_candidate).parameters), ["security_id"])

    def test_valve_derives_the_report_directory_from_the_database_path(self):
        t = Tools()
        base = Path(tempfile.gettempdir()) / "trading-layout" / "data"  # path arithmetic only, nothing is created
        t.valves.database_path = str(base / "trading.db")
        self.assertEqual(t._discovery_report_directory(), base / "candidate-discovery")
        elsewhere = Path(tempfile.gettempdir()) / "elsewhere"
        t.valves.discovery_report_dir = str(elsewhere)
        self.assertEqual(t._discovery_report_directory(), elsewhere)


if __name__ == "__main__":
    unittest.main()
