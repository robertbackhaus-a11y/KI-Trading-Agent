"""
title: Trading SQLite
author: KI-Stack
description: Direct unrestricted SQLite access to the local trading database with multi-statement and multi-result support.
required_open_webui_version: 0.10.0
version: 1.4.0
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
        # A plan token is meaningful only for the exact preview displayed to
        # the user.  Keeping this in the in-process tool instance lets apply
        # distinguish file and DB drift.  A reload safely makes old tokens
        # PLAN_STALE rather than attempting an import.
        self._parqet_preview_states: dict[str, dict[str, str]] = {}

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
        approved_by = str((__user__ or {}).get("id") or "openwebui")
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
    # Parqet incremental import (OpenWebUI-authorized uploads only)
    # ============================================================

    @staticmethod
    def _load_runtime_trading_module(module_name: str):
        """Load one deployed trading helper without accepting a caller path."""
        tools_directory = Path(r"C:\KI-Stack\Tools\trading")
        module_path = tools_directory / f"{module_name}.py"
        if not module_path.is_file():
            raise FileNotFoundError(f"deployed trading helper is missing: {module_name}")
        directory_text = str(tools_directory)
        if directory_text not in sys.path:
            sys.path.insert(0, directory_text)
        loaded_name = f"openwebui_trading_{module_name}"
        spec = importlib.util.spec_from_file_location(loaded_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load deployed trading helper: {module_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[loaded_name] = module
        spec.loader.exec_module(module)
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

    @staticmethod
    def _public_import_plan(plan: dict, upload) -> dict:
        """Return a JSON-safe plan without disclosing OpenWebUI storage paths."""
        rows = plan.pop("rows", [])
        plan.pop("source_file", None)
        plan["uploaded_file"] = {
            "id": upload.file_id,
            "filename": upload.filename,
            "content_type": upload.content_type,
        }
        plan["non_duplicate_rows"] = [
            row for row in rows if row.get("classification") != "DUPLICATE"
        ]
        return plan

    def _preview_parqet_import_sync(self, csv_path: Path) -> dict:
        importer = self._load_runtime_trading_module("parqet_import")
        conn = self._readonly_import_connection(self.valves.database_path)
        try:
            return importer.build_import_plan(conn, csv_path).primitive()
        finally:
            conn.close()

    def _apply_parqet_import_sync(
        self,
        csv_path: Path,
        plan_token: str,
        include_historical: bool,
    ) -> dict:
        importer = self._load_runtime_trading_module("parqet_import")
        conn = self._connect()
        try:
            plan = importer.build_import_plan(conn, csv_path)
            return importer.apply_import_plan(
                conn,
                plan,
                expected_plan_token=plan_token,
                include_historical=include_historical,
                create_backup=True,
            )
        finally:
            conn.close()

    @staticmethod
    def _safe_import_error(exc: Exception) -> dict:
        # Importer exceptions can include a trusted server-side upload path;
        # never reflect that path into the model-visible tool result.
        return {
            "ok": False,
            "error": "IMPORT_FAILED",
            "message": "Parqet import could not be completed; inspect the server log and preview again.",
        }

    async def preview_parqet_import(
        self,
        uploaded_file_id: str,
        __files__: list[dict] | None = None,
        __user__: dict | None = None,
    ) -> dict:
        """Preview a current-chat uploaded Parqet CSV without any DB writes.

        ``uploaded_file_id`` is an opaque OpenWebUI file UUID, not a server
        path.  The file must be attached to this invocation and readable by
        the current OpenWebUI user.  Return the plan token to present to the
        user before calling :meth:`apply_parqet_import`.
        """
        try:
            resolver = self._load_runtime_trading_module("openwebui_upload_resolver")
            upload = await resolver.resolve_openwebui_upload(
                uploaded_file_id,
                attachments=__files__,
                user=__user__,
            )
            raw_plan = await asyncio.to_thread(self._preview_parqet_import_sync, upload.path)
            token = raw_plan["plan_token"]
            self._parqet_preview_states[token] = {
                "file_id": upload.file_id,
                "file_sha256": raw_plan["source_file_sha256"],
                "db_state_token": raw_plan["db_state_token"],
            }
            return {"ok": True, "operation": "preview_parqet_import", **self._public_import_plan(raw_plan, upload)}
        except Exception as exc:
            if getattr(exc, "code", None):
                return {"ok": False, "operation": "preview_parqet_import", "error": exc.code, "message": str(exc)}
            return self._safe_import_error(exc)

    async def apply_parqet_import(
        self,
        uploaded_file_id: str,
        plan_token: str,
        include_historical: bool = False,
        __files__: list[dict] | None = None,
        __user__: dict | None = None,
    ) -> dict:
        """Apply an explicitly approved preview of an attached Parqet CSV.

        Call this only after the user has approved the preview.  The resolver,
        file content hash, normalized plan, and relevant transaction DB state
        are all checked again.  ``include_historical`` is an explicit opt-in;
        conflict, unknown-security, and invalid rows never write.
        """
        try:
            resolver = self._load_runtime_trading_module("openwebui_upload_resolver")
            upload = await resolver.resolve_openwebui_upload(
                uploaded_file_id,
                attachments=__files__,
                user=__user__,
            )
            raw_plan = await asyncio.to_thread(self._preview_parqet_import_sync, upload.path)
            previous = self._parqet_preview_states.get(plan_token)
            if previous is None or previous["file_id"] != upload.file_id:
                return {"ok": False, "operation": "apply_parqet_import", "error": "PLAN_STALE", "message": "preview again before importing"}
            if previous["file_sha256"] != raw_plan["source_file_sha256"]:
                return {"ok": False, "operation": "apply_parqet_import", "error": "FILE_CHANGED", "message": "uploaded file changed; preview again"}
            if previous["db_state_token"] != raw_plan["db_state_token"]:
                return {"ok": False, "operation": "apply_parqet_import", "error": "DB_CHANGED", "message": "trading transaction state changed; preview again"}
            if raw_plan["plan_token"] != plan_token:
                return {"ok": False, "operation": "apply_parqet_import", "error": "PLAN_STALE", "message": "normalized import plan changed; preview again"}

            result = await asyncio.to_thread(
                self._apply_parqet_import_sync,
                upload.path,
                plan_token,
                include_historical,
            )
            backup_path = result.get("backup_path")
            if backup_path:
                result["backup_file"] = Path(backup_path).name
                result.pop("backup_path", None)
            self._parqet_preview_states.pop(plan_token, None)
            return {
                "ok": True,
                "operation": "apply_parqet_import",
                "uploaded_file": {"id": upload.file_id, "filename": upload.filename},
                "include_historical": include_historical,
                **result,
            }
        except Exception as exc:
            if getattr(exc, "code", None):
                return {"ok": False, "operation": "apply_parqet_import", "error": exc.code, "message": str(exc)}
            return self._safe_import_error(exc)
