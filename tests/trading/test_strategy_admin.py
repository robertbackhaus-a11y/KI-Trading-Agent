# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""set_security_strategy: the narrow strategy write (A-Q); synthetic securities and the real schema only."""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import strategy_admin as sta  # noqa: E402
import test_watchlist_admin as twa  # noqa: E402

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
LONG_AGO = "2026-01-01"
# the fixture: 3 = held, swing, open campaign, on the watchlist; 4 / 5 = watchlist / plain, no strategy; 1, 2 = plain; 6 = inactive


class StrategyCase(twa.WatchlistDbCase):
    def strategy(self, security_id, strategy) -> dict:
        return asyncio.run(self.tools.set_security_strategy(security_id, strategy))

    def assignments(self, security_id: int) -> list[tuple]:
        with self.connect() as conn:
            return [tuple(r) for r in conn.execute("SELECT strategy_type, effective_from, effective_to, source FROM strategy_assignment WHERE security_id = ? ORDER BY effective_from, id", (security_id,))]

    def seed(self, security_id: int, strategy: str, start: str = LONG_AGO, end: str | None = None) -> int:
        with self.connect() as conn:
            cursor = conn.execute("INSERT INTO strategy_assignment(security_id, strategy_type, effective_from, effective_to, source) VALUES (?, ?, ?, ?, 'test')", (security_id, strategy, start, end))
            conn.commit()
            return cursor.lastrowid

    def changed_tables(self, before: dict[str, str]) -> set[str]:
        after = self.fingerprint(exclude=())
        return {name for name in after if after[name] != before.get(name)} - {"sqlite_sequence"}


class SetAndChangeTests(StrategyCase):
    def test_a_a_strategy_is_set_for_a_security_that_is_not_held(self) -> None:
        before = self.fingerprint(exclude=())
        result = self.strategy(4, "swing")
        self.assertEqual((result["ok"], result["result"], result["changed"], result["strategy"], result["previous_strategy"]), (True, "STRATEGY_SET", True, "swing", None))
        self.assertEqual(self.assignments(4), [("swing", TODAY, None, "manual")])
        self.assertEqual(self.changed_tables(before), {"strategy_assignment"})  # J, K, L, M, N: nothing else moves (watchlist, positions, campaigns, ...)

    def test_b_a_strategy_started_today_is_corrected_in_place(self) -> None:
        self.strategy(4, "swing")
        result = self.strategy(4, "long_term")
        self.assertEqual((result["result"], result["previous_strategy"]), ("STRATEGY_SET", "swing"))
        self.assertEqual(self.assignments(4), [("long_term", TODAY, None, "manual")])  # one row, no overlap, no history invented

    def test_b_an_older_strategy_is_closed_yesterday_and_the_new_one_starts_today(self) -> None:
        self.seed(2, "swing")
        result = self.strategy(2, "long_term")
        self.assertEqual((result["result"], result["previous_strategy"]), ("STRATEGY_SET", "swing"))
        self.assertEqual(self.assignments(2), [("swing", LONG_AGO, YESTERDAY, "test"), ("long_term", TODAY, None, "manual")])  # the history is kept

    def test_c_a_strategy_is_removed(self) -> None:
        self.seed(2, "swing")
        result = self.strategy(2, None)
        self.assertEqual((result["result"], result["changed"], result["strategy"]), ("STRATEGY_REMOVED", True, None))
        self.assertEqual(self.assignments(2), [("swing", LONG_AGO, YESTERDAY, "test")])  # closed, not deleted
        self.strategy(4, "swing")
        self.assertEqual(self.strategy(4, None)["result"], "STRATEGY_REMOVED")  # started today: undone
        self.assertEqual(self.assignments(4), [])
        self.assertEqual(self.strategy(4, "none")["result"], "UNCHANGED")

    def test_a_strategy_can_be_set_again_after_it_was_removed(self) -> None:
        self.seed(2, "swing")
        self.strategy(2, None)
        self.assertEqual(self.strategy(2, "long_term")["result"], "STRATEGY_SET")
        self.assertEqual([a[0] for a in self.assignments(2)], ["swing", "long_term"])

    def test_i_the_same_value_is_idempotent(self) -> None:
        self.strategy(4, "swing")
        before = self.fingerprint(exclude=())
        for value in ("swing", " SWING "):
            result = self.strategy(4, value)
            self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "UNCHANGED", False))
        self.assertEqual(self.strategy(5, None)["result"], "UNCHANGED")  # nothing to remove
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_p_the_planner_sees_a_swing_security_as_an_entry_candidate(self) -> None:
        import trading_orchestrator as orch

        def entries() -> list[int]:
            with self.connect() as conn:
                return orch._active_swing_zero_positions(conn, TODAY)

        self.assertNotIn(4, entries())
        self.strategy(4, "swing")
        self.assertIn(4, entries())  # expected and documented: a swing candidate that is not held is planned (simulation only)
        self.strategy(4, "long_term")
        self.assertNotIn(4, entries())

    def test_q_no_campaign_is_created_and_no_order(self) -> None:
        before = self.fingerprint(exclude=())
        self.strategy(4, "swing")
        after = self.fingerprint(exclude=())
        for table in ("swing_campaign", "swing_campaign_event", "positions", "transactions", "candidate_promotion", "watchlist", "market_data"):
            self.assertEqual(after[table], before[table], table)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM swing_campaign WHERE security_id = 4").fetchone()[0], 0)
            self.assertEqual([r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%order%'")], [])


class BlockAndValidationTests(StrategyCase):
    def test_d_an_invalid_strategy(self) -> None:
        before = self.fingerprint(exclude=())
        for value in ("tactical", "unknown", "buy", 5, True, ["swing"], "swing;DROP TABLE security"):
            with self.subTest(value=value):
                result = self.strategy(4, value)
                self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "INVALID_STRATEGY", False))
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_e_an_unknown_security(self) -> None:
        before = self.fingerprint(exclude=())
        self.assertEqual(self.strategy(9999, "swing")["result"], "NOT_FOUND")
        for value in ("abc", None, 1.5, True, ["1"], ""):
            with self.subTest(value=value):
                self.assertEqual(self.strategy(value, "swing")["result"], "INVALID_IDENTIFIER")
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_the_order_of_the_checks_security_before_strategy(self) -> None:
        self.assertEqual(self.strategy(9999, "tactical")["result"], "NOT_FOUND")  # 1 before 2

    def test_f_h_a_held_position_is_blocked_and_nothing_is_written(self) -> None:
        before = self.fingerprint(exclude=())
        for value in ("long_term", None, "swing"):  # even the value it already has: the held check comes first
            with self.subTest(value=value):
                result = self.strategy(3, value)
                self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "BLOCKED_HELD_POSITION", False))
                self.assertIn("gehaltenen Position", result["message"])
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_g_h_an_open_campaign_is_blocked_and_nothing_is_written(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE positions SET shares = 0 WHERE security_id = 3")  # not held any more, but the campaign is still open
            conn.commit()
        before = self.fingerprint(exclude=())
        result = self.strategy(3, "long_term")
        self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "BLOCKED_ACTIVE_CAMPAIGN", False))
        self.assertIn("aktiven Campaign", result["message"])
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_a_closed_campaign_does_not_block(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE positions SET shares = 0 WHERE security_id = 3")
            conn.execute("UPDATE swing_campaign SET status = 'closed', closed_at = '2026-02-01' WHERE security_id = 3")
            conn.commit()
        result = self.strategy(3, "long_term")
        self.assertEqual(result["result"], "STRATEGY_SET")
        self.assertEqual([a[0] for a in self.assignments(3)], ["swing", "long_term"])  # the closed campaign keeps pointing at the (now closed) swing row

    def test_an_unclear_history_writes_nothing(self) -> None:
        self.seed(1, "swing", start=TODAY, end=(date.today() + timedelta(days=30)).isoformat())  # ends in the future
        self.seed(2, "swing", start=(date.today() + timedelta(days=5)).isoformat())  # starts in the future
        with self.connect() as conn:  # an assignment of today that a campaign refers to
            conn.execute("INSERT INTO positions(security_id, shares, avg_cost, currency) VALUES (7, 0, 1, 'EUR')")
            conn.commit()
        assignment = self.seed(7, "swing", start=TODAY)
        with self.connect() as conn:
            conn.execute("INSERT INTO swing_campaign(security_id, strategy_assignment_id, opened_at, original_quantity, status, closed_at, source) "
                         "VALUES (7, ?, ?, 1, 'closed', ?, 'test')", (assignment, TODAY, TODAY))
            conn.commit()
        before = self.fingerprint(exclude=())
        for sid in (1, 2, 7):
            with self.subTest(security_id=sid):
                result = self.strategy(sid, "long_term")
                self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "ASSIGNMENT_HISTORY_UNCLEAR", False))
        self.assertEqual(self.fingerprint(exclude=()), before)

    def test_a_failure_rolls_back_and_is_reported(self) -> None:
        self.seed(2, "swing")
        with self.connect() as conn:
            conn.execute("CREATE TRIGGER trg_test_block_assignment BEFORE INSERT ON strategy_assignment BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END")
            conn.commit()
        before = self.fingerprint(exclude=())
        result = self.strategy(2, "long_term")
        self.assertEqual((result["ok"], result["result"], result["changed"]), (False, "ERROR", False))
        self.assertEqual(self.fingerprint(exclude=()), before)  # the close of the old row was undone as well

    def test_o_the_priority_action_is_unchanged_and_independent(self) -> None:
        self.strategy(4, "swing")
        result = self.run_tool("set_watchlist_priority", 4, 9)
        self.assertEqual((result["result"], result["priority"]), ("PRIORITY_SET", 9))
        self.assertEqual(self.watchlist()[4], ("WATCH", 9))
        self.assertEqual(self.assignments(4), [("swing", TODAY, None, "manual")])  # the priority write does not touch the strategy


class ToolSurfaceTests(unittest.TestCase):
    def test_the_signature_is_narrow(self) -> None:
        params = inspect.signature(twa.tools_class().set_security_strategy).parameters
        self.assertEqual(sorted(p for p in params if p != "self"), ["security_id", "strategy"])

    def test_the_module_writes_only_strategy_assignment(self) -> None:
        source = (ROOT / "tools" / "trading" / "strategy_admin.py").read_text(encoding="utf-8")
        written = set(re.findall(r"(?:INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)", source))
        self.assertEqual(written, {"strategy_assignment"})


if __name__ == "__main__":
    unittest.main()
