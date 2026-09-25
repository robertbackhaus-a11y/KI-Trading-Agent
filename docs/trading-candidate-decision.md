# Watchlist candidate decision status

`candidate_decision.evaluate_watchlist_candidates(connection, as_of=None)` is
read-only and returns one `CandidateDecision` per `watchlist.status = 'WATCH'`
entry: `security_id`, `symbol`, `analytics_score`, `analytics_quality`,
`strategy`, `campaign_status`, `portfolio_constraints`, `decision_status`,
`decision_reasons`.

It does not replace or duplicate `swing_promotion.py`. That module answers
*"should this watchlist entry become a Swing strategy assignment"* (see
[trading-swing-promotion.md](trading-swing-promotion.md)). This module
answers a narrower, later question — *"given today's analytics, the current
strategy assignment, any open campaign, and the current portfolio allocation,
is a fresh BUY currently justified"* — by composing the outputs of already
existing, already tested functions rather than recomputing any of them:

- `trading_analytics.rank_watchlist()` → `analytics_score` / `analytics_quality`
- `swing_promotion._active_assignment()` → `strategy`
- `swing_promotion._open_campaign()` → `campaign_status`
- `analysis_engine.build_portfolio_context()` /
  `portfolio_context.derive_allocation_guardrails()` → `portfolio_constraints`

`decision_status` is exactly one of `BUY`, `WATCH`, `DEFERRED`,
`INSUFFICIENT_DATA`, decided in this fixed order:

1. `analytics_quality != "OK"` → `INSUFFICIENT_DATA`
2. no active `strategy_assignment` → `DEFERRED`
3. an open Swing campaign already exists → `WATCH`
4. the active assignment is not `swing` → `WATCH`
5. the portfolio allocation guardrail state is unavailable → `DEFERRED`
6. the `SWING_ALLOCATION_ABOVE_MAX` guardrail is set → `DEFERRED`
7. the analytics score is unavailable → `WATCH`
8. the analytics score is `>= BUY_SCORE_THRESHOLD` (20.0, a named constant in
   `candidate_decision.py`) → `BUY`
9. otherwise → `WATCH`

Every branch carries an explicit `decision_reasons` entry; nothing is
inferred silently. Missing or indeterminate inputs (limited/insufficient
price history, an unclassifiable portfolio denominator, a missing score)
resolve to `INSUFFICIENT_DATA` or `DEFERRED` rather than defaulting to `BUY`.

This function places no order, writes nothing, and never opens a Swing
campaign. A `BUY` status is an input to the still-separate, still-manual
promotion/approval/execution sequence described in
[trading-swing-promotion.md](trading-swing-promotion.md); it is not itself an
execution trigger.

## OpenWebUI exposure

Exposed read-only via the OpenWebUI `trading_sqlite` tool as
`evaluate_watchlist_candidates(as_of=None)`, which calls this function
unchanged — no decision logic is duplicated in the tool layer. See
[openwebui-trading-tool.md](openwebui-trading-tool.md).
