"""Removal of the unused column security.sector: the schema scripts, the migration of an existing database and the readers; synthetic data only."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TRADING = ROOT / "tools" / "trading"
if str(TRADING) not in sys.path:
    sys.path.insert(0, str(TRADING))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


init_db = load(TRADING / "Initialize-TradingDatabase.py", "init_db_for_sector_tests")
mig = load(TRADING / "Migrate-TradingDropSecuritySector.py", "migrate_drop_sector")
import watchlist_view as wv  # noqa: E402

OLD_VALUE = "ZZ_OLD_SECTOR_VALUE"


def normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.replace("IF NOT EXISTS ", "")).strip()


class SectorMigrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "trading.db"
        self.fresh = self.dir / "fresh.db"
        with contextlib.redirect_stdout(io.StringIO()):
            init_db.create_database(self.db)
            init_db.create_database(self.fresh)
        conn = sqlite3.connect(self.db)
        # the shape of an existing production database: the column and the view that selects it
        conn.execute("ALTER TABLE security ADD COLUMN sector TEXT")
        view = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'v_watchlist'").fetchone()[0]
        conn.execute("DROP VIEW v_watchlist")
        conn.execute(view.replace("s.country,", "s.country,\n    s.sector,", 1))
        conn.executemany("INSERT INTO security(id, symbol, isin, name, exchange, currency, country, asset_type, industry, sector) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [(1, "AAA", "ZZ0000000011", "Alpha Corp", "NASDAQ", "USD", "US", "stock", "Software", OLD_VALUE), (2, "BBB", None, "Beta Corp", None, None, None, "etf", None, None),
                          (3, "CCC", None, "Gamma Corp", "XETRA", "EUR", "DE", "stock", "Chemicals", "Technology")])
        conn.execute("INSERT INTO watchlist(security_id, status, priority) VALUES (1, 'WATCH', 5)")
        conn.execute("INSERT INTO positions(security_id, shares, avg_cost, currency) VALUES (3, 10, 20.0, 'EUR')")
        conn.commit()
        conn.close()

    def connect(self, write: bool = False) -> sqlite3.Connection:
        if write:
            return sqlite3.connect(self.db, isolation_level=None)
        return sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True)

    def state(self, db: Path | None = None) -> dict[str, str]:
        conn = sqlite3.connect(f"file:{(db or self.db).as_posix()}?mode=ro", uri=True)
        try:
            out = {}
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
                digest = hashlib.sha256()
                for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
                    digest.update(repr(row).encode("utf-8", "replace"))
                out[name] = digest.hexdigest()
            return out
        finally:
            conn.close()

    def security(self) -> list[tuple]:
        conn = self.connect()
        try:
            columns = [c[1] for c in conn.execute("PRAGMA table_info(security)") if c[1] != "sector"]
            return conn.execute(f"SELECT {', '.join(columns)} FROM security ORDER BY id").fetchall()
        finally:
            conn.close()


class SchemaTests(unittest.TestCase):
    def test_the_schema_scripts_have_no_sector_column_but_keep_industry(self) -> None:
        for name in ("Initialize-TradingDatabase.py", "Reset-TradingDb.py"):
            text = (TRADING / name).read_text(encoding="utf-8")
            self.assertFalse(re.search(r"^\s+sector\s+TEXT", text, flags=re.M), name)
            self.assertNotIn("s.sector", text, name)
            self.assertRegex(text, r"(?m)^\s+industry\s+TEXT", name)
            self.assertIn("s.industry", text, name)

    def test_a_fresh_database_has_neither_the_column_nor_a_view_that_uses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fresh.db"
            with contextlib.redirect_stdout(io.StringIO()):
                init_db.create_database(db)
            conn = sqlite3.connect(db)
            try:
                self.assertNotIn("sector", [c[1] for c in conn.execute("PRAGMA table_info(security)")])
                self.assertIn("industry", [c[1] for c in conn.execute("PRAGMA table_info(security)")])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE sql LIKE '%sector%'").fetchone()[0], 0)
            finally:
                conn.close()


class MigrationTests(SectorMigrationCase):
    def test_the_dry_run_reports_and_changes_nothing(self) -> None:
        before = self.state()
        conn = self.connect()
        try:
            found = mig.plan(conn)
        finally:
            conn.close()
        self.assertEqual((found["status"], found["security_rows"], found["values_discarded"], found["views_recreated"]), ("READY", 3, 2, ["v_watchlist"]))
        self.assertEqual(self.state(), before)

    def test_the_migration_removes_only_the_column(self) -> None:
        ids_and_fields = self.security()
        before = self.state()
        backup = self.dir / "backup.db"
        conn = self.connect(write=True)
        try:
            result = mig.apply(conn, backup)
            columns = [c[1] for c in conn.execute("PRAGMA table_info(security)")]
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT value FROM metadata WHERE key = 'security_sector_removed'").fetchone()[0], "1")
            view = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'v_watchlist'").fetchone()[0]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v_watchlist").fetchone()[0], 1)  # the recreated view works
        finally:
            conn.close()
        self.assertEqual((result["status"], result["values_discarded"]), ("MIGRATED", 2))
        self.assertNotIn("sector", columns)
        self.assertIn("industry", columns)
        self.assertEqual(self.security(), ids_and_fields)  # the same ids, every other field unchanged
        after = self.state()
        self.assertEqual({t for t in before if before[t] != after[t]}, {"security", "metadata"})  # watchlist, positions, strategies, campaigns, transactions ... untouched
        fresh = sqlite3.connect(self.fresh)
        try:
            self.assertEqual(normalized(view), normalized(fresh.execute("SELECT sql FROM sqlite_master WHERE name = 'v_watchlist'").fetchone()[0]))  # the same view as on a new database
        finally:
            fresh.close()
        old = sqlite3.connect(backup)
        try:
            self.assertIn("sector", [c[1] for c in old.execute("PRAGMA table_info(security)")])  # the backup is the way back
        finally:
            old.close()

    def test_the_old_values_are_not_carried_anywhere(self) -> None:
        conn = self.connect(write=True)
        try:
            mig.apply(conn, None)
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
                for row in conn.execute(f'SELECT * FROM "{name}"'):
                    self.assertNotIn(OLD_VALUE, [str(x) for x in row], name)
        finally:
            conn.close()
        for name in ("sector_map_v1.json", "sector_map_local.json"):
            for path in (TRADING / "universe" / name,):
                self.assertNotIn(OLD_VALUE, path.read_text(encoding="utf-8") if path.exists() else "")

    def test_a_second_run_changes_nothing(self) -> None:
        conn = self.connect(write=True)
        try:
            mig.apply(conn, None)
        finally:
            conn.close()
        before = self.state()
        conn = self.connect(write=True)
        try:
            self.assertEqual(mig.apply(conn, None)["status"], "ALREADY_MIGRATED")
        finally:
            conn.close()
        self.assertEqual(self.state(), before)

    def test_anything_else_that_refers_to_the_column_blocks_the_migration(self) -> None:
        conn = self.connect(write=True)
        conn.execute("CREATE INDEX idx_security_sector ON security(sector)")
        before = self.state()
        try:
            self.assertEqual(mig.plan(conn)["status"], "BLOCKED")
            with self.assertRaises(mig.MigrationError):
                mig.apply(conn, None)
            self.assertIn("sector", [c[1] for c in conn.execute("PRAGMA table_info(security)")])
        finally:
            conn.close()
        self.assertEqual(self.state(), before)

    def test_a_failed_check_rolls_everything_back(self) -> None:
        conn = self.connect(write=True)
        before = self.state()
        real = mig._fingerprint
        calls = []

        def broken(c):
            calls.append(1)
            rows = real(c)
            return rows if len(calls) == 1 else rows[:-1]  # the second look (inside the transaction) differs

        try:
            with mock.patch.object(mig, "_fingerprint", broken), self.assertRaises(mig.MigrationError):
                mig.apply(conn, None)
            self.assertIn("sector", [c[1] for c in conn.execute("PRAGMA table_info(security)")])
            self.assertIn("sector", conn.execute("SELECT sql FROM sqlite_master WHERE name = 'v_watchlist'").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(self.state(), before)

    def test_the_readers_work_on_the_migrated_database(self) -> None:
        conn = self.connect(write=True)
        try:
            mig.apply(conn, None)
            rows = wv.load_rows(conn, "2026-10-13")
        finally:
            conn.close()
        self.assertEqual([r["symbol"] for r in rows], ["AAA"])
        self.assertNotIn("sector", rows[0])


if __name__ == "__main__":
    unittest.main()
