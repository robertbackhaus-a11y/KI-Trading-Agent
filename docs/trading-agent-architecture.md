# Trading Agent — Architecture Reference

Authoritative, code-verified reference for every component under
`tools/trading/`, `openwebui-tools/trading_sqlite.py`, and the production
database schema. Extracted directly from the current code on
2026-09-25 — where this document and the code ever disagree in the future,
**the code is the source of truth**, not this file.

This complements, rather than replaces, the narrower existing docs:
[trading-candidate-decision.md](trading-candidate-decision.md),
[trading-strategy-suggestion.md](trading-strategy-suggestion.md),
[trading-swing-promotion.md](trading-swing-promotion.md),
[trading-entry-recommendations.md](trading-entry-recommendations.md),
[openwebui-trading-tool.md](openwebui-trading-tool.md).

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
Agent Output (OpenWebUI trading_sqlite tool, Run-TradingOrchestrator.py)
```

Per transition:

| Transition | Data passed | Who decides | Suggestion-only? | Persisted? | Explicitly no write |
|---|---|---|---|---|---|
| Data → Analytics | OHLCV closes, fundamentals rows | `trading_analytics.rank_watchlist` (pure math) | — | no | yes — read-only |
| Analytics → Strategy Suggestion | score, quality, `asset_type`, open-campaign flag | `strategy_suggestion.suggest_strategy_assignments` | **yes** | no | yes — no DB write anywhere in the module |
| Suggestion → Strategy Assignment | none automatic | a human, via `swing_promotion.approve_swing_promotion` or `Manage-TradingStrategyAssignments.py set --write` | no (this step is the actual write) | **yes**, `strategy_assignment` | — |
| Strategy Assignment → Portfolio Context | all open positions + their strategy | `analysis_engine.build_portfolio_context` (pure aggregation, no rule) | — | no | yes — read-only |
| Portfolio Context → Swing Campaign State | none directly; campaign state is independently recorded | a human, via `Manage-TradingSwingCampaigns.py open/event/close --write` | no | **yes**, `swing_campaign` / `swing_campaign_event` | — |
| → Candidate Decision | analytics + assignment + campaign + guardrails | `candidate_decision.evaluate_watchlist_candidates` | **yes** (produces BUY/WATCH/DEFERRED/INSUFFICIENT_DATA, never executes) | no | yes — no DB write anywhere in the module |
| → Decision Engine / Orchestrator | `AnalysisSnapshot` + `PortfolioContext` | `decision_engine.decide`, composed portfolio-wide by `trading_orchestrator.run_trading_orchestrator` | yes — produces a recommendation only | no | yes — `decision_engine.py` has no `sqlite3` import at all |
| → Agent Output | `OrchestratorResult` / `PresentationSummary` | `orchestrator_presentation.format_orchestrator_summary`, then the OpenWebUI `trading_sqlite` tool / Run-TradingOrchestrator.py | — (formatting only) | no | yes |

---

## 2. Tool / module inventory

### 2.1 Data ingestion & DB lifecycle

| Tool | Purpose | Writes to | External source |
|---|---|---|---|
| `Initialize-TradingDatabase.py` | Idempotent schema creation (`CREATE TABLE/INDEX IF NOT EXISTS`); safe on an existing DB | all base tables/views/triggers, `INSERT OR IGNORE` seed `metadata`/`data_sources` | none |
| `Reset-TradingDb.py` | **Destructive** full reset — deletes the DB file (+ `-wal`/`-shm`) and recreates it from scratch. Requires typing `RESET` at an interactive prompt. | same schema as Initialize, non-idempotent `CREATE` | none |
| `Backfill-TradingMarketData.py` | Daily OHLCV + current snapshot backfill, incremental (5-day overlap window) | `market_data`, `market_snapshot`, `source_symbols` (creates this table itself if missing), `data_sources` | Yahoo Finance chart + search API |
| `Backfill-TradingFXRatesECB.py` | ECB EUR reference-rate backfill, dry-run by default | `fx_rates` (only with `--write`) | ECB SDMX EXR API |
| `Backfill-TradingMarketSnapshotCurrency.py` | Deterministic fallback-fill of missing `market_snapshot.currency` from same-day `market_data`/`source_symbols`/`security` — never guesses | `market_snapshot.currency` (only `--write`, NULL rows only), makes a `.bak` file copy first | none (DB-internal only) |
| `Backfill-TradingFundamentalsSEC.py` | Bulk fundamentals backfill from SEC EDGAR XBRL CompanyFacts, with a 6h local JSON cache | `fundamentals` (DELETE-then-executemany-upsert per security) | SEC EDGAR XBRL API |
| `Backfill-TradingFundamentalsIR.py` | Hand-curated parsers for 6 companies (ASML, ING, TSMC, SK hynix, BAE Systems, HENSOLDT) from company IR sources | `fundamentals` | Company IR sites (XLSX/PDF), OpenDART (SK hynix); BAE Systems from a static local JSON snapshot |
| `Research-TradingFundamentals.py` | LLM-research workflow: builds a research prompt, structurally validates the LLM's JSON answer (never trusts it blindly), then optionally writes | `fundamentals` (only `--write`), plus prompt/audit files under `C:\KI-Stack\data\trading\fundamentals-audit\` | none directly — an external LLM does the actual research |
| `Discover-TradingSECIdentifiers.py` | Read-only: fuzzy-matches portfolio/watchlist stocks to SEC CIK identifiers | none (report only) | SEC company-ticker index + CompanyFacts API |
| `Resolve-TradingSecurities.py` | Resolves canonical symbol/exchange via OpenFIGI; also merges one hardcoded ING watchlist duplicate | `security` (symbol/exchange/asset_type), `watchlist` (duplicate merge) | OpenFIGI mapping API |
| `Canonicalize-TradingUniverse.py` | Force-applies a hardcoded 16-entry reference dict (symbol/ISIN/WKN/exchange/currency/country/asset_type) onto matching `security` rows | `security` (unconditional overwrite for matched names) | none |
| `Audit-TradingDataQuality.py` | Read-only fundamentals completeness/freshness audit, classifies each security `NOT_APPLICABLE`/`MISSING`/`STALE`/`PARTIAL`/`COMPLETE` | none | none |
| `Import-ParqetTransactions.py` | CLI wrapper around `parqet_import.py`; preview by default | `transactions`, `positions` (only `--write`; backs up DB first) | none — local Parqet CSV export |

### 2.2 Read-only analysis / decision pipeline

| Module | Purpose | DB read/write | Depends on |
|---|---|---|---|
| `analysis_contracts.py` | Shared enums/dataclasses (`Action`, `ActionQuantityBasis`, `AvailabilityStatus`, `StrategyType`, `DataQuality`, `AnalysisSnapshot`, `PortfolioContext`, `DecisionResult`, `to_primitive`) | none | — |
| `strategy_config.py` | All tunable trading parameters as frozen dataclasses (`StrategyConfig` and its sub-configs) — parameters only, no I/O | none | — |
| `fx_resolver.py` | ECB-convention (1 EUR = N quote currency), direct-pair-only FX resolution | reads `fx_rates` | `analysis_contracts`, `strategy_config` |
| `capital_state.py` | Read-only temporal resolution of the latest eligible `portfolio_capital_state` row | reads `portfolio_capital_state`, `metadata` | `analysis_contracts`, `strategy_config` |
| `swing_lifecycle.py` | Derives the currently-open campaign's TP1/TP2/add/reduction state from explicit lifecycle events; never infers from transactions | reads `swing_campaign`, `swing_campaign_event`, `metadata` | `analysis_contracts` |
| `analysis_engine.py` | Builds `AnalysisSnapshot` (technical + fundamental + valuation + event-risk + position) and `PortfolioContext` per security / portfolio-wide | reads `security`, `watchlist`, `positions`, `market_data`, `fundamentals`, `estimates`, `ratings`, `price_targets`, `events`, `news`, `strategy_assignment`, `market_snapshot` | `fx_resolver`, `capital_state`, `swing_lifecycle`, `portfolio_context`, `strategy_config`, `analysis_contracts` |
| `portfolio_context.py` | Pure allocation math: guardrail derivation, capital-use projection, sizing readiness — no DB access | none | `analysis_contracts`, `strategy_config` |
| `trading_analytics.py` | Technical scoring library (SMA/RSI/momentum/drawdown/volatility) and `rank_watchlist()` | reads `market_data`, `watchlist`, `security`, `data_sources` | — |
| `decision_engine.py` | Pure per-security recommendation (`BUY`/`ADD`/`HOLD`/`TRIM`/`SELL`/`WATCH`) from an `AnalysisSnapshot` + `PortfolioContext` | none (no `sqlite3` import at all) | `analysis_contracts`, `add_sizing`, `entry_sizing`, `strategy_config` |
| `entry_sizing.py` | Pure zero-position Swing entry eligibility/sizing | none | `analysis_contracts`, `strategy_config` |
| `add_sizing.py` | Pure open-campaign Swing ADD eligibility/sizing | none | `analysis_contracts`, `strategy_config` |
| `swing_promotion.py` | Evaluates/approves WATCH → Swing `strategy_assignment` promotion | reads `watchlist`, `strategy_assignment`, `swing_campaign`; writes `strategy_assignment`, `candidate_promotion` only via explicit `approve_swing_promotion` | `analysis_engine`, `analysis_contracts` |
| `candidate_decision.py` | Combines analytics + assignment + campaign + guardrails into `BUY`/`WATCH`/`DEFERRED`/`INSUFFICIENT_DATA` | reads (via reused functions) `watchlist`, `strategy_assignment`, `swing_campaign`, plus everything `build_portfolio_context` reads; writes nothing | `trading_analytics`, `swing_promotion`, `analysis_engine` |
| `strategy_suggestion.py` | Non-binding `swing`/`long_term`/`unknown` suggestion for unassigned WATCH entries | reads `watchlist`, `strategy_assignment`, `swing_campaign`, `security.asset_type`; writes nothing | `trading_analytics`, `swing_promotion` |
| `orchestrator_presentation.py` | Deterministic, filter/count/format-only presentation contract over an already-computed `OrchestratorResult` | none | `analysis_contracts` |
| `trading_orchestrator.py` | Composes the whole read-only pipeline into one portfolio-wide report (`OrchestratorResult`) | reads everything the above read; writes nothing | `analysis_engine`, `decision_engine`, `swing_promotion`, `orchestrator_presentation`, `portfolio_context` |
| `Run-TradingOrchestrator.py` | Thin read-only CLI launcher for `trading_orchestrator.run_trading_orchestrator` | read-only connection (`mode=ro`, `PRAGMA query_only=ON`) | `trading_orchestrator` |
| `parqet_import.py` | Canonical, split-aware transaction import + position rebuild | reads/writes `transactions`, `positions`; reads `corporate_action` (via `corporate_actions.py`) | `corporate_actions` |
| `openwebui_upload_resolver.py` | Security gate resolving an opaque OpenWebUI file UUID to a validated local CSV path (no local path / URL / filename accepted as input) | none directly (delegates to OpenWebUI's own `Files`/`Storage`/`has_access_to_file`) | none (stdlib only) |

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
| `Manage-TradingCapitalState.py` | `show`, `set`, `validate` | `capital_state.py` |
| `Manage-TradingCorporateActions.py` | `list`, `validate`, `add-split`, `rebuild-position` | `corporate_actions.py`, `parqet_import.rebuild_position` |

All four `Manage-*`/production DB tools default to `C:\KI-Stack\data\trading\trading.db`, overridable via `--db` (Manage-*) or `--db-path` (Migrate-*) — **the flag name is not consistent across the two categories**.

### 2.4 OpenWebUI integration

See section 11 below and [openwebui-trading-tool.md](openwebui-trading-tool.md).

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
| `strategy_suggestion.StrategySuggestion.suggested_strategy` | `swing`, `long_term`, `unknown` | `strategy_suggestion.py` |
| `swing_promotion.PromotionDecision.recommendation` | `PROMOTE`, `KEEP_WATCHING`, `REJECT`, `DATA_INSUFFICIENT` | `swing_promotion.py` |
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
- Decision order (see [trading-candidate-decision.md](trading-candidate-decision.md) for the full rationale): `analytics_quality != "OK"` → `INSUFFICIENT_DATA`; no assignment → `DEFERRED`; open campaign → `WATCH`; non-swing assignment → `WATCH`; allocation guardrails unavailable → `DEFERRED`; `SWING_ALLOCATION_ABOVE_MAX` → `DEFERRED`; score unavailable → `WATCH`; score `>= 20.0` → `BUY`; else `WATCH`.

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
| `SwingStrategyConfig` | `horizon_months_min` / `horizon_months_max` | 3 / 6 | **defined but not consumed by any module** (informational only) |
| | `tp1_gain_pct` / `tp2_gain_pct` | 0.20 / 0.25 | `decision_engine.py` (TP price levels) |
| | `tp1_sell_fraction` / `tp2_sell_fraction` | 0.25 / 0.50 | **defined but not read** — see §16 contradiction below |
| | `hard_stop_loss_pct` | 0.15 | `decision_engine.py` |
| | `remainder_management` | `"momentum_guided"` | defined but not consumed as a rule (informational) |
| | `entry_rule`/`add_rule`/`stop_rule`/`position_sizing_rule` | all `None` | not yet defined |
| `RiskTargetConfig` | `risk_target_min` / `risk_target_max` | 0.60 / 0.70 | **not consumed anywhere** — docstring: "Phase 2 intentionally assigns no mathematical meaning" |
| `DataQualityConfig` | `market_data_max_age_days` | 5 | `analysis_engine.py` (technical staleness gate) |
| `FXConfig` | `portfolio_base_currency` | `"EUR"` | `analysis_engine.build_portfolio_context` (raises if changed) |
| | `fx_max_age_days` | 5 | `fx_resolver.py` |
| | `preferred_fx_source` | `"ECB"` | `fx_resolver.py` |
| `CapitalStateConfig` | `freshness_max_age_days` | `None` (opt-in) | `capital_state.py`, exposed via `Manage-TradingCapitalState.py show --freshness-max-age-days` |

### 3.6 TP1/TP2/stop/runner quantity logic (`decision_engine.py`, independent of §3.5's sell-fraction fields — see §16)

- TP1 trim: sells `floor(original_quantity * 0.25)` → `ActionQuantityBasis.TP1_25_PERCENT_ORIGINAL`
- TP2 trim: sells down to `floor(original_quantity * 0.75)` cumulative from the original → `ActionQuantityBasis.TP2_75_PERCENT_CUMULATIVE_ORIGINAL`
- Hard stop: `stop_price = reference_cost * (1 - hard_stop_loss_pct)` (0.15) → full exit, `ActionQuantityBasis.STOP_FULL_EXIT`
- Runner (post-TP2 remainder): `ActionQuantityBasis.RUNNER_FULL_REMAINDER`, held/exited by momentum (SMA50/SMA200), not a fixed date
- ADD: `base = floor(original_quantity * max_add_pct_of_original)` (0.25) → `ActionQuantityBasis.ADD_25_PERCENT_ORIGINAL`, capped by `max_add_count = 1`
- `swing_lifecycle.py` independently computes `tp2_cumulative_target = floor(original_quantity * 0.75)` (same 75% literal) to detect `post_tp2_add_detected`

### 3.7 Portfolio Context / guardrails (`analysis_engine.py`, `portfolio_context.py`)

- `total_market_value`: sum of every open (`shares > 0`) position's market value, FX-normalized to EUR via `fx_resolver.py`. Withheld (`None`) entirely if even one holding can't be valued — **never computed from a partial denominator**.
- `cash_available`: a **separate** field from `capital_state.py` (`portfolio_capital_state` table) — never folded into `total_market_value`.
- `base_currency`: always `"EUR"` (`FXConfig.portfolio_base_currency`, hardcoded validation).
- Guardrail names (`derive_allocation_guardrails`, all against `PortfolioTargetConfig`): `SWING_ALLOCATION_ABOVE_MAX`, `SWING_ALLOCATION_BELOW_MIN`, `LONG_TERM_ALLOCATION_ABOVE_MAX`, `LONG_TERM_ALLOCATION_BELOW_MIN`, or `("WITHIN_TARGET_RANGE",)` when none apply. Purely informational — "never a trading action" (module docstring).
- Effect on Candidate Decision: only `SWING_ALLOCATION_ABOVE_MAX` blocks a `BUY` (→ `DEFERRED`); the others are informational (surfaced in `portfolio_constraints` but don't change `decision_status`).
- `trading_orchestrator.py` additionally raises a `NON_BLOCKING_WARNING` (`ALLOCATION_OUTSIDE_TARGET`) whenever `allocation_guardrails != ("WITHIN_TARGET_RANGE",)`.

**Note on the small (~0.22%) valuation-reconciliation gap against Parqet found in an earlier session**: accepted as due to differing query timestamps/sources; not re-investigated here per instruction.

### 3.8 Swing campaign rules (schema-enforced)

- One open campaign per security: `CREATE UNIQUE INDEX idx_swing_campaign_one_open_per_security ON swing_campaign(security_id) WHERE status = 'open'`.
- A campaign requires a matching `strategy_assignment` row with `strategy_type = 'swing'` for the same security (enforced by two `BEFORE INSERT`/`BEFORE UPDATE` triggers).
- A linked `swing_campaign_event.transaction_id` must belong to a transaction for the campaign's own security (enforced by two triggers).
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

---

## 4. Fundamentals / Data Quality

- **Verification status vocabulary** (`Research-TradingFundamentals.py`, the LLM-research path): `verified_direct` (primary source), `verified_derived` (computed from verified values via a documented formula), `verified_secondary` (≥2 independent secondary sources, or one strong one + plausibility check), `not_verified` (discarded, stored as NULL — never guessed).
- **Structural validation** (independent of the above, applied to every accepted period): `fiscal_year` in [2000, 2100]; `currency` a 3-char code; `revenue > 0`; `gross_profit <= revenue`; `total_assets ≈ total_liabilities + total_equity` within `max(1% of assets, 1,000,000)`; free-cash-flow check `|FCF - (OCF - capex)| <= max(1% of OCF, 1000)`.
- **`fundamentals` table** stores raw figures only (revenue, gross_profit, operating_income, ebit, ebitda, net_income, eps_basic/diluted, operating_cash_flow, capex, free_cash_flow, cash, total_debt, total_assets/liabilities/equity, shares_outstanding) — "no unnecessarily computed ratios" (schema comment).
- **Source priority is not a single global ranking** — it differs per pipeline: `Backfill-TradingFundamentalsSEC.py` uses SEC EDGAR XBRL exclusively; `Backfill-TradingFundamentalsIR.py` is a fixed 6-company hand-curated set from company IR sites; `Research-TradingFundamentals.py` (the canonical/general-purpose tool per the README) lets an LLM choose primary vs. corroborated-secondary sources under the `verified_*` vocabulary above, and stores the source as `"Company IR"` only if every field used is primary, else `"Web"`.
- **`asset_type` fundamentals exemption**: `{"etf", "fund"}` is treated as fundamentals-not-applicable both in `analysis_engine.py`'s per-security snapshot and in `Audit-TradingDataQuality.py`'s classification, and reused as `strategy_suggestion.LONG_TERM_ASSET_TYPES`.
- **`Audit-TradingDataQuality.py`** classifies each security `MISSING` (0 periods), `STALE` (has a data gap AND `age_days > --stale-days`, default 180), `PARTIAL` (has a gap, not stale), `COMPLETE` (none of `CORE_FIELDS = [revenue, net_income, operating_cash_flow, cash]` missing and FCF is derivable when OCF+capex are present), or `NOT_APPLICABLE` for ETF/fund asset types. Read-only; never writes.

---

## 5. OpenWebUI Tool `trading_sqlite`

See [openwebui-trading-tool.md](openwebui-trading-tool.md) for the deploy procedure (backup → copy runtime helpers → update `content` → regenerate `specs` via `get_tool_specs()` → verify no other tool row changed → targeted restart/reload → smoke test → DB-before/after fingerprint check).

**Current public method inventory** (version `1.4.0`, `openwebui-tools/trading_sqlite.py`), classified read-only vs. write-capable:

| Method | Read-only? | Notes |
|---|---|---|
| `sql_execute(sql)` | **No — unrestricted** | Tool description literally states "Direct unrestricted SQLite access"; opens a normal read-write connection (`isolation_level="DEFERRED"`); multi-statement, one transaction, rolls back the whole batch on any failure. `Valves`: `database_path` (default `C:\KI-Stack\data\trading\trading.db`), `timeout_seconds=1.0`, `busy_timeout_ms=1000`, `max_rows_per_statement=500`. |
| `database_tables()` | Yes | Lists tables |
| `database_schema()` | Yes | Full schema dump |
| `table_info(table)` | Yes | Per-table column info |
| `database_status()` | Yes | Summary counts/integrity |
| `rank_watchlist()` | Yes | Dynamically loads `trading_analytics.py` from `C:\KI-Stack\Tools\trading\` at call time |
| `run_trading_orchestrator(as_of=None)` | Yes | Dynamically loads `trading_orchestrator.py` (and its dependency chain) |
| `evaluate_swing_candidate(security_id)` / `evaluate_swing_candidates()` | Yes | Dynamically loads `swing_promotion.py` |
| `approve_swing_promotion(...)` | **No — writes** `strategy_assignment` + `candidate_promotion` | Requires a still-current plan token from `evaluate_swing_candidate` |
| `preview_parqet_import(uploaded_file_id)` | Yes | Read-only SQLite connection |
| `apply_parqet_import(uploaded_file_id, plan_token, include_historical=False)` | **No — writes** `transactions` + `positions` | Backs up the DB first; only accepts an OpenWebUI file UUID (never a path), resolved via `openwebui_upload_resolver.py` |
| `suggest_strategy_assignments(as_of=None)` | Yes | Dynamically loads `strategy_suggestion.py`; calls it unchanged (see §3.4) |
| `evaluate_watchlist_candidates(as_of=None)` | Yes | Dynamically loads `candidate_decision.py`; calls it unchanged (see §3.3) |

Both new methods follow the exact same pattern as `run_trading_orchestrator`/`evaluate_swing_candidates`: `_load_runtime_trading_module()` loads the deployed module from `C:\KI-Stack\Tools\trading\`, `_readonly_import_connection()` opens a strict `PRAGMA query_only=ON` connection. Deployed and smoke-tested 2026-09-25 (see [openwebui-trading-tool.md](openwebui-trading-tool.md) for the verified test record).

Runtime helper modules dynamically loaded from `C:\KI-Stack\Tools\trading\`: `trading_analytics.py`, `parqet_import.py`, `openwebui_upload_resolver.py`, `swing_lifecycle.py`, `analysis_contracts.py`, `trading_orchestrator.py`, `swing_promotion.py`, `strategy_suggestion.py`, `candidate_decision.py` (and their own import chains, e.g. `analysis_engine.py`, `decision_engine.py`, `portfolio_context.py`, `strategy_config.py`, `fx_resolver.py`, `capital_state.py`, `orchestrator_presentation.py`).

---

## 6. Production paths

| What | Path |
|---|---|
| Production trading DB | `C:\KI-Stack\data\trading\trading.db` |
| Production trading tools | `C:\KI-Stack\tools\trading\` (also referenced as `C:\KI-Stack\Tools\trading\` by the OpenWebUI tool's dynamic loader — Windows paths are case-insensitive, same directory) |
| Production OpenWebUI DB | `C:\KI-Stack\OpenWebUI\data\webui.db` |
| SEC fundamentals cache | `C:\KI-Stack\data\trading\sec-cache` |
| Fundamentals research prompts/audit | `C:\KI-Stack\data\trading\fundamentals-audit\` (prompts in its `prompts\` subfolder) |
| Dev repository (this repo) | `C:\Tading-Agent-Dev` |

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
| `positions` | Current portfolio state, exactly one row per security, rebuilt (not incrementally patched) by `parqet_import._rebuild_position` | PK `security_id` |
| `watchlist` | Swing-candidate research list, exactly one row per security | PK `security_id` |
| `market_snapshot` | Latest known price/quote per security | PK `security_id` |
| `market_data` | Daily OHLCV history, main technical-analysis input | unique `(security_id, trade_date, source_id)` |
| `fundamentals` | Raw annual/quarterly/semiannual/nine-month fundamentals | unique `(security_id, period_end, period_type, source_id)` |
| `estimates`, `ratings`, `price_targets`, `events`, `news` | Row-count-only diagnostic inputs to `ValuationAnalysis`/`EventRiskAnalysis` quality in `analysis_engine.py` — **schema exists and is read, but no current script populates any of these five tables** (verified: no `INSERT` anywhere in `tools/trading/`) | — |
| `decisions`, `analysis_history` | Legacy schema from an earlier scoring-model design — **verified unused**: no read or write anywhere in the current codebase | — |
| `strategy_assignment` | Time-interval strategy per security (`long_term`/`swing`/`tactical`/`unknown`) | no overlapping intervals per security (trigger-enforced) |
| `candidate_promotion` | Audit trail of promotion evaluations/approvals | — |
| `fx_rates` | ECB-convention daily FX rates, EUR-based only | unique `(rate_date, base_currency, quote_currency, source)`; `base_currency` must be `'EUR'` |
| `swing_campaign` | Explicit Swing campaign identity (one open per security) | partial unique index on `status='open'`; FK to a `swing` `strategy_assignment` |
| `swing_campaign_event` | Manually recorded lifecycle events (never inferred) | `event_type` CHECK list; linked transaction must match the campaign's security |
| `portfolio_capital_state` | Recorded cash/buying-power snapshots (`Migrate-TradingCapitalState.py`) | `base_currency` must be a 3-letter code |
| `corporate_action` | Auditable stock splits (`Migrate-TradingCorporateActions.py`); original transactions are never rewritten — this table is *consulted*, not applied destructively | `action_type` currently `'STOCK_SPLIT'` only; unique `(security_id, action_type, effective_date, ratio_numerator, ratio_denominator)` |

**Views** (simple SQL-only helpers, distinct from and simpler than the authoritative FX-aware `analysis_engine.build_portfolio_context` path): `v_active_positions`, `v_watchlist`, `v_portfolio_market` (same-currency valuation only — returns `NULL` market value on any currency mismatch, unlike the FX-normalizing engine).

**Current production state (verified 2026-09-25)**: all listed feature schemas are applied (`schema_version=2.0`; every feature-version marker `=1`, including `corporate_action_schema_version`). The SPXS 100:1 stock split (`corporate_action` id 1, effective 2025-12-15) has been applied and the `positions` row for SPXS reflects the split-corrected quantity (22,545.478 shares). `candidate_promotion` currently has 0 rows (schema present, nothing promoted through it yet). `estimates`/`ratings`/`price_targets`/`events`/`news`/`decisions`/`analysis_history` are all empty (0 rows) in production, consistent with §7's "unused/unpopulated" note above.

---

## 8. Tests / current quality status

- Candidate Decision: `tests/trading/test_candidate_decision.py` — 8 tests (good swing candidate → BUY, insufficient analytics data → INSUFFICIENT_DATA, open campaign → WATCH, missing assignment → DEFERRED, guardrail above max → DEFERRED, non-swing assignment → WATCH, score below threshold → WATCH, unavailable allocation → DEFERRED).
- Strategy Suggestion: `tests/trading/test_strategy_suggestion.py` — 7 tests (active assignment skipped, high swing suitability → swing, insufficient data → unknown, open campaign → swing, no long-term inference from momentum alone, ETF asset_type → long_term, score below threshold → unknown).
- Corporate Actions: `tests/trading/test_corporate_actions.py` — 20 tests (split-factor temporal semantics, schema availability, validation/idempotency, position-rebuild integration).
- Broader coverage exists for: Parqet import (`test_parqet_import.py`), portfolio context (`test_portfolio_context.py`), decision engine (`test_decision_engine.py`), entry/ADD recommendations (`test_swing_entry_recommendation.py`, `test_swing_add_recommendation.py`), Swing TP/stop/runner actions (`test_swing_tp_actions.py`, `test_swing_hard_stop.py`, `test_runner_trend_exit.py`), Swing promotion (`test_swing_promotion.py`, `test_openwebui_swing_promotion.py`), campaign lifecycle (`test_swing_campaign_lifecycle.py`), strategy assignment (`test_strategy_assignment.py`, `test_manage_strategy_assignments.py`), capital state (`test_capital_state.py`), FX/currency (`test_fx_currency.py`, `test_market_snapshot_currency_backfill.py`), analysis contracts/engine (`test_analysis_contracts.py`, `test_analysis_engine.py`), trading analytics (`test_trading_analytics.py`), orchestrator + presentation (`test_trading_orchestrator.py`, `test_orchestrator_presentation.py`), OpenWebUI Parqet upload gate (`test_openwebui_parqet_upload.py`).
- **Last full run: 218/218 passed, 0 failures** (`python -m unittest discover -s tests/trading -p "test_*.py"`, via `C:/KI-Stack/python/venvs/openwebui/Scripts/python.exe`). This number is not re-derived elsewhere in this document — if a later run produces a different count, that later number is authoritative, not this one.

---

## 9. Known inconsistencies (found while writing this document)

1. **TP1/TP2 sell-fraction duplication**: `strategy_config.SwingStrategyConfig.tp1_sell_fraction` (0.25) and `tp2_sell_fraction` (0.50) are defined but **never read** by `decision_engine.py`. Instead, `decision_engine.py` and `swing_lifecycle.py` each independently hardcode the equivalent literals (`floor(original_quantity * 0.25)` for TP1, `floor(original_quantity * 0.75)` cumulative for TP2). The values currently agree (0.25 and 0.25+0.50=0.75), but changing the config fields today would silently do nothing — a future maintainer could reasonably expect them to be load-bearing.
2. **`RiskTargetConfig` (risk_target_min/max = 0.60/0.70) is defined but consumed nowhere** — its own docstring already says "Phase 2 intentionally assigns no mathematical meaning," so this is documented-as-intentional, not a bug, but worth knowing before building anything against it.
3. **`SwingStrategyConfig.horizon_months_min/max` (3/6) and `remainder_management` ("momentum_guided") are informational only** — no module enforces a time-based exit or a concrete "momentum-guided" rule; the runner is currently held/exited purely by the SMA50/SMA200 checks in `decision_engine._runner_decision`.
4. **`estimates`, `ratings`, `price_targets`, `events`, `news` tables are read (as row counts) but never populated** by any script in this repository — `ValuationAnalysis`/`EventRiskAnalysis` quality will always reflect zero rows in the current setup.
5. **`decisions` and `analysis_history` tables are pure dead schema** — created by both `Initialize-TradingDatabase.py` and `Reset-TradingDb.py`, never read or written anywhere else.
6. **CLI flag-name inconsistency**: `Manage-*` scripts use `--db`; `Migrate-*` scripts use `--db-path`. Both default to the same production path.
7. **`Migrate-TradingStrategyAssignments.py` always opens a read-write connection** (unlike its sibling Migrate-* scripts, which open a `mode=ro` URI for the dry-run path) — it relies solely on `main()`'s branching to avoid writing without `--write`. Functionally safe today, but structurally the odd one out.
8. ~~`candidate_decision.py` and `strategy_suggestion.py` are not yet wired into the OpenWebUI `trading_sqlite` tool~~ — **resolved 2026-09-25**: both are now exposed as `evaluate_watchlist_candidates()` / `suggest_strategy_assignments()` (tool version `1.4.0`, see §5).
9. **No path/name/value contradiction was found** between this document and the current code for any of the constants tabulated in §3 — each was read directly from the source file listed next to it, not carried over from an earlier planning state or a prior conversation.

No other stale paths, contradictory thresholds, or duplicated/overlapping documentation files were found in `docs/` at the time of writing.
