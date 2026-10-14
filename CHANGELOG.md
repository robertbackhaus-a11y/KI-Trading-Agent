# Changelog

## [Unreleased]

### Added
- Transaction import in one flow (MCP tools 25 -> 27, page "Import" of the desktop GUI): a Parqet export (or the canonical CSV) is analysed, compared with the database, the difference is shown, and after a check that would change something it is imported and reconciled. `import_workflow.py` adds the report on top of the existing importer: the difference (new securities, supplemented master data, master data conflicts, new and existing transactions, rows with a problem, expected position changes) and, after the import (in the check: for the simulated end state), the reconciliation of the file against the database (counts, portfolio value, net shares of the file against the positions, rows of the file not in the database, unresolved rows, every position against the rebuild of its transactions, `integrity_check`, `foreign_key_check`). The new ACTION tools `check_transaction_import` (reads a local CSV, writes nothing) and `import_transactions` (needs the `plan_token` of the check) are for the desktop GUI only; the Jan front end's `TRADING_EXCLUDE` has to list both. `Import-TradingTransactions.py` prints the same report. Master data: a new security is created through the central create path of `security_onboarding.py` (no ticker is invented; the type defaults to `stock` and the report says so), an empty ISIN / WKN of an existing security is supplemented when the file gives exactly one valid value, a differing database value is reported as `MASTERDATA_CONFLICT` and never overwritten, and a row found only by its name with another ISIN is a conflict. Positions are still rebuilt by the existing rebuild for the touched securities; watchlist, strategy assignments and campaigns are not changed because a position was imported. Prices and FX rates stay with their own tasks (`MARKET_DATA_MISSING`). An identifier of the source that differs from the master ISIN for the same instrument (same WKN; for example an ADR / GDR ISIN) is declared with `--accept-identifier-alias IDENTIFIER=SYMBOL` (`accept_aliases` in the tools): accepted only with evidence, stored as a source symbol of the source `Parqet`, resolved first by the existing resolver afterwards, so the export no longer reports a master data conflict; the master ISIN is never overwritten.

### Fixed
- Two import backups made within the same second no longer collide (the second one gets a counter suffix; an existing backup is still never overwritten).

### Changed
- Single-name guard on the effective exposure: the 20 % limit of the ADD and the initial BUY (`ADD_SECURITY_WEIGHT_LIMIT` / `ENTRY_SECURITY_WEIGHT_LIMIT`, limit and reason codes unchanged) now applies to the direct position plus the share of the same company in the held ETFs (TSMC ADR 6.2 % + 1.1 % through an ETF = 7.3 %), reusing `effective_exposure` (calculated once per run and handed to the sizing in the portfolio context; no second calculation). A company is counted once even through several ETFs or share classes; a company held only through ETFs is visible as a single-name risk. The sizing results, the decisions and the plan carry the direct weight (unchanged), the ETF share, the effective weight and the basis (`effective` / `direct` / `direct_only`); the plan has a new section `single_name_exposure` (largest companies, companies above the limit, status of the holdings). Data quality: the effective basis is used only if every held ETF has present holdings that are at most 120 days old; otherwise the direct limit still applies but the result, the plan (`SINGLE_NAME_EFFECTIVE_EXPOSURE_NOT_ASSESSED`) and the global issues (`EFFECTIVE_EXPOSURE_NOT_ASSESSED`) say that the effective exposure is not assessed - no silent fallback. The rounding drift never counts. The sector and correlation guards are unchanged; no orders, no new tool. The guard works on company level: several directly held share classes of one reviewed issuer group (Alphabet A/C, Fox A/B, News Corp A/B), ETF shares and any mix are one exposure; `get_effective_exposure` lists each company once with all its direct positions (`direct_positions`) and `security_ids`, and the sizing adds the direct value of the other classes (also when the ETF holdings are stale) to the limit base. Without an issuer group nothing changes; no new data structure in the private files.

## [0.5.0] - 2026-10-14

### Highlights
- Watchlist: strategy, priority and sector group are editable in the table (pending until one apply button saves them); the sector comes from the sector map only.
- New Sektoren page: sector overview with treemap, ETF sector look-through and the new tab "Einzelwertrisiko" (effective exposure per company from direct positions and ETF holdings, with treemap).
- Tasks page as an operations overview: rhythm, data freshness, technical dependencies and a warning before a manual start.
- MCP tools 20 -> 25 since v0.4.0 (`set_security_strategy`, `set_security_sector`, `get_sector_overview`, `get_data_freshness`, `get_effective_exposure`); the Qt chat is offered 17 read tools; the front end Jan sees 19 (its exclude list hides the six write actions).
- The risk guard, the planner logic and the database schema (apart from the removed column `security.sector`) are unchanged.

### Added
- Effective exposure treemap: the tab "Einzelwertrisiko" gets an own treemap with one tile per company (area = the effective value, direct and ETF merged; share classes of one issuer and a company in several ETFs are one tile), separate blocks for the remaining companies and cash, colour = the daily change of the company's own share (grey without one; an ETF's change is never used), tooltip with the effective, direct and ETF figures, the contributing ETFs, stand, source and matching hints. It is fed by `get_effective_exposure` (now requested with up to 40 companies); the sector treemap is unchanged. The rounding drift is never a tile.
- Effective exposure display: the "Einzelwertrisiko" tab now opens with the biggest effective single position and its direct / ETF split, the number of companies over 5 % and 10 % and the freshness of the holdings; the company table is sorted by effective weight (descending by default, sortable), highlights the effective columns, shows a dash instead of 0 where there is no direct position or ETF share, names the contributing ETFs and the holdings stand per company and lists the source in the tooltip. Display only: no new data logic, no limit, the risk guard is unchanged.
- Effective single-name exposure (MCP tools 24 -> 25, tab "Einzelwertrisiko" of the Sektoren page): the new READ tool `get_effective_exposure` adds the share of every company in the held ETFs (official provider holdings; a synthetic ETF by its index composition, not the collateral basket) to the direct position and reports the effective value and weight per company with the direct and ETF share separated, the contributing ETFs, the largest companies and the coverage and matching per ETF. A company is aggregated once; holdings are matched exactly (ISIN, a reviewed alias such as an ADR and its ordinary share, or the same ticker on the same exchange), never fuzzy, and unmatched or missing data stay visible. The holdings are a private file `etf_holdings_local.json` (git-ignored, protected from the deploy). No limit is applied and the risk guard is unchanged. Every euro of the invested value falls into one bucket: direct_matched, etf_only (clearly identified companies that are not held directly), ambiguous_unmatched, other (cash, derivatives) and rounding_drift. The published weights of a synthetic ETF's index list (Invesco S&P 500: 102.43 %, coarse rounding) are not scaled to 100 %, because that would push the error into the large names; the excess is a negative rounding_drift block.
- Tasks page as an operations overview (MCP tools 23 -> 24): the new READ tool `get_data_freshness` reports the stand of what the scheduled tasks produce (latest price per held position, FX date per currency, last news fetch, fundamentals, generated_at of the discovery and intelligence reports) together with the existing age rules. The page shows per task the rhythm (from the triggers), state with last run, data stand, last success (not the same as the last run), next run, duration (about, from the log), and in the tooltip the triggers, StartWhenAvailable, IgnoreNew, time limit, dependencies, last error and log path. Dependencies are technical, derived from what the scripts read, not a task chain: Candidate Discovery needs the FX rates, Market Intelligence needs news/events and optionally the discovery report; the other tasks stand alone. The weekly fundamentals task is judged by its last successful run and its rhythm, not by the age of the newest filing (it writes only new filings). A manual start warns when a necessary prerequisite is stale or failed and never starts another task. No scheduler definition is changed.
- ETF look-through in the sector overview (`get_sector_overview(lookthrough=true)`, no new tool): an ETF with an official provider sector allocation is split into its categories and added to the sector exposure next to the direct stocks (`origin` direct / etf_lookthrough, shares of direct and look-through per sector, coverage). Categories that map to exactly one sector group (`etf_sector_mapping_v1.json`) join that group; categories that fit no single group (for example GICS Information Technology) stay their own unmapped groups, ETFs without usable data stay one visible position. The allocations are a private file `etf_lookthrough_local.json` (provider, as-of date, source, weights; git-ignored, protected from the deploy). The risk guard is unchanged and still reads direct sectors only. The Sektoren page gets the new columns, an ETF table and an origin marker in treemap and tooltip.
- Sector overview (MCP tools 22 -> 23, desktop GUI page "Sektoren"): the new READ tool `get_sector_overview` groups the open positions by their effective sector (public map + local override, the same resolution as the risk guard and the news mapping) with EUR market value, share of the invested value, number of positions, value-weighted daily change and the sector-guard limit and status; positions without an entry form their own group "not assigned" and are never dropped. ETFs are one position each (no look-through). The page shows a treemap (area = market value, grouped by sector, colour = daily change, grey = no data) plus a sector table and a position table. Read-only: no write, no change to the risk guard.
- Editable sector group on the Watchlist page (MCP tools 21 -> 22): the new ACTION `set_security_sector` sets or removes the LOCAL sector entry of one security (only an existing group; `null`, or a sector equal to the public one, removes the local entry) and writes only `sector_map_local.json` (atomic, the previous file is kept as `.bak`); `sector_map_v1.json` and the database are never touched, and a held position or an open campaign does not block it. The table column "Sektor" shows the group and whether it comes from the public map, the local map or is not assigned (the replaced public value is in the tooltip); the same pending / apply mechanism as for strategy and priority applies. `get_watchlist` (table and detail shape) carries `sector`, `sector_source`, `sector_public` and `sector_groups`; the compact chat answer is unchanged.
- Inline editing of strategy and priority on the Watchlist page (MCP tools 20 → 21): the new ACTION `set_security_strategy` sets (`swing`, `long_term`) or removes the strategy of one security and writes only `strategy_assignment`; a held position (`BLOCKED_HELD_POSITION`) and a security with an open campaign (`BLOCKED_ACTIVE_CAMPAIGN`) are blocked without any write, and no campaign or order is created. The priority uses the existing `set_watchlist_priority`. A choice in the table is only a pending change until the apply button saves it, strategy first and the priority only if that succeeded; reloading with pending changes asks first. A not held security set to Swing is afterwards an entry candidate of the planner (simulation only). The chat and other MCP clients configured with `TRADING_EXCLUDE` do not see the action.

### Changed
- Sector map resolution: a local entry of a symbol now overrides the public one (before, a symbol in both files was an error that switched the guards off); two sectors for a symbol inside one file are still an error. The risk guards and the news mapping share the rule (`sector_assignment.py`).
- Desktop GUI: the separate "change priority" dialog and button were removed; the priority is edited inline in the table (pending until the apply button saves it), so there is a single path for it. The MCP action `set_watchlist_priority` is unchanged.

### Removed
- The unused column `security.sector` (free text of a former import; nothing read or wrote it, the sector of the risk guards and of the news mapping comes from the sector map). New and reset databases no longer have it, the view `v_watchlist` no longer selects it, `get_watchlist` detail rows and the research prompt no longer carry it, and the new migration `Migrate-TradingDropSecuritySector.py` removes it from an existing database (dry run by default, backup copy, ids and all other fields unchanged; the old values are not copied into any sector map). `security.industry` stays. Run the migration after deploying; the sector map files are not touched.

### Fixed
- SEC requests of the events/news and the SEC fundamentals collectors answered HTTP 403 (no SEC events were stored since 2026-10-09): the SEC rejects every User-Agent that carries a GitHub noreply contact ("...@users.noreply.github.com"), and that was the configured `SEC_USER_AGENT`. Both scripts now refuse such a value, use the placeholder instead and print a warning; the real fix is to set `SEC_USER_AGENT` to "<name> <real e-mail>". No retry, proxy or header architecture was added.
- Desktop GUI, Watchlist page: the ISIN column was pushed far out of the visible area by the wide columns to its left (it sat behind the name, entry status and news columns); it is now displayed right behind the name and no column is wider than 260 px (the full text stays in the tooltip). Column model, editors, sorting and filters are unchanged.
- Migration of the removed column `security.sector`: a relative `--db-path` no longer breaks the dry run (the path is resolved first).
- Master data (production data, not part of the repository): the 16 securities that had no ISIN were completed from public sources (ISIN, and WKN where a source gave one) and the quote currency of one listing was corrected; no security was created, no position, transaction or strategy changed.

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
