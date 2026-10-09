# Watchlist-to-Swing promotion

Generic `watchlist.status = 'WATCH'` expresses research interest only.  It
does not make a security a Swing candidate and it never causes a trade.

`swing_promotion.evaluate_swing_promotion()` is read-only and returns exactly
one of `PROMOTE`, `KEEP_WATCHING`, `REJECT`, or `DATA_INSUFFICIENT`.  The
constructive trend gate is price above SMA50 and SMA200, with SMA50 above
SMA200.  Research and fundamental data are diagnostics only until an
authoritative research threshold exists.  Portfolio cash and allocation are
intentionally excluded; they belong to initial-entry sizing.

An explicit approved `PROMOTE` plan can be applied once through its current
plan token.  The operation rechecks active watchlist status, zero position,
no open campaign, no conflicting strategy assignment, and all evaluated
market/technical state before it inserts exactly one `strategy_assignment`
with `strategy_type = 'swing'`.  `candidate_promotion` records that approval
for audit.  No order, transaction, position, capital state, or campaign is
created.

The controlled sequence remains:

`WATCH` → `promotion evaluation` → `explicit approval` → `swing strategy
assignment` → `Phase 3B.7 BUY recommendation` → `explicit execution` →
`authoritative transaction` → `explicit campaign opening`.
