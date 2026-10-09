# Changelog

## [0.1.1] - 2026-09-28

### Added
- Swing-Horizon (`horizon_months_min`/`horizon_months_max`) jetzt funktional durchgesetzt — Runner schließt automatisch ab `horizon_months_max` (`SWING_MAX_HORIZON_REACHED`)
- Produktive Events/News-Pipeline (`Backfill-TradingEventsNews.py`): `events` aus SEC EDGAR (8-K/6-K), `news` aus Yahoo Finance Search
- Automatisierte tägliche/wöchentliche Backfills als Windows Scheduled Tasks: Market Data, FX-Rates, SEC Fundamentals, Events/News

### Changed
- Swing-TP1/TP2-Mengenlogik zentral aus `SwingStrategyConfig` abgeleitet statt hartcodierter Literale
- `decision_engine._confidence()`: `valuation`/`event_risk` UNAVAILABLE wirkt neutral statt als Malus
- CLI-Datenbankpfad-Parameter über alle Tools auf `--db-path` vereinheitlicht (`--db` bleibt als Alias funktionsfähig)
- Diverse Laufzeit-/Query-Optimierungen im Orchestrator-Runtime-Pfad

### Fixed
- Unbenutztes `RiskTargetConfig` (ohne definierte Semantik) entfernt
- Tote Schema-Tabellen `decisions`/`analysis_history` entfernt
- `Migrate-TradingStrategyAssignments.py`-Dry-Run öffnet jetzt strukturell eine read-only Verbindung (`mode=ro` + `PRAGMA query_only`)

### Known limitations
- estimates/ratings/price_targets currently have no productive data source
- Yahoo quoteSummary requires authenticated/browser-style access
- FMP endpoints required for these data are not available under the chosen home/free plan
- valuation quality therefore remains UNAVAILABLE where no other valuation data exists

## v0.1.0

Erster funktionsfähiger Release des Trading Agenten.

Enthalten:
- Trading-DB und Schema
- Security Master Data / Symbol Resolution
- Market Data und Market Snapshot
- Fundamentals Research / SEC / IR
- FX-Unterstützung
- Portfolio Context und Allocation Guardrails
- Strategy Assignment
- Swing Campaign Lifecycle
- Entry-/Add-Sizing
- Analytics und Watchlist Ranking
- Candidate Decision
- Strategy Suggestion
- Swing Promotion
- Decision Engine
- Trading Orchestrator
- Parqet Import
- Corporate Actions / Split Handling
- OpenWebUI Trading Tool
- CLI-/Management-Tools
- technische Dokumentation
- Apache-2.0 License
- Security / Contributing Policy
- 218 Tests
