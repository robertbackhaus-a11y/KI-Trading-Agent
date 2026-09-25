# Watchlist strategy suggestion

`strategy_suggestion.suggest_strategy_assignments(connection, as_of=None)` is
read-only and returns one `StrategySuggestion` (`security_id`, `symbol`,
`suggested_strategy`, `confidence`, `reasons`) for every
`watchlist.status = 'WATCH'` entry that currently has **no active**
`strategy_assignment`. Entries that already have one are skipped entirely --
never overridden, never re-suggested.

**A suggestion is not an assignment.** This function writes nothing. Turning
a `swing` suggestion into a real `strategy_assignment` row still requires the
separate, existing, explicit
[`swing_promotion.approve_swing_promotion`](trading-swing-promotion.md) path
with its own plan-token and eligibility checks. This module does not call
that path and does not open a Swing campaign. There is currently no
promotion path for `long_term` at all; a `long_term` suggestion is purely
informational.

It reuses rather than reimplements:

- `trading_analytics.rank_watchlist()` for the analytics score/quality.
- `swing_promotion._active_assignment()` for the existing-assignment filter.
- `swing_promotion._open_campaign()` for the open-campaign check.

`suggested_strategy` is exactly one of `swing`, `long_term`, `unknown`,
decided in this fixed order:

1. `analytics_quality != "OK"` → `unknown` (confidence `0.0`)
2. an open Swing campaign already exists for the security → `swing`
   (confidence `0.9` -- it is already being actively traded as Swing)
3. `security.asset_type` is `etf` or `fund` → `long_term` (confidence `0.7`)
4. the momentum/trend score is `>= SWING_SUGGESTION_SCORE_THRESHOLD` (`20.0`,
   a named constant) → `swing` (confidence `0.6`)
5. otherwise → `unknown` (confidence `0.0`)

**`long_term` is never derived from the momentum/trend score.** The only
non-momentum long-term signal available in this codebase today is
`asset_type` (`analysis_engine.py` already treats `{"etf", "fund"}` as a
non-stock category for fundamentals purposes; this module reuses the same
constant, `LONG_TERM_ASSET_TYPES`). A stock can therefore only ever resolve
to `swing` or `unknown` here, never `long_term`, until a genuine
fundamentals-based long-term signal exists elsewhere in this codebase. All
thresholds and confidence values are named module-level constants in
`strategy_suggestion.py`, not inline heuristics.

## OpenWebUI exposure

Exposed read-only via the OpenWebUI `trading_sqlite` tool as
`suggest_strategy_assignments(as_of=None)`, which calls this function
unchanged — no suggestion logic is duplicated in the tool layer. See
[openwebui-trading-tool.md](openwebui-trading-tool.md).
