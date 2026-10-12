"""Synthetic orchestrator results for the GUI tests, produced by the REAL orchestrator and planner (so the shape is the real contract)."""

from __future__ import annotations

import json

import support  # noqa: F401  (import paths)
import test_portfolio_risk_guards as risk_tests  # module import: its test classes are not collected here


def orchestrator_envelope(held_value: float = 5_000.0) -> dict:
    """``run_trading_orchestrator(detail=true)`` of a synthetic portfolio.

    ``held_value`` is the held position in the candidate's sector: 5_000 leaves room (the candidate is planned), 40_000 lets the sector guard
    defer it (``BLOCKED_BY_SECTOR_CONCENTRATION``).
    """
    case = risk_tests.OrchestratorGuardTests("test_orchestrator_blocks_by_sector_and_stays_a_simulation")
    case.setUp()
    try:
        result = case.run_with(held_value, as_of="2026-09-23")  # the synthetic closes end on this date (not stale)
    finally:
        case.tearDown()
    return json.loads(json.dumps({"ok": True, "operation": "run_trading_orchestrator", "result": result.primitive(), "detail": True}, default=str))
