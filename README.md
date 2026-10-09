# Trading Agent

Standalone trading agent / trading data store.

Version: v0.2.0 — see [CHANGELOG.md](CHANGELOG.md).

## Runtime

Production database:

```
C:\tools\trading\data\trading.db
```

Production runtime: `C:\tools\trading` (`app\` domain modules/scripts, `mcp\` MCP adapter, `data\`, `logs\`).
The agent is MCP-based and not tied to a particular front end (any MCP-capable LLM client can use it).
Versioned in this repository: `mcp-tools\` (`server.py`, `trading_sqlite.py`, `smoke_test.py`) and
`scheduler\Manage-TradingTasks.ps1` (management of the scheduled tasks).

Access path: MCP client (LLM front end) → Trading MCP server (`C:\tools\trading\mcp`, repo source `mcp-tools\`, 15 tools)
→ runtime (`C:\tools\trading\app`) → SQLite (`C:\tools\trading\data\trading.db`).

This repository contains code only. The database, backups, audit/candidate JSON files,
PDFs and logs live outside the repository (see `.gitignore`).

## Python dependencies

Python 3.12. The repository has no packaging metadata; `requirements.txt` lists the direct runtime dependencies at their tested versions.

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The tests use only the standard library's `unittest` (plus the runtime packages above):
`.venv\Scripts\python.exe -m unittest discover -s tests/trading -p "test_*.py"`.

## Documentation

The technical documentation exists as a complete pair in German and English (same structure, same technical terms).
This README, the changelog, the contributing guide and the security policy are English only.

| Topic | English | Deutsch |
|---|---|---|
| Queries and examples (15 MCP tools) | [trading-agent-query-examples.en.md](docs/trading-agent-query-examples.en.md) | [trading-agent-query-examples.de.md](docs/trading-agent-query-examples.de.md) |
| Architecture reference | [trading-agent-architecture.en.md](docs/trading-agent-architecture.en.md) | [trading-agent-architecture.de.md](docs/trading-agent-architecture.de.md) |
| Watchlist candidate decision status | [trading-candidate-decision.en.md](docs/trading-candidate-decision.en.md) | [trading-candidate-decision.de.md](docs/trading-candidate-decision.de.md) |
| Watchlist strategy suggestion | [trading-strategy-suggestion.en.md](docs/trading-strategy-suggestion.en.md) | [trading-strategy-suggestion.de.md](docs/trading-strategy-suggestion.de.md) |
| Watchlist-to-Swing promotion | [trading-swing-promotion.en.md](docs/trading-swing-promotion.en.md) | [trading-swing-promotion.de.md](docs/trading-swing-promotion.de.md) |
| Swing entry recommendations | [trading-entry-recommendations.en.md](docs/trading-entry-recommendations.en.md) | [trading-entry-recommendations.de.md](docs/trading-entry-recommendations.de.md) |
| Transaction import (canonical CSV, Parqet) | [trading-import.en.md](docs/trading-import.en.md) | [trading-import.de.md](docs/trading-import.de.md) |

## Deploy

Production (`-Root`, default `C:\tools\trading`) is only updated through the versioned script `deploy\Deploy-TradingAgent.ps1`.
It copies exclusively the files listed in `deploy\deploy-manifest.json` (an explicit list, no wildcard copying; a new runtime file
has to be added there, a test enforces this). Tests, docs, `.git`, database, logs and data are never touched.

```powershell
powershell -File deploy\Deploy-TradingAgent.ps1 -Action Check      # read only: paths, manifest, syntax, DB integrity_check, plan
powershell -File deploy\Deploy-TradingAgent.ps1 -Action Deploy     # backup, copy, hash comparison, py_compile, integrity_check, MCP smoke test (-DryRun: plan only)
powershell -File deploy\Deploy-TradingAgent.ps1 -Action Rollback -BackupPath C:\tools\trading\backup\deploy-<timestamp>
```

- **Backups:** replaced files go to `backup\deploy-<timestamp>\` (with `backup-manifest.json`); the database copy is made through the SQLite backup API to `data\trading.db.bak-deploy-<timestamp>`.
- **Rollback:** copies back only the files saved there; the database is restored only with an explicit `-RestoreDb` (a safety copy `trading.db.bak-pre-rollback-<timestamp>` is made first).
- **After every deploy** restart running MCP processes or the front end (Python modules are cached per process). The scheduler is not re-registered, no collector is started, and there is no schema migration.
- **Private:** `app\universe\sector_map_local.json` (git-ignored, not in the manifest) is never touched; if it is missing, only a warning is printed.

## Architecture

```
Market/Fundamental Data -> Analytics -> Strategy Suggestion -> Strategy Assignment
  -> Portfolio Context/Guardrails -> Swing Campaign State -> Candidate Decision
  -> Decision Engine/Orchestrator -> Agent Output
```

Complete, code-verified reference of all tools/modules, fixed values/thresholds,
the decision flow and the DB schema:
[docs/trading-agent-architecture.en.md](docs/trading-agent-architecture.en.md) (German: [docs/trading-agent-architecture.de.md](docs/trading-agent-architecture.de.md)).

## Main scripts (`tools/trading/`)

Excerpt of the most important entry points — the complete list of all ~40 modules with purpose,
inputs/outputs, DB access and dependencies:
[docs/trading-agent-architecture.en.md](docs/trading-agent-architecture.en.md#2-tool--module-inventory).

| Script | Purpose |
|---|---|
| `Initialize-TradingDatabase.py` | Initialize the DB (tables, indexes, views, base `data_sources`) |
| `Reset-TradingDb.py --db-path <path>` (path required, no default) | Fully reset the DB (deletes the existing DB file) |
| `Backfill-TradingMarketData.py` | Market data backfill (OHLCV) — runs automatically every day at 17:15 via the Windows task `Trading-MarketData-Backfill` (log: `C:\tools\trading\logs\market-data-backfill.log`) |
| `Backfill-TradingFXRatesECB.py` | ECB EUR reference-rate backfill for USD, GBP, AUD and KRW in one run (`--quote-currency`, comma separated, default = these four; GBp pence quotes are normalized to GBP in the resolver) — runs automatically via the Windows task `Trading-FXRates-Backfill` (logon + daily 17:20, `StartWhenAvailable=True`; log: `C:\tools\trading\logs\fx-rates-backfill.log`) |
| `Discover-TradingSECIdentifiers.py` | SEC CIK identifier discovery |
| `Resolve-TradingSecurities.py` | Security master data resolution (OpenFIGI) |
| `Backfill-TradingFundamentalsSEC.py` | Bulk fundamentals import from SEC EDGAR XBRL — **weekly automated** via the Windows task `Trading-Fundamentals-SEC` (logon + Sunday 10:00, `StartWhenAvailable=True`; log: `C:\tools\trading\logs\fundamentals-sec-backfill.log`) |
| `Backfill-TradingFundamentalsIR.py` | Fundamentals from company IR reference parsers — **manual/event-driven** (hardcoded quarterly URLs per company, no automation) |
| `Research-TradingFundamentals.py` | LLM research fundamentals pipeline (canonical tool) — **manual/LLM-assisted** (requires an active Claude session, cannot be automated) |
| `Backfill-TradingEventsNews.py` | `events` (SEC EDGAR 8-K/6-K, 2-year lookback) + `news` (Yahoo Finance search) backfill — runs daily and automatically via the Windows task `Trading-EventsNews-Backfill` (logon + daily 17:25, `StartWhenAvailable=True`; log: `C:\tools\trading\logs\events-news-backfill.log`). This resolves inconsistency #4 part A (events/news); `estimates`/`ratings`/`price_targets` deliberately remain open (no free data path) |
| `Import-TradingTransactions.py` | Transaction import from the canonical Trading CSV (`--format canonical`) or a Parqet export (`--format parqet`): `transactions`, `positions`, strategy assignment and Swing-campaign initialization; the default is a complete dry run, `--write` makes a backup first. See the section "Transaction import and Swing campaigns" |
| `Import-ParqetTransactions.py` | Existing entry point; same as `Import-TradingTransactions.py --format parqet` |
| `transaction_import.py` / `parqet_import.py` | Provider-neutral import engine / Parqet source adapter |
| `trading_analytics.py` | Technical scoring library (SMA/RSI/momentum/drawdown/volatility), `rank_watchlist()` |
| `trading_orchestrator.py` | Read-only portfolio-wide report (composes analytics/decision/promotion) |
| `candidate_discovery.py` / `Discover-TradingCandidates.py` | Read-only watchlist candidate discovery over a controlled universe (`universe/swing_large_cap_v1.json`); not part of the orchestrator, writes nothing to the database; runs daily and automatically via the Windows task `Trading-Candidate-Discovery` (17:35, `StartWhenAvailable=True`, no logon trigger, 2 h time limit; log: `C:\tools\trading\logs\candidate-discovery.log`). See the section "Watchlist candidate discovery" |
| `market_intelligence.py` / `intelligence_report.py` / `Collect-TradingMarketIntelligence.py` | Read-only market intelligence: economic/market news from official sources, SEC and Yahoo headlines, rule-based classification and mapping to portfolio/watchlist/discovery; the CLI run (daily and automatic via the Windows task `Trading-Market-Intelligence`, 08:15 and 17:45, `StartWhenAvailable=True`, no logon trigger, 2 h time limit; log: `C:\tools\trading\logs\market-intelligence.log`) writes a report that the MCP reader only reads. Not part of the orchestrator, not a signal. See the section "Market intelligence" |
| `opportunity_view.py` | Read-only opportunity view: joins engine/planner candidates, the last discovery report and the last market-intelligence report (news only as context, no new score); read through the MCP method `get_opportunity_view`. See the section "Unified opportunity view" |
| `portfolio_action_planner.py` | Deterministic capital/entry plan (`portfolio_action_plan`, simulation without orders); called by the orchestrator at the end. See the section "Portfolio action planner" |

## Transaction import and Swing campaigns

Transactions are imported from a documented canonical CSV (any source, no Parqet needed) or from a Parqet export; both end in the same
provider-neutral pipeline: validation, security resolution, transactions, positions, strategy assignment, Swing campaign, reconciliation.
The example [examples/trading-import-example.csv](examples/trading-import-example.csv) is synthetic. Full reference:
[docs/trading-import.en.md](docs/trading-import.en.md) (German: [docs/trading-import.de.md](docs/trading-import.de.md)).

```
python tools\trading\Import-TradingTransactions.py --format canonical --csv examples\trading-import-example.csv --db-path <db> --create-securities --strategy EXA=swing
```

The import reconciles unambiguously attributable trades **after** `opened_at` of an open Swing campaign
in the same DB transaction with transaction-linked events: SELL → `manual_reduction`, BUY → `add`
(quantity/price exactly from the transaction). Trades **before** `opened_at` belong to the baseline and are ignored.
A trade on the same calendar day as `opened_at` is only reconciled if it is the only unlinked
same-day trade and the quantity history places it unambiguously after the baseline (e.g. 120 − 30 = 90);
otherwise `MANUAL_REVIEW_REQUIRED`. TP1/TP2/stop reasons are never derived, `original_quantity` stays unchanged, campaigns are not
closed automatically. Idempotent through the unique `transaction_id` link. Ambiguous or non-matching
cases (several open campaigns, transfers, overselling, split) write nothing and are reported
as `AMBIGUOUS` / `MANUAL_REVIEW_REQUIRED`. For already imported trades:
`Import-TradingTransactions.py --reconcile-campaigns [--write]`.

## Portfolio action planner

`tools/trading/portfolio_action_planner.py` produces the `portfolio_action_plan` — purely deterministic,
without LLM ranking, without orders, without DB writes and without automatic strategy changes. The orchestrator
returns it as part of `run_trading_orchestrator` (no new MCP method).

**PROMOTE is not a BUY.** PROMOTE is only the input of the entry evaluation:

```
PROMOTE  → ENTRY-EVALUATION → RANKING → POSITION SIZING → PLANNED ENTRY
SELL/TRIM → POST-ACTION CAPITAL → ENTRY PLAN
```

- **Post-action capital:** proceeds per SELL/TRIM (quantity × current EUR price) minus an **estimated tax**
  → cash, Swing/long-term share and free Swing capacity after the actions. The tax (25 % capital gains tax + 5.5 % solidarity surcharge =
  26.375 %, church tax 0 %) applies only to the realized gain, never to the gross proceeds; a loss yields
  no negative tax and no additional cash. The basis is `positions.avg_cost` (average cost,
  **not FIFO**; `tax_estimate_quality = estimated_from_average_cost`). Planning uses the **conservative** view
  (no loss offsetting); the view with in-plan loss offsetting is only shown additionally.
  Loss pot, tax allowance and results already realized this year are unknown and are not invented.
- **Entry status** (PROMOTE candidates only): `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `BLOCKED_BY_ALLOCATION`,
  `BLOCKED_BY_DATA`, `BLOCKED_BY_CONCENTRATION`, `BLOCKED_EXISTING_POSITION`, `BLOCKED_EXISTING_CAMPAIGN`.
- **Ranking** for `ENTRY_READY` only: `entry_score = momentum_score × confidence` (existing analytics score ×
  existing data-quality value of the decision engine); ties: higher watchlist priority, then lower `security_id`.
- **Sizing/allocation:** existing entry logic (Swing maximum 40 %, cash reserve, maximum position weight, whole
  shares rounded down), sequentially by rank; stops at the Swing maximum, the cash reserve or exhausted capital.
- **Not modelled:** fees, FIFO lots, loss pot/allowance, sector concentration (no sector data for held positions — only
  the single-position limits apply).

## Watchlist candidate discovery

`DISCOVERY ≠ PROMOTE ≠ ENTRY_READY ≠ ORDER`: The discovery only **suggests** new symbols. It creates no security, no
watchlist entry, no strategy assignment, no campaign, no BUY and no entry plan; version 1 is read-only.

```
python tools\trading\Discover-TradingCandidates.py [--limit N] [--top N] [--json] [--output FILE] [--cache-dir DIR]
```

- **Universe:** versioned, static list of liquid large caps (`tools/trading/universe/swing_large_cap_v1.json`, not an
  official index composition); Yahoo symbols as in the existing market-data pipeline. No free web/LLM search.
- **Pre-filter** before the first request: already held / on the watchlist (Yahoo symbol), not an equity type, quote currency cannot be
  valued (existing FX resolver). Afterwards: minimum price 5 EUR, 60-day median traded value ≥ 5 M EUR (`DiscoveryConfig`,
  proposal), history/freshness according to the existing technical rule.
- **Analysis:** unchanged engine (`build_analysis_snapshot`, `evaluate_swing_promotion`) on a volatile in-memory copy
  of the production schema; no second rating logic. Fundamentals, valuation and events are **not** fetched for new symbols.
- **Score:** `discovery_score = momentum_score × confidence` (like the planner's `entry_score`), ties: higher momentum score,
  then symbol. Status: `DISCOVERY_READY` (trend intact and momentum ≥ 20), `DISCOVERY_WATCH`, `DISCOVERY_DATA_INSUFFICIENT`, `DISCOVERY_REJECTED`.
- **Flow for the MCP client:** `universe → scan (CLI) → report → MCP reader → MCP client`. The long run (several minutes, Yahoo requests) stays
  the explicit script; after a successful, complete run it writes a structured report to
  `C:\tools\trading\data\candidate-discovery\` (`latest.json`, replaced atomically, plus optionally `candidate-discovery-YYYYMMDD-HHMMSS.json`;
  `--output-dir`, `--no-history`). A failed run leaves the last valid report untouched.
- **MCP:** `get_candidate_discovery(status, limit, detail, symbol)` reads **only** the last finished report — **no** discovery run,
  no Yahoo call, no DB or watchlist access. Without a report: `UNAVAILABLE` / `NO_DISCOVERY_REPORT`. An old report is not
  hidden but shown with its age and `REPORT_STALE` (limit: `market_data_max_age_days`). The MCP client therefore reads the last finished
  report, the discovery stays read-only and there is no automatic watchlist addition. In the reference setup the report is produced daily at 17:35
  by the Windows task `Trading-Candidate-Discovery` (not by the MCP tool).

## Market intelligence

`NEWS ≠ SIGNAL ≠ BUY ≠ SELL`: Market intelligence **only classifies** news (category, importance, affected symbols). It
changes no signal, no recommendation, no ranking, no sizing and creates no orders. Version 1 is read-only and deterministic;
no LLM decides anything or produces news.

```
python tools\trading\Collect-TradingMarketIntelligence.py [--top N] [--json] [--output-dir DIR] [--no-history] [--discovery-news-limit N]
```

- **Flow for the MCP client:** `sources → collect CLI → report → MCP reader → MCP client`. Fetching the sources is the explicit script; after
  a successful run it writes `C:\tools\trading\data\market-intelligence\latest.json` (replaced atomically: temp file → re-read/validate
  → `os.replace`; plus optionally `market-intelligence-YYYYMMDD-HHMMSS.json`). A failed run (not a single source answers)
  leaves the last valid report untouched; individual failed feeds appear as the warning `FEED_FAILED:<feed>`.
- **Sources:** official feeds without an API key — ECB (press releases, statistics), Fed (monetary policy, press releases), BLS (CPI,
  employment), BEA, Destatis, EIA (Today in Energy, press releases); SEC 8-K events and cached Yahoo headlines from the
  database (read only); fresh Yahoo headlines for `DISCOVERY_READY` candidates of the discovery report. Eurostat is not
  connected in v1; `COMMODITIES` is not implemented.
- **Categories:** MACRO, MONETARY_POLICY, INFLATION, LABOR_MARKET, ECONOMIC_GROWTH, GEOPOLITICS, ENERGY, SECTOR, COMPANY, EARNINGS,
  GUIDANCE, REGULATORY, M_AND_A. Fixed rule tables per source and headline keywords; `event_id` is a stable hash
  of source type and canonical URL/accession (duplicates are merged).
- **Importance (HIGH/MEDIUM/LOW):** fixed table. HIGH only for primary sources (e.g. ECB/Fed rate decision, mandatory SEC filings such as
  acquisition/delisting), company press-release wires, or keyword classes whose headline names the linked company
  **by name** (a bare ticker does not count). Secondary sources with plain keywords are MEDIUM at most; stock-pick articles
  are LOW; scheduled macro releases are MEDIUM (whether there is a surprise cannot be checked without consensus data);
  GEOPOLITICS is MEDIUM at most.
- **Impact:** `UNKNOWN` as long as no reliable rule applies. A direction is never read from a headline; only the structure of a
  primary filing (delisting notice, cybersecurity incident 1.05, non-reliance 4.02) yields `NEGATIVE`. Every event states its `impact_basis`.
- **Mapping:** `affected_portfolio_symbols`, `affected_watchlist_symbols`, `affected_discovery_symbols`; sector events (explicit
  keywords, EIA) reach the symbols of that sector through the static mapping `universe/sector_map_v1.json`; macro events carry
  `portfolio_region_exposure` (region per Yahoo suffix of the held symbols).
- **MCP:** `get_market_intelligence(scope, category, importance, symbol, sector, limit, detail)` reads **only** the last finished report —
  no web fetch, no scan, no DB access. `scope`: ALL, PORTFOLIO, WATCHLIST, DISCOVERY, MACRO. Without a report: `UNAVAILABLE` /
  `NO_MARKET_INTELLIGENCE_REPORT`; an old report is shown with its age and `REPORT_STALE` (limit 24 h, `MarketIntelligenceConfig`).
  The compact answer (at most 50 events) contains only short freshness, the active filter, the counters per importance of all matches and the
  events themselves (time, importance, category, headline, source, impact, up to 3 reason codes; with `symbol=...` additionally `link`
  DIRECT/SHARED/SECTOR/LOOSE, otherwise the affected symbols/sectors/regions). No `rendered_de`, no source statistics, no run log,
  no methodology. `detail=true` (at most 25) returns everything. The answer stays below the MCP limit of 40,000 characters (otherwise
  `RESPONSE_TRIMMED_TO_SIZE_BUDGET`). In the reference setup the report is produced daily at 08:15 and 17:45 by the Windows task `Trading-Market-Intelligence` (not by the MCP tool).
- **Known limits:** Yahoo sometimes attaches headlines to symbols loosely (ticker homonyms, market wrap-ups); keyword classification has
  `confidence: LOW`; impact is mostly `UNKNOWN`.

## Unified opportunity view

`DISCOVERY + MARKET INTELLIGENCE + PORTFOLIO/PLANNER CONTEXT → one opportunity output`. The view **only joins** what already
exists; it computes no score, no ranking, no position size and no recommendation and creates no order. News stays context
and warning: it changes neither `discovery_score` nor PROMOTE/ENTRY_READY, rank, sizing or SELL/TRIM/HOLD. There is **no**
combined score (no “score + news bonus/malus”).

- **MCP:** `get_opportunity_view(source, status, news_status, symbol, limit, detail)` (tool no. 15). It only reads: the orchestrator on the
  usual read-only connection, the last discovery report and the last market-intelligence report. No web access, no
  discovery/collector run, no DB writes, no new table, no new persistence. If a report is missing, the result is `PARTIAL`
  (the rest is shown, `market_context` or the discovery part `UNAVAILABLE`); `UNAVAILABLE` only if nothing at all is present. Both reports are shown openly
  with state, age and `stale` (discovery: `market_data_max_age_days`, market intelligence: 24 h).
- **Compact result (default):** only what was asked — status and freshness of both reports, a minimal capital frame (cash, cash after the
  planned sales, conservative net proceeds, planned entries, remaining buying capacity, Swing share and corridor), the matching
  `opportunities` (entry status/rank/score, momentum, plan status with planned capital, capital proposed by the engine's sizing
  `proposed_capital_eur`/`proposed_quantity`, stop reason such as `STOPPED_CAPITAL_EXHAUSTED`, news status with counts and the most important event) and
  `warnings` as codes. With `symbol=...` only these symbols (and, if applicable, the held positions of that name) are returned. No position list, no
  discovery list, no macro block, no global high-attention list, no `rendered_de`. Empty fields are omitted.
- **Result with `detail=true`:** additionally `metadata` fields, `portfolio_context` (with held positions), `watchlist_candidates`,
  `discovery_candidates`, `market_context` (macro, high attention), methodology and `rendered_de` (ready-made tables).
- **Candidates:** A) watchlist/PROMOTE candidates of the planner (all `entry_status`), B) `DISCOVERY_READY` from the discovery report. `source`
  = `WATCHLIST`, `DISCOVERY` or `BOTH` (symbol in both). No automatic watchlist addition, no strategy assignment.
- **Order:** ENTRY_READY → WAIT_FOR_TRIGGER → DISCOVERY_READY → other; within the group the engine rank (then `entry_score`,
  discovery rank, watchlist priority). The existing scores (`discovery_score`, `entry_score`, `momentum_score`, `confidence`) stay visible separately.
- **News context per candidate and held position:** `NEWS_HIGH_ATTENTION` (at least one *directly* symbol-related HIGH event: SEC filing or
  a headline that names the company, also “(TICKER)”), `NEWS_ATTENTION` (a direct MEDIUM event or a HIGH event matched only through the sector or
  loosely), `NEWS_CLEAR` (only LOW / loose MEDIUM / no item), `NEWS_UNAVAILABLE` (no valid MI report). With counts
  (all / direct), the most important events (category, impact, `link` DIRECT/SECTOR/LOOSE, reason codes) and the justification `status_reasons`.
- **Capital:** for candidates without a planner entry (discovery) only an informative comparison `capital_fit` (price of one share against free
  capital after the planned sales or after the planned entries) — no quantity, no order.
- **Typical questions to the assistant:** “best new opportunities” → `source=DISCOVERY`; “discovery with relevant news” → `source=DISCOVERY, news_status=RELEVANT`;
  “ENTRY_READY with high-impact news” → `status=ENTRY_READY, news_status=NEWS_HIGH_ATTENTION`; “technically strong and no news concerns” →
  `news_status=NEWS_CLEAR`; “NVDA ahead of AAPL?” → `symbol=NVDA,AAPL` (rank, score, plan status, delay reason); “news on AAPL, NVDA, GOOGL” →
  `symbol=AAPL,NVDA,GOOGL`; “does it fit into the free capital” → `source=DISCOVERY` (`capital_fit`, section Capital); “discovery READY without HIGH news” →
  `source=DISCOVERY, status=DISCOVERY_READY, news_status=NOT_HIGH`. Default `limit` 30 (at most 40 compact, 10 detail): a status filter such as ENTRY_READY returns all matches in one call. The answer stays below
  34,000 characters (otherwise `RESPONSE_TRIMMED_TO_SIZE_BUDGET`; in detail mode first secondary sections, then the last rows).
- **Limits:** A company is recognised only by its name or a parenthesised ticker (a short form without a name part does not count
  as direct); Yahoo sometimes attaches headlines loosely; many `NEWS_ATTENTION` rows are analyst price targets (COMPANY/MEDIUM).

## Watchlist candidate status

`tools/trading/candidate_decision.py::evaluate_watchlist_candidates()` is read-only and
condenses the analytics score (`trading_analytics.rank_watchlist`), the active strategy assignment,
the open Swing campaign status and the portfolio allocation guardrails into a structured
`BUY` / `WATCH` / `DEFERRED` / `INSUFFICIENT_DATA` per watchlist entry. No orders, no
DB writes, no automatic campaign opening. Also available through the Trading MCP server (MCP client)
(`evaluate_watchlist_candidates()`). For details and the decision order see
[docs/trading-candidate-decision.en.md](docs/trading-candidate-decision.en.md) (German: [docs/trading-candidate-decision.de.md](docs/trading-candidate-decision.de.md)).

## Watchlist strategy suggestion

`tools/trading/strategy_suggestion.py::suggest_strategy_assignments()` is read-only and
suggests `swing` / `long_term` /
`unknown` for watchlist entries **without an active** `strategy_assignment`. A suggestion is **not** an assignment — it writes nothing and does not replace
the existing `swing_promotion.approve_swing_promotion` path. `long_term` is never derived solely
from the momentum score (the only long-term basis at present: `asset_type` ∈
`{etf, fund}`). Also available through the Trading MCP server (MCP client) (`suggest_strategy_assignments()`).
For details see [docs/trading-strategy-suggestion.en.md](docs/trading-strategy-suggestion.en.md) (German: [docs/trading-strategy-suggestion.de.md](docs/trading-strategy-suggestion.de.md)).

## Principle

```
LLM    = Research + Interpretation + Mapping
Python = DB I/O + Prompt + Validation + Write
```

The LLM research delivers candidate JSON; Python validates it structurally and writes
in a controlled way (with overwrite protection) to the database. No new company-specific
parsers without need — existing company IR reference parsers remain exceptions.

## Data sources

- Yahoo Finance
- SEC EDGAR
- Company Investor Relations
- Regulatory sources (e.g. DART/OpenDART, ESEF)

## Contributing, Security and License

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md)
- [LICENSE](LICENSE)
