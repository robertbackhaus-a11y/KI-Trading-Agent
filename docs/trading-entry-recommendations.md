# Swing entry recommendations

Phase 3B.7 produces a `BUY` recommendation only for a security with an
active explicit `strategy_assignment` of `swing`, no open position, and no
open Swing campaign.  The generic `watchlist` is not an entry-candidate
authority because its `WATCH` status does not encode a strategy.

The recommendation is read-only.  It neither creates an order, transaction,
capital-state record, nor a Swing campaign.  The intended controlled workflow
is:

`BUY recommendation` → `explicit user approval / execution` → `authoritative
transaction import or execution record` → `position exists` → `explicit
campaign-open operation` → `lifecycle begins`.

EUR sizing uses the FX-normalized candidate quote and post-trade portfolio
denominators.  Native market prices are retained for technical comparisons.

Blocker precedence is candidate scope and current position/campaign first,
then complete portfolio valuation/allocation, the current Swing ceiling,
cash availability/reserve, and finally technical entry inputs.  This makes a
currently overweight Swing allocation an unconditional `ENTRY_SWING_ALLOCATION_LIMIT`.
