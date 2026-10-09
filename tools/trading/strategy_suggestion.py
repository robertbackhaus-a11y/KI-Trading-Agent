"""Read-only Swing/Long-Term strategy suggestions for watchlist candidates.

Produces a non-binding suggestion for every ``watchlist.status = 'WATCH'``
entry that currently has **no active** ``strategy_assignment`` row. A
suggestion is diagnostic input for a human decision -- it never writes a
``strategy_assignment`` row itself. Turning a suggestion into an actual
assignment remains the separate, existing, explicit
``swing_promotion.approve_swing_promotion`` path (see
``docs/trading-swing-promotion.md``); this module never calls it and never
touches the database.

Reuses rather than reimplements:

- :func:`trading_analytics.rank_watchlist` for the analytics score/quality.
- :func:`swing_promotion._active_assignment` / ``_open_campaign`` for the
  existing-assignment filter and the open-campaign check.

Design constraint (explicit, not a hidden heuristic): a ``long_term``
suggestion is **never** derived from the momentum/trend score. This
codebase currently has exactly one non-momentum long-term signal -- the
security's ``asset_type`` (``analysis_engine.py`` already treats
``{"etf", "fund"}`` as a non-stock category for fundamentals purposes; this
module reuses the same two values as ``LONG_TERM_ASSET_TYPES``). Every
currently long_term-assigned production holding is in fact an ETF, which is
the empirical basis for reusing this signal here -- but it is applied as an
explicit, named, overridable constant, not silently baked into the logic.
A stock (``asset_type`` outside that set) can therefore only ever resolve to
``swing`` or ``unknown`` here, never ``long_term``, until a genuine
fundamentals-based long-term signal exists in this codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import sqlite3
from typing import Optional

from analysis_contracts import to_primitive
from swing_promotion import _active_assignment, _open_campaign
from trading_analytics import rank_watchlist


SWING = "swing"
LONG_TERM = "long_term"
UNKNOWN = "unknown"

# Asset types this module treats as a non-momentum long-term basis. Reuses
# the exact categorization analysis_engine.py already uses for
# fundamentals-not-applicable securities, rather than inventing a new one.
LONG_TERM_ASSET_TYPES = ("etf", "fund")

# Named, visible threshold for a swing suggestion from momentum. Same value
# and rationale as candidate_decision.BUY_SCORE_THRESHOLD (rank_watchlist's
# momentum_score components are each roughly in [-10, +10]; +20 needs at
# least two clearly bullish signals with nothing strongly bearish
# offsetting them). Kept as its own constant in this module -- a future
# change to one suggestion's tuning should not silently retune the other.
SWING_SUGGESTION_SCORE_THRESHOLD = 20.0

# Named confidence values per suggestion basis (0.0-1.0). Fixed, not a
# computed/hidden score, so the rationale for each number stays visible
# and reviewable here rather than inline in the decision logic below.
CONFIDENCE_OPEN_CAMPAIGN = 0.9      # already actively traded as swing
CONFIDENCE_ETF_LONG_TERM = 0.7      # structural asset_type basis
CONFIDENCE_SWING_MOMENTUM = 0.6     # technical basis only, no fundamentals
CONFIDENCE_UNKNOWN = 0.0


@dataclass(frozen=True)
class StrategySuggestion:
    security_id: int
    symbol: Optional[str]
    suggested_strategy: str
    confidence: float
    reasons: tuple[str, ...]

    def primitive(self) -> dict:
        return to_primitive(self)


def suggest_strategy_assignments(
    connection: sqlite3.Connection,
    as_of: Optional[str] = None,
) -> list[StrategySuggestion]:
    """Non-binding strategy suggestion for every unassigned WATCH entry.

    ``as_of`` defaults to today. Entries that already have an active
    ``strategy_assignment`` as of that date are skipped entirely -- an
    existing assignment is never overridden or re-suggested.
    """
    evaluation_date = as_of or date.today().isoformat()

    suggestions: list[StrategySuggestion] = []
    for entry in rank_watchlist(connection):
        security_id = int(entry["security_id"])
        symbol = entry["symbol"]
        analytics_score = entry["score"]
        analytics_quality = entry["quality"]

        if _active_assignment(connection, security_id, evaluation_date) is not None:
            continue

        security = connection.execute(
            "SELECT asset_type FROM security WHERE id = ?", (security_id,)
        ).fetchone()
        asset_type = (security["asset_type"] or "").lower() if security else ""

        strategy: str
        confidence: float
        reasons: tuple[str, ...]

        if analytics_quality != "OK":
            strategy, confidence, reasons = UNKNOWN, CONFIDENCE_UNKNOWN, (f"ANALYTICS_QUALITY_{analytics_quality}",)
        elif _open_campaign(connection, security_id):
            strategy, confidence, reasons = SWING, CONFIDENCE_OPEN_CAMPAIGN, ("OPEN_SWING_CAMPAIGN_INDICATES_SWING",)
        elif asset_type in LONG_TERM_ASSET_TYPES:
            strategy, confidence, reasons = LONG_TERM, CONFIDENCE_ETF_LONG_TERM, (f"ASSET_TYPE_{asset_type.upper()}_INDICATES_LONG_TERM",)
        elif analytics_score is not None and analytics_score >= SWING_SUGGESTION_SCORE_THRESHOLD:
            strategy, confidence, reasons = SWING, CONFIDENCE_SWING_MOMENTUM, ("MOMENTUM_SCORE_ABOVE_THRESHOLD",)
        else:
            strategy, confidence, reasons = UNKNOWN, CONFIDENCE_UNKNOWN, ("NO_SUFFICIENT_STRATEGY_BASIS",)

        suggestions.append(
            StrategySuggestion(
                security_id=security_id,
                symbol=symbol,
                suggested_strategy=strategy,
                confidence=confidence,
                reasons=reasons,
            )
        )

    return suggestions
