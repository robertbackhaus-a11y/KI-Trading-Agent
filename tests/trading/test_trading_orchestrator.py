"""Read-only composition tests for the Phase-4B trading orchestrator."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_contracts import (  # noqa: E402
    Action,
    AvailabilityStatus,
    DataQuality,
    DecisionResult,
    StrategyType,
)
from swing_promotion import PromotionDecision  # noqa: E402
from test_decision_engine import make_snapshot  # noqa: E402
from test_swing_entry_recommendation import context  # noqa: E402
from trading_orchestrator import run_trading_orchestrator  # noqa: E402


class TradingOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT);
            CREATE TABLE positions (security_id INTEGER, shares REAL);
            CREATE TABLE strategy_assignment (security_id INTEGER, strategy_type TEXT, effective_from TEXT, effective_to TEXT);
            CREATE TABLE watchlist (security_id INTEGER, status TEXT, priority INTEGER, entry_reason TEXT);
            CREATE TABLE metadata (key TEXT, value TEXT);
            CREATE TABLE candidate_promotion (id INTEGER PRIMARY KEY);
            CREATE TABLE swing_campaign (security_id INTEGER, status TEXT);
            INSERT INTO metadata VALUES ('candidate_promotion_schema_version', '1');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    @staticmethod
    def _snapshot(
        security_id: int,
        *,
        positioned: bool,
        symbol: str | None = None,
        strategy: StrategyType = StrategyType.SWING,
    ):
        snapshot = make_snapshot(
            has_position=positioned,
            strategy=strategy,
            position_currency="EUR",
            market_currency="EUR",
        )
        return replace(snapshot, security_id=security_id, symbol=symbol or f"S{security_id}")

    @staticmethod
    def _promotion(security_id: int = 3, recommendation: str = "PROMOTE") -> PromotionDecision:
        return PromotionDecision(
            security_id, f"S{security_id}", f"Security {security_id}", "WATCH", 10, "test",
            recommendation, (f"{recommendation}_REASON",), "token", 100.0, "EUR", 100.0,
            95.0, 90.0, None, None, None, None, None, None, None,
            "available", "available", "available", "available",
        )

    def test_healthy_composition_keeps_entry_and_promotion_mutually_exclusive_and_serializable(self) -> None:
        today = date.today().isoformat()
        self.conn.execute("INSERT INTO positions VALUES (1, 10)")
        self.conn.execute("INSERT INTO swing_campaign VALUES (1, 'open')")
        self.conn.execute("INSERT INTO strategy_assignment VALUES (2, 'swing', ?, NULL)", (today,))
        before = "\n".join(self.conn.iterdump())
        existing_snapshot = self._snapshot(1, positioned=True)
        snapshots = {
            1: replace(existing_snapshot, position=replace(existing_snapshot.position, swing_campaign_id=1, swing_campaign_status="open")),
            2: self._snapshot(2, positioned=False),
        }
        decisions = {
            1: DecisionResult(Action.TRIM, 0.8, action_quantity=2, reasons=("TP1_EXECUTION_RECOMMENDED",)),
            2: DecisionResult(Action.BUY, 0.7, action_quantity=5, reasons=("ENTRY_RECOMMENDED",)),
        }
        with patch("trading_orchestrator.build_portfolio_context", return_value=context()), \
             patch("trading_orchestrator.build_analysis_snapshot", side_effect=lambda security_id, **_: snapshots[security_id]), \
             patch("trading_orchestrator.decide", side_effect=lambda snapshot, *_: decisions[snapshot.security_id]), \
             patch("trading_orchestrator.evaluate_swing_candidates", return_value=[self._promotion()]):
            result = run_trading_orchestrator(self.conn)
        self.assertEqual([item["security_id"] for item in result.existing_position_results], [1])
        self.assertEqual([item["security_id"] for item in result.entry_candidate_results], [2])
        self.assertEqual([item["security_id"] for item in result.promotion_results], [3])
        self.assertEqual(result.action_summary["TRIM"], 1)
        self.assertEqual(result.action_summary["BUY"], 1)
        self.assertEqual(result.action_summary["PROMOTE"], 1)
        self.assertEqual([item["priority"] for item in result.next_review_items], ["P2", "P4", "P5"])
        payload = json.dumps(result.primitive(), sort_keys=True)
        self.assertEqual(payload, json.dumps(result.primitive(), sort_keys=True))
        self.assertEqual("\n".join(self.conn.iterdump()), before)

    def test_priority_is_deterministic_and_sell_precedes_trim_add_and_hold(self) -> None:
        for security_id in (1, 2, 3, 4):
            self.conn.execute("INSERT INTO positions VALUES (?, 1)", (security_id,))
        snapshots = {
            security_id: self._snapshot(security_id, positioned=True, strategy=StrategyType.LONG_TERM)
            for security_id in range(1, 5)
        }
        actions = {1: Action.HOLD, 2: Action.ADD, 3: Action.TRIM, 4: Action.SELL}
        with patch("trading_orchestrator.build_portfolio_context", return_value=context()), \
             patch("trading_orchestrator.build_analysis_snapshot", side_effect=lambda security_id, **_: snapshots[security_id]), \
             patch("trading_orchestrator.decide", side_effect=lambda snapshot, *_: DecisionResult(actions[snapshot.security_id], 0.5)), \
             patch("trading_orchestrator.evaluate_swing_candidates", return_value=[]):
            result = run_trading_orchestrator(self.conn)
        self.assertEqual([item["kind"] for item in result.next_review_items], ["SELL", "TRIM", "ADD"])

    def test_lifecycle_delta_blocks_only_affected_security_without_global_failure(self) -> None:
        self.conn.execute("INSERT INTO positions VALUES (1, 10)")
        base = self._snapshot(1, positioned=True)
        position = replace(
            base.position,
            swing_campaign_id=9,
            campaign_reconciliation_delta=1.0,
            campaign_reconciliation_quality=DataQuality(AvailabilityStatus.PARTIAL, ("delta",)),
        )
        with patch("trading_orchestrator.build_portfolio_context", return_value=context()), \
             patch("trading_orchestrator.build_analysis_snapshot", return_value=replace(base, position=position)), \
             patch("trading_orchestrator.decide", return_value=DecisionResult(Action.HOLD, 0.5)), \
             patch("trading_orchestrator.evaluate_swing_candidates", return_value=[]):
            result = run_trading_orchestrator(self.conn)
        self.assertEqual(result.global_status, "AVAILABLE")
        self.assertEqual(result.blocking_summary["security_blocking"], 1)
        self.assertEqual(result.next_review_items[0]["code"], "LIFECYCLE_RECONCILIATION_DELTA")

    def test_explicit_as_of_is_forwarded_and_stale_technical_data_blocks_only_that_security(self) -> None:
        self.conn.execute("INSERT INTO positions VALUES (1, 10)")
        base = self._snapshot(1, positioned=True)
        snapshot = replace(
            base,
            technical=replace(base.technical, quality=DataQuality(AvailabilityStatus.STALE, ("old data",))),
            position=replace(base.position, swing_campaign_id=1, swing_campaign_status="open"),
        )
        with patch("trading_orchestrator.build_portfolio_context", return_value=context()), \
             patch("trading_orchestrator.build_analysis_snapshot", return_value=snapshot) as build_snapshot, \
             patch("trading_orchestrator.decide", return_value=DecisionResult(Action.HOLD, 0.1)), \
             patch("trading_orchestrator.evaluate_swing_candidates", return_value=[]):
            result = run_trading_orchestrator(self.conn, as_of="2026-09-20")
        self.assertEqual(build_snapshot.call_args.kwargs["as_of"], "2026-09-20")
        self.assertEqual(result.evaluation_as_of, "2026-09-20")
        self.assertEqual(result.global_status, "AVAILABLE")
        self.assertEqual(result.next_review_items[0]["code"], "TECHNICAL_DATA_UNAVAILABLE")

    def test_portfolio_context_failure_is_global_and_explicit_as_of_is_preserved(self) -> None:
        with patch("trading_orchestrator.build_portfolio_context", side_effect=RuntimeError("unavailable")):
            result = run_trading_orchestrator(self.conn, as_of="2026-09-20")
        self.assertEqual(result.evaluation_as_of, "2026-09-20")
        self.assertEqual(result.global_status, "GLOBAL_BLOCKING")
        self.assertEqual(result.global_issues[0]["code"], "PORTFOLIO_CONTEXT_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
