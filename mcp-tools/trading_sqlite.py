"""
title: Trading SQLite
author: KI-Stack
description: Direct unrestricted SQLite access to the local trading database with multi-statement and multi-result support.
version: 1.7.0
license: MIT
"""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        database_path: str = Field(
            default=r"C:\tools\trading\data\trading.db",
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

        market_intelligence_report_dir: str = Field(
            default="",
            description=(
                "Directory of the market-intelligence reports. Empty means "
                "<directory of the database>\\market-intelligence."
            ),
        )

        discovery_report_dir: str = Field(
            default="",
            description=(
                "Directory of the candidate-discovery reports. Empty means "
                "<directory of the database>\\candidate-discovery."
            ),
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
            analytics = self._load_runtime_trading_module("trading_analytics")
            conn = self._readonly_import_connection(self.valves.database_path)

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

    # ============================================================
    # Portfolio-level read-only orchestration
    # ============================================================

    def _run_trading_orchestrator_sync(self, as_of: str | None) -> dict:
        orchestrator = self._load_runtime_trading_module("trading_orchestrator")
        conn = self._readonly_import_connection(self.valves.database_path)
        try:
            return orchestrator.run_trading_orchestrator(conn, as_of=as_of).primitive()
        finally:
            conn.close()

    async def run_trading_orchestrator(self, as_of: str | None = None) -> dict:
        """Return one read-only, portfolio-level recommendation report.

        Current evaluation is used when ``as_of`` is omitted.  This operation
        only composes existing decisions, sizing, lifecycle, and promotion
        diagnostics; it cannot import data, create assignments/campaigns, or
        execute orders.

        The result includes result.presentation_summary (structured,
        engine-computed fields and counts) and result.rendered_summary_de
        (a ready-made German portfolio-status text). For a standard
        portfolio-status request, render presentation_summary or
        rendered_summary_de directly. Do not recount items, recompute
        totals, or derive a new recommendation from the raw result.
        """
        try:
            result = await asyncio.to_thread(self._run_trading_orchestrator_sync, as_of)
            return {"ok": True, "operation": "run_trading_orchestrator", "result": result}
        except Exception as exc:
            return {"ok": False, "operation": "run_trading_orchestrator", "error": "ORCHESTRATOR_FAILED", "message": str(exc)}

    # ============================================================
    # Swing candidate promotion (fixed safe operations only)
    # ============================================================

    def _evaluate_swing_candidate_sync(self, security_id: int) -> dict:
        promotion = self._load_runtime_trading_module("swing_promotion")
        conn = self._readonly_import_connection(self.valves.database_path)
        try:
            return promotion.evaluate_swing_promotion(conn, int(security_id)).primitive()
        finally:
            conn.close()

    def _evaluate_swing_candidates_sync(self) -> list[dict]:
        promotion = self._load_runtime_trading_module("swing_promotion")
        conn = self._readonly_import_connection(self.valves.database_path)
        try:
            return [item.primitive() for item in promotion.evaluate_swing_candidates(conn)]
        finally:
            conn.close()

    def _approve_swing_promotion_sync(
        self, security_id: int, plan_token: str, effective_from: str, approved_by: str
    ) -> dict:
        promotion = self._load_runtime_trading_module("swing_promotion")
        conn = self._connect()
        try:
            return promotion.approve_swing_promotion(
                conn,
                security_id=int(security_id),
                plan_token=plan_token,
                effective_from=effective_from,
                approved_by=approved_by,
            )
        finally:
            conn.close()

    async def evaluate_swing_candidate(self, security_id: int) -> dict:
        """Read-only promotion evaluation for one generic WATCH security.

        This evaluates the fixed promotion rules only.  It cannot assign a
        strategy, create a campaign, recommend a BUY, or execute a trade.
        """
        try:
            decision = await asyncio.to_thread(self._evaluate_swing_candidate_sync, security_id)
            return {"ok": True, "operation": "evaluate_swing_candidate", "decision": decision}
        except Exception as exc:
            return {"ok": False, "operation": "evaluate_swing_candidate", "error": "PROMOTION_EVALUATION_FAILED", "message": str(exc)}

    async def evaluate_swing_candidates(self) -> dict:
        """Read-only evaluation of zero-position generic WATCH securities."""
        try:
            decisions = await asyncio.to_thread(self._evaluate_swing_candidates_sync)
            counts = {"PROMOTE": 0, "KEEP_WATCHING": 0, "REJECT": 0, "DATA_INSUFFICIENT": 0}
            for decision in decisions:
                counts[decision["recommendation"]] += 1
            return {"ok": True, "operation": "evaluate_swing_candidates", "count": len(decisions), "counts": counts, "decisions": decisions}
        except Exception as exc:
            return {"ok": False, "operation": "evaluate_swing_candidates", "error": "PROMOTION_EVALUATION_FAILED", "message": str(exc)}

    async def approve_swing_promotion(
        self,
        security_id: int,
        plan_token: str,
        effective_from: str,
        __user__: dict | None = None,
    ) -> dict:
        """Apply one explicitly approved, still-current PROMOTE plan.

        Call only after the user approved the returned token.  The operation
        accepts no strategy, quantity, price, SQL, or campaign parameters.
        It re-evaluates current state before inserting one `swing` assignment.
        """
        approved_by = str((__user__ or {}).get("id") or "mcp")
        try:
            result = await asyncio.to_thread(
                self._approve_swing_promotion_sync,
                security_id,
                plan_token,
                effective_from,
                approved_by,
            )
            return {"ok": True, "operation": "approve_swing_promotion", **result}
        except Exception as exc:
            return {"ok": False, "operation": "approve_swing_promotion", "error": "PROMOTION_APPROVAL_FAILED", "message": str(exc)}

    # ============================================================
    # Strategy suggestion & candidate decision (read-only)
    # ============================================================

    def _suggest_strategy_assignments_sync(self, as_of: str | None) -> list[dict]:
        suggestion = self._load_runtime_trading_module("strategy_suggestion")
        conn = self._readonly_import_connection(self.valves.database_path)
        try:
            return [item.primitive() for item in suggestion.suggest_strategy_assignments(conn, as_of=as_of)]
        finally:
            conn.close()

    def _evaluate_watchlist_candidates_sync(self, as_of: str | None) -> list[dict]:
        decision = self._load_runtime_trading_module("candidate_decision")
        conn = self._readonly_import_connection(self.valves.database_path)
        try:
            return [item.primitive() for item in decision.evaluate_watchlist_candidates(conn, as_of=as_of)]
        finally:
            conn.close()

    async def suggest_strategy_assignments(self, as_of: str | None = None) -> dict:
        """Read-only swing/long_term/unknown strategy suggestion per WATCH entry.

        Only covers watchlist entries that currently have no active
        strategy_assignment; entries that already have one are skipped
        entirely by strategy_suggestion.py, never overridden. A suggestion is
        not an assignment: this call never writes strategy_assignment. It
        delegates entirely to the existing strategy_suggestion.py module; no
        suggestion logic lives in this tool. Current evaluation is used when
        as_of is omitted.
        """
        try:
            suggestions = await asyncio.to_thread(self._suggest_strategy_assignments_sync, as_of)
            counts = {"swing": 0, "long_term": 0, "unknown": 0}
            for item in suggestions:
                counts[item["suggested_strategy"]] += 1
            return {"ok": True, "operation": "suggest_strategy_assignments", "count": len(suggestions), "counts": counts, "suggestions": suggestions}
        except Exception as exc:
            return {"ok": False, "operation": "suggest_strategy_assignments", "error": "STRATEGY_SUGGESTION_FAILED", "message": str(exc)}

    async def evaluate_watchlist_candidates(self, as_of: str | None = None) -> dict:
        """Read-only BUY/WATCH/DEFERRED/INSUFFICIENT_DATA status per WATCH entry.

        Combines analytics score/quality, active strategy assignment, open
        Swing campaign status, and the current portfolio allocation
        guardrails. It delegates entirely to the existing
        candidate_decision.py module; no decision logic lives in this tool.
        Never opens a Swing campaign, never persists a decision, never places
        an order. Current evaluation is used when as_of is omitted.
        """
        try:
            decisions = await asyncio.to_thread(self._evaluate_watchlist_candidates_sync, as_of)
            counts = {"BUY": 0, "WATCH": 0, "DEFERRED": 0, "INSUFFICIENT_DATA": 0}
            for item in decisions:
                counts[item["decision_status"]] += 1
            return {"ok": True, "operation": "evaluate_watchlist_candidates", "count": len(decisions), "counts": counts, "decisions": decisions}
        except Exception as exc:
            return {"ok": False, "operation": "evaluate_watchlist_candidates", "error": "CANDIDATE_DECISION_FAILED", "message": str(exc)}

    # ============================================================
    # Candidate discovery report (read-only, file only)
    # ============================================================

    def _discovery_report_directory(self) -> Path:
        configured = str(self.valves.discovery_report_dir or "").strip()
        if configured:
            return Path(configured)
        return Path(self.valves.database_path).parent / "candidate-discovery"

    def _get_candidate_discovery_sync(self, status: str, limit: int, detail: bool, symbol: str | None) -> dict:
        report_module = self._load_runtime_trading_module("discovery_report")
        directory = self._discovery_report_directory()
        report, reason, problems = report_module.read_latest_report(directory)
        if report is None:
            return report_module.unavailable(reason, directory, problems)
        return report_module.query_report(report, status=status, limit=limit, detail=detail, symbol=symbol)

    async def get_candidate_discovery(
        self,
        status: str = "ALL",
        limit: int = 20,
        detail: bool = False,
        symbol: str | None = None,
    ) -> dict:
        """Last finished watchlist-discovery report (read-only: no scan, no web, no DB). New candidates, not PROMOTE/BUY.

        :param status: ALL, DISCOVERY_READY, DISCOVERY_WATCH, DISCOVERY_DATA_INSUFFICIENT, DISCOVERY_REJECTED or EXCLUDED (pre-filter)
        :param limit: rows (default 20; max 150, detail 40)
        :param detail: true = all stored fields
        :param symbol: explain one symbol
        Returns AVAILABLE (freshness, REPORT_STALE flag) or UNAVAILABLE (NO_DISCOVERY_REPORT).
        """
        try:
            return await asyncio.to_thread(self._get_candidate_discovery_sync, status, int(limit), bool(detail), symbol)
        except Exception as exc:
            return {"ok": False, "operation": "get_candidate_discovery", "error": "DISCOVERY_REPORT_READ_FAILED", "message": str(exc)}

    # ============================================================
    # Market intelligence report (read-only, file only)
    # ============================================================

    def _market_intelligence_report_directory(self) -> Path:
        configured = str(self.valves.market_intelligence_report_dir or "").strip()
        if configured:
            return Path(configured)
        return Path(self.valves.database_path).parent / "market-intelligence"

    def _get_market_intelligence_sync(self, scope: str, category: str | None, importance: str | None, symbol: str | None,
                                      sector: str | None, limit: int, detail: bool) -> dict:
        report_module = self._load_runtime_trading_module("intelligence_report")
        directory = self._market_intelligence_report_directory()
        report, reason, problems = report_module.read_latest_report(directory)
        if report is None:
            return report_module.unavailable(reason, directory, problems)
        return report_module.query_report(report, scope=scope, category=category, importance=importance, symbol=symbol,
                                          sector=sector, limit=limit, detail=detail)

    async def get_market_intelligence(
        self,
        scope: str = "ALL",
        category: str | None = None,
        importance: str | None = None,
        symbol: str | None = None,
        sector: str | None = None,
        limit: int = 20,
        detail: bool = False,
    ) -> dict:
        """Last finished market-intelligence report: classified macro and company news (read-only: no web, no scan, no DB).
        News is context, not a signal; impact is UNKNOWN unless a primary filing states an adverse fact.
        For a normal symbol news question the compact result is sufficient: pass only symbol and omit every optional parameter whose default suffices.

        :param scope: default ALL (do not set it explicitly); PORTFOLIO, WATCHLIST, DISCOVERY or MACRO to narrow
        :param category: MACRO, MONETARY_POLICY, INFLATION, LABOR_MARKET, ECONOMIC_GROWTH, GEOPOLITICS, ENERGY, SECTOR, COMPANY, EARNINGS, GUIDANCE, REGULATORY, M_AND_A
        :param importance: HIGH, MEDIUM or LOW
        :param symbol: e.g. NVDA or SAP (matches SAP.DE)
        :param sector: e.g. SEMICONDUCTORS, ENERGY
        :param limit: events (default 20; max 50, detail 25); set only if a specific count is required
        :param detail: true = raw dump of all stored fields and substantially larger output; use only when the user explicitly requests detailed/raw data or the compact result lacks information required to answer the question
        Returns AVAILABLE (freshness, REPORT_STALE flag) or UNAVAILABLE (NO_MARKET_INTELLIGENCE_REPORT).
        """
        try:
            return await asyncio.to_thread(self._get_market_intelligence_sync, scope, category, importance, symbol, sector, int(limit), bool(detail))
        except Exception as exc:
            return {"ok": False, "operation": "get_market_intelligence", "error": "MARKET_INTELLIGENCE_READ_FAILED", "message": str(exc)}

    # ============================================================
    # Unified opportunity view (read-only: orchestrator + two report files)
    # ============================================================

    def _get_opportunity_view_sync(self, source: str, status: str, news_status: str | None, symbol: str | None, limit: int, detail: bool) -> dict:
        view_module = self._load_runtime_trading_module("opportunity_view")
        discovery_module = self._load_runtime_trading_module("discovery_report")
        intelligence_module = self._load_runtime_trading_module("intelligence_report")
        engine, engine_error, symbol_map = None, None, {}
        try:
            orchestrator = self._load_runtime_trading_module("trading_orchestrator")
            conn = self._readonly_import_connection(self.valves.database_path)
            try:
                engine = orchestrator.run_trading_orchestrator(conn).primitive()
                symbol_map = view_module.load_symbol_map(conn)
            finally:
                conn.close()
        except Exception as exc:
            engine, engine_error = None, f"{type(exc).__name__}: {exc}"
        discovery, discovery_reason, _ = discovery_module.read_latest_report(self._discovery_report_directory())
        intelligence, intelligence_reason, _ = intelligence_module.read_latest_report(self._market_intelligence_report_directory())
        view = view_module.build_opportunity_view(
            engine=engine, symbol_map=symbol_map, discovery=discovery, discovery_reason=discovery_reason,
            mi=intelligence, mi_reason=intelligence_reason, engine_error=engine_error,
        )
        if view["metadata"]["status"] == "UNAVAILABLE":
            return view_module.unavailable({"engine": engine_error, "discovery": discovery_reason, "market_intelligence": intelligence_reason})
        return view_module.query_view(view, source=source, status=status, news_status=news_status, symbol=symbol, limit=limit, detail=detail)

    async def get_opportunity_view(
        self,
        source: str = "ALL",
        status: str = "ALL",
        news_status: str | None = None,
        symbol: str | None = None,
        limit: int = 30,
        detail: bool = False,
    ) -> dict:
        """Opportunities = engine/planner candidates + last discovery report, each with a news-context status
        (read-only: no web, no scan, no DB write, no order). Joins existing results; news never changes a score, rank, size or decision.
        One filtered call is normally enough; detail only when explicitly needed.

        :param source: ALL, WATCHLIST, DISCOVERY or BOTH
        :param status: ALL, ENTRY_READY, WAIT_FOR_TRIGGER, DISCOVERY_READY or OTHER
        :param news_status: NEWS_HIGH_ATTENTION, NEWS_ATTENTION, NEWS_CLEAR, NEWS_UNAVAILABLE, RELEVANT (attention or high) or NOT_HIGH
        :param symbol: one or comma-separated, e.g. NVDA,AAPL (also shows held positions)
        :param limit: rows (default 30; max 40, detail 10)
        :param detail: true = all fields incl. macro, positions, methodology
        Returns rows with entry rank/score, plan status and capital, news status and top event; PARTIAL if a report is missing.
        """
        try:
            return await asyncio.to_thread(self._get_opportunity_view_sync, source, status, news_status, symbol, int(limit), bool(detail))
        except Exception as exc:
            return {"ok": False, "operation": "get_opportunity_view", "error": "OPPORTUNITY_VIEW_FAILED", "message": str(exc)}

    # ============================================================
    # Runtime helper loading and read-only connection
    # ============================================================

    # Shared across every Tools() instance created from this loaded module,
    # so a runtime helper is exec'd at most once per process (restart the
    # MCP server to pick up changed runtime helper files).
    _runtime_module_cache: dict[str, Any] = {}

    @classmethod
    def _load_runtime_trading_module(cls, module_name: str):
        """Load one deployed trading helper without accepting a caller path.

        Cached per module name for the lifetime of this loaded tool version.
        Falls back to a fresh load (and populates the cache) the first time,
        or whenever the process/module was reloaded from scratch."""
        cached = cls._runtime_module_cache.get(module_name)
        if cached is not None:
            return cached
        tools_directory = Path(r"C:\tools\trading\app")
        module_path = tools_directory / f"{module_name}.py"
        if not module_path.is_file():
            raise FileNotFoundError(f"deployed trading helper is missing: {module_name}")
        directory_text = str(tools_directory)
        if directory_text not in sys.path:
            sys.path.insert(0, directory_text)
        loaded_name = f"trading_runtime_{module_name}"
        spec = importlib.util.spec_from_file_location(loaded_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load deployed trading helper: {module_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[loaded_name] = module
        spec.loader.exec_module(module)
        cls._runtime_module_cache[module_name] = module
        return module

    @staticmethod
    def _readonly_import_connection(database_path: str) -> sqlite3.Connection:
        db_path = Path(database_path)
        if not db_path.is_file():
            raise FileNotFoundError("Trading database not found")
        conn = sqlite3.connect(
            f"file:///{db_path.as_posix()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
