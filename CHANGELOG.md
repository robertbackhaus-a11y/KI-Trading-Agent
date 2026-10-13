# Changelog

## [Unreleased]

### Removed
- The unused column `security.sector` (free text of a former import; nothing read or wrote it, the sector of the risk guards and of the news mapping comes from the sector map). New and reset databases no longer have it, the view `v_watchlist` no longer selects it, `get_watchlist` detail rows and the research prompt no longer carry it, and the new migration `Migrate-TradingDropSecuritySector.py` removes it from an existing database (dry run by default, backup copy, ids and all other fields unchanged; the old values are not copied into any sector map). `security.industry` stays. Run the migration after deploying; the sector map files are not touched.

### Changed
- Sector map resolution: a local entry of a symbol now overrides the public one (before, a symbol in both files was an error that switched the guards off); two sectors for a symbol inside one file are still an error. The risk guards and the news mapping share the rule (`sector_assignment.py`).
- Desktop GUI: the separate "change priority" dialog and button were removed; the priority is edited inline in the table (pending until the apply button saves it), so there is a single path for it. The MCP action `set_watchlist_priority` is unchanged.

### Added
- Editable sector group on the Watchlist page (MCP tools 21 -> 22): the new ACTION `set_security_sector` sets or removes the LOCAL sector entry of one security (only an existing group; `null`, or a sector equal to the public one, removes the local entry) and writes only `sector_map_local.json` (atomic, the previous file is kept as `.bak`); `sector_map_v1.json` and the database are never touched, and a held position or an open campaign does not block it. The table column "Sektor" shows the group and whether it comes from the public map, the local map or is not assigned (the replaced public value is in the tooltip); the same pending / apply mechanism as for strategy and priority applies. `get_watchlist` (table and detail shape) carries `sector`, `sector_source`, `sector_public` and `sector_groups`; the compact chat answer is unchanged.
- Inline editing of strategy and priority on the Watchlist page (MCP tools 20 → 21): the new ACTION `set_security_strategy` sets (`swing`, `long_term`) or removes the strategy of one security and writes only `strategy_assignment`; a held position (`BLOCKED_HELD_POSITION`) and a security with an open campaign (`BLOCKED_ACTIVE_CAMPAIGN`) are blocked without any write, and no campaign or order is created. The priority uses the existing `set_watchlist_priority`. A choice in the table is only a pending change until the apply button saves it, strategy first and the priority only if that succeeded; reloading with pending changes asks first. A not held security set to Swing is afterwards an entry candidate of the planner (simulation only). The chat and other MCP clients configured with `TRADING_EXCLUDE` do not see the action.

## [0.4.0] - 2026-10-13

### Added
- Desktop GUI (`gui/`, PySide6, development status): cockpit with Dashboard, Planner / Actions, Watchlist, Tasks (Run now starts the existing scheduled task after a confirmation), Chat and Diagnostics / Settings; it talks to the Trading MCP server as a stdio child process through the official MCP SDK with a deny-by-default tool policy; own environment and `requirements-gui.txt`; deployed through the manifest groups `gui*` of `Deploy-TradingAgent.ps1`; started from the desktop shortcut "KI TA". The chat (streamed local Ollama model with an own tool loop) is offered the 14 READ tools only, at most 6 tool rounds, Stop, optional thinking, history in memory only.
- Readable reason-code presentation in the desktop GUI (`gui/viewmodels/reason_codes.py`): technical codes are shown as short German texts, the hover tooltip keeps the explanation and the original code, a status that equals its reason is shown once, and a position that is sold or reduced no longer reads "hold"; the backend codes are unchanged.
- Watchlist management (MCP tools 15 → 19): `get_watchlist` (read-only; compact answer with a `summary` by default for chat clients, `table=true` for the table shape of the desktop GUI, `detail=true` for diagnosis) and the narrow actions `add_watchlist_candidate`, `remove_watchlist_candidate` and `set_watchlist_priority`, which write only the `watchlist` table in one atomic transaction and are never offered to the chat. The Watchlist page shows the whole watchlist with filters and sorting and offers add (only securities that are already in the Security Master), remove (with a confirmation) and change priority.
- Security onboarding (`onboard_security`, MCP tools 19 → 20, an ACTION): a security that is not yet in the Security Master is created from explicitly supplied values only, after a dry run (`READY` or every conflict: `ALREADY_EXISTS`, `ISIN_CONFLICT`, `SYMBOL_CONFLICT`, `WKN_CONFLICT`, `ALIAS_CONFLICT`, `INVALID_INPUT`), in one transaction with optional provider symbols (Yahoo Finance, FMP) and an optional watchlist entry. Nothing is researched or guessed, and no sector, strategy, campaign, position, order or market data is created. The desktop GUI offers it as "Neues Wertpapier aufnehmen" from the unknown-security message of the Watchlist add dialog. The chat and other MCP clients configured with `TRADING_EXCLUDE` do not see the write actions.
- Sector and correlation guards in the portfolio action planner (`portfolio_risk.py`): a purchase is deferred with `BLOCKED_BY_SECTOR_CONCENTRATION` or `BLOCKED_BY_CORRELATION` when its sector or its correlation cluster would exceed 30 % of the invested value; works for ADD/BUY of the position engine and new entries, sequentially and without re-ranking; external funding does not lift the guards; missing sector or price data never blocks.
- Security alias resolution for market intelligence and the opportunity view (`security_aliases.py`): one `security_id` per security, symbols/ISIN/WKN as aliases; a news query by public symbol, Yahoo symbol, other alias or ISIN returns the same security and the same events.
- Company-wire RSS source adapter on the existing market-intelligence pipeline (PR Newswire feeds, configurable in `universe/news_feeds_v1.json`), with per-feed health in the report and isolation of broken feeds.
- Deterministic cross-source deduplication of news (event id, URL, normalized headline within a time window); merged sources are listed as `duplicate_sources` in detail answers.
- Matching diagnostics in detail answers (`security_ids`, `matches`, `source_event_id`).

### Changed
- Free-text attribution of wire releases requires an explicit identification (ISIN, exchange-qualified ticker or full company name); a bare ticker word never matches.
- Market-intelligence events and reports remain readable by earlier versions (new fields are optional).
- The MCP tool set grows from 15 to 20 tools; the 15 earlier tools keep their description and schema.
- Dependency: `mcp` 2.2.0 → 2.3.0 (`requirements.txt`, `requirements-gui.txt`).

## [0.3.0] - 2026-10-09

### Added
- External funding in the Portfolio Action Planner (`metadata.external_funding_available`, default off): lifts only the cash/reserve shortage for BUY and ADD; the Swing maximum and the weight, data and campaign guards stay binding, and nothing is booked
- Generic canonical CSV transaction import (`Import-TradingTransactions.py --format canonical`), independent of any broker or portfolio tool
- Parqet source adapter on the same import path (`--format parqet`); cumulative exports, duplicate and historical detection and the dry run keep working
- Strategy-aware initialization: `--strategy`, `--create-securities`, `effective_from` taken from the start of the held position
- Automatic Swing-campaign initialization and reconciliation, with `--campaign-opened-at` for starts that cannot be derived (for example after a transfer-in)
- Complete dry run on an in-memory copy of the database, and a result with `validation_status` and `open_items`
- Import documentation in German and English (`docs/trading-import.de.md`, `docs/trading-import.en.md`) and a synthetic example CSV (`examples/trading-import-example.csv`)
- GitHub CodeQL, Dependency Review and Dependabot (GitHub Actions and Python), with SHA-pinned actions
- `requirements.txt` with the direct runtime dependencies at their tested versions

### Changed
- The Parqet importer is now a thin adapter on the provider-neutral import engine `transaction_import.py` (the former names are still exported); `Import-ParqetTransactions.py` runs `Import-TradingTransactions.py --format parqet`
- `imports.records_failed` now counts only rows that could not be processed

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
