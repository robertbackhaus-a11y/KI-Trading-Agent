# Changelog

## [0.2.0] - 2026-10-09

### Added
- Deterministic Portfolio Action Planner (`portfolio_action_plan`): post-action capital, PROMOTE entry evaluation, ranking and sizing — simulation only, no orders
- Net proceeds / tax simulation for planned SELL/TRIM actions (average-cost basis, conservative view)
- Multi-currency FX support (ECB reference rates for USD, GBP, AUD and KRW; GBp pence quotes are normalized to GBP)
- Point-in-time visibility rules for fundamentals (`filing_date` / `fetched_at`)
- Swing campaign reconciliation for Parqet imports (transaction-linked `add` / `manual_reduction` events; ambiguous cases write nothing)
- Candidate Discovery over a versioned large-cap universe (read-only; report plus the MCP reader `get_candidate_discovery`)
- Market Intelligence from official feeds, SEC filings and Yahoo headlines (rule-based classification; report plus the MCP reader `get_market_intelligence`)
- Unified Opportunity View (`get_opportunity_view`, MCP tool no. 15)
- Automated Discovery and Market Intelligence scheduled tasks
- Versioned deployment workflow (`deploy/Deploy-TradingAgent.ps1`: Check / Deploy / Rollback)
- Versioned test suite (`tests/trading`, 601 tests, synthetic data only)
- Bilingual DE/EN technical and query documentation

### Changed
- MCP responses optimized for lower context usage (compact default, `detail=true` on request, response size guard)
- Frontend-neutral MCP architecture and documentation
- Repository and privacy cleanup: synthetic test data, neutral default SEC User-Agent (set `SEC_USER_AGENT` for production use)
- Public/private sector-map split (`sector_map_v1.json` is public; an optional local `sector_map_local.json` is merged by the collector)
- Deployment and scheduler documentation updated

### Fixed
- Point-in-time handling of fundamentals: the same visibility rule on the current and the explicit `as_of` path; rows without a publication date are capped at `partial`
- Stale or inconsistent documentation corrected (scheduler, deployment, test tracking, task names, examples)

### Removed
- `Canonicalize-TradingUniverse.py` (hand-maintained security reference list)

### Operations / Deployment
- 6 scheduled trading tasks (market data, FX rates, events/news, SEC fundamentals, candidate discovery, market intelligence)
- Explicit runtime manifest (`deploy/deploy-manifest.json`)
- Backup, hash, integrity and MCP smoke verification on every deploy
- Protected local sector map (never overwritten or created by the deploy)

### Documentation
- README English-only
- 6 complete DE/EN documentation pairs
- Query examples for all 15 MCP tools

## [0.1.1] - 2026-09-28

### Added
- Swing horizon (`horizon_months_min`/`horizon_months_max`) is now functionally enforced — the runner closes automatically from `horizon_months_max` onward (`SWING_MAX_HORIZON_REACHED`)
- Productive events/news pipeline (`Backfill-TradingEventsNews.py`): `events` from SEC EDGAR (8-K/6-K), `news` from Yahoo Finance search
- Automated daily/weekly backfills as Windows Scheduled Tasks: market data, FX rates, SEC fundamentals, events/news

### Changed
- Swing TP1/TP2 quantity logic is derived centrally from `SwingStrategyConfig` instead of hardcoded literals
- `decision_engine._confidence()`: `valuation`/`event_risk` UNAVAILABLE now acts neutrally instead of as a penalty
- CLI database path parameter unified to `--db-path` across all tools (`--db` remains a working alias)
- Various runtime/query optimizations in the orchestrator runtime path

### Fixed
- Removed the unused `RiskTargetConfig` (without defined semantics)
- Removed the dead schema tables `decisions`/`analysis_history`
- The `Migrate-TradingStrategyAssignments.py` dry run now structurally opens a read-only connection (`mode=ro` + `PRAGMA query_only`)

### Known limitations
- estimates/ratings/price_targets currently have no productive data source
- Yahoo quoteSummary requires authenticated/browser-style access
- FMP endpoints required for these data are not available under the chosen home/free plan
- valuation quality therefore remains UNAVAILABLE where no other valuation data exists

## v0.1.0

First functional release of the Trading Agent.

Included:
- Trading DB and schema
- Security master data / symbol resolution
- Market data and market snapshot
- Fundamentals research / SEC / IR
- FX support
- Portfolio context and allocation guardrails
- Strategy assignment
- Swing campaign lifecycle
- Entry/add sizing
- Analytics and watchlist ranking
- Candidate decision
- Strategy suggestion
- Swing promotion
- Decision engine
- Trading orchestrator
- Parqet import
- Corporate actions / split handling
- OpenWebUI trading tool
- CLI/management tools
- Technical documentation
- Apache-2.0 license
- Security / contributing policy
- 218 tests
