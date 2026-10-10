# Trading Agent — Architecture Reference

Deutsche Version: [trading-agent-architecture.de.md](trading-agent-architecture.de.md)

Authoritative, code-verified reference for every component under
`tools/trading/`, `mcp-tools/trading_sqlite.py`, and the production
database schema. Extracted directly from the current code on
2026-09-25 — where this document and the code ever disagree in the future,
**the code is the source of truth**, not this file.

This complements, rather than replaces, the narrower existing docs:
[trading-candidate-decision.en.md](trading-candidate-decision.en.md),
[trading-strategy-suggestion.en.md](trading-strategy-suggestion.en.md),
[trading-swing-promotion.en.md](trading-swing-promotion.en.md),
[trading-entry-recommendations.en.md](trading-entry-recommendations.en.md).

---

## 1. Component flow

```
Market/Fundamental Data (Backfill-*, Research-TradingFundamentals.py)
    |  writes: market_data, market_snapshot, fundamentals, fx_rates
    v
Analytics (trading_analytics.py — rank_watchlist / analyze_security)
    |  read-only; no writes
    v
Strategy Suggestion (strategy_suggestion.py)      -- suggestion only, no write
    |
    v  (a human, or a separate explicit approval step, decides)
Strategy Assignment (strategy_assignment table; swing_promotion.py's
    approve_swing_promotion / Manage-TradingStrategyAssignments.py --write)
    |  writes: strategy_assignment
    v
Portfolio Context / Guardrails (analysis_engine.build_portfolio_context,
    portfolio_context.derive_allocation_guardrails)     -- read-only
    |
    v
Swing Campaign State (swing_lifecycle.py, swing_campaign /
    swing_campaign_event tables, Manage-TradingSwingCampaigns.py)
    |
    v
Candidate Decision (candidate_decision.py)        -- read-only, no write
    |
    v
Decision Engine / Orchestrator (decision_engine.py, trading_orchestrator.py,
    orchestrator_presentation.py)                  -- read-only, no write
    |
    v
Portfolio Action Planner (portfolio_action_planner.py)
    -- read-only simulation: post-action capital, PROMOTE entry evaluation,
       ranking, sizing, planned entries (no orders; PROMOTE is not BUY)
    |
    v
Agent Output (trading_sqlite MCP tool via an MCP client, Run-TradingOrchestrator.py)
```

Per transition:

| Transition | Data passed | Who decides | Suggestion-only? | Persisted? | Explicitly no write |
|---|---|---|---|---|---|
| Data → Analytics | OHLCV closes, fundamentals rows | `trading_analytics.rank_watchlist` (pure math) | — | no | yes — read-only |
| Analytics → Strategy Suggestion | score, quality, `asset_type`, open-campaign flag | `strategy_suggestion.suggest_strategy_assignments` | **yes** | no | yes — no DB write anywhere in the module |
| Suggestion → Strategy Assignment | none automatic | a human, via `swing_promotion.approve_swing_promotion` or `Manage-TradingStrategyAssignments.py set --write` | no (this step is the actual write) | **yes**, `strategy_assignment` | — |
| Strategy Assignment → Portfolio Context | all open positions + their strategy | `analysis_engine.build_portfolio_context` (pure aggregation, no rule) | — | no | yes — read-only |
| Portfolio Context → Swing Campaign State | none directly; campaign state is independently recorded | a human, via `Manage-TradingSwingCampaigns.py open/event/close --write`; plus `parqet_import.reconcile_campaign_transactions` for unambiguous post-campaign BUY/SELL (neutral `add` / `manual_reduction` only, see §3) | no | **yes**, `swing_campaign` / `swing_campaign_event` | — |
| → Candidate Decision | analytics + assignment + campaign + guardrails | `candidate_decision.evaluate_watchlist_candidates` | **yes** (produces BUY/WATCH/DEFERRED/INSUFFICIENT_DATA, never executes) | no | yes — no DB write anywhere in the module |
| → Decision Engine / Orchestrator | `AnalysisSnapshot` + `PortfolioContext` | `decision_engine.decide`, composed portfolio-wide by `trading_orchestrator.run_trading_orchestrator` | yes — produces a recommendation only | no | yes — `decision_engine.py` has no `sqlite3` import at all |
| → Portfolio Action Plan | existing SELL/TRIM (+ ADD/BUY) actions, PROMOTE candidates, `PortfolioContext` | `portfolio_action_planner.build_portfolio_action_plan`, called at the end of `run_trading_orchestrator` (see §3.10) | yes — simulation only, never an order | no | yes — pure functions, no `sqlite3` import |
| → Agent Output | `OrchestratorResult` / `PresentationSummary` | `orchestrator_presentation.format_orchestrator_summary`, then the Trading MCP `trading_sqlite` tool (MCP client) / Run-TradingOrchestrator.py | — (formatting only) | no | yes |

---

## 2. Tool / module inventory

### 2.1 Data ingestion & DB lifecycle

| Tool | Purpose | Writes to | External source |
|---|---|---|---|
| `Initialize-TradingDatabase.py` | Idempotent schema creation (`CREATE TABLE/INDEX IF NOT EXISTS`); safe on an existing DB | all base tables/views/triggers, `INSERT OR IGNORE` seed `metadata`/`data_sources` | none |
| `Reset-TradingDb.py` | **Destructive** full reset — deletes the DB file (+ `-wal`/`-shm`) and recreates it from scratch. Requires an explicit `--db-path` (no default, aborts without it) and typing `RESET` at an interactive prompt. | same schema as Initialize, non-idempotent `CREATE` | none |
| `Backfill-TradingMarketData.py` | Daily OHLCV + current snapshot backfill, incremental (5-day overlap window). **Automated since 2026-09-28**: Windows Scheduled Task `Trading-MarketData-Backfill`, daily 17:15 local, runs `--write`, stdout/stderr appended to `C:\tools\trading\logs\market-data-backfill.log` | `market_data`, `market_snapshot`, `source_symbols` (creates this table itself if missing), `data_sources` | Yahoo Finance chart + search API |
| `Backfill-TradingFXRatesECB.py` | ECB EUR reference-rate backfill, dry-run by default. One run loads every currency of `--quote-currency` (comma separated; default `USD,GBP,AUD,KRW`, the quote currencies of held/watched securities); a failing currency does not discard the others but makes the run exit 1. **Automated since 2026-09-28**: Windows Scheduled Task `Trading-FXRates-Backfill`, triggers on logon and daily 17:20 local, `StartWhenAvailable=True`, runs `--write`, stdout/stderr appended to `C:\tools\trading\logs\fx-rates-backfill.log`. `Manage-TradingTasks.ps1 Register` skips an existing task; the script default therefore carries the four currencies for the already registered task | `fx_rates` (only with `--write`) | ECB SDMX EXR API |
| `Backfill-TradingMarketSnapshotCurrency.py` | Deterministic fallback-fill of missing `market_snapshot.currency` from same-day `market_data`/`source_symbols`/`security` — never guesses | `market_snapshot.currency` (only `--write`, NULL rows only), makes a `.bak` file copy first | none (DB-internal only) |
| `Backfill-TradingFundamentalsSEC.py` | Bulk fundamentals backfill from SEC EDGAR XBRL CompanyFacts, with a 6h local JSON cache. No `--write` flag — every run writes unconditionally (confirmed: passing `--write` fails with exit code 2, unlike the other Backfill-* tools). **Automated since 2026-09-28**: Windows Scheduled Task `Trading-Fundamentals-SEC`, triggers on logon and weekly Sunday 10:00 local, `StartWhenAvailable=True`, stdout/stderr appended to `C:\tools\trading\logs\fundamentals-sec-backfill.log` | `fundamentals` (DELETE-then-executemany-upsert per security) | SEC EDGAR XBRL API |
| `Backfill-TradingFundamentalsIR.py` | Hand-curated parsers for 6 companies (ASML, ING, TSMC, SK hynix, BAE Systems, HENSOLDT) from company IR sources. Report URLs are hardcoded per company per quarter (e.g. `ASML_URL` names "Q2-2026") — **manual/event-driven only**, not scheduled: an unattended run would silently keep re-fetching a stale quarter, or 404, once a new report is published | `fundamentals` | Company IR sites (XLSX/PDF), OpenDART (SK hynix); BAE Systems from a static local JSON snapshot |
| `Research-TradingFundamentals.py` | LLM-research workflow: builds a research prompt, structurally validates the LLM's JSON answer (never trusts it blindly), then optionally writes. **Manual/LLM-assisted only** — by its own docstring "NOT a parser"; requires an interactive LLM (Claude) session to do the actual research, so it cannot run headless/scheduled | `fundamentals` (only `--write`), plus prompt/audit files under `C:\tools\trading\data\fundamentals-audit\` | none directly — an external LLM does the actual research |
| `Backfill-TradingEventsNews.py` | `events` from SEC EDGAR company-submissions JSON (8-K filings for domestic filers, 6-K for foreign private issuers; SEC's own item codes → `event_type`, 2-year lookback window, full replace per `(security_id, source_id)` each run), `news` from Yahoo Finance's public search endpoint (falls back from ticker to company name when the ticker search returns nothing, e.g. for foreign listings). Dry-run by default, `--db-path`/`--db` canonical+alias. **Automated since 2026-09-28**: Windows Scheduled Task `Trading-EventsNews-Backfill`, triggers on logon and daily 17:25 local, `StartWhenAvailable=True`, runs `--write`, stdout/stderr appended to `C:\tools\trading\logs\events-news-backfill.log`. Idempotency verified: back-to-back runs leave `events`/`news` row counts unchanged (0 new rows on an immediate rerun) | `events` (DELETE-then-executemany-INSERT per security/source), `news` (`INSERT OR IGNORE` keyed on the existing `idx_news_url` unique index) | SEC EDGAR submissions API; Yahoo Finance `v1/finance/search` (both unauthenticated, no API key) |
| `Discover-TradingSECIdentifiers.py` | Read-only: fuzzy-matches portfolio/watchlist stocks to SEC CIK identifiers | none (report only) | SEC company-ticker index + CompanyFacts API |
| `Resolve-TradingSecurities.py` | Resolves canonical symbol/exchange via OpenFIGI; also merges one hardcoded ING watchlist duplicate | `security` (symbol/exchange/asset_type), `watchlist` (duplicate merge) | OpenFIGI mapping API |
| `Audit-TradingDataQuality.py` | Read-only fundamentals completeness/freshness audit, classifies each security `NOT_APPLICABLE`/`MISSING`/`STALE`/`PARTIAL`/`COMPLETE` | none | none |
| `Import-TradingTransactions.py` | CLI of the provider-neutral transaction import (`--format canonical` or `--format parqet`); a complete dry run on an in-memory copy by default; `--write` backs up the DB and writes everything in one transaction. Options `--create-securities`, `--strategy`, `--campaign-opened-at`, `--include-historical`; `--reconcile-campaigns` reconciles already-imported trades with open Swing campaigns (no CSV) | `security`, `transactions`, `positions`, `strategy_assignment`, `swing_campaign`, `swing_campaign_event`, `imports` (only `--write`; backs up DB first) | none — local CSV file |
| `Import-ParqetTransactions.py` | Existing entry point; runs `Import-TradingTransactions.py` with `--format parqet` as the default | as `Import-TradingTransactions.py` | none — local Parqet CSV export |

### 2.2 Read-only analysis / decision pipeline

| Module | Purpose | DB read/write | Depends on |
|---|---|---|---|
| `analysis_contracts.py` | Shared enums/dataclasses (`Action`, `ActionQuantityBasis`, `AvailabilityStatus`, `StrategyType`, `DataQuality`, `AnalysisSnapshot`, `PortfolioContext`, `DecisionResult`, `to_primitive`) | none | — |
| `strategy_config.py` | All tunable trading parameters as frozen dataclasses (`StrategyConfig` and its sub-configs) — parameters only, no I/O | none | — |
| `fx_resolver.py` | ECB-convention (1 EUR = N quote currency), direct-pair-only FX resolution; provider pence quotes (`GBp`, the only such unit in the data) are normalized centrally to GBP (price factor 0.01) before the GBP rate is applied | reads `fx_rates` | `analysis_contracts`, `strategy_config` |
| `capital_state.py` | Read-only temporal resolution of the latest eligible `portfolio_capital_state` row | reads `portfolio_capital_state`, `metadata` | `analysis_contracts`, `strategy_config` |
| `swing_lifecycle.py` | Derives the currently-open campaign's TP1/TP2/add/reduction state from explicit lifecycle events; never infers from transactions | reads `swing_campaign`, `swing_campaign_event`, `metadata` | `analysis_contracts` |
| `analysis_engine.py` | Builds `AnalysisSnapshot` (technical + fundamental + valuation + event-risk + position) and `PortfolioContext` per security / portfolio-wide | reads `security`, `watchlist`, `positions`, `market_data`, `fundamentals`, `estimates`, `ratings`, `price_targets`, `events`, `news`, `strategy_assignment`, `market_snapshot` | `fx_resolver`, `capital_state`, `swing_lifecycle`, `portfolio_context`, `strategy_config`, `analysis_contracts` |
| `portfolio_context.py` | Pure allocation math: guardrail derivation, capital-use projection, sizing readiness — no DB access | none | `analysis_contracts`, `strategy_config` |
| `trading_analytics.py` | Technical scoring library (SMA/RSI/momentum/drawdown/volatility) and `rank_watchlist()` | reads `market_data`, `watchlist`, `security`, `data_sources` | — |
| `decision_engine.py` | Pure per-security recommendation (`BUY`/`ADD`/`HOLD`/`TRIM`/`SELL`/`WATCH`) from an `AnalysisSnapshot` + `PortfolioContext` | none (no `sqlite3` import at all) | `analysis_contracts`, `add_sizing`, `entry_sizing`, `strategy_config` |
| `entry_sizing.py` | Pure zero-position Swing entry eligibility/sizing | none | `analysis_contracts`, `strategy_config` |
| `add_sizing.py` | Pure open-campaign Swing ADD eligibility/sizing | none | `analysis_contracts`, `strategy_config` |
| `candidate_discovery.py` | Read-only watchlist candidate discovery (§3.11): universe → pre-filter → data availability → existing-engine analysis → discovery score; runs the unchanged engine on an in-memory copy of the production schema, never writes | production DB: read only (`source_symbols`, `positions`, `watchlist`, `security`, `fx_rates`, `data_sources`) | `analysis_engine`, `swing_promotion`, `decision_engine`, `candidate_decision`, `fx_resolver`, `strategy_config` |
| `Discover-TradingCandidates.py` | Explicit CLI for the discovery: Yahoo history via the market-data backfill's helpers (optional per-day cache), prints the ranked table / JSON and, after a completely successful run, writes the report (`--output-dir`, `--no-history`); **automated since 2026-10-09**: Windows Scheduled Task `Trading-Candidate-Discovery`, daily 17:35 local (after MarketData 17:15 / FX 17:20 / EventsNews 17:25), no logon trigger, 2 h time limit, stdout/stderr appended to `C:\tools\trading\logs\candidate-discovery.log`; not part of the orchestrator | none (DB opened `mode=ro`); report files `C:\tools\trading\data\candidate-discovery\latest.json` (+ history copies) | Yahoo Finance chart API, `candidate_discovery`, `discovery_report` |
| `discovery_report.py` | Versioned report of a discovery run (schema v1): build, validate (schema + `results_sha256` digest), atomic write (temp file → re-read/validate → replace; history copy first) and the pure query used by the MCP reader. File I/O only: no network, no database | report directory only | `strategy_config` (`DataQualityConfig`) |
| `market_intelligence.py` | Pure, deterministic market-intelligence core (§3.12): feed/SEC/Yahoo normalization, rule-based category/importance/impact, duplicate merge, mapping to portfolio/watchlist/discovery; `load_db_context` reads the DB (`mode=ro`) | production DB: read only (`news`, `events`, `source_symbols`, `positions`, `watchlist`, `security`, `data_sources`) | `strategy_config` (`MarketIntelligenceConfig`), static `universe/sector_map_v1.json` |
| `Collect-TradingMarketIntelligence.py` | Explicit CLI: fetches 10 official feeds (ECB, Fed, BLS, BEA, Destatis, EIA) and Yahoo headlines for READY discovery candidates, builds and atomically writes the report; **automated since 2026-10-09**: Windows Scheduled Task `Trading-Market-Intelligence`, daily 08:15 and 17:45 local (the evening run follows the 17:35 discovery and reads its report), no logon trigger, 2 h time limit, stdout/stderr appended to `C:\tools\trading\logs\market-intelligence.log`; not part of the orchestrator | none (DB opened `mode=ro`); report files `C:\tools\trading\data\market-intelligence\latest.json` (+ history copies) | official RSS/Atom feeds, Yahoo Finance news, `market_intelligence`, `intelligence_report`, `discovery_report` (atomic write) |
| `intelligence_report.py` | Versioned report of a collection run (schema v1): build, validate (schema, enums, counts, `events_sha256`), write, and the pure query used by the MCP reader. File I/O only: no network, no database | report directory only | `strategy_config`, `discovery_report` (`_atomic_write`) |
| `opportunity_view.py` | Pure, deterministic join of the orchestrator/planner result, the last discovery report and the last market-intelligence report (§3.13): opportunities, news context status, planner capital context, report freshness; query/compact/render for the MCP reader; no score, rank, sizing or signal of its own. `load_symbol_map` is one read-only SELECT | production DB: read only (`source_symbols`, `data_sources`) via the caller's read-only connection | `discovery_report`, `intelligence_report`, `market_intelligence` (`company_mentioned`), `strategy_config` |
| `portfolio_action_planner.py` | Deterministic capital/entry plan (`portfolio_action_plan`): post-action capital, PROMOTE entry evaluation, ranking, sizing, sequential allocation — simulation only (§3.10) | none | `entry_sizing`, `portfolio_context`, `candidate_decision`, `decision_engine`, `orchestrator_presentation`, `strategy_config` |
| `swing_promotion.py` | Evaluates/approves WATCH → Swing `strategy_assignment` promotion | reads `watchlist`, `strategy_assignment`, `swing_campaign`; writes `strategy_assignment`, `candidate_promotion` only via explicit `approve_swing_promotion` | `analysis_engine`, `analysis_contracts` |
| `candidate_decision.py` | Combines analytics + assignment + campaign + guardrails into `BUY`/`WATCH`/`DEFERRED`/`INSUFFICIENT_DATA` | reads (via reused functions) `watchlist`, `strategy_assignment`, `swing_campaign`, plus everything `build_portfolio_context` reads; writes nothing | `trading_analytics`, `swing_promotion`, `analysis_engine` |
| `strategy_suggestion.py` | Non-binding `swing`/`long_term`/`unknown` suggestion for unassigned WATCH entries | reads `watchlist`, `strategy_assignment`, `swing_campaign`, `security.asset_type`; writes nothing | `trading_analytics`, `swing_promotion` |
| `orchestrator_presentation.py` | Deterministic, filter/count/format-only presentation contract over an already-computed `OrchestratorResult` | none | `analysis_contracts` |
| `trading_orchestrator.py` | Composes the whole read-only pipeline into one portfolio-wide report (`OrchestratorResult`) | reads everything the above read; writes nothing | `analysis_engine`, `decision_engine`, `swing_promotion`, `orchestrator_presentation`, `portfolio_context` |
| `Run-TradingOrchestrator.py` | Thin read-only CLI launcher for `trading_orchestrator.run_trading_orchestrator` | read-only connection (`mode=ro`, `PRAGMA query_only=ON`) | `trading_orchestrator` |
| `transaction_import.py` | Provider-neutral import engine: canonical CSV, validation, security resolution, duplicate/historical detection, plan and dry run, split-aware position rebuild, strategy assignment, Swing-campaign initialization and campaign reconciliation in one transaction | reads/writes `security`, `transactions`, `positions`, `strategy_assignment`, `swing_campaign`, `swing_campaign_event`, `imports`; reads `corporate_action` (via `corporate_actions.py`) | `corporate_actions`, `swing_lifecycle` |
| `parqet_import.py` | Parqet adapter: maps the Parqet export (semicolon, decimal comma) onto the canonical model; keeps exporting the former import names | none | `transaction_import` |

### 2.3 CLI managers & migrations (auditable, additive features)

All `Migrate-*` scripts require `metadata.schema_version == "2.0"` and are dry-run unless `--write`. All `Manage-*` scripts are dry-run unless `--write` and never create schema themselves (they error out pointing at the matching `Migrate-*` script).

| Migration | Creates | Feature-version key (value `"1"`) |
|---|---|---|
| `Migrate-TradingStrategyAssignments.py` | `strategy_assignment` + 2 no-overlap triggers | `strategy_assignment_schema_version` |
| `Migrate-TradingCandidatePromotions.py` | `candidate_promotion` | `candidate_promotion_schema_version` |
| `Migrate-TradingFXRates.py` | `fx_rates`, recreates views `v_active_positions`/`v_portfolio_market` | `fx_rates_schema_version` |
| `Migrate-TradingSwingLifecycle.py` | `swing_campaign`, `swing_campaign_event` + 4 triggers (requires `strategy_assignment_schema_version == "1"` first) | `swing_campaign_schema_version` |
| `Migrate-TradingCapitalState.py` | `portfolio_capital_state` | `portfolio_capital_state_schema_version` |
| `Migrate-TradingCorporateActions.py` | `corporate_action` | `corporate_action_schema_version` |

| Manager | Subcommands | Wraps |
|---|---|---|
| `Manage-TradingStrategyAssignments.py` | `list`, `set`, `close`, `validate` | `strategy_assignment` directly (no separate module) |
| `Manage-TradingSwingCampaigns.py` | `list`, `open`, `event`, `close`, `validate` | `swing_lifecycle.py` |
| `Manage-TradingCapitalState.py` | `show`, `set`, `external-funding`, `validate` | `capital_state.py` |
| `Manage-TradingCorporateActions.py` | `list`, `validate`, `add-split`, `rebuild-position` | `corporate_actions.py`, `transaction_import.rebuild_position` |

All four `Manage-*`/production DB tools default to `C:\tools\trading\data\trading.db`, overridable via `--db-path` — the canonical flag name across the whole `tools/trading/` CLI surface since 2026-09-28 (`--db` remains a working legacy alias, see §9 inconsistency #6).

### 2.4 Agent access (MCP client / Trading MCP)

```
MCP client (LLM frontend)
  |
  v
Trading MCP Server       C:\tools\trading\mcp   (repo source: mcp-tools\)
  |
  v
Trading Runtime          C:\tools\trading\app
  |
  v
SQLite                   C:\tools\trading\data\trading.db
```

The MCP server (`server.py`) exposes every public method of the `Tools` class in
`trading_sqlite.py` as an MCP tool — currently 15 (see §5).

---

## 3. Fixed values & thresholds (exact, from code)

### 3.1 Strategy types & status vocabularies

| Vocabulary | Allowed values | Defined in |
|---|---|---|
| `StrategyType` enum | `long_term`, `swing`, `tactical`, `unknown` | `analysis_contracts.py` |
| `strategy_assignment.strategy_type` CHECK | `long_term`, `swing`, `tactical`, `unknown` (matches the enum) | schema |
| `Action` enum | `BUY`, `ADD`, `HOLD`, `TRIM`, `SELL`, `WATCH` | `analysis_contracts.py` |
| `ActionQuantityBasis` enum | `TP1_25_PERCENT_ORIGINAL`, `TP2_75_PERCENT_CUMULATIVE_ORIGINAL`, `RUNNER_FULL_REMAINDER`, `STOP_FULL_EXIT`, `ADD_25_PERCENT_ORIGINAL`, `INITIAL_ENTRY_SIZING` | `analysis_contracts.py` |
| `AvailabilityStatus` enum | `available`, `partial`, `insufficient`, `unavailable`, `stale`, `not_applicable` | `analysis_contracts.py` |
| `candidate_decision.CandidateDecision.decision_status` | `BUY`, `WATCH`, `DEFERRED`, `INSUFFICIENT_DATA` | `candidate_decision.py` |
| `candidate_discovery` status | `DISCOVERY_READY`, `DISCOVERY_WATCH`, `DISCOVERY_DATA_INSUFFICIENT`, `DISCOVERY_REJECTED` | `candidate_discovery.py` |
| `portfolio_action_planner` `entry_status` | `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `BLOCKED_BY_ALLOCATION`, `BLOCKED_BY_DATA`, `BLOCKED_BY_CONCENTRATION`, `BLOCKED_EXISTING_POSITION`, `BLOCKED_EXISTING_CAMPAIGN` | `portfolio_action_planner.py` |
| `strategy_suggestion.StrategySuggestion.suggested_strategy` | `swing`, `long_term`, `unknown` | `strategy_suggestion.py` |
| `swing_promotion.PromotionDecision.recommendation` | `PROMOTE`, `KEEP_WATCHING`, `REJECT`, `DATA_INSUFFICIENT` | `swing_promotion.py` |
| `DATA_INSUFFICIENT` reason codes (`swing_promotion.py`) | `PROMOTION_TECHNICAL_DATA_STALE`, `PROMOTION_REQUIRED_DATA_INCOMPLETE`, `PROMOTION_PRICE_UNAVAILABLE`, `PROMOTION_SMA50_UNAVAILABLE`, `PROMOTION_SMA200_UNAVAILABLE`, `PROMOTION_MARKET_DATA_MISSING` (no market_snapshot quote or quote currency), `PROMOTION_FX_RATE_MISSING` (quote and currency present, but no usable ECB rate for the EUR valuation) | `swing_promotion.py` |
| `candidate_promotion.status` CHECK | `watching`, `ready`, `promoted`, `rejected`, `deferred` | schema |
| `swing_campaign.status` CHECK | `open`, `closed` | schema |
| `swing_campaign_event.event_type` CHECK | `baseline`, `add`, `tp1_signal`, `tp1_execution`, `tp2_signal`, `tp2_execution`, `manual_reduction`, `stop_execution`, `close` | schema, `swing_lifecycle.py` |
| `portfolio_capital_state.quality` CHECK | `available`, `partial`, `unavailable`, `stale` | schema |
| `corporate_action.action_type` CHECK | `STOCK_SPLIT` (only value currently supported) | schema, `corporate_actions.py` |
| `trading_orchestrator` global/security issue severities | `GLOBAL_BLOCKING`, `SECURITY_BLOCKING`, `NON_BLOCKING_WARNING` | `trading_orchestrator.py` |
| `Research-TradingFundamentals.py` verification status | `verified_direct`, `verified_derived`, `verified_secondary`, `not_verified` (`not_verified` is always discarded → NULL) | `Research-TradingFundamentals.py` |
| `Research-TradingFundamentals.py` period types | `quarterly`, `semiannual`, `nine_month`, `annual` | `Research-TradingFundamentals.py` |
| `Audit-TradingDataQuality.py` classification | `NOT_APPLICABLE`, `MISSING`, `STALE`, `PARTIAL`, `COMPLETE` | `Audit-TradingDataQuality.py` |

### 3.2 Trading Analytics (`trading_analytics.py`)

| Constant | Value |
|---|---|
| `PERF_1M_DAYS` / `PERF_3M_DAYS` / `PERF_6M_DAYS` | 21 / 63 / 126 trading days |
| `SMA_SHORT_DAYS` / `SMA_LONG_DAYS` | 50 / 200 |
| `RSI_DAYS` | 14 (plain, non-Wilder-smoothed average of gains/losses) |
| `VOLATILITY_DAYS` | 60 (annualized with √252) |
| `DRAWDOWN_WINDOW_DAYS` | 252 (~52 trading weeks); uses whatever history is shorter than that if less is available |
| `TRADING_DAYS_PER_YEAR` | 252 |
| `QUALITY_OK_MIN_ROWS` | 200 (= `SMA_LONG_DAYS`) → quality `"OK"` |
| `QUALITY_LIMITED_MIN_ROWS` | 30 → quality `"LIMITED"`; below this → `"INSUFFICIENT"` |
| Momentum score components (each capped, summed, `None` if absent — never counted as 0) | `price>SMA50` ±10, `price>SMA200` ±10, `SMA50>SMA200` ±10, `perf_1m`/`perf_3m`/`perf_6m` each clipped to [-10,+10], `(RSI14-50)/50*10`, `max(drawdown_52w, -20)` |
| `rank_watchlist()` scope | `watchlist.status = 'WATCH'` only |
| `rank_watchlist()` sort | scored entries descending by `score`; entries with `score is None` appended after, in original watchlist order (never dropped) |
| Price series source | `adjusted_close` preferred, falls back to `close`; restricted to the `"Yahoo Finance"` `data_sources` row when it exists |

### 3.3 Candidate Decision (`candidate_decision.py`)

- `BUY_SCORE_THRESHOLD = 20.0`
- Decision order (see [trading-candidate-decision.en.md](trading-candidate-decision.en.md) for the full rationale): `analytics_quality != "OK"` → `INSUFFICIENT_DATA`; no assignment → `DEFERRED`; open campaign → `WATCH`; non-swing assignment → `WATCH`; allocation guardrails unavailable → `DEFERRED`; `SWING_ALLOCATION_ABOVE_MAX` → `DEFERRED`; score unavailable → `WATCH`; score `>= 20.0` → `BUY`; else `WATCH`.

### 3.4 Strategy Suggestion (`strategy_suggestion.py`)

- `SWING_SUGGESTION_SCORE_THRESHOLD = 20.0`
- `LONG_TERM_ASSET_TYPES = ("etf", "fund")` (same set `analysis_engine.py`/`Audit-TradingDataQuality.py` treat as fundamentals-not-applicable)
- Confidence constants: `CONFIDENCE_OPEN_CAMPAIGN = 0.9`, `CONFIDENCE_ETF_LONG_TERM = 0.7`, `CONFIDENCE_SWING_MOMENTUM = 0.6`, `CONFIDENCE_UNKNOWN = 0.0`
- `long_term` is **never** derived from the momentum score (only from `asset_type`)

### 3.5 Portfolio / sizing / swing parameters (`strategy_config.py`)

| Config | Field | Value | Consumed by |
|---|---|---|---|
| `PortfolioTargetConfig` | `long_term_min_pct` / `long_term_max_pct` | 0.60 / 0.70 | `portfolio_context.derive_allocation_guardrails`, `entry_sizing.py`, `add_sizing.py` |
| | `swing_min_pct` / `swing_max_pct` | 0.30 / 0.40 | same |
| `SizingPolicyConfig` | `max_security_weight` | 0.20 | `entry_sizing.py`, `add_sizing.py` |
| | `max_initial_swing_weight` | 0.10 | `entry_sizing.py` |
| | `max_add_pct_of_original` | 0.25 | `add_sizing.py` |
| | `minimum_cash_reserve` | 10,000.0 EUR | `entry_sizing.py`, `add_sizing.py`, `trading_orchestrator.py` (`CASH_RESERVE_LIMIT` warning) |
| | `max_add_count` | 1 | `add_sizing.py` |
| | `whole_share_policy` | `"floor"` | `entry_sizing.py`, `add_sizing.py` |
| `SwingStrategyConfig` | `horizon_months_min` / `horizon_months_max` | 3 / 6 | `decision_engine._runner_decision` (post-TP2 runner horizon gate, see §3.6) |
| | `tp1_gain_pct` / `tp2_gain_pct` | 0.20 / 0.25 | `decision_engine.py` (TP price levels) |
| | `tp1_sell_fraction` / `tp2_sell_fraction` | 0.25 / 0.50 | `decision_engine.py`, `swing_lifecycle.py` (see §3.6) |
| | `hard_stop_loss_pct` | 0.15 | `decision_engine.py` |
| | `remainder_management` | `"momentum_guided"` | `decision_engine._runner_decision` — the only implemented policy; runs the existing SMA50/SMA200 trend logic below `horizon_months_max` |
| | `entry_rule`/`add_rule`/`stop_rule`/`position_sizing_rule` | all `None` | not yet defined |
| `DataQualityConfig` | `market_data_max_age_days` | 5 | `analysis_engine.py` (technical staleness gate) |
| `DiscoveryConfig` | `min_price_eur` / `min_median_daily_value_eur` / `liquidity_window_days` | 5.0 / 5,000,000 / 60 (proposal, pre-filter of the discovery only) | `candidate_discovery.py` |
| `MarketIntelligenceConfig` | `lookback_days` / `report_max_age_hours` | 7 / 24 (proposal; news are time critical, so the report is flagged `REPORT_STALE` after one day) | `market_intelligence.py`, `intelligence_report.py` |
| `TaxConfig` | `capital_gains_tax_rate` / `solidarity_surcharge_rate` / `church_tax_rate` | 0.25 / 0.055 (on the tax) / 0.0 (only 0.0 accepted) → `combined_rate` 26.375 % of the realized gain | `portfolio_action_planner.py` (net-proceeds simulation) |
| `FXConfig` | `portfolio_base_currency` | `"EUR"` | `analysis_engine.build_portfolio_context` (raises if changed) |
| | `fx_max_age_days` | 5 | `fx_resolver.py` |
| | `preferred_fx_source` | `"ECB"` | `fx_resolver.py` |
| `CapitalStateConfig` | `freshness_max_age_days` | `None` (opt-in) | `capital_state.py`, exposed via `Manage-TradingCapitalState.py show --freshness-max-age-days` |

### 3.6 TP1/TP2/stop/runner quantity logic (`decision_engine.py`, see §9)

- TP1 trim: sells `floor(original_quantity * config.swing.tp1_sell_fraction)` (0.25) → `ActionQuantityBasis.TP1_25_PERCENT_ORIGINAL`
- TP2 trim: sells down to `floor(original_quantity * (tp1_sell_fraction + tp2_sell_fraction))` (0.75) cumulative from the original → `ActionQuantityBasis.TP2_75_PERCENT_CUMULATIVE_ORIGINAL`
- Hard stop: `stop_price = reference_cost * (1 - hard_stop_loss_pct)` (0.15) → full exit, `ActionQuantityBasis.STOP_FULL_EXIT`
- Runner (post-TP2 remainder), `decision_engine._runner_decision`, `ActionQuantityBasis.RUNNER_FULL_REMAINDER`:
  - Campaign age in whole calendar months = `swing_campaign.opened_at` (via `PositionContext.swing_campaign_opened_at`) vs. the snapshot's `evaluation_as_of`. Unknown/unparseable age (e.g. `opened_at` missing) always falls back to the trend-only behavior below.
  - Below `horizon_months_max` (`horizon_months_min` is **not** a minimum holding requirement): `remainder_management == "momentum_guided"` — held/exited purely by momentum (SMA50/SMA200), unconditionally, regardless of whether the campaign is below or already past `horizon_months_min`.
  - At/after `horizon_months_max` (6): closed unconditionally — same SELL/full-remainder result as the SMA200 exit, with reason `SWING_MAX_HORIZON_REACHED` — regardless of trend or technical-data availability.
  - The hard stop (evaluated only before TP2 execution) and the TP1/TP2/ADD rules are unaffected by campaign age; the horizon only ever gates an already-open runner.
  - Worked example (`opened_at = 2026-01-01`, default `horizon_months_min/max = 3/6`): at `evaluation_as_of = 2026-03-15` (age 2 months, `< min`) and at `2026-06-15` (age 5 months, `min <= age < max`) the runner behaves identically — SMA50/SMA200 trend only. At `2026-07-01` (age 6 months, `>= max`) it closes regardless of trend.
- ADD: `base = floor(original_quantity * max_add_pct_of_original)` (0.25) → `ActionQuantityBasis.ADD_25_PERCENT_ORIGINAL`, capped by `max_add_count = 1`
- `swing_lifecycle.py` independently derives `tp2_cumulative_target = floor(original_quantity * (tp1_sell_fraction + tp2_sell_fraction))` (0.75, via an optional `config: Optional[SwingStrategyConfig] = None` parameter) to detect `post_tp2_add_detected`

### 3.7 Portfolio Context / guardrails (`analysis_engine.py`, `portfolio_context.py`)

- `total_market_value`: sum of every open (`shares > 0`) position's market value, FX-normalized to EUR via `fx_resolver.py`. Withheld (`None`) entirely if even one holding can't be valued — **never computed from a partial denominator**.
- `cash_available`: a **separate** field from `capital_state.py` (`portfolio_capital_state` table) — never folded into `total_market_value`.
- `external_funding_available`: planning switch from `metadata.external_funding_available` (default `false`, a missing key/table means `false`), read by `capital_state.py` and carried unchanged into `PortfolioContext`. It is neither cash nor buying power and books nothing; for its effect see §3.10.
- `base_currency`: always `"EUR"` (`FXConfig.portfolio_base_currency`, hardcoded validation).
- Guardrail names (`derive_allocation_guardrails`, all against `PortfolioTargetConfig`): `SWING_ALLOCATION_ABOVE_MAX`, `SWING_ALLOCATION_BELOW_MIN`, `LONG_TERM_ALLOCATION_ABOVE_MAX`, `LONG_TERM_ALLOCATION_BELOW_MIN`, or `("WITHIN_TARGET_RANGE",)` when none apply. Purely informational — "never a trading action" (module docstring).
- Effect on Candidate Decision: only `SWING_ALLOCATION_ABOVE_MAX` blocks a `BUY` (→ `DEFERRED`); the others are informational (surfaced in `portfolio_constraints` but don't change `decision_status`).
- `trading_orchestrator.py` additionally raises a `NON_BLOCKING_WARNING` (`ALLOCATION_OUTSIDE_TARGET`) whenever `allocation_guardrails != ("WITHIN_TARGET_RANGE",)`.

**Note on the small (~0.22%) valuation-reconciliation gap against Parqet found in an earlier session**: accepted as due to differing query timestamps/sources; not re-investigated here per instruction.

### 3.8 Swing campaign rules (schema-enforced)

- One open campaign per security: `CREATE UNIQUE INDEX idx_swing_campaign_one_open_per_security ON swing_campaign(security_id) WHERE status = 'open'`.
- A campaign requires a matching `strategy_assignment` row with `strategy_type = 'swing'` for the same security (enforced by two `BEFORE INSERT`/`BEFORE UPDATE` triggers).
- A linked `swing_campaign_event.transaction_id` must belong to a transaction for the campaign's own security (enforced by two triggers) and is unique (`idx_swing_campaign_event_transaction`) — this is the idempotency key of the reconciliation below.

**Transaction import → campaign reconciliation** (`transaction_import.reconcile_campaign_transactions`, runs inside the import transaction; standalone: `Import-TradingTransactions.py --reconcile-campaigns`). The whole import path (canonical CSV, strategy, campaign initialization, dry run) is described in [trading-import.en.md](trading-import.en.md):

- Per open campaign, every not-yet-linked BUY/SELL dated on/after `opened_at` (date compare) becomes a transaction-linked event: SELL → `manual_reduction`, BUY → `add`; exact quantity/price/currency from the transaction, source `parqet_reconciliation` (Parqet) or `canonical_reconciliation` (canonical CSV). Trades before `opened_at` belong to the baseline and are skipped.
- No TP1/TP2/stop reason is ever derived from a broker trade; `original_quantity` is never changed; campaigns are never closed automatically (a full exit is flagged `FULL_EXIT_CAMPAIGN_STILL_OPEN`).
- Same-day rule (`opened_at` has date resolution only): a trade on the `opened_at` calendar day is reconciled only if it is the **single** unlinked same-day trade and the quantity history proves it lies after the baseline (baseline ± trade == position, e.g. 120 − 30 == 90). Several same-day trades or no exact quantity proof → `MANUAL_REVIEW_REQUIRED` (`SAME_DAY_TRADES_AMBIGUOUS` / `SAME_DAY_TRADE_NOT_PROVEN`), nothing written.
- All-or-nothing per campaign: events are written only if the campaign then reconciles exactly (delta 0). Otherwise `MANUAL_REVIEW_REQUIRED` (transfers, oversell, split after the trade, unlinked manual events already covering the trade) or `AMBIGUOUS` (several open campaigns) — nothing is written and the engine stays blocked.
- Re-running is idempotent (already-linked transactions are skipped). The import's `lifecycle` report carries `reconciled_transactions`, `skipped_pre_campaign`, `skipped_already_processed`, `ambiguous`, `manual_review_required`, `position_quantity`, `campaign_expected_quantity`, `delta_after_reconciliation`.
- `effective_to IS NULL OR effective_to >= effective_from` on `strategy_assignment`; no two `strategy_assignment` intervals for the same security may overlap (enforced by two triggers).

### 3.9 Data-source/request operational values

| Script | Timeout | Retries | Delay/cache | Other |
|---|---|---|---|---|
| `Backfill-TradingFundamentalsSEC.py` | 10s | 1 | 0.20s between real requests; 6h JSON cache | max 6 annual / 12 quarterly periods kept |
| `Backfill-TradingMarketData.py` | 10s | 2 | 0.75s; incremental 5-day overlap | 2-year, 1-day interval history range |
| `Discover-TradingSECIdentifiers.py` | 20s | — | 0.20s | — |
| `Resolve-TradingSecurities.py` (OpenFIGI) | 10s | 3 | 0.5s (no API key) | batch size 5; 429 backoff up to 65s |
| `Backfill-TradingFXRatesECB.py` | 30s | — | — | — |
| `Research-TradingFundamentals.py` | — | — | — | diff tolerance 0.1% rel / 1.0 abs; balance-sheet tolerance `max(1% of assets, 1,000,000)` |

### 3.10 Portfolio Action Planner (`portfolio_action_planner.py`, v0.2.0)

Pure, read-only simulation appended to `run_trading_orchestrator` as `portfolio_action_plan`: no DB write, no order, no LLM, no strategy change. **PROMOTE is not BUY** — it is only the input of the entry evaluation. Flows: `SELL/TRIM → POST-ACTION CAPITAL → ENTRY PLAN` and `PROMOTE → ENTRY-EVALUATION → RANKING → POSITION SIZING → PLANNED ENTRY`.

| Item | Rule |
|---|---|
| Post-action capital | Gross proceeds = min(action qty, held qty) × current EUR price for every SELL/TRIM; ADD/BUY recommendations of the existing engine consume cash/Swing capacity first. The position value falls by the gross proceeds, **cash rises by the net proceeds** (`proceeds_basis: net_conservative`). Weights are percent of the invested market value (cash is not in the denominator, as in `PortfolioContext`), so tax does not move them |
| Tax estimate | `TaxConfig.combined_rate` (26.375 %) applies to the **realized gain only**: `gain = gross − qty × positions.avg_cost`; a loss gives tax 0 (never negative, never extra cash). Cost basis is the **average cost, not FIFO lots** (`tax_estimate_quality = estimated_from_average_cost`; FIFO is not implemented). Planning uses the **conservative** view (no loss offset, `net_proceeds_total_conservative`). The indicative view offsets losses of the planned SELL/TRIM actions against their gains (tax spread over the gaining actions pro rata; display only, never used for planning). Loss pot, tax-free allowance and this year's realized results are unknown (`unknown_inputs`) and never assumed. A missing/non-EUR cost basis taxes the whole proceeds as an upper bound (`cost_basis_unavailable_upper_bound`); without a `position_cost_basis` input no estimate is made and proceeds stay gross (`not_estimated`) |
| Entry status (PROMOTE items only) | Evaluated in this order: `BLOCKED_EXISTING_POSITION` → `BLOCKED_EXISTING_CAMPAIGN` → `BLOCKED_BY_DATA` (portfolio state, technical data, EUR price, FX) → `WAIT_FOR_TRIGGER` (`momentum_score` missing or below `candidate_decision.BUY_SCORE_THRESHOLD` = 20, or trend filter lost) → `entry_sizing` result: `BLOCKED_BY_ALLOCATION` (Swing maximum, cash reserve, less than one share) / `BLOCKED_BY_CONCENTRATION` (initial 10 % / security 20 % weight) → `ENTRY_READY` |
| Ranking (`ENTRY_READY` only) | `entry_score = momentum_score × confidence` (existing analytics score × existing `decision_engine._confidence` data-quality value); ties: higher watchlist priority, then lower `security_id` |
| Sizing | `entry_sizing.evaluate_entry_recommendation` on the simulated state, with an in-memory "if promoted" Swing view (nothing is persisted): min(initial-weight cap, security cap, Swing-maximum cap, cash above the reserve), whole shares floored; with external funding only the cash cap is lifted |
| Allocation | Strict rank order, state updated after every planned entry. Stops (remaining candidates deferred) with `STOPPED_SWING_MAX_REACHED`, `STOPPED_CASH_RESERVE_REACHED` or `STOPPED_CAPITAL_EXHAUSTED`; a lower rank never leapfrogs into leftover crumbs. Size/concentration failures defer only that candidate |
| External funding (optional, default off) | Planning capability `metadata.external_funding_available` (no schema, a missing key means `false`; set with `Manage-TradingCapitalState.py external-funding`). **`true` is not unlimited buying power**: it lifts only the cash/reserve shortage. Swing maximum, initial/security weight, existing position/campaign, data quality and candidate status stay binding. No deposit is booked (no `portfolio_capital_state` row, no transaction, no order); the externally funded amounts are pure planning figures. Terms (`plan.capital`): `internal_deployable_capital = max(0, min(cash, buying_power) + net_proceeds - reserve)`; `allocation_headroom` = free Swing capacity after the actions and the engine's own ADD/BUY purchases = `max(0, (swing_max * total - swing) / (1 - swing_max))`; `deployable_capital` = `min(internal_deployable_capital, allocation_headroom)` without, `allocation_headroom` with external funding (upper bound for new BUY/ADD; position weights and whole shares can yield less); `external_funding_required` = `max(0, planned_capital - internal_deployable_capital)` (0 without external funding). Mathematics: weights are percent of the invested value, a purchase raises numerator **and** denominator; the ceilings `max(0, (max_weight * total - position) / (1 - max_weight))` contain no cash. External money is therefore invested exactly like internal money and changes no ceiling. Accounting is sequential (engine ADD/BUY first, then entries by rank): per purchase `internal = min(amount, max(0, cash - reserve))`, `external = amount - internal`, cash falls by `internal` only; the reserve is never touched. With external funding `STOPPED_CASH_RESERVE_REACHED` and the cash-driven `STOPPED_CAPITAL_EXHAUSTED` (and the engine reasons `ENTRY_CASH_RESERVE_LIMIT`/`ADD_CASH_RESERVE_LIMIT`) no longer occur; if less than one share of Swing capacity is left the plan ends with `STOPPED_SWING_MAX_REACHED`. Unknown cash stays a data problem. `capital.limiting_guard` names the guard that ended the plan (`STOPPED_*`, `ALL_READY_CANDIDATES_PLANNED` or `NO_ENTRY_PLANNED`); warnings `EXTERNAL_FUNDING_ASSUMED_AVAILABLE` and `EXTERNAL_FUNDING_REQUIRED_FOR_PLAN` |
| Not modelled | Fees; FIFO tax lots, loss pot, tax-free allowance; sector/industry exposure (no data for held positions, so only per-security limits apply: warning `CONCENTRATION_SINGLE_SECURITY_LIMITS_ONLY`); buying power (cash is used while it is unavailable) |

Output keys: `status`, `simulation_only`, `orders_created`, `current_state`, `existing_position_actions`, `post_action_state`, `entry_candidates`, `planned_entries`, `deferred_entries`, `final_simulated_state`, `capital` (capital terms, see external funding), `tax_estimate` (parameters, quality, `gross_proceeds_total_eur`, `estimated_tax_total_conservative`, `estimated_tax_total_with_plan_offset`, `net_proceeds_total_conservative`, `net_proceeds_total_with_plan_offset`), `entry_summary`, `methodology`, `warnings`, `rendered_de`; every SELL/TRIM action carries `gross_proceeds_eur`, `estimated_cost_basis_eur`, `estimated_realized_gain_eur`, `estimated_tax_eur_conservative`/`_with_plan_offset` and `estimated_net_proceeds_eur_conservative`/`_with_plan_offset`. A planner failure yields an `UNAVAILABLE` plan (`PORTFOLIO_ACTION_PLAN_ERROR:<type>`), never a failed orchestrator run. In MCP compact mode (`detail=false`) the whole orchestrator result is reduced to the structured status of `mcp-tools/orchestrator_compact.py` (see §5); the complete planner output stays in `detail=true`.

### 3.11 Watchlist Candidate Discovery (`candidate_discovery.py`, `Discover-TradingCandidates.py`)

`DISCOVERY != PROMOTE != ENTRY_READY != ORDER` — the output is only a suggestion list; nothing is written (`read_only`, `watchlist_written: false`, `orders_created: false`), no entry plan is triggered. No MCP method runs the discovery (the MCP client's tool timeout - 30 s in the current setup - is shorter than a run with market-data requests): the run is the CLI (scheduled daily, see §2), and the MCP client reads the report it wrote through the read-only `get_candidate_discovery` (see below).

| Stage | Rule |
|---|---|
| Universe | Versioned static file `tools/trading/universe/swing_large_cap_v1.json` (liquid large caps, Yahoo symbols, expected quote currency as a pre-filter hint); not an official index composition |
| Pre-filter (before any request) | `ALREADY_IN_PORTFOLIO` / `ALREADY_ON_WATCHLIST` (exact Yahoo symbol in `source_symbols`), `UNSUPPORTED_SECURITY_TYPE` (universe flag), `UNSUPPORTED_CURRENCY` (the existing FX resolver cannot value the hinted currency in EUR) |
| After the fetch | `UNSUPPORTED_SECURITY_TYPE` (provider type not EQUITY), `UNSUPPORTED_CURRENCY`, `PRICE_BELOW_MINIMUM` (< 5 EUR), `LOW_LIQUIDITY` (median daily traded value of the last 60 sessions < 5 M EUR), `LIQUIDITY_DATA_UNAVAILABLE` / `NO_MARKET_DATA` (insufficient data) |
| Analysis | Unchanged engine on an in-memory copy of the production schema (production is only read): `build_analysis_snapshot` (technical quality needs ≥ 200 points and fresh data, SMA50/200, performance, momentum, EUR valuation) and `evaluate_swing_promotion` (trend gate) |
| Status | Promotion `DATA_INSUFFICIENT` → `DISCOVERY_DATA_INSUFFICIENT` (`INSUFFICIENT_HISTORY`, `MARKET_DATA_STALE`, `FX_RATE_UNAVAILABLE`, `MARKET_DATA_MISSING`); `PROMOTE` with momentum ≥ `BUY_SCORE_THRESHOLD` (20) → `DISCOVERY_READY` (`TREND_INTACT`, `MOMENTUM_ABOVE_THRESHOLD`), below → `DISCOVERY_WATCH` (`MOMENTUM_BELOW_THRESHOLD`); `KEEP_WATCHING` with price ≤ SMA200 → `DISCOVERY_REJECTED` (`PRICE_BELOW_SMA200`), otherwise `DISCOVERY_WATCH` (`PRICE_BELOW_SMA50`, `SMA50_BELOW_SMA200`) |
| Score / ranking | `discovery_score = momentum_score × decision_engine._confidence` (the planner's `entry_score` formula); order: status, score (desc), momentum (desc), symbol (asc) |
| Not evaluated | Fundamentals, valuation and events are not fetched for new securities (`fundamentals_evaluated: false`); their quality shows the engine's view without cached data. Sector/correlation, market cap and any LLM input are not used |

**Report and MCP reader.** The CLI writes `latest.json` only after a completely successful run (a run in which no symbol delivered market data is a failed run) and replaces it atomically; a failed/aborted run leaves the previous report untouched. Schema v1: `metadata` (`schema_version`, `generated_at`, `evaluation_as_of`, `universe_name`, `universe_size`, `analyzed_count`, `excluded_count`, the four status counts, `source` (fetched/cache counts), `deterministic`, `results_sha256`), `results` (ranked candidates, no raw price series) and `excluded` (symbol, reason codes). The new read-only MCP method `get_candidate_discovery(status, limit, detail, symbol)` (tool count 12 → 13) only reads that file: **it never starts a discovery run, calls a provider, or touches the database or the watchlist**. No report → `status: UNAVAILABLE`, `reason: NO_DISCOVERY_REPORT` (`REPORT_INVALID` for a corrupt or tampered file); an old report is returned with `generated_at`, age and `report_stale` / `REPORT_STALE` (limit: the existing `DataQualityConfig.market_data_max_age_days`, measured on `evaluation_as_of`). Compact mode returns rank, symbol, name, discovery_score, status and reason codes (at most 150 rows); detail returns all stored fields (at most 40 rows); `status=EXCLUDED` lists the pre-filter exclusions; `symbol` explains a single title. The run is scheduled daily since 2026-10-09 (Windows Scheduled Task `Trading-Candidate-Discovery`, see §2).

Output keys: `status`, `read_only`, `watchlist_written`, `orders_created`, `evaluation_as_of`, `universe`, `universe_size`, `prefiltered_count`, `analyzed_count`, `discovery_ready_count`, `discovery_watch_count`, `insufficient_count`, `rejected_count`, `candidates` (rank, security_id if known, symbol, name, market, currency, prices, `momentum_score`, `discovery_score`, qualities, `status`, `reason_codes`), `excluded_candidates`, `methodology`, `warnings`, `rendered_de`.

---

### 3.12 Market Intelligence (`market_intelligence.py`, `intelligence_report.py`, `Collect-TradingMarketIntelligence.py`)

`NEWS != SIGNAL != BUY != SELL` — events are context and warnings only. The block is read-only and deterministic (`read_only`, `orders_created: false`, `signals_changed: false`); no decision module (`trading_orchestrator`, `portfolio_action_planner`, `swing_promotion`, `decision_engine`, `candidate_decision`, `entry_sizing`, `candidate_discovery`, `analysis_engine`, `swing_lifecycle`, `strategy_suggestion`) imports it (checked by a test). No LLM produces or classifies events, and no event is invented: every event carries `source_url` of a fetched item. Flow: `sources -> Collect CLI -> report -> MCP reader -> MCP client`; the MCP reader never fetches, scans or opens the database.

| Aspect | Rule |
|---|---|
| Sources | 10 official feeds without key (`ECB_PRESS`, `ECB_STATS`, `FED_MONETARY`, `FED_PRESS`, `BLS_CPI`, `BLS_EMPLOYMENT`, `BEA`, `DESTATIS`, `EIA_TODAY`, `EIA_PRESS`); SEC 8-K rows of the `events` table; cached Yahoo headlines of the `news` table; fresh Yahoo headlines for `DISCOVERY_READY` candidates of the discovery report. Eurostat not connected; `COMMODITIES` not implemented |
| Window | `MarketIntelligenceConfig.lookback_days` = 7 on `published_at` (future-dated items are excluded) |
| Event id | `sha256(source_type \| canonical_url_or_accession)[:16]`; same id → one event (symbols, sectors, reasons united, earliest time kept, more important classification kept) |
| Categories | MACRO, MONETARY_POLICY, INFLATION, LABOR_MARKET, ECONOMIC_GROWTH, GEOPOLITICS, ENERGY, SECTOR, COMPANY, EARNINGS, GUIDANCE, REGULATORY, M_AND_A (fixed rule table per feed; SEC item table; ordered headline keyword rules, first match wins, stock-pick articles first) |
| Importance | fixed table. HIGH: ECB/Fed rate decision, SEC acquisition/control change/delisting/cybersecurity/non-reliance, company press-release wires, or a keyword class whose headline names the linked company by name. A bare ticker never carries HIGH (`TICKER_ONLY_MATCH_CAPPED_AT_MEDIUM`); keyword-only secondary headlines are capped (`SECONDARY_SOURCE_CAPPED_AT_MEDIUM`, `SYMBOL_LINK_NOT_CONFIRMED_BY_HEADLINE_CAPPED_AT_MEDIUM`). Scheduled macro releases are MEDIUM (`MACRO_RELEASE_SURPRISE_NOT_ASSESSED`); GEOPOLITICS at most MEDIUM |
| Impact | `UNKNOWN` unless the structure of a primary filing states an adverse fact (`delisting_notice`, 8-K 1.05, 8-K 4.02 → `NEGATIVE`, `impact_basis: STRUCTURE_OF_PRIMARY_FILING`); otherwise `NOT_READ_FROM_HEADLINE` / `NOT_ASSESSABLE_WITHOUT_CONSENSUS`. `POSITIVE`/`MIXED`/`NEUTRAL` exist in the vocabulary but no v1 rule produces them |
| Mapping | `affected_portfolio_symbols` / `affected_watchlist_symbols` / `affected_discovery_symbols` by Yahoo symbol; explicit sector events (keywords, EIA) reach all symbols of the sector via `sector_map_v1.json` (`SECTOR_SCOPED_EVENT`); macro events carry `portfolio_region_exposure` (region from the Yahoo suffix of held symbols) |
| Ordering | importance, then portfolio relevance, then recency (newest first), then `event_id` |

**Report and MCP reader.** `latest.json` is written only if at least one feed answered; schema v1: `metadata` (`schema_version`, `generated_at`, `evaluation_as_of`, `lookback_since`, `source_counts`, `event_count`, `high/medium/low_count`, `portfolio/watchlist/discovery_relevant_count`, `macro_event_count`, `collection` (per-feed status), `warnings`, `deterministic`, `events_sha256`, `methodology`) and `events` (compact JSON, no article text; summaries ≤ 200 characters). The write is atomic (temp file → re-read and validate → `os.replace`, history copy first); a failed or invalid run leaves the previous report untouched. The read-only MCP method `get_market_intelligence(scope, category, importance, symbol, sector, limit, detail)` (tool count 13 → 14) only reads that file. `scope`: `ALL`, `PORTFOLIO`, `WATCHLIST`, `DISCOVERY`, `MACRO`; `symbol` accepts base symbols (`SAP` matches `SAP.DE`). No report → `UNAVAILABLE` / `NO_MARKET_INTELLIGENCE_REPORT` (`REPORT_INVALID` for a corrupt or tampered file); a report older than `report_max_age_hours` is returned with age and `REPORT_STALE`. **Compact** (default, at most 50 events) is slim: short freshness (`status`, `generated_at`, `age_hours`, `evaluation_as_of`), the active filter, the importance counters of *all* matching events, `matching`/`returned`/`truncated` and the events (`published_at`, `importance`, `category`, full `headline`, `source`, `impact`, up to three `reason_codes` without the generic `CLASSIFIED_BY_HEADLINE_KEYWORD` marker; with `symbol` additionally `link`, otherwise the affected symbols (at most 6), sectors and regions). `link` is derived from the stored classification only: `DIRECT` (SEC filing, or the headline names the company and the event concerns that one symbol), `SHARED` (headline names a company but the event is attached to several symbols), `SECTOR` (`SECTOR_SCOPED_EVENT`), `LOOSE`. No `rendered_de`, source statistics, collection log, notes or methodology; a `hint` appears only for a stale report or a truncated list. **Detail** (at most 25 events) is complete and unchanged. The collector CLI still prints its table from the broad compact answer (`slim=False`). The serialized response is held below 34,000 characters (the MCP server rejects results above 40,000) by dropping the least important events (`RESPONSE_TRIMMED_TO_SIZE_BUDGET`). The run is scheduled daily since 2026-10-09 (Windows Scheduled Task `Trading-Market-Intelligence`, 08:15 and 17:45, see §2).

**Known limits (v1).** Yahoo attaches headlines to symbols loosely (market wrap-ups, ticker homonyms such as "ACME Supply"); keyword classification is `confidence: LOW`; most events have `impact: UNKNOWN`; sector scope is a static, versioned mapping.

### 3.13 Unified Opportunity View (`opportunity_view.py`)

`DISCOVERY + MARKET INTELLIGENCE + PORTFOLIO/PLANNER CONTEXT -> one opportunity output`, read-only and deterministic (`read_only`, `orders_created: false`, `signals_changed: false`). The module joins existing results; it calculates **no** score, rank, size or recommendation, and there is **no combined score** (no "score + news bonus"). The decision modules and both report modules do not import it (checked by a test). No new table or persistence layer: the MCP method reads the orchestrator result (usual read-only connection) and the two report files.

| Aspect | Rule |
|---|---|
| Inputs | orchestrator primitive (`promotion_results`, `portfolio_action_plan.entry_candidates/planned_entries/deferred_entries/current_state/post_action_state/final_simulated_state`, `existing_position_results`), `discovery_report.read_latest_report`, `intelligence_report.read_latest_report`, `security_id -> Yahoo symbol` from `source_symbols` |
| Candidates | A) PROMOTE candidates of the planner (all `entry_status` values); B) `DISCOVERY_READY` results of the discovery report. `source`: `WATCHLIST`, `DISCOVERY`, `BOTH` (a symbol of the report that the engine already knows, e.g. added after the report was written; warning `DISCOVERY_SYMBOL_ALREADY_KNOWN`). `DISCOVERY_WATCH`, `KEEP_WATCHING` and held-only symbols are not opportunities (held positions appear under `portfolio_context.positions`) |
| Group / order | `ENTRY_READY` > `WAIT_FOR_TRIGGER` > `DISCOVERY_READY` > `OTHER`; inside a group: engine rank, `entry_score` (desc), discovery rank, watchlist priority (desc), `security_id`, symbol — never news |
| Fields | symbol, name, source, group, discovery_rank/score/status, promotion_status, entry_status/rank/score, momentum_score, confidence, current_price_eur, technical/fundamental/event_risk quality (`not_evaluated` for discovery-only), plan_status (`PLANNED`/`DEFERRED`/`NOT_PLANNED`/`NOT_APPLICABLE` + planner reason + planned capital/quantity as computed by the planner), `capital_fit`, `market_intelligence_summary` |
| News link | per event and symbol: `DIRECT` (SEC filing, or the headline names the company by legal name / a distinctive word of the name / a ticker in parentheses such as `(ABCD)`; a bare ticker is not enough), `SECTOR` (event has `SECTOR_SCOPED_EVENT`), `LOOSE` (provider attached it without the headline naming the company) |
| News status | `NEWS_HIGH_ATTENTION`: at least one DIRECT HIGH event; `NEWS_ATTENTION`: a DIRECT MEDIUM event, or a HIGH event that is only SECTOR/LOOSE; `NEWS_CLEAR`: only LOW, loosely linked MEDIUM, or none; `NEWS_UNAVAILABLE`: no valid MI report. Counts (all/direct), top non-LOW events (1 compact, 6 detail), `status_reasons` |
| Capital context | `portfolio_context`: current cash/deployable/reserve/swing share and corridor, `after_actions` (cash after planned sales, conservative net proceeds, taxes/fees flags), `after_plan` (planned entries, total, `remaining_buying_capacity_eur`, remaining swing capacity), planned and deferred entries, `funding` (with external funding active: internal/external split, `external_funding_required_eur`; otherwise only `external_funding_available: false`). `capital_fit` (discovery rows only): price of one share vs deployable cash after the actions / after the plan (`FITS_AFTER_PLAN`, `COMMITTED_BY_PLANNED_ENTRIES`, `DOES_NOT_FIT`) — informational, no sizing, no order |
| Macro / attention | `market_context.macro_context`: HIGH/MEDIUM events per macro category (monetary policy, inflation, labor, growth, geopolitics, energy, macro) with official-source count; `high_attention`: direct HIGH events of candidates and held positions |
| Freshness | `sources.discovery` / `sources.market_intelligence`: `status`, `generated_at`, `evaluation_as_of`, `age_hours`, `stale` (discovery: `market_data_max_age_days`; MI: `report_max_age_hours`). Status `AVAILABLE` (all three present), `PARTIAL` (a report or the engine missing — the rest is shown, missing news → `NEWS_UNAVAILABLE`), `UNAVAILABLE` (nothing present). A stale report is shown and flagged, never hidden |

**MCP reader.** `get_opportunity_view(source="ALL", status="ALL", news_status=None, symbol=None, limit=30, detail=False)` (tool count 14 → 15; `trading_sqlite.py` 1.7.0): `source` ALL/WATCHLIST/DISCOVERY/BOTH, `status` ALL/ENTRY_READY/WAIT_FOR_TRIGGER/DISCOVERY_READY/OTHER, `news_status` a `NEWS_*` value or `RELEVANT` (attention or high) / `NOT_HIGH`, `symbol` one or a comma-separated list (base symbols match listings; also filters the held positions shown). The **compact** answer (default, at most 40 rows, default `limit` 30 so that e.g. all 12 `ENTRY_READY` rows arrive in one call) contains only: status and freshness of both reports, a minimal capital frame (cash, cash after the planned actions, conservative net proceeds, planned entries total, remaining buying capacity, swing share and corridor), the matching rows without empty fields (entry status/rank/score, momentum, plan status with planned capital, the engine's own sizing proposal `proposed_capital_eur`/`proposed_quantity` passed through unchanged, stop reason, quality, news status with counts and the single most important event, capital fit for discovery rows) and the warning codes; with `symbol` only the requested symbols and the so-named held positions. No position list, discovery list, macro block, global high-attention list, source metadata, notes or `rendered_de`. **Detail** (at most 10 rows) keeps every block incl. methodology and the German tables in `rendered_de`. The serialized response is kept below 34,000 characters (the MCP server rejects results above 40,000): compact drops the last rows, detail first reduces secondary sections (methodology, position events, macro detail, discovery list) and then the last rows (`RESPONSE_TRIMMED_TO_SIZE_BUDGET`). It never fetches, scans, starts a collector or discovery run, or writes.

**Known limits (v1).** The company is recognised by name or a parenthesised ticker only (a short form without a name part is not DIRECT); provider-attached headlines can be loose; many `NEWS_ATTENTION` rows are analyst price-target items (COMPANY/MEDIUM); `capital_fit` ignores concentration and Swing-maximum rules because no entry plan is created for discovery candidates.

---

## 4. Fundamentals / Data Quality

- **Verification status vocabulary** (`Research-TradingFundamentals.py`, the LLM-research path): `verified_direct` (primary source), `verified_derived` (computed from verified values via a documented formula), `verified_secondary` (≥2 independent secondary sources, or one strong one + plausibility check), `not_verified` (discarded, stored as NULL — never guessed).
- **Structural validation** (independent of the above, applied to every accepted period): `fiscal_year` in [2000, 2100]; `currency` a 3-char code; `revenue > 0`; `gross_profit <= revenue`; `total_assets ≈ total_liabilities + total_equity` within `max(1% of assets, 1,000,000)`; free-cash-flow check `|FCF - (OCF - capex)| <= max(1% of OCF, 1000)`.
- **`fundamentals` table** stores raw figures only (revenue, gross_profit, operating_income, ebit, ebitda, net_income, eps_basic/diluted, operating_cash_flow, capex, free_cash_flow, cash, total_debt, total_assets/liabilities/equity, shares_outstanding) — "no unnecessarily computed ratios" (schema comment).
- **Source priority is not a single global ranking** — it differs per pipeline: `Backfill-TradingFundamentalsSEC.py` uses SEC EDGAR XBRL exclusively; `Backfill-TradingFundamentalsIR.py` is a fixed 6-company hand-curated set from company IR sites; `Research-TradingFundamentals.py` (the canonical/general-purpose tool per the README) lets an LLM choose primary vs. corroborated-secondary sources under the `verified_*` vocabulary above, and stores the source as `"Company IR"` only if every field used is primary, else `"Web"`.
- **`asset_type` fundamentals exemption**: `{"etf", "fund"}` is treated as fundamentals-not-applicable both in `analysis_engine.py`'s per-security snapshot and in `Audit-TradingDataQuality.py`'s classification, and reused as `strategy_suggestion.LONG_TERM_ASSET_TYPES`.
- **`Audit-TradingDataQuality.py`** classifies each security `MISSING` (0 periods), `STALE` (has a data gap AND `age_days > --stale-days`, default 180), `PARTIAL` (has a gap, not stale), `COMPLETE` (none of `CORE_FIELDS = [revenue, net_income, operating_cash_flow, cash]` missing and FCF is derivable when OCF+capex are present), or `NOT_APPLICABLE` for ETF/fund asset types. Read-only; never writes.

---

## 5. Trading MCP tool `trading_sqlite`

> **Current access path:** the agent (through an MCP client) uses this tool through the Trading MCP server (`C:\tools\trading\mcp\server.py`; repo source `mcp-tools/`), which exposes every public method of the `Tools` class in `trading_sqlite.py` as an MCP tool — currently 15. Parqet imports run via `Import-ParqetTransactions.py` (there are no upload methods in the MCP server).

**Deploy (current):** `deploy\Deploy-TradingAgent.ps1` (`-Action Check` / `Deploy` / `Rollback`, see the README) copies only the files listed in `deploy/deploy-manifest.json` to `C:\tools\trading\app\`, `mcp\` and `scheduler\` after a file and database backup, compares hashes and runs an MCP smoke test (expects 15 tools). Afterwards restart the MCP server process (in the current setup the MCP client restarts it automatically when it is ended).

**Current public method inventory** (version `1.7.0`, 15 public methods, now `mcp-tools/trading_sqlite.py`), classified read-only vs. write-capable:

| Method | Read-only? | Notes |
|---|---|---|
| `sql_execute(sql)` | **No — unrestricted** | Tool description literally states "Direct unrestricted SQLite access"; opens a normal read-write connection (`isolation_level="DEFERRED"`); multi-statement, one transaction, rolls back the whole batch on any failure. `Valves`: `database_path` (default `C:\tools\trading\data\trading.db`), `timeout_seconds=1.0`, `busy_timeout_ms=1000`, `max_rows_per_statement=500`. |
| `database_tables()` | Yes | Lists tables |
| `database_schema()` | Yes | Full schema dump |
| `table_info(table)` | Yes | Per-table column info |
| `database_status()` | Yes | Summary counts/integrity |
| `rank_watchlist()` | Yes | Loads `trading_analytics.py` via the cached `_load_runtime_trading_module()` path (see note below); uses the same read-only connection as the other analytics wrappers since 2026-09-27 (previously a read-write connection) |
| `run_trading_orchestrator(as_of=None)` | Yes | Dynamically loads `trading_orchestrator.py` (and its dependency chain); since v0.2.0 the result also carries the simulation-only `portfolio_action_plan` (§3.10) |
| `evaluate_swing_candidate(security_id)` / `evaluate_swing_candidates()` | Yes | Dynamically loads `swing_promotion.py` |
| `approve_swing_promotion(...)` | **No — writes** `strategy_assignment` + `candidate_promotion` | Requires a still-current plan token from `evaluate_swing_candidate` |
| `suggest_strategy_assignments(as_of=None)` | Yes | Dynamically loads `strategy_suggestion.py`; calls it unchanged (see §3.4) |
| `evaluate_watchlist_candidates(as_of=None)` | Yes | Dynamically loads `candidate_decision.py`; calls it unchanged (see §3.3) |
| `get_candidate_discovery(status="ALL", limit=20, detail=False, symbol=None)` | Yes — reads one file | Version `1.5.0`. Dynamically loads `discovery_report.py` and reads `<database directory>\candidate-discovery\latest.json` (valve `discovery_report_dir`); **no discovery run, no provider call, no database access** (§3.11) |
| `get_market_intelligence(scope="ALL", category=None, importance=None, symbol=None, sector=None, limit=20, detail=False)` | Yes — reads one file | Version `1.6.0`. Dynamically loads `intelligence_report.py` and reads `<database directory>\market-intelligence\latest.json` (valve `market_intelligence_report_dir`); **no collection run, no web request, no database access** (§3.12) |
| `get_opportunity_view(source="ALL", status="ALL", news_status=None, symbol=None, limit=30, detail=False)` | Yes — orchestrator on the read-only connection + two report files | Version `1.7.0`. Dynamically loads `opportunity_view.py`, `trading_orchestrator.py`, `discovery_report.py`, `intelligence_report.py`; **no web request, no collection/discovery run, no database write, no order** (§3.13) |

Both new methods follow the exact same pattern as `run_trading_orchestrator`/`evaluate_swing_candidates`: `_load_runtime_trading_module()` loads the deployed module from `C:\tools\trading\app\`, `_readonly_import_connection()` opens a strict `PRAGMA query_only=ON` connection.

**Compact orchestrator view.** `server.py` answers `run_trading_orchestrator` with the structured status of `orchestrator_compact.py` unless `detail=true` is passed (about 8,000 instead of 95,000 characters). The module only copies values of the full result — nothing is recomputed, re-ranked or re-rounded — and keeps two sources apart: `position_engine` (decision engine for existing positions: SELL/TRIM/HOLD/ADD, `engine_buy_count`, `engine_add_count`) and `entry_plan` (planner: `planned_entries`, `deferred`, `stop_reason`, `remaining_buying_capacity_eur`, `funding`). The other blocks are `portfolio`, `positions`, `promotion`, `proceeds` (gross/tax/net), `capital` and `issues`. A missing or failing projection falls back to the former list-omitting view, so the tool keeps working.

Runtime helper modules dynamically loaded from `C:\tools\trading\app\`: `trading_analytics.py`, `parqet_import.py`, `transaction_import.py`, `swing_lifecycle.py`, `analysis_contracts.py`, `trading_orchestrator.py`, `swing_promotion.py`, `strategy_suggestion.py`, `candidate_decision.py` (and their own import chains, e.g. `analysis_engine.py`, `decision_engine.py`, `portfolio_context.py`, `strategy_config.py`, `fx_resolver.py`, `capital_state.py`, `orchestrator_presentation.py`).

**Since 2026-09-27**, `_load_runtime_trading_module()` caches each loaded module per class (i.e. per currently-loaded `trading_sqlite.py` version) instead of re-reading and re-`exec_module`-ing the file on every tool call. A runtime helper file change under `C:\tools\trading\app\` therefore only takes effect after the Trading MCP server process restarts (the cache lives per process; in the current setup the MCP client restarts the server automatically when it is ended).

---

## 6. Production paths

| What | Path |
|---|---|
| Production trading DB | `C:\tools\trading\data\trading.db` |
| Production trading tools | `C:\tools\trading\app\` (also the module directory of the MCP server copy `C:\tools\trading\mcp\trading_sqlite.py`) |
| Candidate-discovery report | `C:\tools\trading\data\candidate-discovery\latest.json` |
| Market-intelligence report | `C:\tools\trading\data\market-intelligence\latest.json` (+ `market-intelligence-YYYYMMDD-HHMMSS.json`) |
| SEC fundamentals cache | `C:\tools\trading\data\sec-cache` |
| Fundamentals research prompts/audit | `C:\tools\trading\data\fundamentals-audit\` (prompts in its `prompts\` subfolder) |
| Dev repository (this repo) | `C:\tools\src\Trading-Agent-Dev-clean` |

---

## 7. Database schema — tables relevant to the architecture

| Table | Purpose | Key constraints |
|---|---|---|
| `metadata` | Global key/value settings, incl. every feature's schema-version marker | — |
| `data_sources` | Registry of external data providers | `name` unique |
| `security` | Central security identity (symbol/ISIN/WKN/name/exchange/currency/country/`asset_type`) | `isin` unique where not null |
| `source_symbols` | Security → per-source external ticker/CIK mapping | unique `(security_id, source_id)` |
| `imports` | Import run history (e.g. Parqet) | — |
| `transactions` | Full, append-only portfolio transaction history — **never rewritten**, even by a corporate action | unique `external_id` where not null |
| `positions` | Current portfolio state, exactly one row per security, rebuilt (not incrementally patched) by `transaction_import._rebuild_position` | PK `security_id` |
| `watchlist` | Swing-candidate research list, exactly one row per security | PK `security_id` |
| `market_snapshot` | Latest known price/quote per security | PK `security_id` |
| `market_data` | Daily OHLCV history, main technical-analysis input | unique `(security_id, trade_date, source_id)` |
| `fundamentals` | Raw annual/quarterly/semiannual/nine-month fundamentals | unique `(security_id, period_end, period_type, source_id)` |
| `estimates`, `ratings`, `price_targets` | Row-count-only diagnostic inputs to `ValuationAnalysis.quality` in `analysis_engine.py` — **schema exists and is read, but no current script populates these three tables** (no free/already-available source; see known inconsistency #4) | — |
| `events`, `news` | Row-count-only diagnostic inputs to `EventRiskAnalysis.quality` — **populated since 2026-09-28** by `Backfill-TradingEventsNews.py` (SEC EDGAR 8-K/6-K filings for `events`, Yahoo Finance search for `news`; see §2 and known inconsistency #4) | — |
| `strategy_assignment` | Time-interval strategy per security (`long_term`/`swing`/`tactical`/`unknown`) | no overlapping intervals per security (trigger-enforced) |
| `candidate_promotion` | Audit trail of promotion evaluations/approvals | — |
| `fx_rates` | ECB-convention daily FX rates, EUR-based only | unique `(rate_date, base_currency, quote_currency, source)`; `base_currency` must be `'EUR'` |
| `swing_campaign` | Explicit Swing campaign identity (one open per security) | partial unique index on `status='open'`; FK to a `swing` `strategy_assignment` |
| `swing_campaign_event` | Lifecycle events: manually recorded, plus neutral transaction-linked `add`/`manual_reduction` from the Parqet reconciliation (never TP/stop reasons) | `event_type` CHECK list; linked transaction must match the campaign's security; unique `transaction_id` |
| `portfolio_capital_state` | Recorded cash/buying-power snapshots (`Migrate-TradingCapitalState.py`); the planner switch `external_funding_available` lives separately as a `metadata` entry | `base_currency` must be a 3-letter code |
| `corporate_action` | Auditable stock splits (`Migrate-TradingCorporateActions.py`); original transactions are never rewritten — this table is *consulted*, not applied destructively | `action_type` currently `'STOCK_SPLIT'` only; unique `(security_id, action_type, effective_date, ratio_numerator, ratio_denominator)` |

**Views** (simple SQL-only helpers, distinct from and simpler than the authoritative FX-aware `analysis_engine.build_portfolio_context` path): `v_active_positions`, `v_watchlist`, `v_portfolio_market` (same-currency valuation only — returns `NULL` market value on any currency mismatch, unlike the FX-normalizing engine).

**Current production state**: all listed feature schemas are applied (`schema_version=2.0`; every feature-version marker `=1`, including `corporate_action_schema_version`). Recorded stock splits (`corporate_action`) have been applied and the affected `positions` rows reflect the split-corrected quantities. `candidate_promotion` currently has 0 rows (schema present, nothing promoted through it yet). `estimates`/`ratings`/`price_targets` are still empty in production, consistent with §7's "unused/unpopulated" note above; `events`/`news` are populated by `Backfill-TradingEventsNews.py`. `decisions`/`analysis_history` are **no longer part of the production schema** — both tables were dropped (see inconsistency #5 below), not merely empty.

---

## 8. Tests / current quality status

- Candidate Decision: `tests/trading/test_candidate_decision.py` — 8 tests (good swing candidate → BUY, insufficient analytics data → INSUFFICIENT_DATA, open campaign → WATCH, missing assignment → DEFERRED, guardrail above max → DEFERRED, non-swing assignment → WATCH, score below threshold → WATCH, unavailable allocation → DEFERRED).
- Strategy Suggestion: `tests/trading/test_strategy_suggestion.py` — 7 tests (active assignment skipped, high swing suitability → swing, insufficient data → unknown, open campaign → swing, no long-term inference from momentum alone, ETF asset_type → long_term, score below threshold → unknown).
- Portfolio Action Planner: `tests/trading/test_portfolio_action_planner.py` — 29 tests (post-action capital for SELL/TRIM/ADD/BUY, every entry status, ranking determinism and tie-breaks, sizing parity with `entry_sizing`, cash reserve, Swing maximum, sequential/strict-order allocation, final-state consistency, input immutability, orchestrator integration and planner-failure isolation); `tests/trading/test_mcp_server_slimming.py` — 7 tests (adapter default is the compact status, `detail=true` untouched, fallbacks, legacy view, tool signature and 15 tools; needs the Trading venv, skipped otherwise).
- Orchestrator compact view: `tests/trading/test_orchestrator_compact.py` — 11 tests (portfolio-status core fields present, large duplicate blocks and rendered texts absent, every value identical to the full result, position engine and planner kept apart, deferred entries grouped with stop reason, unavailable planner, empty input, no database access, no orders).
- Corporate Actions: `tests/trading/test_corporate_actions.py` — 20 tests (split-factor temporal semantics, schema availability, validation/idempotency, position-rebuild integration).
- Broader coverage exists for: Parqet import (`test_parqet_import.py`), portfolio context (`test_portfolio_context.py`), decision engine (`test_decision_engine.py`), entry/ADD recommendations (`test_swing_entry_recommendation.py`, `test_swing_add_recommendation.py`), Swing TP/stop/runner actions (`test_swing_tp_actions.py`, `test_swing_hard_stop.py`, `test_runner_trend_exit.py`), Swing promotion (`test_swing_promotion.py`, `test_mcp_swing_promotion.py`), campaign lifecycle (`test_swing_campaign_lifecycle.py`), strategy assignment (`test_strategy_assignment.py`, `test_manage_strategy_assignments.py`), capital state (`test_capital_state.py`), FX/currency (`test_fx_currency.py`, `test_market_snapshot_currency_backfill.py`), analysis contracts/engine (`test_analysis_contracts.py`, `test_analysis_engine.py`), trading analytics (`test_trading_analytics.py`), orchestrator + presentation (`test_trading_orchestrator.py`, `test_orchestrator_presentation.py`).
- Market Intelligence: `tests/trading/test_market_intelligence.py` — 70 tests (normalization of feed/SEC/Yahoo items, duplicate merge and stable ids, category and importance tables, ticker-homonym cap, UNKNOWN impact and no direction from headlines, portfolio/watchlist/discovery/sector/region mapping, window filter, determinism and unmodified inputs, atomic report with history, failed run and failed replace keep `latest.json`, tampered/corrupt report, stale report, scope/category/importance/symbol/sector filters, compact/detail, response size guard, no network/DB/write in the reader, MCP tool surface of 14 tools with the 13 existing ones unchanged, no decision module imports the news modules).
- Opportunity View: `tests/trading/test_opportunity_view.py` — 72 tests (discovery/MI present, missing, stale; engine missing; WATCHLIST/DISCOVERY/BOTH sources; ENTRY_READY/WAIT_FOR_TRIGGER/DISCOVERY_READY/OTHER groups and order; HIGH/MEDIUM/LOW news and the four `NEWS_*` statuses incl. DIRECT/SECTOR/LOOSE links, ticker homonyms and parenthesised tickers; scores, ranking, sizing and SELL/TRIM/HOLD unchanged with and without news; inputs not modified; compact/detail; filters; size guard; MCP reader with a read-only connection that rejects writes, no network/subprocess/collector, corrupt/stale reports; 15-tool surface with the 14 earlier tools unchanged; decision modules do not import the view).
- Transaction import: `tests/trading/test_transaction_import.py` — 40 tests (canonical CSV valid/malformed, idempotency, historical rows, conflicts, new position without/with strategy, Swing-campaign initialization, `long_term` without campaign, existing campaign, TransferIn with an explicit campaign start, dry run without writes, example CSV, Parqet compatibility, orchestrator readiness after a complete import, result semantics: `failed` and INCOMPLETE).
- External funding: `tests/trading/test_external_funding.py` — 34 tests (flag `false`/missing/old DB unchanged, BUY and ADD with low cash, sufficient cash needs no external funding, Swing maximum/weight limits/data/campaign blockers stay binding, sale proceeds in the internal capital, no booking or order, flag storage and `Manage-TradingCapitalState.py external-funding`, compact/opportunity views).
- GitHub security configuration: `tests/trading/test_github_security_config.py` — 10 tests (minimal workflow permissions, SHA-pinned official actions, no risky triggers, CodeQL and Dependabot settings, `requirements.txt` pins matching the code's third-party imports).
- **Last full run: 700/700 passed, 0 failures** (2026-10-09, from the repo root: `C:\tools\trading\.venv\Scripts\python.exe -m unittest discover -s tests/trading -p "test_*.py"`). This number is not re-derived elsewhere in this document — if a later run produces a different count, that later number is authoritative, not this one.
- `tests/trading/` is versioned in this repository (synthetic test data only); the documentation parity of the language versions is covered by `tests/trading/test_docs_languages.py` and `tests/trading/test_query_docs.py`.

---

## 9. Known inconsistencies (found while writing this document)

1. ~~**TP1/TP2 sell-fraction duplication**: `strategy_config.SwingStrategyConfig.tp1_sell_fraction` (0.25) and `tp2_sell_fraction` (0.50) are defined but **never read** by `decision_engine.py`. Instead, `decision_engine.py` and `swing_lifecycle.py` each independently hardcode the equivalent literals (`floor(original_quantity * 0.25)` for TP1, `floor(original_quantity * 0.75)` cumulative for TP2). The values currently agree (0.25 and 0.25+0.50=0.75), but changing the config fields today would silently do nothing — a future maintainer could reasonably expect them to be load-bearing.~~ — **resolved 2026-09-27**: `decision_engine.py` (`_runner_eligibility_gaps`, `decide`'s TP1/TP2 trim branches) and `swing_lifecycle.py` (`derive_open_lifecycle`) now derive every TP1/TP2 quantity threshold from `config.swing.tp1_sell_fraction`/`tp2_sell_fraction` (with `swing_lifecycle.py` taking an optional `config: Optional[SwingStrategyConfig] = None` parameter, defaulting to `SwingStrategyConfig()` for existing callers — no behavior change). No hardcoded `0.25`/`0.75` literals remain for TP quantity logic.
2. ~~**`RiskTargetConfig` (risk_target_min/max = 0.60/0.70) is defined but consumed nowhere** — its own docstring already says "Phase 2 intentionally assigns no mathematical meaning," so this is documented-as-intentional, not a bug, but worth knowing before building anything against it.~~ — **resolved 2026-09-27**: Removed: no defined semantics, no runtime consumer; existing portfolio allocation guardrails remain authoritative. `RiskTargetConfig` and `StrategyConfig.risk` no longer exist in `strategy_config.py`.
3. ~~**`SwingStrategyConfig.horizon_months_min/max` (3/6) and `remainder_management` ("momentum_guided") are informational only** — no module enforces a time-based exit or a concrete "momentum-guided" rule; the runner is currently held/exited purely by the SMA50/SMA200 checks in `decision_engine._runner_decision`.~~ — **resolved 2026-09-27**: `decision_engine._runner_decision` now reads campaign age from the new `PositionContext.swing_campaign_opened_at` field (sourced from `swing_lifecycle.LifecycleContext.opened_at`, threaded through `analysis_engine.py`) against `config.swing.horizon_months_min/max`. Below `horizon_months_max`, `remainder_management == "momentum_guided"` runs the existing SMA50/SMA200 trend logic unchanged (`horizon_months_min` is deliberately not a minimum holding requirement — a trend break still exits at any age). At/after `horizon_months_max`, the runner closes unconditionally via the existing SELL/full-remainder path with reason `SWING_MAX_HORIZON_REACHED`, regardless of trend or technical-data availability. The hard stop, TP1/TP2, and ADD rules are untouched and keep their existing priority — the horizon only ever gates the already-open runner (post-TP2 remainder). See §3.6 above for the worked before/after examples.
4. **`estimates`, `ratings`, `price_targets`, `events`, `news` tables are read (as row counts) but never populated** by any script in this repository — `ValuationAnalysis`/`EventRiskAnalysis` quality will always reflect zero rows in the current setup. **Amended 2026-09-27**: `decision_engine._confidence()` now treats `valuation`/`event_risk` `UNAVAILABLE` as neutral (`0.00`, previously `-0.05` each) — a missing data pipeline no longer discounts confidence, since the `UNAVAILABLE` state here reflects an absent importer, not an actual diagnostic finding (`swing_promotion.py` already treats the same data as non-authoritative for the same reason, see §3.6). `AVAILABLE`/`PARTIAL` bonuses for both modules are unchanged (`+0.05`/`+0.02`); `fundamental` (backed by the actively populated `fundamentals` table) is unchanged (`+0.10`/`+0.05`/`-0.05`). **Split 2026-09-28 — Part A resolved**: `events`/`news` now have a real, verified import path (`Backfill-TradingEventsNews.py`: SEC EDGAR 8-K/6-K for `events`, Yahoo Finance search for `news`, both unauthenticated and already-registered sources), are populated in production, and run automated daily (Windows Scheduled Task `Trading-EventsNews-Backfill`, logon + daily 17:25 local, `StartWhenAvailable=True`) — `EventRiskAnalysis.quality` now correctly moves `UNAVAILABLE → PARTIAL/AVAILABLE` for covered securities. `estimates`/`ratings`/`price_targets` remain **deliberately open, not a bug**: the only source with this data (Yahoo Finance `quoteSummary`) verified live to require a browser-authenticated session (plain HTTP — even with the standard cookie+crumb handshake — returns `401 Unauthorized`); FMP has this data but was verified live to require a paid plan (`402 Payment Required` on a real key across `price-target-summary`, `grades-consensus`, `analyst-estimates`), rejected as economically not viable for private use. No free, already-available, already-integrated alternative exists in this repo. Inconsistency #4 therefore stays **OPEN** for these three tables until either a paid FMP plan or browser-session automation is deliberately chosen.
5. ~~**`decisions` and `analysis_history` tables are pure dead schema** — created by both `Initialize-TradingDatabase.py` and `Reset-TradingDb.py`, never read or written anywhere else.~~ — **resolved 2026-09-28**: the "never read" part of that claim was itself imprecise — `Resolve-TradingSecurities.py` did read both tables (a generic `COUNT(*)` reference check before deleting a stub security), which is why this now says *resolved* rather than merely *removed*. Both tables (and their indexes) removed from `Initialize-TradingDatabase.py`/`Reset-TradingDb.py`; the two now-nonexistent table names removed from `Resolve-TradingSecurities.py`'s reference-check list. No writer and no fachlicher (content) reader ever existed for either table.
6. ~~**CLI flag-name inconsistency**: `Manage-*` scripts use `--db`; `Migrate-*` scripts use `--db-path`. Both default to the same production path.~~ — **resolved 2026-09-28**: `--db-path` is now the canonical flag (shown first in `--help`, `dest="db_path"`) across the whole `tools/trading/` CLI surface; `--db` remains a working alias (`add_argument("--db-path", "--db", dest="db_path", ...)`) — no breaking change. Affected: `Backfill-TradingMarketSnapshotCurrency.py`, `Manage-TradingCorporateActions.py`, `Manage-TradingCapitalState.py`, `Manage-TradingStrategyAssignments.py`, `Manage-TradingSwingCampaigns.py`, `Import-ParqetTransactions.py`. The three `Manage-*` tools with per-subcommand hidden DB overrides (`CapitalState`, `StrategyAssignments`, `SwingCampaigns`) got the same `--db-path`/`--db` alias on `dest="command_db_path"` for every subcommand; `Manage-TradingCorporateActions.py` never had a per-subcommand override to begin with. `Migrate-*` scripts and `Initialize-TradingDatabase.py`/`Run-TradingOrchestrator.py` already used `--db-path` and are unchanged. Scheduled Tasks and tests were unaffected — none pass this flag explicitly.
7. ~~**`Migrate-TradingStrategyAssignments.py` always opens a read-write connection** (unlike its sibling Migrate-* scripts, which open a `mode=ro` URI for the dry-run path) — it relies solely on `main()`'s branching to avoid writing without `--write`. Functionally safe today, but structurally the odd one out.~~ — **resolved 2026-09-28**: added a `_connect(path, *, write: bool)` helper matching the sibling Migrate-* scripts exactly — the dry-run path now opens a `mode=ro` URI plus `PRAGMA query_only = ON`, so SQLite itself refuses any write attempt (`OperationalError: attempt to write a readonly database`), verified with a deliberate `INSERT`/`UPDATE` probe. The `--write` path (`apply_migration()`, `BEGIN IMMEDIATE`/`INSERT ... ON CONFLICT`) is unchanged.
8. ~~`candidate_decision.py` and `strategy_suggestion.py` are not yet wired into the `trading_sqlite` MCP tool~~ — **resolved 2026-09-25**: both are now exposed as `evaluate_watchlist_candidates()` / `suggest_strategy_assignments()` (tool version `1.4.0`, see §5).
9. **No path/name/value contradiction was found** between this document and the current code for any of the constants tabulated in §3 — each was read directly from the source file listed next to it, not carried over from an earlier planning state or a prior conversation.
10. **`FUNDAMENTALS_POINT_IN_TIME_DECISION_REQUIRED` — decided and implemented**: `Backfill-TradingFundamentalsIR.py` (always `filing_date = NULL`) and `Research-TradingFundamentals.py` (LLM-supplied, unvalidated, often NULL) leave many `fundamentals` rows without a `filing_date`; the strict `filing_date IS NOT NULL` guard hid them from every snapshot built with an explicit `as_of` (e.g. Swing promotion: R3NK, BA., ATS, BAYN, HO unavailable). Visibility rule (`analysis_engine._FUNDAMENTAL_VISIBLE_SQL`, used by both `_latest_fundamental` and `_previous_comparable_fundamental`): `filing_date <= as_of` -> visible, quality by field completeness; `filing_date IS NULL` and `date(fetched_at) <= as_of` -> visible, detail `FUNDAMENTALS_PUBLICATION_DATE_UNVERIFIED`, quality at most `partial`; otherwise not visible for that date. `period_end` is never a publication date and NULL is never released unconditionally. `fetched_at` is only an upper bound of the publication (and is overwritten on a refresh), hence the cap. The same rule applies on the current path: `build_analysis_snapshot` resolves `as_of=None` to today once (`evaluation_as_of`) and hands that date to the loaders, which never read the system clock; before this change `as_of=None` applied no temporal filter at all, so positions and the planner's candidate snapshots saw undated rows as `available` while the Swing promotion (explicit `as_of`) did not. `fetched_at IS NULL` is never visible. Still open: real publication dates ("Option A", e.g. DART filing id for SK hynix, sourced dates in the research tool) and the fact that `Backfill-TradingFundamentalsIR.write_company` deletes all IR rows of a security before rewriting them.

No other stale paths, contradictory thresholds, or duplicated/overlapping documentation files were found in `docs/` at the time of writing.
