"""
title: Trading SQLite
author: KI-Stack
description: Direct unrestricted SQLite access to the local trading database with multi-statement and multi-result support.
required_open_webui_version: 0.10.0
version: 1.3.0
license: MIT
"""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        database_path: str = Field(
            default=r"C:\KI-Stack\data\trading\trading.db",
            description="Full path to the local trading SQLite database.",
        )

        timeout_seconds: float = Field(
            default=1.0,
            description="SQLite connection timeout in seconds.",
        )

        busy_timeout_ms: int = Field(
            default=1000,
            description="SQLite busy timeout in milliseconds.",
        )

        max_rows_per_statement: int = Field(
            default=500,
            description="Maximum number of rows returned per SQL statement.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ============================================================
    # Connection
    # ============================================================

    def _connect(self) -> sqlite3.Connection:
        db_path = Path(self.valves.database_path)

        if not db_path.exists():
            raise FileNotFoundError(f"Trading database not found: {db_path}")

        conn = sqlite3.connect(
            str(db_path),
            timeout=float(self.valves.timeout_seconds),
            isolation_level="DEFERRED",
        )

        conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(f"PRAGMA busy_timeout={int(self.valves.busy_timeout_ms)};")

        return conn

    # ============================================================
    # SQL statement splitting
    # ============================================================

    def _split_sql_statements(self, sql: str) -> list[str]:
        """
        Split one SQL script into individual executable SQLite statements.

        Uses sqlite3.complete_statement() so semicolons inside valid SQL
        constructs do not cause naive splitting.

        Supports a final statement without a trailing semicolon.
        """

        statements: list[str] = []
        buffer = ""

        for char in sql:
            buffer += char

            if char == ";" and sqlite3.complete_statement(buffer):
                statement = buffer.strip()

                if statement.endswith(";"):
                    statement = statement[:-1].rstrip()

                if statement:
                    statements.append(statement)

                buffer = ""

        if buffer.strip():
            statement = buffer.strip()

            if statement:
                statements.append(statement)

        return statements

    # ============================================================
    # Serialization
    # ============================================================

    def _serialize_value(self, value: Any) -> Any:
        """
        Convert SQLite values into JSON-safe values.
        """

        if isinstance(value, bytes):
            return value.hex()

        return value

    def _serialize_row(self, row: sqlite3.Row) -> dict:
        """
        Convert sqlite3.Row into a JSON-safe dictionary.
        """

        return {key: self._serialize_value(row[key]) for key in row.keys()}

    # ============================================================
    # Main SQL execution
    # ============================================================

    def _sql_execute_sync(self, sql: str) -> dict:
        """
        Synchronous SQLite implementation.

        All statements in one tool call are executed in one transaction.

        If any statement fails:
        - execution stops
        - the whole transaction is rolled back
        """

        total_started = time.perf_counter()

        sql_clean = (sql or "").strip()

        if not sql_clean:
            return {
                "ok": False,
                "error": "No SQL provided.",
                "statement_count": 0,
                "executed_statements": 0,
                "rolled_back": False,
                "results": [],
                "elapsed_ms": round(
                    (time.perf_counter() - total_started) * 1000,
                    3,
                ),
            }

        # --------------------------------------------------------
        # Parse SQL
        # --------------------------------------------------------

        try:
            statements = self._split_sql_statements(sql_clean)

        except Exception as exc:
            return {
                "ok": False,
                "error": f"SQL parsing failed: {exc}",
                "statement_count": 0,
                "executed_statements": 0,
                "rolled_back": False,
                "results": [],
                "elapsed_ms": round(
                    (time.perf_counter() - total_started) * 1000,
                    3,
                ),
            }

        if not statements:
            return {
                "ok": False,
                "error": "No executable SQL statement found.",
                "statement_count": 0,
                "executed_statements": 0,
                "rolled_back": False,
                "results": [],
                "elapsed_ms": round(
                    (time.perf_counter() - total_started) * 1000,
                    3,
                ),
            }

        conn: sqlite3.Connection | None = None
        results: list[dict] = []

        try:
            conn = self._connect()

            # Explicit transaction.
            conn.execute("BEGIN")

            for index, statement in enumerate(
                statements,
                start=1,
            ):
                statement_started = time.perf_counter()

                try:
                    before_changes = conn.total_changes

                    cursor = conn.execute(statement)

                    affected_rows = conn.total_changes - before_changes

                    # ------------------------------------------------
                    # Query / RETURNING result
                    # ------------------------------------------------

                    if cursor.description is not None:
                        max_rows = max(
                            1,
                            int(self.valves.max_rows_per_statement),
                        )

                        fetched_rows = cursor.fetchmany(max_rows + 1)

                        truncated = len(fetched_rows) > max_rows

                        if truncated:
                            fetched_rows = fetched_rows[:max_rows]

                        rows = [self._serialize_row(row) for row in fetched_rows]

                        columns = [column[0] for column in cursor.description]

                        result = {
                            "statement_index": index,
                            "ok": True,
                            "type": "query",
                            "columns": columns,
                            "rows": rows,
                            "row_count": len(rows),
                            "truncated": truncated,
                            "affected_rows": affected_rows,
                        }

                    # ------------------------------------------------
                    # Non-query result
                    # ------------------------------------------------

                    else:
                        result = {
                            "statement_index": index,
                            "ok": True,
                            "type": "execute",
                            "affected_rows": affected_rows,
                        }

                    cursor.close()

                except Exception as exc:
                    conn.rollback()

                    results.append(
                        {
                            "statement_index": index,
                            "ok": False,
                            "type": "error",
                            "error": str(exc),
                            "elapsed_ms": round(
                                (time.perf_counter() - statement_started) * 1000,
                                3,
                            ),
                        }
                    )

                    return {
                        "ok": False,
                        "error": str(exc),
                        "statement_count": len(statements),
                        "executed_statements": len(results),
                        "rolled_back": True,
                        "results": results,
                        "elapsed_ms": round(
                            (time.perf_counter() - total_started) * 1000,
                            3,
                        ),
                    }

                result["elapsed_ms"] = round(
                    (time.perf_counter() - statement_started) * 1000,
                    3,
                )

                results.append(result)

            # ----------------------------------------------------
            # All statements succeeded
            # ----------------------------------------------------

            conn.commit()

            return {
                "ok": True,
                "statement_count": len(statements),
                "executed_statements": len(results),
                "rolled_back": False,
                "results": results,
                "elapsed_ms": round(
                    (time.perf_counter() - total_started) * 1000,
                    3,
                ),
            }

        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass

            return {
                "ok": False,
                "error": str(exc),
                "statement_count": len(statements),
                "executed_statements": len(results),
                "rolled_back": True,
                "results": results,
                "elapsed_ms": round(
                    (time.perf_counter() - total_started) * 1000,
                    3,
                ),
            }

        finally:
            if conn is not None:
                conn.close()

    async def sql_execute(
        self,
        sql: str,
    ) -> dict:
        """
        Execute arbitrary SQLite SQL against the local trading database.

        Multiple SQL statements are supported in one tool call.

        All statements execute sequentially on one connection and inside
        one transaction.

        If any statement fails, the complete transaction is rolled back.

        Supported:
        - SELECT
        - WITH / CTE
        - PRAGMA
        - EXPLAIN
        - INSERT
        - UPDATE
        - DELETE
        - CREATE
        - ALTER
        - DROP
        - RETURNING
        - multiple SQL statements separated by semicolons

        Prefer targeted SQL queries.

        :param sql: One or more SQLite SQL statements.
        :return: Per-statement results and execution timing.
        """

        return await asyncio.to_thread(
            self._sql_execute_sync,
            sql,
        )

    # ============================================================
    # Database tables
    # ============================================================

    def _database_tables_sync(self) -> dict:
        started = time.perf_counter()

        conn = self._connect()

        try:
            tables = conn.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """).fetchall()

            result = []

            for row in tables:
                table_name = row["name"]

                quoted_name = table_name.replace(
                    '"',
                    '""',
                )

                count_row = conn.execute(
                    f"SELECT COUNT(*) AS count " f'FROM "{quoted_name}"'
                ).fetchone()

                result.append(
                    {
                        "table": table_name,
                        "rows": count_row["count"],
                    }
                )

            return {
                "ok": True,
                "table_count": len(result),
                "tables": result,
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        finally:
            conn.close()

    async def database_tables(self) -> dict:
        """
        Return all SQLite tables and exact row counts.
        """

        return await asyncio.to_thread(self._database_tables_sync)

    # ============================================================
    # Complete database schema
    # ============================================================

    def _database_schema_sync(self) -> dict:
        started = time.perf_counter()

        conn = self._connect()

        try:
            rows = conn.execute("""
                SELECT
                    type,
                    name,
                    tbl_name,
                    sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY
                    CASE type
                        WHEN 'table' THEN 1
                        WHEN 'view' THEN 2
                        WHEN 'index' THEN 3
                        WHEN 'trigger' THEN 4
                        ELSE 5
                    END,
                    name
                """).fetchall()

            objects = [self._serialize_row(row) for row in rows]

            return {
                "ok": True,
                "object_count": len(objects),
                "objects": objects,
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        finally:
            conn.close()

    async def database_schema(self) -> dict:
        """
        Return the complete SQLite schema.

        Includes:
        - tables
        - views
        - indexes
        - triggers
        """

        return await asyncio.to_thread(self._database_schema_sync)

    # ============================================================
    # Table information
    # ============================================================

    def _table_info_sync(
        self,
        table_name: str,
    ) -> dict:
        started = time.perf_counter()

        conn = self._connect()

        try:
            safe_table = table_name.replace(
                '"',
                '""',
            )

            columns = conn.execute(f'PRAGMA table_info("{safe_table}")').fetchall()

            indexes = conn.execute(f'PRAGMA index_list("{safe_table}")').fetchall()

            foreign_keys = conn.execute(
                f'PRAGMA foreign_key_list("{safe_table}")'
            ).fetchall()

            return {
                "ok": True,
                "table": table_name,
                "columns": [self._serialize_row(row) for row in columns],
                "indexes": [self._serialize_row(row) for row in indexes],
                "foreign_keys": [self._serialize_row(row) for row in foreign_keys],
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        finally:
            conn.close()

    async def table_info(
        self,
        table_name: str,
    ) -> dict:
        """
        Return SQLite metadata for one table.

        Includes:
        - columns
        - indexes
        - foreign keys
        """

        return await asyncio.to_thread(
            self._table_info_sync,
            table_name,
        )

    # ============================================================
    # Database status
    # ============================================================

    def _database_status_sync(self) -> dict:
        started = time.perf_counter()

        db_path = Path(self.valves.database_path)

        conn = self._connect()

        try:
            sqlite_version = conn.execute(
                "SELECT sqlite_version() AS version"
            ).fetchone()["version"]

            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

            foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]

            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

            return {
                "ok": True,
                "database_path": str(db_path),
                "database_exists": db_path.exists(),
                "database_size_bytes": (
                    db_path.stat().st_size if db_path.exists() else None
                ),
                "sqlite_version": sqlite_version,
                "journal_mode": journal_mode,
                "foreign_keys": bool(foreign_keys),
                "busy_timeout_ms": busy_timeout,
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        finally:
            conn.close()

    async def database_status(self) -> dict:
        """
        Return SQLite runtime and database status.

        Includes:
        - database path
        - database file size
        - SQLite version
        - journal mode
        - foreign key mode
        - busy timeout
        """

        return await asyncio.to_thread(self._database_status_sync)

    # ============================================================
    # Watchlist ranking (read-only, via trading_analytics.py)
    # ============================================================

    def _rank_watchlist_sync(self) -> dict:
        started = time.perf_counter()

        conn: sqlite3.Connection | None = None

        try:
            conn = self._connect()

            module_path = Path(
                r"C:\KI-Stack\tools\trading\trading_analytics.py"
            )

            if not module_path.exists():
                raise FileNotFoundError(
                    f"trading_analytics.py not found: {module_path}"
                )

            spec = importlib.util.spec_from_file_location(
                "trading_analytics",
                module_path,
            )

            analytics = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(analytics)

            rankings = analytics.rank_watchlist(conn)

            return {
                "ok": True,
                "count": len(rankings),
                "rankings": rankings,
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
            }

        finally:
            if conn is not None:
                conn.close()

    async def rank_watchlist(self) -> dict:
        """
        Rank watchlist securities (status='WATCH') by a technical
        momentum/trend score.

        Delegates to the read-only trading_analytics.py library
        (SMA50/SMA200, RSI14, 1M/3M/6M performance, 52-week
        drawdown, annualized volatility) against the same local
        trading database. No writes, no web access.

        :return: {"ok": true, "count": <n>, "rankings": [...]}
        """

        return await asyncio.to_thread(self._rank_watchlist_sync)
