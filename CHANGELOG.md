# Changelog

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
