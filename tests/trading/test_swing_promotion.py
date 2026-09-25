"""Phase-4A promotion evaluation and approval safety tests."""

from __future__ import annotations

from datetime import date, timedelta
import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixtures import MARKET_END, MARKET_START, make_connection  # noqa: E402
from swing_promotion import (  # noqa: E402
    DATA_INSUFFICIENT,
    KEEP_WATCHING,
    PROMOTE,
    REJECT,
    PromotionApprovalError,
    approve_swing_promotion,
    evaluate_swing_candidates,
    evaluate_swing_promotion,
)
import trading_analytics  # noqa: E402


class SwingPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_connection()
        self.today = date.today().isoformat()
        self.conn.execute("ALTER TABLE watchlist ADD COLUMN priority INTEGER")
        self.conn.execute("ALTER TABLE watchlist ADD COLUMN entry_reason TEXT")
        self.conn.execute("ALTER TABLE watchlist ADD COLUMN updated_at TEXT")
        self.conn.execute("INSERT INTO security(id, symbol, name, asset_type) VALUES (4, 'READY', 'Ready Corp', 'stock')")
        self.conn.execute("INSERT INTO watchlist(security_id, status, priority, entry_reason) VALUES (4, 'WATCH', 7, 'test')")
        rows = []
        for offset in range(260):
            day = (MARKET_START + timedelta(days=offset)).isoformat()
            price = 100.0 + offset
            rows.append((4, day, price, price, 'EUR', 1))
        self.conn.executemany("INSERT INTO market_data(security_id, trade_date, close, adjusted_close, currency, source_id) VALUES (?, ?, ?, ?, ?, ?)", rows)
        self.conn.execute("INSERT INTO market_snapshot(security_id, as_of_at, price, currency) VALUES (4, ?, 359, 'EUR')", (MARKET_END.isoformat() + 'T00:00:00+00:00',))
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _assignment(self, strategy: str) -> None:
        self.conn.execute("INSERT INTO strategy_assignment(security_id, strategy_type, effective_from) VALUES (4, ?, ?)", (strategy, self.today))
        self.conn.commit()

    def _approval_schema(self) -> None:
        self.conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        self.conn.execute("INSERT INTO metadata(key, value) VALUES ('schema_version', '2.0')")
        migration_path = ROOT / 'tools' / 'trading' / 'Migrate-TradingCandidatePromotions.py'
        spec = importlib.util.spec_from_file_location('promotion_migration', migration_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        module.apply_migration(self.conn)

    def test_ready_trend_promotes_without_fundamental_or_capital_gate(self) -> None:
        decision = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(decision.recommendation, PROMOTE)
        self.assertEqual(decision.reasons, ('PROMOTION_READY',))
        self.assertEqual(decision.fundamental_quality, 'unavailable')
        self.assertIsNotNone(decision.plan_token)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM strategy_assignment WHERE security_id=4").fetchone()[0], 0)

    def test_scope_structural_and_assignment_outcomes(self) -> None:
        self.assertEqual(evaluate_swing_promotion(self.conn, 3).recommendation, REJECT)
        self.assertIn('PROMOTION_NOT_ON_WATCHLIST', evaluate_swing_promotion(self.conn, 3).reasons)
        self.conn.execute("INSERT INTO positions(security_id, shares, currency) VALUES (4, 1, 'EUR')")
        self.conn.commit()
        self.assertIn('PROMOTION_EXISTING_POSITION', evaluate_swing_promotion(self.conn, 4).reasons)
        self.conn.execute("DELETE FROM positions WHERE security_id=4")
        self._assignment('long_term')
        self.assertIn('PROMOTION_STRATEGY_CONFLICT', evaluate_swing_promotion(self.conn, 4).reasons)

    def test_already_swing_and_open_campaign_do_not_duplicate(self) -> None:
        self._assignment('swing')
        already = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(already.recommendation, KEEP_WATCHING)
        self.assertIn('PROMOTION_ALREADY_SWING', already.reasons)
        self.conn.execute("DELETE FROM strategy_assignment WHERE security_id=4")
        self.conn.execute("CREATE TABLE swing_campaign(id INTEGER PRIMARY KEY, security_id INTEGER, status TEXT)")
        self.conn.execute("INSERT INTO swing_campaign(id, security_id, status) VALUES (1, 4, 'open')")
        self.conn.commit()
        self.assertIn('PROMOTION_OPEN_CAMPAIGN', evaluate_swing_promotion(self.conn, 4).reasons)

    def test_nonready_and_incomplete_technical_states_are_distinct(self) -> None:
        self.conn.execute("UPDATE market_data SET close=100, adjusted_close=100 WHERE security_id=4")
        self.conn.commit()
        watching = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(watching.recommendation, KEEP_WATCHING)
        self.assertIn('PROMOTION_PRICE_BELOW_OR_EQUAL_SMA50', watching.reasons)
        self.conn.execute("DELETE FROM market_data WHERE security_id=4")
        self.conn.commit()
        incomplete = evaluate_swing_promotion(self.conn, 4)
        self.assertEqual(incomplete.recommendation, DATA_INSUFFICIENT)
        self.assertIn('PROMOTION_REQUIRED_DATA_INCOMPLETE', incomplete.reasons)

    def test_evaluation_and_ranking_are_read_only(self) -> None:
        before = trading_analytics.rank_watchlist(self.conn)
        decisions = evaluate_swing_candidates(self.conn)
        after = trading_analytics.rank_watchlist(self.conn)
        self.assertTrue(any(item.security_id == 4 for item in decisions))
        self.assertEqual(before, after)
        self.assertFalse(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_promotion'").fetchone())

    def test_approval_writes_one_assignment_and_audit_only_after_valid_plan(self) -> None:
        self._approval_schema()
        decision = evaluate_swing_promotion(self.conn, 4)
        result = approve_swing_promotion(self.conn, security_id=4, plan_token=decision.plan_token, effective_from=self.today, approved_by='test')
        self.assertTrue(result['written'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM strategy_assignment WHERE security_id=4 AND strategy_type='swing'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidate_promotion WHERE security_id=4 AND status='promoted'").fetchone()[0], 1)
        repeated = approve_swing_promotion(self.conn, security_id=4, plan_token=decision.plan_token, effective_from=self.today, approved_by='test')
        self.assertTrue(repeated['idempotent'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM strategy_assignment WHERE security_id=4").fetchone()[0], 1)

    def test_stale_or_conflicting_approval_cannot_write(self) -> None:
        self._approval_schema()
        decision = evaluate_swing_promotion(self.conn, 4)
        self.conn.execute("UPDATE watchlist SET entry_reason='changed' WHERE security_id=4")
        self.conn.commit()
        with self.assertRaisesRegex(PromotionApprovalError, 'PROMOTION_PLAN_STALE'):
            approve_swing_promotion(self.conn, security_id=4, plan_token=decision.plan_token, effective_from=self.today, approved_by='test')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM strategy_assignment WHERE security_id=4").fetchone()[0], 0)
        decision = evaluate_swing_promotion(self.conn, 4)
        self._assignment('long_term')
        with self.assertRaises(PromotionApprovalError):
            approve_swing_promotion(self.conn, security_id=4, plan_token=decision.plan_token, effective_from=self.today, approved_by='test')


if __name__ == '__main__':
    unittest.main()
