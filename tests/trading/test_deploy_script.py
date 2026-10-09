"""Deploy manifest and Deploy-TradingAgent.ps1: explicit runtime file list, protected private files, and the script's behaviour in a temporary fake production root (never the real one)."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
MANIFEST = json.loads((DEPLOY / "deploy-manifest.json").read_text(encoding="utf-8"))
SCRIPT = DEPLOY / "Deploy-TradingAgent.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
FORBIDDEN_NAME = re.compile(r"(\.(db|sqlite3?|csv|xlsx?|log|bak|pem|key|env)$)|(^\.env)|latest\.json|_local\.|secret|credential|backup", re.I)


def manifest_files() -> list[tuple[str, str]]:
    """(group source, file name) pairs, exactly as listed."""
    return [(g["source"], name) for g in MANIFEST["groups"] for name in g["files"]]


class ManifestTests(unittest.TestCase):
    def test_every_listed_file_exists_once(self) -> None:
        listed = manifest_files()
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual([f"{s}/{n}" for s, n in listed if not (ROOT / s / n).is_file()], [])

    def test_manifest_is_complete_every_runtime_file_is_listed_or_explicitly_not_deployed(self) -> None:
        listed = {f"{s}/{n}" for s, n in manifest_files()}
        candidates = set()
        for folder, pattern in (("tools/trading", "*.py"), ("tools/trading/universe", "*.json"), ("mcp-tools", "*.py"), ("scheduler", "*.ps1"), ("tools", "*.py")):
            candidates |= {f"{folder}/{p.name}" for p in (ROOT / folder).glob(pattern)}
        candidates = {c for c in candidates if not Path(c).name.endswith("_local.json")}
        not_deployed = set(MANIFEST["not_deployed"])
        self.assertEqual(sorted(c for c in candidates if c not in listed and c not in not_deployed), [], "new runtime file: add it to deploy-manifest.json (or to not_deployed)")
        self.assertEqual(sorted(listed & not_deployed), [])

    def test_manifest_lists_no_private_data_log_backup_test_or_doc_paths(self) -> None:
        allowed_targets = {"app", "app/universe", "mcp", "scheduler"}
        for group in MANIFEST["groups"]:
            self.assertIn(group["target"], allowed_targets)
            self.assertFalse(re.search(r"(^|/)(tests|docs|\.git|deploy|data|logs|backup)(/|$)", group["source"]), group["source"])
            for name in group["files"]:
                self.assertNotRegex(name, r"[\\/*?:]", name)
                self.assertIsNone(FORBIDDEN_NAME.search(name), name)

    def test_the_database_is_not_part_of_the_manifest(self) -> None:
        self.assertFalse([n for _s, n in manifest_files() if n.lower().endswith((".db", ".sqlite", ".sqlite3")) or "trading.db" in n.lower()])

    def test_private_local_sector_map_is_protected_and_git_ignored(self) -> None:
        self.assertIn("app/universe/sector_map_local.json", MANIFEST["protected"])
        self.assertFalse([n for _s, n in manifest_files() if "local" in n.lower()])
        self.assertIn("tools/trading/universe/*_local.json", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_expected_tool_count_matches_the_mcp_tools_class(self) -> None:
        spec = importlib.util.spec_from_file_location("trading_sqlite_deploy_test", ROOT / "mcp-tools" / "trading_sqlite.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        public = [name for name, _f in inspect.getmembers(module.Tools, inspect.isfunction) if not name.startswith("_")]
        self.assertEqual(MANIFEST["expected_tool_count"], len(public))
        self.assertEqual(MANIFEST["expected_tool_count"], 15)

    def test_script_never_registers_tasks_runs_collectors_or_migrates(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("Register-ScheduledTask", "Start-ScheduledTask", "Unregister-ScheduledTask", "Collect-Trading", "Discover-Trading", "Backfill-Trading", "Migrate-Trading", "Run-TradingOrchestrator"):
            self.assertNotIn(forbidden, code, forbidden)
        self.assertEqual(text, text.encode("ascii", "ignore").decode("ascii"), "keep the script ASCII-only (Windows PowerShell 5.1 reads it without BOM)")

    def test_database_restore_only_runs_behind_the_explicit_switch(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"Invoke-Helper @\('restore'", text)), 1)
        block = text[text.index("if ($RestoreDb) {\n        $stamp"):]
        self.assertLess(block.index("@('restore'"), block.index("} else { Write-Host \"  database left untouched"))


def snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            result[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return result


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "needs Windows PowerShell / pwsh")
class DeployScriptBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "trading"
        for sub in ("app/universe", "mcp", "scheduler", "data", "backup"):
            (self.root / sub).mkdir(parents=True)
        for group in MANIFEST["groups"]:  # production starts identical to the repo ...
            for name in group["files"]:
                shutil.copy2(ROOT / group["source"] / name, self.root / group["target"] / name)
        self.old_file = self.root / "app" / "opportunity_view.py"  # ... except one older file, one missing file, and private/legacy files
        self.old_file.write_text("# old production version\n", encoding="utf-8")
        (self.root / "app" / "strategy_suggestion.py").unlink()
        self.local_map = self.root / "app" / "universe" / "sector_map_local.json"
        self.local_map.write_text('{"sectors": {"PRIVATE": ["XYZ"]}}', encoding="utf-8")
        self.legacy = self.root / "app" / "Legacy-Only.py"
        self.legacy.write_text("# production-only\n", encoding="utf-8")
        self.db = self.root / "data" / "trading.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE marker(id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO marker(note) VALUES ('original')")
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, *args: str, manifest: Path | None = None, shell: str | None = None) -> subprocess.CompletedProcess:
        command = [shell or POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Root", str(self.root), "-Python", sys.executable, "-SkipSmoke", *args]
        if manifest:
            command += ["-Manifest", str(manifest)]
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)

    def marker_notes(self) -> list[str]:
        conn = sqlite3.connect(self.db)
        try:
            return [row[0] for row in conn.execute("SELECT note FROM marker ORDER BY id")]
        finally:
            conn.close()

    def test_check_changes_nothing_and_reports_the_plan(self) -> None:
        before = snapshot(self.root)
        result = self.run_script("-Action", "Check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CHECK OK", result.stdout)
        self.assertIn("update  app\\opportunity_view.py", result.stdout)
        self.assertIn("new     app\\strategy_suggestion.py", result.stdout)
        self.assertEqual(snapshot(self.root), before)

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell 5.1 not available")
    def test_check_and_dry_run_also_work_in_windows_powershell_5_1(self) -> None:
        # `powershell -File deploy\Deploy-TradingAgent.ps1` is the documented call; 5.1 has an empty $PSScriptRoot inside param()
        before = snapshot(self.root)
        for args in (("-Action", "Check"), ("-Action", "Deploy", "-DryRun")):
            result = self.run_script(*args, shell=shutil.which("powershell"))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_dry_run_changes_nothing(self) -> None:
        before = snapshot(self.root)
        result = self.run_script("-Action", "Deploy", "-DryRun")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(list((self.root / "backup").iterdir()), [])

    def test_deploy_backs_up_copies_only_manifest_files_and_protects_private_files(self) -> None:
        db_before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = self.run_script("-Action", "Deploy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DEPLOY OK", result.stdout)
        self.assertIn("Restart every running MCP process", result.stdout)
        # replaced and recreated files now equal the repo
        self.assertEqual(self.old_file.read_bytes(), (ROOT / "tools/trading/opportunity_view.py").read_bytes())
        self.assertEqual((self.root / "app" / "strategy_suggestion.py").read_bytes(), (ROOT / "tools/trading/strategy_suggestion.py").read_bytes())
        # private / production-only files untouched
        self.assertEqual(self.local_map.read_text(encoding="utf-8"), '{"sectors": {"PRIVATE": ["XYZ"]}}')
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "# production-only\n")
        # file backup holds the old version, with a record; database copy exists and is intact
        backups = list((self.root / "backup").glob("deploy-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "app" / "opportunity_view.py").read_text(encoding="utf-8"), "# old production version\n")
        record = json.loads((backups[0] / "backup-manifest.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(sorted(f["rel"] for f in record["files"]), ["app\\opportunity_view.py", "app\\strategy_suggestion.py"])
        db_copies = list((self.root / "data").glob("trading.db.bak-deploy-*"))
        self.assertEqual(len(db_copies), 1)
        self.assertEqual(Path(record["db_backup"]), db_copies[0])
        conn = sqlite3.connect(db_copies[0])
        self.assertEqual([r[0] for r in conn.execute("SELECT note FROM marker")], ["original"])
        conn.close()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), db_before)  # the live database is never modified by a deploy
        # nothing outside the manifest was added
        self.assertFalse((self.root / "tests").exists() or (self.root / "docs").exists() or (self.root / ".git").exists())

    def test_second_deploy_finds_nothing_to_do(self) -> None:
        self.assertEqual(self.run_script("-Action", "Deploy").returncode, 0)
        before = snapshot(self.root)
        result = self.run_script("-Action", "Deploy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NOTHING TO DEPLOY", result.stdout)
        self.assertEqual(snapshot(self.root), before)

    def test_rollback_needs_an_explicit_backup_path(self) -> None:
        self.assertEqual(self.run_script("-Action", "Deploy").returncode, 0)
        before = snapshot(self.root)
        result = self.run_script("-Action", "Rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit -BackupPath", result.stdout)
        self.assertEqual(snapshot(self.root), before)

    def test_rollback_restores_files_but_the_database_only_on_explicit_request(self) -> None:
        self.assertEqual(self.run_script("-Action", "Deploy").returncode, 0)
        backup = next((self.root / "backup").glob("deploy-*"))
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO marker(note) VALUES ('written after deploy')")
        conn.commit()
        conn.close()

        result = self.run_script("-Action", "Rollback", "-BackupPath", str(backup))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ROLLBACK OK", result.stdout)
        self.assertEqual(self.old_file.read_text(encoding="utf-8"), "# old production version\n")
        self.assertEqual(self.local_map.read_text(encoding="utf-8"), '{"sectors": {"PRIVATE": ["XYZ"]}}')
        self.assertEqual(self.marker_notes(), ["original", "written after deploy"])  # database untouched without -RestoreDb
        self.assertEqual(list((self.root / "data").glob("trading.db.bak-pre-rollback-*")), [])

        result = self.run_script("-Action", "Rollback", "-BackupPath", str(backup), "-RestoreDb")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.marker_notes(), ["original"])
        self.assertEqual(len(list((self.root / "data").glob("trading.db.bak-pre-rollback-*"))), 1)  # current database was saved first

    def test_check_rejects_a_manifest_that_lists_the_database_or_a_private_file(self) -> None:
        for bad in ("trading.db", "sector_map_local.json"):
            manifest = Path(self.tmp.name) / "bad-manifest.json"
            broken = json.loads(json.dumps(MANIFEST))
            broken["groups"][0]["files"].append(bad)
            manifest.write_text(json.dumps(broken), encoding="utf-8")
            before = snapshot(self.root)
            result = self.run_script("-Action", "Check", manifest=manifest)
            self.assertEqual(result.returncode, 1, bad)
            self.assertIn("ERROR", result.stdout)
            self.assertEqual(snapshot(self.root), before)

    def test_missing_protected_file_is_a_warning_and_is_never_created(self) -> None:
        self.local_map.unlink()
        result = self.run_script("-Action", "Deploy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN   protected file missing", result.stdout)
        self.assertFalse(self.local_map.exists())

    def test_corrupt_database_blocks_the_deploy_before_anything_is_written(self) -> None:
        self.db.write_bytes(b"this is not a database" * 100)
        before = snapshot(self.root)
        result = self.run_script("-Action", "Deploy")
        self.assertEqual(result.returncode, 1)
        self.assertIn("DEPLOY ABORTED", result.stdout)
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
