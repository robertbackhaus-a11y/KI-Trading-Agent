# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Deliberate migration: remove the unused column ``security.sector``.

Nothing reads or writes ``security.sector`` any more: the risk guards and the market-intelligence mapping take the sector from the versioned sector map
(``universe/sector_map_v1.json`` plus the optional local one), keyed by the Yahoo / public symbol.  The column held free text of a former import ("Technology",
...), was never maintained and is not part of any decision.  ``security.industry`` stays.

What the migration does (dry run unless ``--write``):

* checks that the column exists (otherwise it reports ``ALREADY_MIGRATED`` and changes nothing);
* refuses if anything other than a view select-list item refers to the column (an index, a trigger, another table);
* in ONE transaction: drops each view that selects ``security.sector`` (today ``v_watchlist``), runs ``ALTER TABLE security DROP COLUMN sector``, recreates
  the views with the ``sector`` select-list item removed and records ``metadata.security_sector_removed = 1``;
* before committing it compares every remaining ``security`` column of every row with the state before and the ``integrity_check`` / ``foreign_key_check``;
  any difference rolls the whole migration back;
* makes a backup copy of the database through the SQLite backup API first (``--no-backup`` to skip, ``--backup-path`` to choose it).

The old values are NOT copied anywhere (in particular not into a sector map); the dry run prints how many values are discarded.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime
from pathlib import Path
import runtime_paths  # sibling module: the runtime root (VIPER_TRADING_ROOT) and its default paths
from typing import Any, Optional

DB_PATH = runtime_paths.default_db_path()
FEATURE_KEY = "security_sector_removed"
FEATURE_VALUE = "1"
COLUMN = "sector"
_SELECT_ITEM = re.compile(r"\r?\n[ \t]*\w+\.sector[ \t]*,[ \t]*(?=\r?\n)")  # one line "    s.sector," of a view select list


class MigrationError(RuntimeError):
    """The migration cannot be applied safely."""


def _columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(security)")]


def _references(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """``(type, name, table, sql)`` of every schema object (other than the table ``security`` itself) whose SQL mentions the column."""
    out = []
    for kind, name, table, sql in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL"):
        if re.search(r"\bsector\b", sql, flags=re.IGNORECASE) and not (kind == "table" and name == "security"):
            out.append((kind, name, table, sql))
    return out


def _remaining_columns(conn: sqlite3.Connection) -> str:
    return ", ".join(f'"{c}"' for c in _columns(conn) if c != COLUMN)


def _fingerprint(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(f"SELECT {_remaining_columns(conn)} FROM security ORDER BY id").fetchall()


def plan(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read-only: what the migration would do, or why it cannot."""
    columns = _columns(conn)
    if COLUMN not in columns:
        return {"status": "ALREADY_MIGRATED", "security_columns": columns}
    views, blockers = [], []
    for kind, name, table, sql in _references(conn):
        new_sql = _SELECT_ITEM.sub("", sql, count=1)
        if kind == "view" and new_sql != sql and not re.search(r"\bsector\b", new_sql, flags=re.IGNORECASE):
            views.append({"name": name, "sql": sql, "new_sql": new_sql})
        else:
            blockers.append(f"{kind} {name} refers to the column")
    row = conn.execute("SELECT COUNT(*), SUM(sector IS NOT NULL) FROM security").fetchone()
    return {"status": "BLOCKED" if blockers else "READY", "security_rows": row[0], "values_discarded": int(row[1] or 0), "views_recreated": [v["name"] for v in views],
            "blockers": blockers, "_views": views}


def _backup(conn: sqlite3.Connection, target: Path) -> Path:
    destination = sqlite3.connect(str(target))
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return target


def apply(conn: sqlite3.Connection, backup_path: Optional[Path] = None) -> dict[str, Any]:
    """Apply the migration (the connection must be opened with ``isolation_level=None``).  Raises ``MigrationError`` and changes nothing if it cannot."""
    found = plan(conn)
    if found["status"] == "ALREADY_MIGRATED":
        return {k: v for k, v in found.items() if not k.startswith("_")}
    if found["status"] != "READY":
        raise MigrationError("; ".join(found["blockers"]))
    if backup_path is not None:
        _backup(conn, backup_path)
    before = _fingerprint(conn)
    ids_before = [row[0] for row in conn.execute("SELECT id FROM security ORDER BY id")]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for view in found["_views"]:
            conn.execute(f'DROP VIEW "{view["name"]}"')
        conn.execute(f"ALTER TABLE security DROP COLUMN {COLUMN}")
        for view in found["_views"]:
            conn.execute(view["new_sql"])
        conn.execute("INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (FEATURE_KEY, FEATURE_VALUE))
        if COLUMN in _columns(conn):
            raise MigrationError("the column is still there")
        if [row[0] for row in conn.execute("SELECT id FROM security ORDER BY id")] != ids_before:
            raise MigrationError("the security ids changed")
        if _fingerprint(conn) != before:
            raise MigrationError("a remaining security column changed")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("integrity_check or foreign_key_check failed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"status": "MIGRATED", "security_rows": found["security_rows"], "values_discarded": found["values_discarded"], "views_recreated": found["views_recreated"],
            "backup": str(backup_path) if backup_path else None, "security_columns": _columns(conn)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove the unused column security.sector (dry run by default)")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--write", action="store_true", help="apply the migration")
    parser.add_argument("--no-backup", action="store_true", help="skip the backup copy of the database (not recommended)")
    parser.add_argument("--backup-path", type=Path, default=None, help="where to put the backup copy (default: next to the database)")
    args = parser.parse_args()
    args.db_path = args.db_path.resolve()  # a relative path must work for the read-only URI of the dry run as well
    if args.backup_path is not None:
        args.backup_path = args.backup_path.resolve()
    if not args.db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {args.db_path}")
    if not args.write:
        conn = sqlite3.connect(f"file:///{args.db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        try:
            print({"dry_run": True, **{k: v for k, v in plan(conn).items() if not k.startswith("_")}})
        finally:
            conn.close()
        return
    conn = sqlite3.connect(str(args.db_path), timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        backup = None if args.no_backup else (args.backup_path or args.db_path.with_name(f"{args.db_path.name}.bak-pre-sector-drop-{datetime.now():%Y%m%d-%H%M%S}"))
        print(apply(conn, backup))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
