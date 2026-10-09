"""Read-only watchlist-candidate BUY/WATCH/DEFERRED decision layer.

Combines four existing, already-tested building blocks into one structured
per-security status -- it deliberately reimplements none of them:

- :func:`trading_analytics.rank_watchlist` for the analytics score/quality.
- :func:`swing_promotion._active_assignment` / ``_open_campaign`` for the
  current strategy assignment and open Swing-campaign lookup.
- :func:`analysis_engine.build_portfolio_context` /
  :func:`portfolio_context.derive_allocation_guardrails` for the portfolio
  allocation guardrail state (already exposed as
  ``PortfolioContext.allocation_guardrails``).

No orders, no DB writes, no campaign openings, no external calls. Missing
inputs are never guessed: they surface as ``INSUFFICIENT_DATA`` or
``DEFERRED`` with an explicit reason instead of silently defaulting to a
permissive outcome.

Decision flow (evaluated in this exact order for every WATCH-status
watchlist entry)::

    1. analytics_quality != "OK"                -> INSUFFICIENT_DATA
    2. no active strategy_assignment             -> DEFERRED
    3. an open Swing campaign already exists      -> WATCH
    4. strategy assignment is not "swing"         -> WATCH
    5. portfolio allocation guardrails unavailable -> DEFERRED
    6. "SWING_ALLOCATION_ABOVE_MAX" guardrail set  -> DEFERRED
    7. analytics score unavailable                -> WATCH
    8. analytics score >= BUY_SCORE_THRESHOLD      -> BUY
    9. otherwise                                   -> WATCH

Steps 1-3 and 6 are the rules given in the Phase spec verbatim. Steps 4, 5
and 7 are necessary completions of the same state machine (a non-swing
assignment, an indeterminate portfolio denominator, and a missing score can
all occur and must resolve to *some* explicit, documented status rather
than falling through to an implicit BUY).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import sqlite3
from typing import Optional

from analysis_contracts import AvailabilityStatus, to_primitive
from analysis_engine import build_portfolio_context
from swing_promotion import _active_assignment, _open_campaign
from trading_analytics import rank_watchlist


BUY = "BUY"
WATCH = "WATCH"
DEFERRED = "DEFERRED"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# Named, visible threshold (deliberately not hidden inside a conditional).
# rank_watchlist's momentum_score sums components that are each roughly in
# [-10, +10]; +20 requires at least two clearly bullish signals (e.g. price
# above both SMAs plus positive momentum) with nothing strongly bearish
# offsetting them. This is a conservative starting point, not backtested --
# tune it here, not inline in the decision logic below.
BUY_SCORE_THRESHOLD = 20.0


@dataclass(frozen=True)
class CandidateDecision:
    security_id: int
    symbol: Optional[str]
    analytics_score: Optional[float]
    analytics_quality: str
    strategy: Optional[str]
    campaign_status: str
    portfolio_constraints: tuple[str, ...]
    decision_status: str
    decision_reasons: tuple[str, ...]

    def primitive(self) -> dict:
        return to_primitive(self)


def evaluate_watchlist_candidates(
    connection: sqlite3.Connection,
    as_of: Optional[str] = None,
) -> list[CandidateDecision]:
    """Read-only BUY/WATCH/DEFERRED/INSUFFICIENT_DATA status per WATCH entry.

    ``as_of`` defaults to today, matching every other as_of-based function in
    this codebase. Never writes, never opens a campaign, never places an
    order.
    """
    evaluation_date = as_of or date.today().isoformat()

    portfolio = build_portfolio_context(as_of=as_of, connection=connection)
    allocation_available = portfolio.allocation_quality.status is AvailabilityStatus.AVAILABLE
    guardrails = portfolio.allocation_guardrails

    decisions: list[CandidateDecision] = []
    for entry in rank_watchlist(connection):
        security_id = int(entry["security_id"])
        symbol = entry["symbol"]
        analytics_score = entry["score"]
        analytics_quality = entry["quality"]

        assignment = _active_assignment(connection, security_id, evaluation_date)
        strategy = assignment["strategy_type"] if assignment is not None else None
        has_open_campaign = _open_campaign(connection, security_id)
        campaign_status = "OPEN" if has_open_campaign else "NONE"

        status: str
        reasons: tuple[str, ...]

        if analytics_quality != "OK":
            status, reasons = INSUFFICIENT_DATA, (f"ANALYTICS_QUALITY_{analytics_quality}",)
        elif assignment is None:
            status, reasons = DEFERRED, ("NO_STRATEGY_ASSIGNMENT",)
        elif has_open_campaign:
            status, reasons = WATCH, ("OPEN_SWING_CAMPAIGN_BLOCKS_NEW_BUY",)
        elif strategy != "swing":
            status, reasons = WATCH, ("STRATEGY_NOT_SWING",)
        elif not allocation_available:
            status, reasons = DEFERRED, ("PORTFOLIO_ALLOCATION_UNAVAILABLE",)
        elif "SWING_ALLOCATION_ABOVE_MAX" in guardrails:
            status, reasons = DEFERRED, ("SWING_ALLOCATION_ABOVE_MAX",)
        elif analytics_score is None:
            status, reasons = WATCH, ("SCORE_UNAVAILABLE",)
        elif analytics_score >= BUY_SCORE_THRESHOLD:
            status, reasons = BUY, ("SCORE_AT_OR_ABOVE_THRESHOLD",)
        else:
            status, reasons = WATCH, ("SCORE_BELOW_THRESHOLD",)

        decisions.append(
            CandidateDecision(
                security_id=security_id,
                symbol=symbol,
                analytics_score=analytics_score,
                analytics_quality=analytics_quality,
                strategy=strategy,
                campaign_status=campaign_status,
                portfolio_constraints=guardrails,
                decision_status=status,
                decision_reasons=reasons,
            )
        )

    return decisions
