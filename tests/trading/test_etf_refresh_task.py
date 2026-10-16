"""The scheduled ETF refresh: the task definition (weekly, Sunday, after Fundamentals, no collision), the exit code that makes provider / parser problems visible to the scheduler,
and the ``etf_data`` source of ``get_data_freshness`` that the Tasks page shows; synthetic data only (no network)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "trading"
for path in (TOOLS, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import data_freshness as df  # noqa: E402
import etf_refresh as er  # noqa: E402
from test_etf_refresh import (IS3N, IS3N_CSV_URL, IS3N_PAGE_URL, NOW, SPXS, SPXS_FUND_URL, SPXS_INDEX_URL, XUSE, XUSE_CSV_URL, XUSE_PAGE_URL, Provider, good_answers,  # noqa: E402
                              ishares_csv, equal_rows, stored_lookthrough, stored_payload)

PS1 = (ROOT / "scheduler" / "Manage-TradingTasks.ps1").read_text(encoding="utf-8-sig")


def jobs() -> dict[str, dict[str, str]]:
    found = {}
    for block in re.findall(r"@\{ Name = '([^']+)';(.*?)\}(?:,|\s*\n\))", PS1, flags=re.S):
        found[block[0]] = dict(re.findall(r"(\w+) = ('[^']*'|@\([^)]*\)|\$\w+|\d+)", block[1]))
    return found


class TaskDefinitionTests(unittest.TestCase):
    def test_the_seventh_task_is_the_etf_refresh(self) -> None:
        all_jobs = jobs()
        self.assertEqual(list(all_jobs)[-1], "ETF-Refresh")
        self.assertEqual(len(all_jobs), 7)
        job = all_jobs["ETF-Refresh"]
        self.assertEqual(job["Script"], "'Refresh-TradingEtfData.py'")
        self.assertEqual(job["Args"], "'--write'")  # the productive call; no retry flag, no follow-up
        self.assertEqual(job["Log"], "'etf-refresh.log'")  # under C:\\tools\\trading\\logs like the others
        self.assertEqual((job["Kind"], job["At"]), ("'Weekly'", "'11:00'"))
        self.assertEqual(job["Logon"], "$false")  # weekly only, no logon trigger
        self.assertEqual(job["LimitHours"], "1")

    def test_it_runs_on_sundays_after_fundamentals_and_collides_with_nothing(self) -> None:
        all_jobs = jobs()
        weekly = {name: re.findall(r"\d\d:\d\d", job["At"]) for name, job in all_jobs.items() if job["Kind"] == "'Weekly'"}
        self.assertEqual(weekly, {"Fundamentals-SEC": ["10:00"], "ETF-Refresh": ["11:00"]})
        self.assertIn("DaysOfWeek Sunday", PS1)  # the one weekday of all weekly jobs
        times = [(name, t) for name, job in all_jobs.items() for t in re.findall(r"\d\d:\d\d", job["At"])]
        self.assertEqual([t for _n, t in times].count("11:00"), 1)  # nobody else starts at 11:00, on Sundays or any other day
        self.assertEqual(sorted(t for _n, t in times if "09:00" < t < "12:00"), ["10:00", "11:00"])  # Fundamentals, then (an hour later) the ETF refresh

    def test_the_common_settings_apply(self) -> None:
        register = PS1.split("'Register' {")[1].split("'Enable' {")[0]
        self.assertIn("StartWhenAvailable = $true", register)  # a missed Sunday run is caught up, like the other tasks
        self.assertIn("MultipleInstances = 'IgnoreNew'", register)  # never parallel with itself
        self.assertNotIn("RestartCount", PS1)  # the scheduler does not retry either
        self.assertIn("$j.LimitHours", register)
        self.assertIn("New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday", register)
        self.assertNotIn("Start-ScheduledTask", PS1)  # nothing starts another task

    def test_the_script_is_deployed_and_the_status_lists_it(self) -> None:
        manifest = json.loads((ROOT / "deploy" / "deploy-manifest.json").read_text(encoding="utf-8"))
        listed = {name for group in manifest["groups"] for name in group.get("files", [])}
        self.assertTrue({"Refresh-TradingEtfData.py", "etf_refresh.py"} <= listed)
        self.assertIn("foreach ($j in $jobs) { Get-TaskLine", PS1)  # Status, Enable, Disable and Remove walk the same list


class ExitCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / er.HOLDINGS_FILE).write_text(json.dumps(stored_payload(), separators=(",", ":")), encoding="utf-8")
        (self.dir / er.LOOKTHROUGH_FILE).write_text(json.dumps(stored_lookthrough(), indent=2) + "\n", encoding="utf-8")

    def run_cli(self, answers: dict, *argv: str) -> tuple[int, str]:
        spec = importlib.util.spec_from_file_location("refresh_cli_task_test", TOOLS / "Refresh-TradingEtfData.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        provider = Provider(answers)
        out = io.StringIO()
        with mock.patch.object(er, "fetch", lambda url, timeout=0: provider(url)), contextlib.redirect_stdout(out):
            code = cli.main(["--universe-dir", str(self.dir), "--write", *argv])
        return code, out.getvalue()

    def stored(self, name: str) -> dict:
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def test_a_successful_run_exits_with_zero_and_the_manual_spxs_sector_is_no_failure(self) -> None:
        code, text = self.run_cli(good_answers())
        self.assertEqual(code, 0)
        self.assertIn("outcome: OK", text)
        self.assertIn("SPXS  lookthrough NOT_AUTOMATED", text)
        self.assertEqual(self.stored(er.HOLDINGS_FILE)["etfs"][XUSE]["as_of"], "2026-10-13")

    def test_nothing_new_is_a_success_too(self) -> None:
        self.run_cli(good_answers())
        code, text = self.run_cli(good_answers())
        self.assertEqual(code, 0)
        self.assertIn("UNCHANGED", text)

    def test_a_provider_error_fails_the_run_but_the_other_datasets_are_refreshed(self) -> None:
        answers = {**good_answers(), XUSE_CSV_URL: er.RefreshError("HTTP_ERROR", "503")}
        before = self.stored(er.HOLDINGS_FILE)["etfs"][XUSE]
        code, text = self.run_cli(answers)
        self.assertEqual(code, er.EXIT_PARTIAL)
        self.assertIn("outcome: PARTIAL", text)
        self.assertIn("HTTP_ERROR", text)  # the problem is in the log, not only in the exit code
        self.assertEqual(self.stored(er.HOLDINGS_FILE)["etfs"][XUSE], before)  # the old valid entry stays
        self.assertEqual(self.stored(er.HOLDINGS_FILE)["etfs"][IS3N]["as_of"], "2026-10-13")  # the others were refreshed
        self.assertEqual(self.stored(er.LOOKTHROUGH_FILE)["etfs"][XUSE]["as_of"], "2026-10-13")

    def test_a_parser_error_is_a_failure_too(self) -> None:
        answers = {**good_answers(), IS3N_PAGE_URL: b"<html><head><title>iShares | EIMI | iShares</title></head><body>nothing here</body></html>"}
        code, text = self.run_cli(answers)
        self.assertEqual(code, er.EXIT_PARTIAL)
        self.assertIn("FORMAT_CHANGED", text)
        self.assertEqual(self.stored(er.LOOKTHROUGH_FILE)["etfs"][IS3N], stored_lookthrough()["etfs"][IS3N])

    def test_a_rejected_validation_fails_the_run(self) -> None:
        code, text = self.run_cli({**good_answers(), IS3N_CSV_URL: ishares_csv(equal_rows(10, "EM", 99.8))})
        self.assertEqual(code, er.EXIT_PARTIAL)
        self.assertIn("REJECTED", text)

    def test_when_nothing_can_be_refreshed_the_exit_code_says_failed_and_nothing_changes(self) -> None:
        before = ((self.dir / er.HOLDINGS_FILE).read_bytes(), (self.dir / er.LOOKTHROUGH_FILE).read_bytes())
        code, text = self.run_cli({})  # every download fails
        self.assertEqual(code, er.EXIT_FAILED)
        self.assertIn("outcome: FAILED", text)
        self.assertEqual(((self.dir / er.HOLDINGS_FILE).read_bytes(), (self.dir / er.LOOKTHROUGH_FILE).read_bytes()), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted([er.HOLDINGS_FILE, er.LOOKTHROUGH_FILE]))

    def test_a_failed_write_is_a_failure(self) -> None:
        with mock.patch.object(er.os, "replace", side_effect=OSError("disk full")):
            code, text = self.run_cli(good_answers())
        self.assertEqual(code, er.EXIT_FAILED)
        self.assertIn("WRITE_FAILED", text)

    def test_the_outcome_helper(self) -> None:
        def report(*statuses, error=None):
            return {"etfs": [{"status": s} for s in statuses], "files": {"f": {"error": error}}}
        self.assertEqual(er.outcome_of(report("WRITTEN", "UNCHANGED", "NOT_AUTOMATED")), "OK")
        self.assertEqual(er.outcome_of(report("WRITTEN", "SKIPPED")), "PARTIAL")
        self.assertEqual(er.outcome_of(report("SKIPPED", "REJECTED", "NOT_AUTOMATED")), "FAILED")
        self.assertEqual(er.outcome_of(report("UNCHANGED", error="WRITE_FAILED")), "PARTIAL")
        self.assertEqual([er.exit_code({"outcome": o}) for o in ("OK", "PARTIAL", "FAILED")], [0, 3, 2])


class FreshnessSourceTests(unittest.TestCase):
    TODAY = date(2026, 10, 16)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, holdings=None, lookthrough=None) -> None:
        (self.dir / er.HOLDINGS_FILE).write_text(json.dumps(holdings or stored_payload()), encoding="utf-8")
        (self.dir / er.LOOKTHROUGH_FILE).write_text(json.dumps(lookthrough or stored_lookthrough()), encoding="utf-8")

    def facts(self) -> dict:
        return df.etf_data(self.dir, self.TODAY)

    def test_the_six_datasets_with_their_stand_and_age(self) -> None:
        self.write()
        facts = self.facts()
        self.assertEqual(facts["stale_after_days"], 120)  # the loaders' limit, passed on, not redefined
        self.assertEqual([(r["symbol"], r["as_of"], r["age_days"], r["status"]) for r in facts["holdings"]],
                         [("XUSE", "2026-10-12", 4, "APPLIED"), ("IS3N", "2026-10-12", 4, "APPLIED"), ("SPXS", "2026-08-28", 49, "APPLIED")])
        self.assertEqual([(r["symbol"], r["as_of"], r["manual"]) for r in facts["lookthrough"]], [("XUSE", "2026-10-12", False), ("IS3N", "2026-10-12", False), ("SPXS", "2026-07-31", True)])
        self.assertEqual(facts["holdings"][0]["rows"], 60)

    def test_stale_missing_and_invalid_entries_are_told_apart(self) -> None:
        holdings, lookthrough = stored_payload(), stored_lookthrough()
        holdings["etfs"][XUSE]["as_of"] = "2026-05-01"  # 168 days
        del holdings["etfs"][IS3N]
        holdings["etfs"][SPXS]["holdings"] = holdings["etfs"][SPXS]["holdings"][:3]  # the weights no longer add up
        lookthrough["etfs"][SPXS]["as_of"] = "2026-04-01"  # the manual one, 198 days
        self.write(holdings, lookthrough)
        facts = self.facts()
        self.assertEqual([r["status"] for r in facts["holdings"]], ["STALE", "MISSING", "INVALID"])
        self.assertIn("weights add up", facts["holdings"][2]["detail"])
        self.assertEqual((facts["lookthrough"][2]["status"], facts["lookthrough"][2]["manual"], facts["lookthrough"][2]["age_days"]), ("STALE", True, 198))

    def test_missing_files_and_unreadable_files_do_not_raise(self) -> None:
        facts = self.facts()
        self.assertEqual({r["status"] for r in facts["holdings"] + facts["lookthrough"]}, {"MISSING"})
        (self.dir / er.HOLDINGS_FILE).write_text("{not json", encoding="utf-8")
        facts = self.facts()
        self.assertEqual(facts["problems"], [f"{er.HOLDINGS_FILE}: JSONDecodeError"])

    def test_it_is_part_of_the_freshness_answer_and_isolated_from_the_other_sources(self) -> None:
        self.write()
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        modules = SimpleNamespace(read_latest_report=lambda directory: (None, "NO_REPORT", []))
        result = df.build_data_freshness(conn, discovery_module=modules, intelligence_module=modules, discovery_dir=Path("d"), intelligence_dir=Path("i"), universe_dir=self.dir,
                                         now=datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
        self.assertTrue(result["ok"])
        self.assertTrue(result["sources"]["etf_data"]["available"])
        self.assertEqual(len(result["sources"]["etf_data"]["holdings"]), 3)
        self.assertFalse(result["sources"]["market_data"]["available"])  # the empty database does not hide the ETF data (and the other way round)
        with mock.patch.object(df, "etf_data", side_effect=RuntimeError("boom")):
            broken = df.build_data_freshness(conn, discovery_module=modules, intelligence_module=modules, discovery_dir=Path("d"), intelligence_dir=Path("i"), universe_dir=self.dir)
        self.assertFalse(broken["sources"]["etf_data"]["available"])


if __name__ == "__main__":
    unittest.main()
