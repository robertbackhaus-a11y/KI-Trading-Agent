# Trading Agent — Architekturreferenz

English version: [trading-agent-architecture.en.md](trading-agent-architecture.en.md)

Maßgebliche, am Code verifizierte Referenz für jede Komponente unter
`tools/trading/`, `mcp-tools/trading_sqlite.py` und das Schema der Produktionsdatenbank.
Direkt aus dem aktuellen Code am
2026-09-25 extrahiert — sollten dieses Dokument und der Code künftig voneinander abweichen,
ist **der Code die maßgebliche Quelle**, nicht diese Datei.

Es ergänzt die bestehenden, engeren Dokumente, ohne sie zu ersetzen:
[trading-candidate-decision.de.md](trading-candidate-decision.de.md),
[trading-strategy-suggestion.de.md](trading-strategy-suggestion.de.md),
[trading-swing-promotion.de.md](trading-swing-promotion.de.md),
[trading-entry-recommendations.de.md](trading-entry-recommendations.de.md).

---

## 1. Komponentenfluss

```
Market/Fundamental Data (Backfill-*, Research-TradingFundamentals.py)
    |  schreibt: market_data, market_snapshot, fundamentals, fx_rates
    v
Analytics (trading_analytics.py — rank_watchlist / analyze_security)
    |  rein lesend; keine Schreibzugriffe
    v
Strategy Suggestion (strategy_suggestion.py)      -- nur Vorschlag, kein Schreibzugriff
    |
    v  (ein Mensch oder ein separater expliziter Freigabeschritt entscheidet)
Strategy Assignment (Tabelle strategy_assignment; swing_promotion.py's
    approve_swing_promotion / Manage-TradingStrategyAssignments.py --write)
    |  schreibt: strategy_assignment
    v
Portfolio Context / Guardrails (analysis_engine.build_portfolio_context,
    portfolio_context.derive_allocation_guardrails)     -- rein lesend
    |
    v
Swing Campaign State (swing_lifecycle.py, Tabellen swing_campaign /
    swing_campaign_event, Manage-TradingSwingCampaigns.py)
    |
    v
Candidate Decision (candidate_decision.py)        -- rein lesend, kein Schreibzugriff
    |
    v
Decision Engine / Orchestrator (decision_engine.py, trading_orchestrator.py,
    orchestrator_presentation.py)                  -- rein lesend, kein Schreibzugriff
    |
    v
Portfolio Action Planner (portfolio_action_planner.py)
    -- rein lesende Simulation: Post-Action-Kapital, PROMOTE-Entry-Bewertung,
       Ranking, Sizing, geplante Entries (keine Orders; PROMOTE ist kein BUY)
    |
    v
Agent Output (MCP-Tool trading_sqlite über einen MCP-Client, Run-TradingOrchestrator.py)
```

Pro Übergang:

| Übergang | Übergebene Daten | Wer entscheidet | Nur Vorschlag? | Persistiert? | Ausdrücklich kein Schreibzugriff |
|---|---|---|---|---|---|
| Data → Analytics | OHLCV-Schlusskurse, Fundamentals-Zeilen | `trading_analytics.rank_watchlist` (reine Mathematik) | — | nein | ja — rein lesend |
| Analytics → Strategy Suggestion | Score, Qualität, `asset_type`, Flag für offene Campaign | `strategy_suggestion.suggest_strategy_assignments` | **ja** | nein | ja — nirgends im Modul ein DB-Schreibzugriff |
| Suggestion → Strategy Assignment | nichts automatisch | ein Mensch, über `swing_promotion.approve_swing_promotion` oder `Manage-TradingStrategyAssignments.py set --write` | nein (dieser Schritt ist der eigentliche Schreibzugriff) | **ja**, `strategy_assignment` | — |
| Strategy Assignment → Portfolio Context | alle offenen Positionen + ihre Strategie | `analysis_engine.build_portfolio_context` (reine Aggregation, keine Regel) | — | nein | ja — rein lesend |
| Portfolio Context → Swing Campaign State | nichts direkt; der Campaign-Zustand wird unabhängig erfasst | ein Mensch, über `Manage-TradingSwingCampaigns.py open/event/close --write`; zusätzlich `parqet_import.reconcile_campaign_transactions` für eindeutige BUY/SELL nach Campaign-Start (nur neutrale `add` / `manual_reduction`, siehe §3) | nein | **ja**, `swing_campaign` / `swing_campaign_event` | — |
| → Candidate Decision | Analytics + Zuordnung + Campaign + Guardrails | `candidate_decision.evaluate_watchlist_candidates` | **ja** (erzeugt BUY/WATCH/DEFERRED/INSUFFICIENT_DATA, führt nie aus) | nein | ja — nirgends im Modul ein DB-Schreibzugriff |
| → Decision Engine / Orchestrator | `AnalysisSnapshot` + `PortfolioContext` | `decision_engine.decide`, portfolioweit zusammengesetzt durch `trading_orchestrator.run_trading_orchestrator` | ja — erzeugt nur eine Empfehlung | nein | ja — `decision_engine.py` hat überhaupt keinen `sqlite3`-Import |
| → Portfolio Action Plan | bestehende SELL/TRIM- (+ ADD/BUY-)Aktionen, PROMOTE-Kandidaten, `PortfolioContext` | `portfolio_action_planner.build_portfolio_action_plan`, am Ende von `run_trading_orchestrator` aufgerufen (siehe §3.10) | ja — nur Simulation, nie eine Order | nein | ja — reine Funktionen, kein `sqlite3`-Import |
| → Agent Output | `OrchestratorResult` / `PresentationSummary` | `orchestrator_presentation.format_orchestrator_summary`, danach das Trading-MCP-Tool `trading_sqlite` (MCP-Client) / Run-TradingOrchestrator.py | — (nur Formatierung) | nein | ja |

---

## 2. Tool-/Modulinventar

### 2.1 Datenaufnahme & DB-Lebenszyklus

| Tool | Zweck | Schreibt nach | Externe Quelle |
|---|---|---|---|
| `Initialize-TradingDatabase.py` | Idempotente Schema-Erstellung (`CREATE TABLE/INDEX IF NOT EXISTS`); sicher auf einer bestehenden DB | alle Basistabellen/Views/Trigger, `INSERT OR IGNORE`-Seed für `metadata`/`data_sources` | keine |
| `Reset-TradingDb.py` | **Destruktiver** Komplett-Reset — löscht die DB-Datei (+ `-wal`/`-shm`) und erstellt sie von Grund auf neu. Erfordert ein explizites `--db-path` (kein Default, Abbruch ohne) und die Eingabe von `RESET` an einer interaktiven Abfrage. | dasselbe Schema wie Initialize, nicht idempotentes `CREATE` | keine |
| `Backfill-TradingMarketData.py` | Täglicher OHLCV- + aktueller Snapshot-Backfill, inkrementell (5-Tage-Überlappungsfenster). **Automatisiert seit 2026-09-28**: Windows Scheduled Task `Trading-MarketData-Backfill`, täglich 17:15 lokal, läuft mit `--write`, stdout/stderr werden an `C:\tools\trading\logs\market-data-backfill.log` angehängt | `market_data`, `market_snapshot`, `source_symbols` (legt diese Tabelle bei Bedarf selbst an), `data_sources` | Yahoo-Finance-Chart- + Such-API |
| `Backfill-TradingFXRatesECB.py` | EZB-EUR-Referenzkurs-Backfill, standardmäßig Dry-Run. Ein Lauf lädt jede Währung aus `--quote-currency` (kommagetrennt; Default `USD,GBP,AUD,KRW`, die Kurswährungen der gehaltenen/beobachteten Wertpapiere); eine fehlschlagende Währung verwirft die anderen nicht, lässt den Lauf aber mit Exit 1 enden. **Automatisiert seit 2026-09-28**: Windows Scheduled Task `Trading-FXRates-Backfill`, Auslöser bei Anmeldung und täglich 17:20 lokal, `StartWhenAvailable=True`, läuft mit `--write`, stdout/stderr werden an `C:\tools\trading\logs\fx-rates-backfill.log` angehängt. `Manage-TradingTasks.ps1 Register` überspringt einen bestehenden Task; der Skript-Default trägt deshalb die vier Währungen für den bereits registrierten Task | `fx_rates` (nur mit `--write`) | EZB-SDMX-EXR-API |
| `Backfill-TradingMarketSnapshotCurrency.py` | Deterministisches Fallback-Auffüllen fehlender `market_snapshot.currency` aus `market_data`/`source_symbols`/`security` desselben Tages — rät nie | `market_snapshot.currency` (nur `--write`, nur NULL-Zeilen), legt vorher eine `.bak`-Dateikopie an | keine (nur DB-intern) |
| `Backfill-TradingFundamentalsSEC.py` | Fundamentals-Bulk-Backfill aus SEC-EDGAR-XBRL-CompanyFacts mit einem lokalen 6-h-JSON-Cache. Kein `--write`-Flag — jeder Lauf schreibt bedingungslos (bestätigt: die Übergabe von `--write` scheitert mit Exit-Code 2, anders als bei den übrigen Backfill-*-Tools). **Automatisiert seit 2026-09-28**: Windows Scheduled Task `Trading-Fundamentals-SEC`, Auslöser bei Anmeldung und wöchentlich sonntags 10:00 lokal, `StartWhenAvailable=True`, stdout/stderr werden an `C:\tools\trading\logs\fundamentals-sec-backfill.log` angehängt | `fundamentals` (DELETE-then-executemany-Upsert je Wertpapier) | SEC-EDGAR-XBRL-API |
| `Backfill-TradingFundamentalsIR.py` | Handkuratierte Parser für 6 Unternehmen (ASML, ING, TSMC, SK hynix, BAE Systems, HENSOLDT) aus IR-Quellen der Unternehmen. Berichts-URLs sind je Unternehmen und Quartal fest hinterlegt (z. B. nennt `ASML_URL` "Q2-2026") — **nur manuell/ereignisgetrieben**, nicht geplant: ein unbeaufsichtigter Lauf würde stillschweigend ein veraltetes Quartal erneut abrufen oder mit 404 scheitern, sobald ein neuer Bericht erscheint | `fundamentals` | IR-Seiten der Unternehmen (XLSX/PDF), OpenDART (SK hynix); BAE Systems aus einem statischen lokalen JSON-Snapshot |
| `Research-TradingFundamentals.py` | LLM-Recherche-Workflow: baut einen Recherche-Prompt, validiert die JSON-Antwort des LLM strukturell (vertraut ihr nie blind) und schreibt dann optional. **Nur manuell/LLM-gestützt** — laut eigenem Docstring "NOT a parser"; erfordert eine interaktive LLM-Sitzung (Claude) für die eigentliche Recherche und kann daher nicht headless/geplant laufen | `fundamentals` (nur `--write`), außerdem Prompt-/Audit-Dateien unter `C:\tools\trading\data\fundamentals-audit\` | keine direkt — ein externes LLM erledigt die eigentliche Recherche |
| `Backfill-TradingEventsNews.py` | `events` aus dem SEC-EDGAR-Company-Submissions-JSON (8-K-Meldungen für inländische Emittenten, 6-K für ausländische Privatemittenten; SECs eigene Item-Codes → `event_type`, 2-Jahres-Lookback-Fenster, vollständiger Austausch je `(security_id, source_id)` bei jedem Lauf), `news` vom öffentlichen Suchendpunkt von Yahoo Finance (weicht vom Ticker auf den Firmennamen aus, wenn die Ticker-Suche nichts liefert, z. B. bei ausländischen Listings). Standardmäßig Dry-Run, `--db-path`/`--db` kanonisch+Alias. **Automatisiert seit 2026-09-28**: Windows Scheduled Task `Trading-EventsNews-Backfill`, Auslöser bei Anmeldung und täglich 17:25 lokal, `StartWhenAvailable=True`, läuft mit `--write`, stdout/stderr werden an `C:\tools\trading\logs\events-news-backfill.log` angehängt. Idempotenz verifiziert: aufeinanderfolgende Läufe lassen die Zeilenzahlen von `events`/`news` unverändert (0 neue Zeilen bei einem sofortigen Wiederholungslauf) | `events` (DELETE-then-executemany-INSERT je Wertpapier/Quelle), `news` (`INSERT OR IGNORE` auf dem bestehenden Unique-Index `idx_news_url`) | SEC-EDGAR-Submissions-API; Yahoo Finance `v1/finance/search` (beide ohne Authentifizierung, kein API-Key) |
| `Discover-TradingSECIdentifiers.py` | Rein lesend: ordnet Portfolio-/Watchlist-Aktien per Fuzzy-Matching SEC-CIK-Kennungen zu | keine (nur Report) | SEC-Company-Ticker-Index + CompanyFacts-API |
| `Resolve-TradingSecurities.py` | Löst kanonisches Symbol/Börse über OpenFIGI auf; führt außerdem ein fest hinterlegtes ING-Watchlist-Duplikat zusammen | `security` (symbol/exchange/asset_type), `watchlist` (Duplikat-Zusammenführung) | OpenFIGI-Mapping-API |
| `Audit-TradingDataQuality.py` | Rein lesendes Audit der Fundamentals-Vollständigkeit/-Aktualität, klassifiziert jedes Wertpapier als `NOT_APPLICABLE`/`MISSING`/`STALE`/`PARTIAL`/`COMPLETE` | keine | keine |
| `Import-ParqetTransactions.py` | CLI-Wrapper um `parqet_import.py`; standardmäßig Vorschau. `--reconcile-campaigns` gleicht bereits importierte Trades mit offenen Swing-Campaigns ab (kein CSV; standardmäßig Vorschau) | `transactions`, `positions`, `swing_campaign_event` (nur `--write`; sichert vorher die DB) | keine — lokaler Parqet-CSV-Export |

### 2.2 Rein lesende Analyse-/Entscheidungspipeline

| Modul | Zweck | DB lesen/schreiben | Hängt ab von |
|---|---|---|---|
| `analysis_contracts.py` | Gemeinsame Enums/Dataclasses (`Action`, `ActionQuantityBasis`, `AvailabilityStatus`, `StrategyType`, `DataQuality`, `AnalysisSnapshot`, `PortfolioContext`, `DecisionResult`, `to_primitive`) | keine | — |
| `strategy_config.py` | Alle einstellbaren Trading-Parameter als frozen Dataclasses (`StrategyConfig` und seine Unter-Configs) — nur Parameter, kein I/O | keine | — |
| `fx_resolver.py` | FX-Auflösung nach EZB-Konvention (1 EUR = N Kurswährung), nur direkte Paare; Pence-Kurse des Providers (`GBp`, die einzige solche Einheit in den Daten) werden zentral zu GBP normalisiert (Preisfaktor 0.01), bevor der GBP-Kurs angewendet wird | liest `fx_rates` | `analysis_contracts`, `strategy_config` |
| `capital_state.py` | Rein lesende zeitliche Auflösung der letzten zulässigen Zeile aus `portfolio_capital_state` | liest `portfolio_capital_state`, `metadata` | `analysis_contracts`, `strategy_config` |
| `swing_lifecycle.py` | Leitet den TP1/TP2/Add/Reduktions-Zustand der aktuell offenen Campaign aus expliziten Lifecycle-Events ab; schließt nie aus Transaktionen | liest `swing_campaign`, `swing_campaign_event`, `metadata` | `analysis_contracts` |
| `analysis_engine.py` | Baut `AnalysisSnapshot` (technisch + fundamental + Bewertung + Event-Risiko + Position) und `PortfolioContext` je Wertpapier / portfolioweit | liest `security`, `watchlist`, `positions`, `market_data`, `fundamentals`, `estimates`, `ratings`, `price_targets`, `events`, `news`, `strategy_assignment`, `market_snapshot` | `fx_resolver`, `capital_state`, `swing_lifecycle`, `portfolio_context`, `strategy_config`, `analysis_contracts` |
| `portfolio_context.py` | Reine Allokationsmathematik: Guardrail-Ableitung, Projektion der Kapitalnutzung, Sizing-Bereitschaft — kein DB-Zugriff | keine | `analysis_contracts`, `strategy_config` |
| `trading_analytics.py` | Bibliothek für technisches Scoring (SMA/RSI/Momentum/Drawdown/Volatilität) und `rank_watchlist()` | liest `market_data`, `watchlist`, `security`, `data_sources` | — |
| `decision_engine.py` | Reine Empfehlung je Wertpapier (`BUY`/`ADD`/`HOLD`/`TRIM`/`SELL`/`WATCH`) aus einem `AnalysisSnapshot` + `PortfolioContext` | keine (überhaupt kein `sqlite3`-Import) | `analysis_contracts`, `add_sizing`, `entry_sizing`, `strategy_config` |
| `entry_sizing.py` | Reine Swing-Entry-Eignung/-Sizing für Positionen mit Bestand null | keine | `analysis_contracts`, `strategy_config` |
| `add_sizing.py` | Reine Swing-ADD-Eignung/-Sizing bei offener Campaign | keine | `analysis_contracts`, `strategy_config` |
| `candidate_discovery.py` | Rein lesende Watchlist-Kandidaten-Discovery (§3.11): Universum → Vorfilter → Datenverfügbarkeit → Analyse mit der bestehenden Engine → Discovery-Score; führt die unveränderte Engine auf einer In-Memory-Kopie des Produktionsschemas aus, schreibt nie | Produktions-DB: nur lesend (`source_symbols`, `positions`, `watchlist`, `security`, `fx_rates`, `data_sources`) | `analysis_engine`, `swing_promotion`, `decision_engine`, `candidate_decision`, `fx_resolver`, `strategy_config` |
| `Discover-TradingCandidates.py` | Explizite CLI für die Discovery: Yahoo-Historie über die Helfer des Market-Data-Backfills (optionaler Tages-Cache), gibt die gerankte Tabelle / JSON aus und schreibt nach einem vollständig erfolgreichen Lauf den Report (`--output-dir`, `--no-history`); **automatisiert seit 2026-10-09**: Windows Scheduled Task `Trading-Candidate-Discovery`, täglich 17:35 lokal (nach MarketData 17:15 / FX 17:20 / EventsNews 17:25), kein Anmelde-Auslöser, 2-h-Zeitlimit, stdout/stderr werden an `C:\tools\trading\logs\candidate-discovery.log` angehängt; nicht Teil des Orchestrators | keine (DB mit `mode=ro` geöffnet); Report-Dateien `C:\tools\trading\data\candidate-discovery\latest.json` (+ History-Kopien) | Yahoo-Finance-Chart-API, `candidate_discovery`, `discovery_report` |
| `discovery_report.py` | Versionierter Report eines Discovery-Laufs (Schema v1): Aufbau, Validierung (Schema + `results_sha256`-Digest), atomares Schreiben (Temp-Datei → erneut lesen/validieren → ersetzen; History-Kopie zuerst) und die reine Abfrage für den MCP-Reader. Nur Datei-I/O: kein Netzwerk, keine Datenbank | nur Report-Verzeichnis | `strategy_config` (`DataQualityConfig`) |
| `market_intelligence.py` | Reiner, deterministischer Market-Intelligence-Kern (§3.12): Normalisierung von Feeds/SEC/Yahoo, regelbasierte Kategorie/Wichtigkeit/Impact, Zusammenführung von Duplikaten, Zuordnung zu Portfolio/Watchlist/Discovery; `load_db_context` liest die DB (`mode=ro`) | Produktions-DB: nur lesend (`news`, `events`, `source_symbols`, `positions`, `watchlist`, `security`, `data_sources`) | `strategy_config` (`MarketIntelligenceConfig`), statisches `universe/sector_map_v1.json` |
| `Collect-TradingMarketIntelligence.py` | Explizite CLI: ruft 10 offizielle Feeds (EZB, Fed, BLS, BEA, Destatis, EIA) und Yahoo-Schlagzeilen für READY-Discovery-Kandidaten ab, baut den Report und schreibt ihn atomar; **automatisiert seit 2026-10-09**: Windows Scheduled Task `Trading-Market-Intelligence`, täglich 08:15 und 17:45 lokal (der Abendlauf folgt der Discovery um 17:35 und liest deren Report), kein Anmelde-Auslöser, 2-h-Zeitlimit, stdout/stderr werden an `C:\tools\trading\logs\market-intelligence.log` angehängt; nicht Teil des Orchestrators | keine (DB mit `mode=ro` geöffnet); Report-Dateien `C:\tools\trading\data\market-intelligence\latest.json` (+ History-Kopien) | offizielle RSS/Atom-Feeds, Yahoo-Finance-News, `market_intelligence`, `intelligence_report`, `discovery_report` (atomares Schreiben) |
| `intelligence_report.py` | Versionierter Report eines Collection-Laufs (Schema v1): Aufbau, Validierung (Schema, Enums, Zähler, `events_sha256`), Schreiben und die reine Abfrage für den MCP-Reader. Nur Datei-I/O: kein Netzwerk, keine Datenbank | nur Report-Verzeichnis | `strategy_config`, `discovery_report` (`_atomic_write`) |
| `opportunity_view.py` | Reine, deterministische Zusammenführung aus Orchestrator-/Planner-Ergebnis, letztem Discovery-Report und letztem Market-Intelligence-Report (§3.13): Opportunities, News-Kontext-Status, Planner-Kapitalkontext, Report-Aktualität; Abfrage/Kompaktierung/Rendering für den MCP-Reader; kein eigener Score, Rang, kein eigenes Sizing oder Signal. `load_symbol_map` ist ein einziges lesendes SELECT | Produktions-DB: nur lesend (`source_symbols`, `data_sources`) über die lesende Verbindung des Aufrufers | `discovery_report`, `intelligence_report`, `market_intelligence` (`company_mentioned`), `strategy_config` |
| `portfolio_action_planner.py` | Deterministischer Kapital-/Entry-Plan (`portfolio_action_plan`): Post-Action-Kapital, PROMOTE-Entry-Bewertung, Ranking, Sizing, sequentielle Allokation — nur Simulation (§3.10) | keine | `entry_sizing`, `portfolio_context`, `candidate_decision`, `decision_engine`, `orchestrator_presentation`, `strategy_config` |
| `swing_promotion.py` | Bewertet/genehmigt die Promotion WATCH → Swing-`strategy_assignment` | liest `watchlist`, `strategy_assignment`, `swing_campaign`; schreibt `strategy_assignment`, `candidate_promotion` nur über explizites `approve_swing_promotion` | `analysis_engine`, `analysis_contracts` |
| `candidate_decision.py` | Verdichtet Analytics + Zuordnung + Campaign + Guardrails zu `BUY`/`WATCH`/`DEFERRED`/`INSUFFICIENT_DATA` | liest (über wiederverwendete Funktionen) `watchlist`, `strategy_assignment`, `swing_campaign` sowie alles, was `build_portfolio_context` liest; schreibt nichts | `trading_analytics`, `swing_promotion`, `analysis_engine` |
| `strategy_suggestion.py` | Unverbindlicher Vorschlag `swing`/`long_term`/`unknown` für nicht zugeordnete WATCH-Einträge | liest `watchlist`, `strategy_assignment`, `swing_campaign`, `security.asset_type`; schreibt nichts | `trading_analytics`, `swing_promotion` |
| `orchestrator_presentation.py` | Deterministischer Präsentationsvertrag (nur Filtern/Zählen/Formatieren) über einem bereits berechneten `OrchestratorResult` | keine | `analysis_contracts` |
| `trading_orchestrator.py` | Setzt die gesamte rein lesende Pipeline zu einem portfolioweiten Report (`OrchestratorResult`) zusammen | liest alles, was die oben genannten lesen; schreibt nichts | `analysis_engine`, `decision_engine`, `swing_promotion`, `orchestrator_presentation`, `portfolio_context` |
| `Run-TradingOrchestrator.py` | Dünner, rein lesender CLI-Starter für `trading_orchestrator.run_trading_orchestrator` | lesende Verbindung (`mode=ro`, `PRAGMA query_only=ON`) | `trading_orchestrator` |
| `parqet_import.py` | Kanonischer, Split-bewusster Transaktionsimport + Positionsneuaufbau + Campaign-Abgleich nach Campaign-Start | liest/schreibt `transactions`, `positions`; schreibt `swing_campaign_event` (Abgleich); liest `corporate_action` (über `corporate_actions.py`) | `corporate_actions`, `swing_lifecycle` |

### 2.3 CLI-Manager & Migrationen (nachvollziehbare, additive Features)

Alle `Migrate-*`-Skripte verlangen `metadata.schema_version == "2.0"` und sind ein Dry-Run, solange nicht `--write` gesetzt ist. Alle `Manage-*`-Skripte sind ein Dry-Run, solange nicht `--write` gesetzt ist, und legen nie selbst Schema an (sie brechen mit einem Verweis auf das passende `Migrate-*`-Skript ab).

| Migration | Legt an | Feature-Versionsschlüssel (Wert `"1"`) |
|---|---|---|
| `Migrate-TradingStrategyAssignments.py` | `strategy_assignment` + 2 No-Overlap-Trigger | `strategy_assignment_schema_version` |
| `Migrate-TradingCandidatePromotions.py` | `candidate_promotion` | `candidate_promotion_schema_version` |
| `Migrate-TradingFXRates.py` | `fx_rates`, erstellt die Views `v_active_positions`/`v_portfolio_market` neu | `fx_rates_schema_version` |
| `Migrate-TradingSwingLifecycle.py` | `swing_campaign`, `swing_campaign_event` + 4 Trigger (verlangt zuvor `strategy_assignment_schema_version == "1"`) | `swing_campaign_schema_version` |
| `Migrate-TradingCapitalState.py` | `portfolio_capital_state` | `portfolio_capital_state_schema_version` |
| `Migrate-TradingCorporateActions.py` | `corporate_action` | `corporate_action_schema_version` |

| Manager | Unterbefehle | Kapselt |
|---|---|---|
| `Manage-TradingStrategyAssignments.py` | `list`, `set`, `close`, `validate` | `strategy_assignment` direkt (kein separates Modul) |
| `Manage-TradingSwingCampaigns.py` | `list`, `open`, `event`, `close`, `validate` | `swing_lifecycle.py` |
| `Manage-TradingCapitalState.py` | `show`, `set`, `validate` | `capital_state.py` |
| `Manage-TradingCorporateActions.py` | `list`, `validate`, `add-split`, `rebuild-position` | `corporate_actions.py`, `parqet_import.rebuild_position` |

Alle vier `Manage-*`-/Produktions-DB-Tools verwenden standardmäßig `C:\tools\trading\data\trading.db`, überschreibbar über `--db-path` — den seit 2026-09-28 kanonischen Flag-Namen über die gesamte CLI-Oberfläche von `tools/trading/` (`--db` bleibt als funktionierender Legacy-Alias erhalten, siehe §9 Inkonsistenz #6).

### 2.4 Agent-Zugriff (MCP-Client / Trading MCP)

```
MCP client (LLM frontend)
  |
  v
Trading MCP Server       C:\tools\trading\mcp   (Repo-Quelle: mcp-tools\)
  |
  v
Trading Runtime          C:\tools\trading\app
  |
  v
SQLite                   C:\tools\trading\data\trading.db
```

Der MCP-Server (`server.py`) stellt jede öffentliche Methode der Klasse `Tools` in
`trading_sqlite.py` als MCP-Tool bereit — derzeit 15 (siehe §5).

---

## 3. Feste Werte & Schwellenwerte (exakt, aus dem Code)

### 3.1 Strategietypen & Statusvokabulare

| Vokabular | Erlaubte Werte | Definiert in |
|---|---|---|
| `StrategyType`-Enum | `long_term`, `swing`, `tactical`, `unknown` | `analysis_contracts.py` |
| `strategy_assignment.strategy_type` CHECK | `long_term`, `swing`, `tactical`, `unknown` (entspricht dem Enum) | Schema |
| `Action`-Enum | `BUY`, `ADD`, `HOLD`, `TRIM`, `SELL`, `WATCH` | `analysis_contracts.py` |
| `ActionQuantityBasis`-Enum | `TP1_25_PERCENT_ORIGINAL`, `TP2_75_PERCENT_CUMULATIVE_ORIGINAL`, `RUNNER_FULL_REMAINDER`, `STOP_FULL_EXIT`, `ADD_25_PERCENT_ORIGINAL`, `INITIAL_ENTRY_SIZING` | `analysis_contracts.py` |
| `AvailabilityStatus`-Enum | `available`, `partial`, `insufficient`, `unavailable`, `stale`, `not_applicable` | `analysis_contracts.py` |
| `candidate_decision.CandidateDecision.decision_status` | `BUY`, `WATCH`, `DEFERRED`, `INSUFFICIENT_DATA` | `candidate_decision.py` |
| `candidate_discovery`-Status | `DISCOVERY_READY`, `DISCOVERY_WATCH`, `DISCOVERY_DATA_INSUFFICIENT`, `DISCOVERY_REJECTED` | `candidate_discovery.py` |
| `portfolio_action_planner` `entry_status` | `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `BLOCKED_BY_ALLOCATION`, `BLOCKED_BY_DATA`, `BLOCKED_BY_CONCENTRATION`, `BLOCKED_EXISTING_POSITION`, `BLOCKED_EXISTING_CAMPAIGN` | `portfolio_action_planner.py` |
| `strategy_suggestion.StrategySuggestion.suggested_strategy` | `swing`, `long_term`, `unknown` | `strategy_suggestion.py` |
| `swing_promotion.PromotionDecision.recommendation` | `PROMOTE`, `KEEP_WATCHING`, `REJECT`, `DATA_INSUFFICIENT` | `swing_promotion.py` |
| `DATA_INSUFFICIENT`-Reason-Codes (`swing_promotion.py`) | `PROMOTION_TECHNICAL_DATA_STALE`, `PROMOTION_REQUIRED_DATA_INCOMPLETE`, `PROMOTION_PRICE_UNAVAILABLE`, `PROMOTION_SMA50_UNAVAILABLE`, `PROMOTION_SMA200_UNAVAILABLE`, `PROMOTION_MARKET_DATA_MISSING` (kein market_snapshot-Kurs oder keine Kurswährung), `PROMOTION_FX_RATE_MISSING` (Kurs und Währung vorhanden, aber kein verwendbarer EZB-Kurs für die EUR-Bewertung) | `swing_promotion.py` |
| `candidate_promotion.status` CHECK | `watching`, `ready`, `promoted`, `rejected`, `deferred` | Schema |
| `swing_campaign.status` CHECK | `open`, `closed` | Schema |
| `swing_campaign_event.event_type` CHECK | `baseline`, `add`, `tp1_signal`, `tp1_execution`, `tp2_signal`, `tp2_execution`, `manual_reduction`, `stop_execution`, `close` | Schema, `swing_lifecycle.py` |
| `portfolio_capital_state.quality` CHECK | `available`, `partial`, `unavailable`, `stale` | Schema |
| `corporate_action.action_type` CHECK | `STOCK_SPLIT` (derzeit einziger unterstützter Wert) | Schema, `corporate_actions.py` |
| Schweregrade der globalen/wertpapierbezogenen Probleme in `trading_orchestrator` | `GLOBAL_BLOCKING`, `SECURITY_BLOCKING`, `NON_BLOCKING_WARNING` | `trading_orchestrator.py` |
| Verifikationsstatus in `Research-TradingFundamentals.py` | `verified_direct`, `verified_derived`, `verified_secondary`, `not_verified` (`not_verified` wird stets verworfen → NULL) | `Research-TradingFundamentals.py` |
| Periodentypen in `Research-TradingFundamentals.py` | `quarterly`, `semiannual`, `nine_month`, `annual` | `Research-TradingFundamentals.py` |
| Klassifikation in `Audit-TradingDataQuality.py` | `NOT_APPLICABLE`, `MISSING`, `STALE`, `PARTIAL`, `COMPLETE` | `Audit-TradingDataQuality.py` |

### 3.2 Trading Analytics (`trading_analytics.py`)

| Konstante | Wert |
|---|---|
| `PERF_1M_DAYS` / `PERF_3M_DAYS` / `PERF_6M_DAYS` | 21 / 63 / 126 Handelstage |
| `SMA_SHORT_DAYS` / `SMA_LONG_DAYS` | 50 / 200 |
| `RSI_DAYS` | 14 (einfacher, nicht Wilder-geglätteter Durchschnitt aus Gewinnen/Verlusten) |
| `VOLATILITY_DAYS` | 60 (annualisiert mit √252) |
| `DRAWDOWN_WINDOW_DAYS` | 252 (~52 Handelswochen); verwendet die kürzere Historie, falls weniger vorhanden ist |
| `TRADING_DAYS_PER_YEAR` | 252 |
| `QUALITY_OK_MIN_ROWS` | 200 (= `SMA_LONG_DAYS`) → Qualität `"OK"` |
| `QUALITY_LIMITED_MIN_ROWS` | 30 → Qualität `"LIMITED"`; darunter → `"INSUFFICIENT"` |
| Komponenten des Momentum-Scores (jeweils gedeckelt, summiert, `None`, wenn nicht vorhanden — nie als 0 gezählt) | `price>SMA50` ±10, `price>SMA200` ±10, `SMA50>SMA200` ±10, `perf_1m`/`perf_3m`/`perf_6m` jeweils auf [-10,+10] begrenzt, `(RSI14-50)/50*10`, `max(drawdown_52w, -20)` |
| Geltungsbereich von `rank_watchlist()` | nur `watchlist.status = 'WATCH'` |
| Sortierung von `rank_watchlist()` | bewertete Einträge absteigend nach `score`; Einträge mit `score is None` werden danach in der ursprünglichen Watchlist-Reihenfolge angehängt (nie verworfen) |
| Quelle der Kursreihe | `adjusted_close` bevorzugt, Rückfall auf `close`; beschränkt auf die `data_sources`-Zeile `"Yahoo Finance"`, sofern sie existiert |

### 3.3 Candidate Decision (`candidate_decision.py`)

- `BUY_SCORE_THRESHOLD = 20.0`
- Entscheidungsreihenfolge (die vollständige Begründung steht in [trading-candidate-decision.de.md](trading-candidate-decision.de.md)): `analytics_quality != "OK"` → `INSUFFICIENT_DATA`; keine Zuordnung → `DEFERRED`; offene Campaign → `WATCH`; Nicht-Swing-Zuordnung → `WATCH`; Allokations-Guardrails nicht verfügbar → `DEFERRED`; `SWING_ALLOCATION_ABOVE_MAX` → `DEFERRED`; Score nicht verfügbar → `WATCH`; Score `>= 20.0` → `BUY`; sonst `WATCH`.

### 3.4 Strategy Suggestion (`strategy_suggestion.py`)

- `SWING_SUGGESTION_SCORE_THRESHOLD = 20.0`
- `LONG_TERM_ASSET_TYPES = ("etf", "fund")` (dieselbe Menge, die `analysis_engine.py`/`Audit-TradingDataQuality.py` als Fundamentals-nicht-anwendbar behandeln)
- Confidence-Konstanten: `CONFIDENCE_OPEN_CAMPAIGN = 0.9`, `CONFIDENCE_ETF_LONG_TERM = 0.7`, `CONFIDENCE_SWING_MOMENTUM = 0.6`, `CONFIDENCE_UNKNOWN = 0.0`
- `long_term` wird **nie** aus dem Momentum-Score abgeleitet (nur aus `asset_type`)

### 3.5 Portfolio-/Sizing-/Swing-Parameter (`strategy_config.py`)

| Config | Feld | Wert | Verwendet von |
|---|---|---|---|
| `PortfolioTargetConfig` | `long_term_min_pct` / `long_term_max_pct` | 0.60 / 0.70 | `portfolio_context.derive_allocation_guardrails`, `entry_sizing.py`, `add_sizing.py` |
| | `swing_min_pct` / `swing_max_pct` | 0.30 / 0.40 | dasselbe |
| `SizingPolicyConfig` | `max_security_weight` | 0.20 | `entry_sizing.py`, `add_sizing.py` |
| | `max_initial_swing_weight` | 0.10 | `entry_sizing.py` |
| | `max_add_pct_of_original` | 0.25 | `add_sizing.py` |
| | `minimum_cash_reserve` | 10,000.0 EUR | `entry_sizing.py`, `add_sizing.py`, `trading_orchestrator.py` (Warnung `CASH_RESERVE_LIMIT`) |
| | `max_add_count` | 1 | `add_sizing.py` |
| | `whole_share_policy` | `"floor"` | `entry_sizing.py`, `add_sizing.py` |
| `SwingStrategyConfig` | `horizon_months_min` / `horizon_months_max` | 3 / 6 | `decision_engine._runner_decision` (Horizont-Gate für den Runner nach TP2, siehe §3.6) |
| | `tp1_gain_pct` / `tp2_gain_pct` | 0.20 / 0.25 | `decision_engine.py` (TP-Preisniveaus) |
| | `tp1_sell_fraction` / `tp2_sell_fraction` | 0.25 / 0.50 | `decision_engine.py`, `swing_lifecycle.py` (siehe §3.6) |
| | `hard_stop_loss_pct` | 0.15 | `decision_engine.py` |
| | `remainder_management` | `"momentum_guided"` | `decision_engine._runner_decision` — die einzige implementierte Policy; führt unterhalb von `horizon_months_max` die bestehende SMA50/SMA200-Trendlogik aus |
| | `entry_rule`/`add_rule`/`stop_rule`/`position_sizing_rule` | alle `None` | noch nicht definiert |
| `DataQualityConfig` | `market_data_max_age_days` | 5 | `analysis_engine.py` (Gate für technische Veralterung) |
| `DiscoveryConfig` | `min_price_eur` / `min_median_daily_value_eur` / `liquidity_window_days` | 5.0 / 5,000,000 / 60 (Vorschlag, nur Vorfilter der Discovery) | `candidate_discovery.py` |
| `MarketIntelligenceConfig` | `lookback_days` / `report_max_age_hours` | 7 / 24 (Vorschlag; News sind zeitkritisch, daher wird der Report nach einem Tag mit `REPORT_STALE` gekennzeichnet) | `market_intelligence.py`, `intelligence_report.py` |
| `TaxConfig` | `capital_gains_tax_rate` / `solidarity_surcharge_rate` / `church_tax_rate` | 0.25 / 0.055 (auf die Steuer) / 0.0 (nur 0.0 wird akzeptiert) → `combined_rate` 26.375 % des realisierten Gewinns | `portfolio_action_planner.py` (Netto-Erlös-Simulation) |
| `FXConfig` | `portfolio_base_currency` | `"EUR"` | `analysis_engine.build_portfolio_context` (wirft bei Änderung einen Fehler) |
| | `fx_max_age_days` | 5 | `fx_resolver.py` |
| | `preferred_fx_source` | `"ECB"` | `fx_resolver.py` |
| `CapitalStateConfig` | `freshness_max_age_days` | `None` (Opt-in) | `capital_state.py`, bereitgestellt über `Manage-TradingCapitalState.py show --freshness-max-age-days` |

### 3.6 TP1/TP2/Stop/Runner-Mengenlogik (`decision_engine.py`, siehe §9)

- TP1-Trim: verkauft `floor(original_quantity * config.swing.tp1_sell_fraction)` (0.25) → `ActionQuantityBasis.TP1_25_PERCENT_ORIGINAL`
- TP2-Trim: verkauft bis auf `floor(original_quantity * (tp1_sell_fraction + tp2_sell_fraction))` (0.75) kumuliert vom Original → `ActionQuantityBasis.TP2_75_PERCENT_CUMULATIVE_ORIGINAL`
- Hard Stop: `stop_price = reference_cost * (1 - hard_stop_loss_pct)` (0.15) → vollständiger Ausstieg, `ActionQuantityBasis.STOP_FULL_EXIT`
- Runner (Restposition nach TP2), `decision_engine._runner_decision`, `ActionQuantityBasis.RUNNER_FULL_REMAINDER`:
  - Alter der Campaign in vollen Kalendermonaten = `swing_campaign.opened_at` (über `PositionContext.swing_campaign_opened_at`) gegenüber dem `evaluation_as_of` des Snapshots. Ein unbekanntes/nicht parsebares Alter (z. B. fehlendes `opened_at`) fällt immer auf das reine Trendverhalten unten zurück.
  - Unterhalb von `horizon_months_max` (`horizon_months_min` ist **keine** Mindesthaltedauer): `remainder_management == "momentum_guided"` — gehalten/verlassen ausschließlich nach Momentum (SMA50/SMA200), bedingungslos, unabhängig davon, ob die Campaign unterhalb oder bereits oberhalb von `horizon_months_min` liegt.
  - Bei/nach `horizon_months_max` (6): bedingungslos geschlossen — dasselbe SELL-/Full-Remainder-Ergebnis wie beim SMA200-Ausstieg, mit dem Grund `SWING_MAX_HORIZON_REACHED` — unabhängig von Trend oder Verfügbarkeit technischer Daten.
  - Der Hard Stop (nur vor der TP2-Ausführung geprüft) und die TP1/TP2/ADD-Regeln sind vom Campaign-Alter unberührt; der Horizont steuert immer nur einen bereits offenen Runner.
  - Durchgerechnetes Beispiel (`opened_at = 2026-01-01`, Standard `horizon_months_min/max = 3/6`): bei `evaluation_as_of = 2026-03-15` (Alter 2 Monate, `< min`) und bei `2026-06-15` (Alter 5 Monate, `min <= age < max`) verhält sich der Runner identisch — nur SMA50/SMA200-Trend. Bei `2026-07-01` (Alter 6 Monate, `>= max`) wird unabhängig vom Trend geschlossen.
- ADD: `base = floor(original_quantity * max_add_pct_of_original)` (0.25) → `ActionQuantityBasis.ADD_25_PERCENT_ORIGINAL`, begrenzt durch `max_add_count = 1`
- `swing_lifecycle.py` leitet unabhängig `tp2_cumulative_target = floor(original_quantity * (tp1_sell_fraction + tp2_sell_fraction))` (0.75, über einen optionalen Parameter `config: Optional[SwingStrategyConfig] = None`) ab, um `post_tp2_add_detected` zu erkennen

### 3.7 Portfolio Context / Guardrails (`analysis_engine.py`, `portfolio_context.py`)

- `total_market_value`: Summe der Marktwerte aller offenen (`shares > 0`) Positionen, über `fx_resolver.py` FX-normalisiert nach EUR. Wird vollständig zurückgehalten (`None`), wenn auch nur eine Position nicht bewertet werden kann — **nie aus einem unvollständigen Nenner berechnet**.
- `cash_available`: ein **eigenes** Feld aus `capital_state.py` (Tabelle `portfolio_capital_state`) — nie in `total_market_value` eingerechnet.
- `base_currency`: immer `"EUR"` (`FXConfig.portfolio_base_currency`, fest einprogrammierte Validierung).
- Guardrail-Namen (`derive_allocation_guardrails`, jeweils gegen `PortfolioTargetConfig`): `SWING_ALLOCATION_ABOVE_MAX`, `SWING_ALLOCATION_BELOW_MIN`, `LONG_TERM_ALLOCATION_ABOVE_MAX`, `LONG_TERM_ALLOCATION_BELOW_MIN` oder `("WITHIN_TARGET_RANGE",)`, wenn keiner zutrifft. Rein informativ — "never a trading action" (Modul-Docstring).
- Auswirkung auf die Candidate Decision: nur `SWING_ALLOCATION_ABOVE_MAX` blockiert ein `BUY` (→ `DEFERRED`); die übrigen sind informativ (erscheinen in `portfolio_constraints`, ändern aber `decision_status` nicht).
- `trading_orchestrator.py` löst zusätzlich eine `NON_BLOCKING_WARNING` (`ALLOCATION_OUTSIDE_TARGET`) aus, sobald `allocation_guardrails != ("WITHIN_TARGET_RANGE",)`.

**Hinweis zur kleinen (~0.22 %) Bewertungsabweichung gegenüber Parqet, die in einer früheren Sitzung gefunden wurde**: als Folge abweichender Abfragezeitpunkte/Quellen akzeptiert; hier auf Anweisung nicht erneut untersucht.

### 3.8 Swing-Campaign-Regeln (vom Schema erzwungen)

- Eine offene Campaign je Wertpapier: `CREATE UNIQUE INDEX idx_swing_campaign_one_open_per_security ON swing_campaign(security_id) WHERE status = 'open'`.
- Eine Campaign verlangt eine passende `strategy_assignment`-Zeile mit `strategy_type = 'swing'` für dasselbe Wertpapier (erzwungen durch zwei `BEFORE INSERT`-/`BEFORE UPDATE`-Trigger).
- Eine verknüpfte `swing_campaign_event.transaction_id` muss zu einer Transaktion des eigenen Wertpapiers der Campaign gehören (erzwungen durch zwei Trigger) und ist eindeutig (`idx_swing_campaign_event_transaction`) — das ist der Idempotenzschlüssel des folgenden Abgleichs.

**Parqet-Import → Campaign-Abgleich** (`parqet_import.reconcile_campaign_transactions`, läuft innerhalb der Import-Transaktion; eigenständig: `Import-ParqetTransactions.py --reconcile-campaigns`):

- Je offener Campaign wird jeder noch nicht verknüpfte BUY/SELL mit Datum am/nach `opened_at` (Datumsvergleich) zu einem transaktionsverknüpften Event: SELL → `manual_reduction`, BUY → `add`; exakte Menge/Preis/Währung aus der Transaktion, Quelle `parqet_reconciliation`. Trades vor `opened_at` gehören zur Baseline und werden übersprungen.
- Aus einem Broker-Trade wird nie ein TP1/TP2-/Stop-Grund abgeleitet; `original_quantity` wird nie geändert; Campaigns werden nie automatisch geschlossen (ein vollständiger Ausstieg wird mit `FULL_EXIT_CAMPAIGN_STILL_OPEN` markiert).
- Same-Day-Regel (`opened_at` hat nur Datumsauflösung): Ein Trade am Kalendertag von `opened_at` wird nur abgeglichen, wenn er der **einzige** unverknüpfte Trade dieses Tages ist und die Mengenhistorie belegt, dass er nach der Baseline liegt (Baseline ± Trade == Position, z. B. 120 − 30 == 90). Mehrere Trades am selben Tag oder kein exakter Mengenbeleg → `MANUAL_REVIEW_REQUIRED` (`SAME_DAY_TRADES_AMBIGUOUS` / `SAME_DAY_TRADE_NOT_PROVEN`), es wird nichts geschrieben.
- Alles-oder-nichts je Campaign: Events werden nur geschrieben, wenn die Campaign danach exakt aufgeht (Delta 0). Sonst `MANUAL_REVIEW_REQUIRED` (Transfers, Oversell, Split nach dem Trade, unverknüpfte manuelle Events, die den Trade bereits abdecken) oder `AMBIGUOUS` (mehrere offene Campaigns) — es wird nichts geschrieben und die Engine bleibt blockiert.
- Ein erneuter Lauf ist idempotent (bereits verknüpfte Transaktionen werden übersprungen). Der `lifecycle`-Report des Imports enthält `reconciled_transactions`, `skipped_pre_campaign`, `skipped_already_processed`, `ambiguous`, `manual_review_required`, `position_quantity`, `campaign_expected_quantity`, `delta_after_reconciliation`.
- `effective_to IS NULL OR effective_to >= effective_from` auf `strategy_assignment`; keine zwei `strategy_assignment`-Intervalle desselben Wertpapiers dürfen sich überlappen (erzwungen durch zwei Trigger).

### 3.9 Betriebliche Werte der Datenquellen/Requests

| Skript | Timeout | Retries | Delay/Cache | Sonstiges |
|---|---|---|---|---|
| `Backfill-TradingFundamentalsSEC.py` | 10s | 1 | 0.20s zwischen echten Requests; 6-h-JSON-Cache | höchstens 6 Jahres- / 12 Quartalsperioden werden behalten |
| `Backfill-TradingMarketData.py` | 10s | 2 | 0.75s; inkrementelle 5-Tage-Überlappung | Historienbereich 2 Jahre, 1-Tages-Intervall |
| `Discover-TradingSECIdentifiers.py` | 20s | — | 0.20s | — |
| `Resolve-TradingSecurities.py` (OpenFIGI) | 10s | 3 | 0.5s (kein API-Key) | Batch-Größe 5; 429-Backoff bis zu 65s |
| `Backfill-TradingFXRatesECB.py` | 30s | — | — | — |
| `Research-TradingFundamentals.py` | — | — | — | Differenztoleranz 0.1 % relativ / 1.0 absolut; Bilanztoleranz `max(1% of assets, 1,000,000)` |

### 3.10 Portfolio Action Planner (`portfolio_action_planner.py`, v0.2.0)

Reine, rein lesende Simulation, die als `portfolio_action_plan` an `run_trading_orchestrator` angehängt wird: kein DB-Schreibzugriff, keine Order, kein LLM, keine Strategieänderung. **PROMOTE ist kein BUY** — es ist nur die Eingabe der Entry-Bewertung. Abläufe: `SELL/TRIM → POST-ACTION CAPITAL → ENTRY PLAN` und `PROMOTE → ENTRY-EVALUATION → RANKING → POSITION SIZING → PLANNED ENTRY`.

| Punkt | Regel |
|---|---|
| Post-Action-Kapital | Brutto-Erlös = min(Aktionsmenge, gehaltene Menge) × aktueller EUR-Kurs für jedes SELL/TRIM; ADD/BUY-Empfehlungen der bestehenden Engine verbrauchen zuerst Cash/Swing-Kapazität. Der Positionswert sinkt um den Brutto-Erlös, **Cash steigt um den Netto-Erlös** (`proceeds_basis: net_conservative`). Gewichte sind Prozent des investierten Marktwerts (Cash steht wie im `PortfolioContext` nicht im Nenner), die Steuer verschiebt sie daher nicht |
| Steuerschätzung | `TaxConfig.combined_rate` (26.375 %) gilt **nur für den realisierten Gewinn**: `gain = gross − qty × positions.avg_cost`; ein Verlust ergibt Steuer 0 (nie negativ, nie zusätzliches Cash). Kostenbasis ist der **Durchschnitts-Einstand, nicht FIFO-Lots** (`tax_estimate_quality = estimated_from_average_cost`; FIFO ist nicht implementiert). Geplant wird mit der **konservativen** Sicht (keine Verlustverrechnung, `net_proceeds_total_conservative`). Die indikative Sicht verrechnet Verluste der geplanten SELL/TRIM-Aktionen mit deren Gewinnen (Steuer anteilig auf die gewinnbringenden Aktionen verteilt; nur Anzeige, nie für die Planung verwendet). Verlusttopf, Freistellungsauftrag und die in diesem Jahr realisierten Ergebnisse sind unbekannt (`unknown_inputs`) und werden nie angenommen. Eine fehlende/Nicht-EUR-Kostenbasis besteuert den gesamten Erlös als Obergrenze (`cost_basis_unavailable_upper_bound`); ohne die Eingabe `position_cost_basis` erfolgt keine Schätzung und die Erlöse bleiben brutto (`not_estimated`) |
| Entry-Status (nur PROMOTE-Einträge) | Wird in dieser Reihenfolge ausgewertet: `BLOCKED_EXISTING_POSITION` → `BLOCKED_EXISTING_CAMPAIGN` → `BLOCKED_BY_DATA` (Portfolio-Zustand, technische Daten, EUR-Kurs, FX) → `WAIT_FOR_TRIGGER` (`momentum_score` fehlt oder liegt unter `candidate_decision.BUY_SCORE_THRESHOLD` = 20, oder Trendfilter verloren) → Ergebnis von `entry_sizing`: `BLOCKED_BY_ALLOCATION` (Swing-Maximum, Cash-Reserve, weniger als eine Aktie) / `BLOCKED_BY_CONCENTRATION` (Initialgewicht 10 % / Wertpapiergewicht 20 %) → `ENTRY_READY` |
| Ranking (nur `ENTRY_READY`) | `entry_score = momentum_score × confidence` (bestehender Analytics-Score × bestehender Datenqualitätswert aus `decision_engine._confidence`); Gleichstand: höhere Watchlist-Priorität, dann niedrigere `security_id` |
| Sizing | `entry_sizing.evaluate_entry_recommendation` auf dem simulierten Zustand, mit einer In-Memory-Sicht "wenn promoted" auf Swing (nichts wird persistiert): min(Initialgewicht-Obergrenze, Wertpapier-Obergrenze, Swing-Maximum-Obergrenze, Cash über der Reserve), ganze Stück abgerundet |
| Allokation | Strikte Rangfolge, Zustand nach jedem geplanten Entry aktualisiert. Stoppt (verbleibende Kandidaten zurückgestellt) mit `STOPPED_SWING_MAX_REACHED`, `STOPPED_CASH_RESERVE_REACHED` oder `STOPPED_CAPITAL_EXHAUSTED`; ein niedrigerer Rang springt nie in übrig gebliebene Reste. Größen-/Konzentrationsfehler stellen nur diesen einen Kandidaten zurück |
| Nicht modelliert | Gebühren; FIFO-Steuer-Lots, Verlusttopf, Freistellungsauftrag; Sektor-/Branchenexposition (keine Daten für gehaltene Positionen, daher gelten nur die Einzeltitel-Limits: Warnung `CONCENTRATION_SINGLE_SECURITY_LIMITS_ONLY`); Buying Power (solange sie nicht verfügbar ist, wird Cash verwendet) |

Ausgabeschlüssel: `status`, `simulation_only`, `orders_created`, `current_state`, `existing_position_actions`, `post_action_state`, `entry_candidates`, `planned_entries`, `deferred_entries`, `final_simulated_state`, `tax_estimate` (Parameter, Qualität, `gross_proceeds_total_eur`, `estimated_tax_total_conservative`, `estimated_tax_total_with_plan_offset`, `net_proceeds_total_conservative`, `net_proceeds_total_with_plan_offset`), `entry_summary`, `methodology`, `warnings`, `rendered_de`; jede SELL/TRIM-Aktion trägt `gross_proceeds_eur`, `estimated_cost_basis_eur`, `estimated_realized_gain_eur`, `estimated_tax_eur_conservative`/`_with_plan_offset` und `estimated_net_proceeds_eur_conservative`/`_with_plan_offset`. Ein Planner-Fehler ergibt einen `UNAVAILABLE`-Plan (`PORTFOLIO_ACTION_PLAN_ERROR:<type>`), nie einen fehlgeschlagenen Orchestrator-Lauf. Im MCP-Kompaktmodus (`detail=false`) wird `entry_candidates` auf `rank`/`symbol`/`entry_status`/`entry_score`/`reason_codes` projiziert.

### 3.11 Watchlist Candidate Discovery (`candidate_discovery.py`, `Discover-TradingCandidates.py`)

`DISCOVERY != PROMOTE != ENTRY_READY != ORDER` — die Ausgabe ist nur eine Vorschlagsliste; es wird nichts geschrieben (`read_only`, `watchlist_written: false`, `orders_created: false`), ein Entry-Plan wird nicht ausgelöst. Keine MCP-Methode führt die Discovery aus (das Tool-Timeout des MCP-Clients - 30 s im aktuellen Setup - ist kürzer als ein Lauf mit Marktdaten-Requests): Der Lauf ist die CLI (täglich geplant, siehe §2), und der MCP-Client liest den von ihr geschriebenen Report über das rein lesende `get_candidate_discovery` (siehe unten).

| Stufe | Regel |
|---|---|
| Universum | Versionierte statische Datei `tools/trading/universe/swing_large_cap_v1.json` (liquide Large Caps, Yahoo-Symbole, erwartete Kurswährung als Vorfilter-Hinweis); keine offizielle Indexzusammensetzung |
| Vorfilter (vor jedem Request) | `ALREADY_IN_PORTFOLIO` / `ALREADY_ON_WATCHLIST` (exaktes Yahoo-Symbol in `source_symbols`), `UNSUPPORTED_SECURITY_TYPE` (Universe-Flag), `UNSUPPORTED_CURRENCY` (der bestehende FX-Resolver kann die angegebene Währung nicht in EUR bewerten) |
| Nach dem Abruf | `UNSUPPORTED_SECURITY_TYPE` (Provider-Typ nicht EQUITY), `UNSUPPORTED_CURRENCY`, `PRICE_BELOW_MINIMUM` (< 5 EUR), `LOW_LIQUIDITY` (Median des täglichen Handelsvolumenwerts der letzten 60 Handelstage < 5 Mio. EUR), `LIQUIDITY_DATA_UNAVAILABLE` / `NO_MARKET_DATA` (unzureichende Daten) |
| Analyse | Unveränderte Engine auf einer In-Memory-Kopie des Produktionsschemas (die Produktion wird nur gelesen): `build_analysis_snapshot` (technische Qualität verlangt ≥ 200 Datenpunkte und frische Daten, SMA50/200, Performance, Momentum, EUR-Bewertung) und `evaluate_swing_promotion` (Trend-Gate) |
| Status | Promotion `DATA_INSUFFICIENT` → `DISCOVERY_DATA_INSUFFICIENT` (`INSUFFICIENT_HISTORY`, `MARKET_DATA_STALE`, `FX_RATE_UNAVAILABLE`, `MARKET_DATA_MISSING`); `PROMOTE` mit Momentum ≥ `BUY_SCORE_THRESHOLD` (20) → `DISCOVERY_READY` (`TREND_INTACT`, `MOMENTUM_ABOVE_THRESHOLD`), darunter → `DISCOVERY_WATCH` (`MOMENTUM_BELOW_THRESHOLD`); `KEEP_WATCHING` mit Kurs ≤ SMA200 → `DISCOVERY_REJECTED` (`PRICE_BELOW_SMA200`), sonst `DISCOVERY_WATCH` (`PRICE_BELOW_SMA50`, `SMA50_BELOW_SMA200`) |
| Score / Ranking | `discovery_score = momentum_score × decision_engine._confidence` (die `entry_score`-Formel des Planners); Reihenfolge: Status, Score (absteigend), Momentum (absteigend), Symbol (aufsteigend) |
| Nicht bewertet | Fundamentals, Bewertung und Events werden für neue Wertpapiere nicht abgerufen (`fundamentals_evaluated: false`); ihre Qualität zeigt die Sicht der Engine ohne gecachte Daten. Sektor/Korrelation, Marktkapitalisierung und jegliche LLM-Eingabe werden nicht verwendet |

**Report und MCP-Reader.** Die CLI schreibt `latest.json` nur nach einem vollständig erfolgreichen Lauf (ein Lauf, in dem kein Symbol Marktdaten geliefert hat, gilt als fehlgeschlagen) und ersetzt sie atomar; ein fehlgeschlagener/abgebrochener Lauf lässt den vorherigen Report unberührt. Schema v1: `metadata` (`schema_version`, `generated_at`, `evaluation_as_of`, `universe_name`, `universe_size`, `analyzed_count`, `excluded_count`, die vier Status-Zähler, `source` (Abruf-/Cache-Zähler), `deterministic`, `results_sha256`), `results` (gerankte Kandidaten, keine rohen Kursreihen) und `excluded` (Symbol, Reason-Codes). Die neue, rein lesende MCP-Methode `get_candidate_discovery(status, limit, detail, symbol)` (Tool-Anzahl 12 → 13) liest nur diese Datei: **Sie startet nie einen Discovery-Lauf, ruft keinen Provider auf und berührt weder die Datenbank noch die Watchlist**. Kein Report → `status: UNAVAILABLE`, `reason: NO_DISCOVERY_REPORT` (`REPORT_INVALID` bei einer beschädigten oder manipulierten Datei); ein alter Report wird mit `generated_at`, Alter und `report_stale` / `REPORT_STALE` zurückgegeben (Grenze: das bestehende `DataQualityConfig.market_data_max_age_days`, gemessen an `evaluation_as_of`). Der Kompaktmodus liefert Rang, Symbol, Name, discovery_score, Status und Reason-Codes (höchstens 150 Zeilen); Detail liefert alle gespeicherten Felder (höchstens 40 Zeilen); `status=EXCLUDED` listet die Vorfilter-Ausschlüsse; `symbol` erklärt einen einzelnen Titel. Der Lauf ist seit 2026-10-09 täglich geplant (Windows Scheduled Task `Trading-Candidate-Discovery`, siehe §2).

Ausgabeschlüssel: `status`, `read_only`, `watchlist_written`, `orders_created`, `evaluation_as_of`, `universe`, `universe_size`, `prefiltered_count`, `analyzed_count`, `discovery_ready_count`, `discovery_watch_count`, `insufficient_count`, `rejected_count`, `candidates` (Rang, security_id falls bekannt, Symbol, Name, Markt, Währung, Preise, `momentum_score`, `discovery_score`, Qualitäten, `status`, `reason_codes`), `excluded_candidates`, `methodology`, `warnings`, `rendered_de`.

---

### 3.12 Market Intelligence (`market_intelligence.py`, `intelligence_report.py`, `Collect-TradingMarketIntelligence.py`)

`NEWS != SIGNAL != BUY != SELL` — Ereignisse sind nur Kontext und Warnungen. Der Block ist rein lesend und deterministisch (`read_only`, `orders_created: false`, `signals_changed: false`); kein Entscheidungsmodul (`trading_orchestrator`, `portfolio_action_planner`, `swing_promotion`, `decision_engine`, `candidate_decision`, `entry_sizing`, `candidate_discovery`, `analysis_engine`, `swing_lifecycle`, `strategy_suggestion`) importiert ihn (durch einen Test geprüft). Kein LLM erzeugt oder klassifiziert Ereignisse, und kein Ereignis wird erfunden: Jedes Ereignis trägt die `source_url` eines abgerufenen Eintrags. Ablauf: `sources -> Collect CLI -> report -> MCP reader -> MCP client`; der MCP-Reader ruft nie ab, scannt nie und öffnet nie die Datenbank.

| Aspekt | Regel |
|---|---|
| Quellen | 10 offizielle Feeds ohne Schlüssel (`ECB_PRESS`, `ECB_STATS`, `FED_MONETARY`, `FED_PRESS`, `BLS_CPI`, `BLS_EMPLOYMENT`, `BEA`, `DESTATIS`, `EIA_TODAY`, `EIA_PRESS`); SEC-8-K-Zeilen der Tabelle `events`; gecachte Yahoo-Schlagzeilen der Tabelle `news`; frische Yahoo-Schlagzeilen für `DISCOVERY_READY`-Kandidaten des Discovery-Reports. Eurostat nicht angebunden; `COMMODITIES` nicht implementiert |
| Fenster | `MarketIntelligenceConfig.lookback_days` = 7 auf `published_at` (Einträge mit zukünftigem Datum werden ausgeschlossen) |
| Event-ID | `sha256(source_type \| canonical_url_or_accession)[:16]`; gleiche ID → ein Ereignis (Symbole, Sektoren, Gründe vereinigt, frühester Zeitpunkt behalten, wichtigere Klassifikation behalten) |
| Kategorien | MACRO, MONETARY_POLICY, INFLATION, LABOR_MARKET, ECONOMIC_GROWTH, GEOPOLITICS, ENERGY, SECTOR, COMPANY, EARNINGS, GUIDANCE, REGULATORY, M_AND_A (feste Regeltabelle je Feed; SEC-Item-Tabelle; geordnete Schlagzeilen-Schlüsselwortregeln, der erste Treffer gewinnt, Stock-Pick-Artikel zuerst) |
| Wichtigkeit | feste Tabelle. HIGH: EZB-/Fed-Zinsentscheid, SEC-Übernahme/Kontrollwechsel/Delisting/Cybersecurity/Non-Reliance, Pressemitteilungsdienste der Unternehmen oder eine Schlüsselwortklasse, deren Schlagzeile das verknüpfte Unternehmen mit Namen nennt. Ein bloßer Ticker trägt nie HIGH (`TICKER_ONLY_MATCH_CAPPED_AT_MEDIUM`); Sekundär-Schlagzeilen nur mit Schlüsselwörtern werden gedeckelt (`SECONDARY_SOURCE_CAPPED_AT_MEDIUM`, `SYMBOL_LINK_NOT_CONFIRMED_BY_HEADLINE_CAPPED_AT_MEDIUM`). Geplante Makro-Veröffentlichungen sind MEDIUM (`MACRO_RELEASE_SURPRISE_NOT_ASSESSED`); GEOPOLITICS höchstens MEDIUM |
| Impact | `UNKNOWN`, außer die Struktur einer Primärmeldung nennt eine belastende Tatsache (`delisting_notice`, 8-K 1.05, 8-K 4.02 → `NEGATIVE`, `impact_basis: STRUCTURE_OF_PRIMARY_FILING`); sonst `NOT_READ_FROM_HEADLINE` / `NOT_ASSESSABLE_WITHOUT_CONSENSUS`. `POSITIVE`/`MIXED`/`NEUTRAL` existieren im Vokabular, aber keine v1-Regel erzeugt sie |
| Zuordnung | `affected_portfolio_symbols` / `affected_watchlist_symbols` / `affected_discovery_symbols` nach Yahoo-Symbol; explizite Sektor-Ereignisse (Schlüsselwörter, EIA) erreichen alle Symbole des Sektors über `sector_map_v1.json` (`SECTOR_SCOPED_EVENT`); Makro-Ereignisse tragen `portfolio_region_exposure` (Region aus dem Yahoo-Suffix der gehaltenen Symbole) |
| Sortierung | Wichtigkeit, dann Portfolio-Relevanz, dann Aktualität (neueste zuerst), dann `event_id` |

**Report und MCP-Reader.** `latest.json` wird nur geschrieben, wenn mindestens ein Feed geantwortet hat; Schema v1: `metadata` (`schema_version`, `generated_at`, `evaluation_as_of`, `lookback_since`, `source_counts`, `event_count`, `high/medium/low_count`, `portfolio/watchlist/discovery_relevant_count`, `macro_event_count`, `collection` (Status je Feed), `warnings`, `deterministic`, `events_sha256`, `methodology`) und `events` (kompaktes JSON, kein Artikeltext; Zusammenfassungen ≤ 200 Zeichen). Das Schreiben ist atomar (Temp-Datei → erneut lesen und validieren → `os.replace`, History-Kopie zuerst); ein fehlgeschlagener oder ungültiger Lauf lässt den vorherigen Report unberührt. Die rein lesende MCP-Methode `get_market_intelligence(scope, category, importance, symbol, sector, limit, detail)` (Tool-Anzahl 13 → 14) liest nur diese Datei. `scope`: `ALL`, `PORTFOLIO`, `WATCHLIST`, `DISCOVERY`, `MACRO`; `symbol` akzeptiert Basissymbole (`SAP` passt auf `SAP.DE`). Kein Report → `UNAVAILABLE` / `NO_MARKET_INTELLIGENCE_REPORT` (`REPORT_INVALID` bei einer beschädigten oder manipulierten Datei); ein Report, der älter als `report_max_age_hours` ist, wird mit Alter und `REPORT_STALE` zurückgegeben. **Kompakt** (Standard, höchstens 50 Ereignisse) ist schlank: kurze Frische (`status`, `generated_at`, `age_hours`, `evaluation_as_of`), der aktive Filter, die Wichtigkeitszähler *aller* passenden Ereignisse, `matching`/`returned`/`truncated` und die Ereignisse (`published_at`, `importance`, `category`, vollständige `headline`, `source`, `impact`, bis zu drei `reason_codes` ohne den generischen Marker `CLASSIFIED_BY_HEADLINE_KEYWORD`; mit `symbol` zusätzlich `link`, sonst die betroffenen Symbole (höchstens 6), Sektoren und Regionen). `link` wird nur aus der gespeicherten Klassifikation abgeleitet: `DIRECT` (SEC-Meldung, oder die Schlagzeile nennt das Unternehmen und das Ereignis betrifft dieses eine Symbol), `SHARED` (Schlagzeile nennt ein Unternehmen, das Ereignis ist aber mehreren Symbolen zugeordnet), `SECTOR` (`SECTOR_SCOPED_EVENT`), `LOOSE`. Kein `rendered_de`, keine Quellenstatistik, kein Collection-Log, keine Notizen oder Methodik; ein `hint` erscheint nur bei einem veralteten Report oder einer gekürzten Liste. **Detail** (höchstens 25 Ereignisse) ist vollständig und unverändert. Die Collector-CLI gibt ihre Tabelle weiterhin aus der breiten Kompaktantwort aus (`slim=False`). Die serialisierte Antwort wird unter 34,000 Zeichen gehalten (der MCP-Server lehnt Ergebnisse über 40,000 ab), indem die unwichtigsten Ereignisse entfallen (`RESPONSE_TRIMMED_TO_SIZE_BUDGET`). Der Lauf ist seit 2026-10-09 täglich geplant (Windows Scheduled Task `Trading-Market-Intelligence`, 08:15 und 17:45, siehe §2).

**Bekannte Grenzen (v1).** Yahoo ordnet Schlagzeilen Symbolen lose zu (Marktberichte, Ticker-Homonyme wie "ACME Supply"); die Stichwort-Klassifikation hat `confidence: LOW`; die meisten Ereignisse haben `impact: UNKNOWN`; der Sektorbezug ist eine statische, versionierte Zuordnung.

### 3.13 Unified Opportunity View (`opportunity_view.py`)

`DISCOVERY + MARKET INTELLIGENCE + PORTFOLIO/PLANNER CONTEXT -> one opportunity output`, rein lesend und deterministisch (`read_only`, `orders_created: false`, `signals_changed: false`). Das Modul führt bestehende Ergebnisse zusammen; es berechnet **keinen** Score, Rang, keine Größe und keine Empfehlung, und es gibt **keinen kombinierten Score** (kein "Score + News-Bonus"). Die Entscheidungsmodule und beide Report-Module importieren es nicht (durch einen Test geprüft). Keine neue Tabelle und keine neue Persistenzschicht: Die MCP-Methode liest das Orchestrator-Ergebnis (übliche lesende Verbindung) und die beiden Report-Dateien.

| Aspekt | Regel |
|---|---|
| Eingaben | Orchestrator-Primitive (`promotion_results`, `portfolio_action_plan.entry_candidates/planned_entries/deferred_entries/current_state/post_action_state/final_simulated_state`, `existing_position_results`), `discovery_report.read_latest_report`, `intelligence_report.read_latest_report`, `security_id -> Yahoo symbol` aus `source_symbols` |
| Kandidaten | A) PROMOTE-Kandidaten des Planners (alle `entry_status`-Werte); B) `DISCOVERY_READY`-Ergebnisse des Discovery-Reports. `source`: `WATCHLIST`, `DISCOVERY`, `BOTH` (ein Symbol des Reports, das die Engine bereits kennt, z. B. nach dem Schreiben des Reports hinzugefügt; Warnung `DISCOVERY_SYMBOL_ALREADY_KNOWN`). `DISCOVERY_WATCH`, `KEEP_WATCHING` und rein gehaltene Symbole sind keine Opportunities (gehaltene Positionen erscheinen unter `portfolio_context.positions`) |
| Gruppe / Reihenfolge | `ENTRY_READY` > `WAIT_FOR_TRIGGER` > `DISCOVERY_READY` > `OTHER`; innerhalb einer Gruppe: Engine-Rang, `entry_score` (absteigend), Discovery-Rang, Watchlist-Priorität (absteigend), `security_id`, Symbol — nie News |
| Felder | symbol, name, source, group, discovery_rank/score/status, promotion_status, entry_status/rank/score, momentum_score, confidence, current_price_eur, technische/fundamentale/event_risk-Qualität (`not_evaluated` bei reinen Discovery-Einträgen), plan_status (`PLANNED`/`DEFERRED`/`NOT_PLANNED`/`NOT_APPLICABLE` + Planner-Grund + geplantes Kapital/geplante Menge, wie vom Planner berechnet), `capital_fit`, `market_intelligence_summary` |
| News-Verknüpfung | je Ereignis und Symbol: `DIRECT` (SEC-Meldung, oder die Schlagzeile nennt das Unternehmen mit dem juristischen Namen / einem unterscheidenden Namensbestandteil / einem Ticker in Klammern wie `(ABCD)`; ein bloßer Ticker reicht nicht), `SECTOR` (das Ereignis hat `SECTOR_SCOPED_EVENT`), `LOOSE` (der Provider hat es zugeordnet, ohne dass die Schlagzeile das Unternehmen nennt) |
| News-Status | `NEWS_HIGH_ATTENTION`: mindestens ein DIRECT-HIGH-Ereignis; `NEWS_ATTENTION`: ein DIRECT-MEDIUM-Ereignis oder ein HIGH-Ereignis, das nur SECTOR/LOOSE ist; `NEWS_CLEAR`: nur LOW, lose verknüpftes MEDIUM oder keines; `NEWS_UNAVAILABLE`: kein gültiger MI-Report. Zähler (alle/direkt), wichtigste Nicht-LOW-Ereignisse (1 kompakt, 6 Detail), `status_reasons` |
| Kapitalkontext | `portfolio_context`: aktueller Cash/verfügbarer Betrag/Reserve/Swing-Anteil und -Korridor, `after_actions` (Cash nach geplanten Verkäufen, konservativer Netto-Erlös, Steuer-/Gebühren-Flags), `after_plan` (geplante Entries, Summe, `remaining_buying_capacity_eur`, verbleibende Swing-Kapazität), geplante und zurückgestellte Entries. `capital_fit` (nur Discovery-Zeilen): Preis einer Aktie gegenüber verfügbarem Cash nach den Aktionen / nach dem Plan (`FITS_AFTER_PLAN`, `COMMITTED_BY_PLANNED_ENTRIES`, `DOES_NOT_FIT`) — informativ, kein Sizing, keine Order |
| Makro / Aufmerksamkeit | `market_context.macro_context`: HIGH/MEDIUM-Ereignisse je Makro-Kategorie (Geldpolitik, Inflation, Arbeitsmarkt, Wachstum, Geopolitik, Energie, Makro) mit Anzahl offizieller Quellen; `high_attention`: direkte HIGH-Ereignisse von Kandidaten und gehaltenen Positionen |
| Frische | `sources.discovery` / `sources.market_intelligence`: `status`, `generated_at`, `evaluation_as_of`, `age_hours`, `stale` (Discovery: `market_data_max_age_days`; MI: `report_max_age_hours`). Status `AVAILABLE` (alle drei vorhanden), `PARTIAL` (ein Report oder die Engine fehlt — der Rest wird gezeigt, fehlende News → `NEWS_UNAVAILABLE`), `UNAVAILABLE` (nichts vorhanden). Ein veralteter Report wird angezeigt und gekennzeichnet, nie verborgen |

**MCP-Reader.** `get_opportunity_view(source="ALL", status="ALL", news_status=None, symbol=None, limit=30, detail=False)` (Tool-Anzahl 14 → 15; `trading_sqlite.py` 1.7.0): `source` ALL/WATCHLIST/DISCOVERY/BOTH, `status` ALL/ENTRY_READY/WAIT_FOR_TRIGGER/DISCOVERY_READY/OTHER, `news_status` ein `NEWS_*`-Wert oder `RELEVANT` (Attention oder High) / `NOT_HIGH`, `symbol` eines oder eine kommagetrennte Liste (Basissymbole passen auf Listings; filtert auch die angezeigten gehaltenen Positionen). Die **kompakte** Antwort (Standard, höchstens 40 Zeilen, Standard-`limit` 30, damit z. B. alle 12 `ENTRY_READY`-Zeilen in einem Aufruf ankommen) enthält nur: Status und Frische beider Reports, einen minimalen Kapitalrahmen (Cash, Cash nach den geplanten Aktionen, konservativer Netto-Erlös, Summe der geplanten Entries, verbleibende Kaufkapazität, Swing-Anteil und -Korridor), die passenden Zeilen ohne leere Felder (Entry-Status/-Rang/-Score, Momentum, Plan-Status mit geplantem Kapital, der eigene Sizing-Vorschlag der Engine `proposed_capital_eur`/`proposed_quantity` unverändert durchgereicht, Stopp-Grund, Qualität, News-Status mit Zählern und dem einen wichtigsten Ereignis, Capital Fit für Discovery-Zeilen) und die Warn-Codes; mit `symbol` nur die angefragten Symbole und die so benannten gehaltenen Positionen. Keine Positionsliste, Discovery-Liste, kein Makro-Block, keine globale High-Attention-Liste, keine Quellen-Metadaten, Notizen oder `rendered_de`. **Detail** (höchstens 10 Zeilen) behält jeden Block inkl. Methodik und der deutschen Tabellen in `rendered_de`. Die serialisierte Antwort wird unter 34,000 Zeichen gehalten (der MCP-Server lehnt Ergebnisse über 40,000 ab): Kompakt lässt die letzten Zeilen weg, Detail reduziert zuerst Nebenabschnitte (Methodik, Positions-Ereignisse, Makro-Detail, Discovery-Liste) und danach die letzten Zeilen (`RESPONSE_TRIMMED_TO_SIZE_BUDGET`). Sie ruft nie ab, scannt nie, startet nie einen Collector oder Discovery-Lauf und schreibt nie.

**Bekannte Grenzen (v1).** Das Unternehmen wird nur über Namen oder einen geklammerten Ticker erkannt (eine Kurzform ohne Namensbestandteil ist nicht DIRECT); vom Provider zugeordnete Schlagzeilen können lose sein; viele `NEWS_ATTENTION`-Zeilen sind Analysten-Kursziele (COMPANY/MEDIUM); `capital_fit` ignoriert Konzentrations- und Swing-Maximum-Regeln, weil für Discovery-Kandidaten kein Entry-Plan erstellt wird.

---

## 4. Fundamentals / Datenqualität

- **Vokabular des Verifikationsstatus** (`Research-TradingFundamentals.py`, der LLM-Recherchepfad): `verified_direct` (Primärquelle), `verified_derived` (aus verifizierten Werten über eine dokumentierte Formel berechnet), `verified_secondary` (≥2 unabhängige Sekundärquellen oder eine starke + Plausibilitätsprüfung), `not_verified` (verworfen, als NULL gespeichert — nie geraten).
- **Strukturelle Validierung** (unabhängig vom Obigen, auf jede akzeptierte Periode angewendet): `fiscal_year` in [2000, 2100]; `currency` ein 3-Zeichen-Code; `revenue > 0`; `gross_profit <= revenue`; `total_assets ≈ total_liabilities + total_equity` innerhalb von `max(1% of assets, 1,000,000)`; Free-Cashflow-Prüfung `|FCF - (OCF - capex)| <= max(1% of OCF, 1000)`.
- Die **Tabelle `fundamentals`** speichert nur Rohzahlen (revenue, gross_profit, operating_income, ebit, ebitda, net_income, eps_basic/diluted, operating_cash_flow, capex, free_cash_flow, cash, total_debt, total_assets/liabilities/equity, shares_outstanding) — "no unnecessarily computed ratios" (Schema-Kommentar).
- **Die Quellenpriorität ist kein einzelnes globales Ranking** — sie unterscheidet sich je Pipeline: `Backfill-TradingFundamentalsSEC.py` verwendet ausschließlich SEC-EDGAR-XBRL; `Backfill-TradingFundamentalsIR.py` ist ein fester, handkuratierter Satz von 6 Unternehmen aus deren IR-Seiten; `Research-TradingFundamentals.py` (laut README das kanonische/allgemeine Tool) lässt ein LLM unter dem obigen `verified_*`-Vokabular zwischen primären und bestätigten sekundären Quellen wählen und speichert die Quelle nur dann als `"Company IR"`, wenn jedes verwendete Feld primär ist, sonst als `"Web"`.
- **Ausnahme für `asset_type` bei Fundamentals**: `{"etf", "fund"}` wird sowohl im Snapshot je Wertpapier in `analysis_engine.py` als auch in der Klassifikation von `Audit-TradingDataQuality.py` als Fundamentals-nicht-anwendbar behandelt und als `strategy_suggestion.LONG_TERM_ASSET_TYPES` wiederverwendet.
- **`Audit-TradingDataQuality.py`** klassifiziert jedes Wertpapier als `MISSING` (0 Perioden), `STALE` (hat eine Datenlücke UND `age_days > --stale-days`, Standard 180), `PARTIAL` (hat eine Lücke, nicht veraltet), `COMPLETE` (keines der `CORE_FIELDS = [revenue, net_income, operating_cash_flow, cash]` fehlt und FCF ist ableitbar, wenn OCF+capex vorhanden sind) oder `NOT_APPLICABLE` für ETF-/Fonds-Asset-Typen. Rein lesend; schreibt nie.

---

## 5. Trading-MCP-Tool `trading_sqlite`

> **Aktueller Zugangsweg:** Der Agent (über einen MCP-Client) nutzt dieses Tool über den Trading-MCP-Server (`C:\tools\trading\mcp\server.py`; Repo-Quelle `mcp-tools/`), der jede öffentliche Methode der Klasse `Tools` in `trading_sqlite.py` als MCP-Tool bereitstellt — derzeit 15. Parqet-Importe laufen über `Import-ParqetTransactions.py` (der MCP-Server hat keine Upload-Methoden).

**Deploy (aktuell):** `deploy\Deploy-TradingAgent.ps1` (`-Action Check` / `Deploy` / `Rollback`, siehe README) kopiert nach einem Datei- und Datenbank-Backup nur die in `deploy/deploy-manifest.json` aufgeführten Dateien nach `C:\tools\trading\app\`, `mcp\` und `scheduler\`, vergleicht Hashes und führt einen MCP-Smoke-Test aus (erwartet 15 Tools). Danach den MCP-Server-Prozess neu starten (im aktuellen Setup startet der MCP-Client ihn automatisch neu, wenn er beendet wird).

**Aktuelles Inventar der öffentlichen Methoden** (Version `1.7.0`, 15 öffentliche Methoden, jetzt `mcp-tools/trading_sqlite.py`), eingeteilt in rein lesend vs. schreibfähig:

| Methode | Rein lesend? | Hinweise |
|---|---|---|
| `sql_execute(sql)` | **Nein — uneingeschränkt** | Die Tool-Beschreibung nennt wörtlich "Direct unrestricted SQLite access"; öffnet eine normale Lese-/Schreib-Verbindung (`isolation_level="DEFERRED"`); mehrere Statements, eine Transaktion, der gesamte Batch wird bei jedem Fehler zurückgerollt. `Valves`: `database_path` (Standard `C:\tools\trading\data\trading.db`), `timeout_seconds=1.0`, `busy_timeout_ms=1000`, `max_rows_per_statement=500`. |
| `database_tables()` | Ja | Listet Tabellen auf |
| `database_schema()` | Ja | Vollständiger Schema-Dump |
| `table_info(table)` | Ja | Spalteninformationen je Tabelle |
| `database_status()` | Ja | Zusammenfassung Zähler/Integrität |
| `rank_watchlist()` | Ja | Lädt `trading_analytics.py` über den gecachten Pfad `_load_runtime_trading_module()` (siehe Hinweis unten); verwendet seit 2026-09-27 dieselbe lesende Verbindung wie die übrigen Analytics-Wrapper (vorher eine Lese-/Schreib-Verbindung) |
| `run_trading_orchestrator(as_of=None)` | Ja | Lädt `trading_orchestrator.py` (und seine Abhängigkeitskette) dynamisch; seit v0.2.0 trägt das Ergebnis zusätzlich den reinen Simulations-`portfolio_action_plan` (§3.10) |
| `evaluate_swing_candidate(security_id)` / `evaluate_swing_candidates()` | Ja | Lädt `swing_promotion.py` dynamisch |
| `approve_swing_promotion(...)` | **Nein — schreibt** `strategy_assignment` + `candidate_promotion` | Verlangt ein noch aktuelles Plan-Token aus `evaluate_swing_candidate` |
| `suggest_strategy_assignments(as_of=None)` | Ja | Lädt `strategy_suggestion.py` dynamisch; ruft es unverändert auf (siehe §3.4) |
| `evaluate_watchlist_candidates(as_of=None)` | Ja | Lädt `candidate_decision.py` dynamisch; ruft es unverändert auf (siehe §3.3) |
| `get_candidate_discovery(status="ALL", limit=20, detail=False, symbol=None)` | Ja — liest eine Datei | Version `1.5.0`. Lädt `discovery_report.py` dynamisch und liest `<database directory>\candidate-discovery\latest.json` (Valve `discovery_report_dir`); **kein Discovery-Lauf, kein Provider-Aufruf, kein Datenbankzugriff** (§3.11) |
| `get_market_intelligence(scope="ALL", category=None, importance=None, symbol=None, sector=None, limit=20, detail=False)` | Ja — liest eine Datei | Version `1.6.0`. Lädt `intelligence_report.py` dynamisch und liest `<database directory>\market-intelligence\latest.json` (Valve `market_intelligence_report_dir`); **kein Collection-Lauf, kein Web-Request, kein Datenbankzugriff** (§3.12) |
| `get_opportunity_view(source="ALL", status="ALL", news_status=None, symbol=None, limit=30, detail=False)` | Ja — Orchestrator auf der lesenden Verbindung + zwei Report-Dateien | Version `1.7.0`. Lädt `opportunity_view.py`, `trading_orchestrator.py`, `discovery_report.py`, `intelligence_report.py` dynamisch; **kein Web-Request, kein Collection-/Discovery-Lauf, kein Datenbank-Schreibzugriff, keine Order** (§3.13) |

Beide neuen Methoden folgen exakt demselben Muster wie `run_trading_orchestrator`/`evaluate_swing_candidates`: `_load_runtime_trading_module()` lädt das deployte Modul aus `C:\tools\trading\app\`, `_readonly_import_connection()` öffnet eine strikte `PRAGMA query_only=ON`-Verbindung.

Zur Laufzeit dynamisch aus `C:\tools\trading\app\` geladene Hilfsmodule: `trading_analytics.py`, `parqet_import.py`, `swing_lifecycle.py`, `analysis_contracts.py`, `trading_orchestrator.py`, `swing_promotion.py`, `strategy_suggestion.py`, `candidate_decision.py` (und ihre eigenen Importketten, z. B. `analysis_engine.py`, `decision_engine.py`, `portfolio_context.py`, `strategy_config.py`, `fx_resolver.py`, `capital_state.py`, `orchestrator_presentation.py`).

**Seit 2026-09-27** cached `_load_runtime_trading_module()` jedes geladene Modul je Klasse (d. h. je aktuell geladener `trading_sqlite.py`-Version), statt die Datei bei jedem Tool-Aufruf neu zu lesen und erneut per `exec_module` auszuführen. Eine Änderung an einer Runtime-Hilfsdatei unter `C:\tools\trading\app\` wirkt daher erst nach einem Neustart des Trading-MCP-Server-Prozesses (der Cache lebt je Prozess; im aktuellen Setup startet der MCP-Client den Server automatisch neu, wenn er beendet wird).

---

## 6. Produktionspfade

| Was | Pfad |
|---|---|
| Produktions-Trading-DB | `C:\tools\trading\data\trading.db` |
| Produktions-Trading-Tools | `C:\tools\trading\app\` (zugleich das Modulverzeichnis der MCP-Server-Kopie `C:\tools\trading\mcp\trading_sqlite.py`) |
| Candidate-Discovery-Report | `C:\tools\trading\data\candidate-discovery\latest.json` |
| Market-Intelligence-Report | `C:\tools\trading\data\market-intelligence\latest.json` (+ `market-intelligence-YYYYMMDD-HHMMSS.json`) |
| SEC-Fundamentals-Cache | `C:\tools\trading\data\sec-cache` |
| Fundamentals-Recherche-Prompts/Audit | `C:\tools\trading\data\fundamentals-audit\` (Prompts im Unterordner `prompts\`) |
| Dev-Repository (dieses Repo) | `C:\tools\src\Trading-Agent-Dev-clean` |

---

## 7. Datenbankschema — für die Architektur relevante Tabellen

| Tabelle | Zweck | Wichtige Constraints |
|---|---|---|
| `metadata` | Globale Key/Value-Einstellungen, inkl. des Schema-Versionsmarkers jedes Features | — |
| `data_sources` | Register externer Datenanbieter | `name` eindeutig |
| `security` | Zentrale Wertpapieridentität (symbol/ISIN/WKN/name/exchange/currency/country/`asset_type`) | `isin` eindeutig, sofern nicht null |
| `source_symbols` | Zuordnung Wertpapier → externer Ticker/CIK je Quelle | eindeutig `(security_id, source_id)` |
| `imports` | Historie der Importläufe (z. B. Parqet) | — |
| `transactions` | Vollständige, nur anhängende Portfolio-Transaktionshistorie — **wird nie umgeschrieben**, auch nicht durch eine Corporate Action | eindeutig `external_id`, sofern nicht null |
| `positions` | Aktueller Portfolio-Zustand, genau eine Zeile je Wertpapier, durch `parqet_import._rebuild_position` neu aufgebaut (nicht inkrementell gepatcht) | PK `security_id` |
| `watchlist` | Recherche-Liste für Swing-Kandidaten, genau eine Zeile je Wertpapier | PK `security_id` |
| `market_snapshot` | Letzter bekannter Kurs/Quote je Wertpapier | PK `security_id` |
| `market_data` | Tägliche OHLCV-Historie, Haupteingabe der technischen Analyse | eindeutig `(security_id, trade_date, source_id)` |
| `fundamentals` | Rohe jährliche/Quartals-/Halbjahres-/Neunmonats-Fundamentals | eindeutig `(security_id, period_end, period_type, source_id)` |
| `estimates`, `ratings`, `price_targets` | Rein zählende Diagnoseeingaben für `ValuationAnalysis.quality` in `analysis_engine.py` — **Schema existiert und wird gelesen, aber kein aktuelles Skript befüllt diese drei Tabellen** (keine kostenlose/bereits verfügbare Quelle; siehe bekannte Inkonsistenz #4) | — |
| `events`, `news` | Rein zählende Diagnoseeingaben für `EventRiskAnalysis.quality` — **seit 2026-09-28 befüllt** durch `Backfill-TradingEventsNews.py` (SEC-EDGAR-8-K/6-K-Meldungen für `events`, Yahoo-Finance-Suche für `news`; siehe §2 und bekannte Inkonsistenz #4) | — |
| `strategy_assignment` | Strategie je Wertpapier als Zeitintervall (`long_term`/`swing`/`tactical`/`unknown`) | keine überlappenden Intervalle je Wertpapier (per Trigger erzwungen) |
| `candidate_promotion` | Audit-Trail der Promotion-Bewertungen/-Freigaben | — |
| `fx_rates` | Tägliche FX-Kurse nach EZB-Konvention, nur EUR-basiert | eindeutig `(rate_date, base_currency, quote_currency, source)`; `base_currency` muss `'EUR'` sein |
| `swing_campaign` | Explizite Identität einer Swing-Campaign (eine offene je Wertpapier) | partieller Unique-Index auf `status='open'`; FK auf eine `swing`-`strategy_assignment` |
| `swing_campaign_event` | Lifecycle-Events: manuell erfasst, plus neutrale transaktionsverknüpfte `add`/`manual_reduction` aus dem Parqet-Abgleich (nie TP-/Stop-Gründe) | `event_type`-CHECK-Liste; verknüpfte Transaktion muss zum Wertpapier der Campaign passen; eindeutig `transaction_id` |
| `portfolio_capital_state` | Erfasste Snapshots von Cash/Buying Power (`Migrate-TradingCapitalState.py`) | `base_currency` muss ein 3-Buchstaben-Code sein |
| `corporate_action` | Nachvollziehbare Aktiensplits (`Migrate-TradingCorporateActions.py`); Originaltransaktionen werden nie umgeschrieben — diese Tabelle wird *konsultiert*, nicht destruktiv angewendet | `action_type` derzeit nur `'STOCK_SPLIT'`; eindeutig `(security_id, action_type, effective_date, ratio_numerator, ratio_denominator)` |

**Views** (einfache reine SQL-Helfer, getrennt von und einfacher als der maßgebliche FX-bewusste Pfad `analysis_engine.build_portfolio_context`): `v_active_positions`, `v_watchlist`, `v_portfolio_market` (nur Bewertung in gleicher Währung — liefert bei jedem Währungskonflikt `NULL` als Marktwert, anders als die FX-normalisierende Engine).

**Aktueller Produktionsstand**: Alle aufgeführten Feature-Schemas sind angewendet (`schema_version=2.0`; jeder Feature-Versionsmarker `=1`, einschließlich `corporate_action_schema_version`). Erfasste Aktiensplits (`corporate_action`) wurden angewendet, und die betroffenen `positions`-Zeilen spiegeln die splitkorrigierten Mengen wider. `candidate_promotion` hat derzeit 0 Zeilen (Schema vorhanden, bisher nichts darüber promotet). `estimates`/`ratings`/`price_targets` sind in der Produktion weiterhin leer, passend zum Hinweis "unbenutzt/unbefüllt" in §7 oben; `events`/`news` werden durch `Backfill-TradingEventsNews.py` befüllt. `decisions`/`analysis_history` sind **nicht mehr Teil des Produktionsschemas** — beide Tabellen wurden gelöscht (siehe Inkonsistenz #5 unten), nicht nur geleert.

---

## 8. Tests / aktueller Qualitätsstand

- Candidate Decision: `tests/trading/test_candidate_decision.py` — 8 Tests (guter Swing-Kandidat → BUY, unzureichende Analytics-Daten → INSUFFICIENT_DATA, offene Campaign → WATCH, fehlende Zuordnung → DEFERRED, Guardrail über Maximum → DEFERRED, Nicht-Swing-Zuordnung → WATCH, Score unter Schwelle → WATCH, nicht verfügbare Allokation → DEFERRED).
- Strategy Suggestion: `tests/trading/test_strategy_suggestion.py` — 7 Tests (aktive Zuordnung übersprungen, hohe Swing-Eignung → swing, unzureichende Daten → unknown, offene Campaign → swing, keine Long-Term-Ableitung allein aus Momentum, ETF-asset_type → long_term, Score unter Schwelle → unknown).
- Portfolio Action Planner: `tests/trading/test_portfolio_action_planner.py` — 29 Tests (Post-Action-Kapital für SELL/TRIM/ADD/BUY, jeder Entry-Status, Determinismus des Rankings und Tie-Breaks, Sizing-Parität mit `entry_sizing`, Cash-Reserve, Swing-Maximum, sequentielle/strikt geordnete Allokation, Konsistenz des Endzustands, Unveränderlichkeit der Eingaben, Orchestrator-Integration und Isolation von Planner-Fehlern); `tests/trading/test_mcp_server_slimming.py` — 3 Tests (MCP-Kompaktmodus-Projektion; braucht das Trading-venv, sonst übersprungen).
- Corporate Actions: `tests/trading/test_corporate_actions.py` — 20 Tests (zeitliche Semantik des Split-Faktors, Schema-Verfügbarkeit, Validierung/Idempotenz, Integration des Positions-Neuaufbaus).
- Breitere Abdeckung gibt es für: Parqet-Import (`test_parqet_import.py`), Portfolio Context (`test_portfolio_context.py`), Decision Engine (`test_decision_engine.py`), Entry-/ADD-Empfehlungen (`test_swing_entry_recommendation.py`, `test_swing_add_recommendation.py`), Swing-TP-/Stop-/Runner-Aktionen (`test_swing_tp_actions.py`, `test_swing_hard_stop.py`, `test_runner_trend_exit.py`), Swing-Promotion (`test_swing_promotion.py`, `test_mcp_swing_promotion.py`), Campaign-Lifecycle (`test_swing_campaign_lifecycle.py`), Strategiezuordnung (`test_strategy_assignment.py`, `test_manage_strategy_assignments.py`), Capital State (`test_capital_state.py`), FX/Währung (`test_fx_currency.py`, `test_market_snapshot_currency_backfill.py`), Analyse-Verträge/-Engine (`test_analysis_contracts.py`, `test_analysis_engine.py`), Trading Analytics (`test_trading_analytics.py`), Orchestrator + Präsentation (`test_trading_orchestrator.py`, `test_orchestrator_presentation.py`).
- Market Intelligence: `tests/trading/test_market_intelligence.py` — 70 Tests (Normalisierung von Feed-/SEC-/Yahoo-Einträgen, Duplikat-Zusammenführung und stabile IDs, Kategorie- und Wichtigkeitstabellen, Deckel für Ticker-Homonyme, UNKNOWN-Impact und keine Richtung aus Schlagzeilen, Zuordnung zu Portfolio/Watchlist/Discovery/Sektor/Region, Fensterfilter, Determinismus und unveränderte Eingaben, atomarer Report mit History, fehlgeschlagener Lauf und fehlgeschlagenes Ersetzen behalten `latest.json`, manipulierter/beschädigter Report, veralteter Report, Filter scope/category/importance/symbol/sector, Kompakt/Detail, Größenschutz der Antwort, kein Netzwerk/keine DB/kein Schreiben im Reader, MCP-Tool-Oberfläche mit 14 Tools bei unveränderten 13 bestehenden, kein Entscheidungsmodul importiert die News-Module).
- Opportunity View: `tests/trading/test_opportunity_view.py` — 72 Tests (Discovery/MI vorhanden, fehlend, veraltet; Engine fehlt; Quellen WATCHLIST/DISCOVERY/BOTH; Gruppen ENTRY_READY/WAIT_FOR_TRIGGER/DISCOVERY_READY/OTHER und Reihenfolge; HIGH/MEDIUM/LOW-News und die vier `NEWS_*`-Status inkl. DIRECT-/SECTOR-/LOOSE-Verknüpfungen, Ticker-Homonyme und geklammerte Ticker; Scores, Ranking, Sizing und SELL/TRIM/HOLD mit und ohne News unverändert; Eingaben nicht verändert; Kompakt/Detail; Filter; Größenschutz; MCP-Reader mit einer lesenden Verbindung, die Schreibzugriffe ablehnt, kein Netzwerk/Subprozess/Collector, beschädigte/veraltete Reports; 15-Tool-Oberfläche mit unveränderten 14 früheren Tools; Entscheidungsmodule importieren die View nicht).
- **Letzter vollständiger Lauf: 601/601 bestanden, 0 Fehler** (2026-10-09, aus dem Repo-Root: `C:\tools\trading\.venv\Scripts\python.exe -m unittest discover -s tests/trading -p "test_*.py"`). Diese Zahl wird an keiner anderen Stelle dieses Dokuments neu abgeleitet — liefert ein späterer Lauf eine andere Anzahl, ist diese spätere Zahl maßgeblich, nicht diese.
- `tests/trading/` ist in diesem Repository versioniert (nur synthetische Testdaten); die Parität der Sprachfassungen der Dokumentation wird durch `tests/trading/test_docs_languages.py` und `tests/trading/test_query_docs.py` abgedeckt.

---

## 9. Bekannte Inkonsistenzen (beim Schreiben dieses Dokuments gefunden)

1. ~~**Duplizierung der TP1/TP2-Verkaufsanteile**: `strategy_config.SwingStrategyConfig.tp1_sell_fraction` (0.25) und `tp2_sell_fraction` (0.50) sind definiert, werden aber von `decision_engine.py` **nie gelesen**. Stattdessen codieren `decision_engine.py` und `swing_lifecycle.py` jeweils unabhängig die entsprechenden Literale fest (`floor(original_quantity * 0.25)` für TP1, `floor(original_quantity * 0.75)` kumuliert für TP2). Die Werte stimmen derzeit überein (0.25 und 0.25+0.50=0.75), doch eine Änderung der Config-Felder bliebe heute stillschweigend wirkungslos — ein künftiger Maintainer könnte vernünftigerweise erwarten, dass sie tragend sind.~~ — **gelöst 2026-09-27**: `decision_engine.py` (`_runner_eligibility_gaps`, die TP1/TP2-Trim-Zweige in `decide`) und `swing_lifecycle.py` (`derive_open_lifecycle`) leiten nun jede TP1/TP2-Mengenschwelle aus `config.swing.tp1_sell_fraction`/`tp2_sell_fraction` ab (`swing_lifecycle.py` nimmt einen optionalen Parameter `config: Optional[SwingStrategyConfig] = None` entgegen, der für bestehende Aufrufer standardmäßig `SwingStrategyConfig()` ist — keine Verhaltensänderung). Für die TP-Mengenlogik bleiben keine fest codierten `0.25`/`0.75`-Literale übrig.
2. ~~**`RiskTargetConfig` (risk_target_min/max = 0.60/0.70) ist definiert, wird aber nirgends verwendet** — der eigene Docstring sagt bereits "Phase 2 intentionally assigns no mathematical meaning", es ist also als beabsichtigt dokumentiert, kein Fehler, aber wissenswert, bevor man etwas darauf aufbaut.~~ — **gelöst 2026-09-27**: Entfernt: keine definierte Semantik, kein Laufzeit-Verbraucher; die bestehenden Portfolio-Allokations-Guardrails bleiben maßgeblich. `RiskTargetConfig` und `StrategyConfig.risk` existieren in `strategy_config.py` nicht mehr.
3. ~~**`SwingStrategyConfig.horizon_months_min/max` (3/6) und `remainder_management` ("momentum_guided") sind nur informativ** — kein Modul erzwingt einen zeitbasierten Ausstieg oder eine konkrete "momentum-guided"-Regel; der Runner wird derzeit ausschließlich durch die SMA50/SMA200-Prüfungen in `decision_engine._runner_decision` gehalten/verlassen.~~ — **gelöst 2026-09-27**: `decision_engine._runner_decision` liest das Campaign-Alter nun aus dem neuen Feld `PositionContext.swing_campaign_opened_at` (stammt aus `swing_lifecycle.LifecycleContext.opened_at`, durch `analysis_engine.py` durchgereicht) gegenüber `config.swing.horizon_months_min/max`. Unterhalb von `horizon_months_max` führt `remainder_management == "momentum_guided"` die bestehende SMA50/SMA200-Trendlogik unverändert aus (`horizon_months_min` ist bewusst keine Mindesthaltedauer — ein Trendbruch führt in jedem Alter zum Ausstieg). Bei/nach `horizon_months_max` schließt der Runner bedingungslos über den bestehenden SELL-/Full-Remainder-Pfad mit dem Grund `SWING_MAX_HORIZON_REACHED`, unabhängig von Trend oder Verfügbarkeit technischer Daten. Hard Stop, TP1/TP2 und ADD-Regeln bleiben unberührt und behalten ihre bestehende Priorität — der Horizont steuert immer nur den bereits offenen Runner (Restposition nach TP2). Die durchgerechneten Vorher-/Nachher-Beispiele stehen in §3.6 oben.
4. **Die Tabellen `estimates`, `ratings`, `price_targets`, `events`, `news` werden (als Zeilenzahlen) gelesen, aber von keinem Skript dieses Repositorys je befüllt** — die Qualität von `ValuationAnalysis`/`EventRiskAnalysis` spiegelt im aktuellen Setup immer null Zeilen wider. **Ergänzt 2026-09-27**: `decision_engine._confidence()` behandelt `UNAVAILABLE` bei `valuation`/`event_risk` nun neutral (`0.00`, vorher je `-0.05`) — eine fehlende Daten-Pipeline senkt die Confidence nicht mehr, denn der `UNAVAILABLE`-Zustand spiegelt hier einen fehlenden Importer wider und keinen tatsächlichen Diagnosebefund (`swing_promotion.py` behandelt dieselben Daten aus demselben Grund bereits als nicht maßgeblich, siehe §3.6). Die `AVAILABLE`/`PARTIAL`-Boni für beide Module sind unverändert (`+0.05`/`+0.02`); `fundamental` (gestützt auf die aktiv befüllte Tabelle `fundamentals`) ist unverändert (`+0.10`/`+0.05`/`-0.05`). **Aufgeteilt 2026-09-28 — Teil A gelöst**: `events`/`news` haben nun einen echten, verifizierten Importpfad (`Backfill-TradingEventsNews.py`: SEC-EDGAR-8-K/6-K für `events`, Yahoo-Finance-Suche für `news`, beide ohne Authentifizierung und bereits registrierte Quellen), sind in der Produktion befüllt und laufen automatisiert täglich (Windows Scheduled Task `Trading-EventsNews-Backfill`, Anmeldung + täglich 17:25 lokal, `StartWhenAvailable=True`) — `EventRiskAnalysis.quality` wechselt für abgedeckte Wertpapiere nun korrekt von `UNAVAILABLE → PARTIAL/AVAILABLE`. `estimates`/`ratings`/`price_targets` bleiben **bewusst offen, kein Fehler**: Die einzige Quelle mit diesen Daten (Yahoo Finance `quoteSummary`) verlangt nachweislich (live geprüft) eine browser-authentifizierte Sitzung (reines HTTP — auch mit dem üblichen Cookie+Crumb-Handshake — liefert `401 Unauthorized`); FMP hat diese Daten, verlangt aber nachweislich (live geprüft) einen kostenpflichtigen Plan (`402 Payment Required` mit einem echten Key bei `price-target-summary`, `grades-consensus`, `analyst-estimates`) und wurde als für die private Nutzung wirtschaftlich nicht tragfähig verworfen. Eine kostenlose, bereits verfügbare, bereits integrierte Alternative gibt es in diesem Repo nicht. Inkonsistenz #4 bleibt für diese drei Tabellen daher **OFFEN**, bis bewusst entweder ein kostenpflichtiger FMP-Plan oder Browser-Session-Automatisierung gewählt wird.
5. ~~**Die Tabellen `decisions` und `analysis_history` sind reines totes Schema** — von `Initialize-TradingDatabase.py` und `Reset-TradingDb.py` angelegt, nirgends sonst gelesen oder geschrieben.~~ — **gelöst 2026-09-28**: Der Teil "nie gelesen" dieser Behauptung war selbst ungenau — `Resolve-TradingSecurities.py` las beide Tabellen (eine generische `COUNT(*)`-Referenzprüfung vor dem Löschen eines Stub-Wertpapiers), weshalb hier *gelöst* steht und nicht nur *entfernt*. Beide Tabellen (und ihre Indizes) wurden aus `Initialize-TradingDatabase.py`/`Reset-TradingDb.py` entfernt; die beiden nun nicht mehr existierenden Tabellennamen wurden aus der Referenzprüfungsliste von `Resolve-TradingSecurities.py` entfernt. Weder ein Schreiber noch ein fachlicher (inhaltlicher) Leser existierte je für eine der beiden Tabellen.
6. ~~**Inkonsistenz der CLI-Flag-Namen**: `Manage-*`-Skripte verwenden `--db`; `Migrate-*`-Skripte verwenden `--db-path`. Beide haben denselben Produktionspfad als Standard.~~ — **gelöst 2026-09-28**: `--db-path` ist nun der kanonische Flag (in `--help` zuerst gezeigt, `dest="db_path"`) über die gesamte CLI-Oberfläche von `tools/trading/`; `--db` bleibt als funktionierender Alias erhalten (`add_argument("--db-path", "--db", dest="db_path", ...)`) — keine Breaking Change. Betroffen: `Backfill-TradingMarketSnapshotCurrency.py`, `Manage-TradingCorporateActions.py`, `Manage-TradingCapitalState.py`, `Manage-TradingStrategyAssignments.py`, `Manage-TradingSwingCampaigns.py`, `Import-ParqetTransactions.py`. Die drei `Manage-*`-Tools mit versteckten DB-Overrides je Unterbefehl (`CapitalState`, `StrategyAssignments`, `SwingCampaigns`) erhielten denselben `--db-path`/`--db`-Alias auf `dest="command_db_path"` für jeden Unterbefehl; `Manage-TradingCorporateActions.py` hatte nie einen Override je Unterbefehl. `Migrate-*`-Skripte sowie `Initialize-TradingDatabase.py`/`Run-TradingOrchestrator.py` verwendeten bereits `--db-path` und sind unverändert. Scheduled Tasks und Tests waren nicht betroffen — keiner übergibt diesen Flag explizit.
7. ~~**`Migrate-TradingStrategyAssignments.py` öffnet immer eine Lese-/Schreib-Verbindung** (anders als die übrigen Migrate-*-Skripte, die für den Dry-Run-Pfad eine `mode=ro`-URI öffnen) — es verlässt sich allein auf die Verzweigung in `main()`, um ohne `--write` nicht zu schreiben. Funktional heute sicher, strukturell aber der Ausreißer.~~ — **gelöst 2026-09-28**: Ein Helfer `_connect(path, *, write: bool)` wurde ergänzt, der exakt den Geschwister-Skripten Migrate-* entspricht — der Dry-Run-Pfad öffnet nun eine `mode=ro`-URI plus `PRAGMA query_only = ON`, sodass SQLite selbst jeden Schreibversuch ablehnt (`OperationalError: attempt to write a readonly database`), geprüft mit einer gezielten `INSERT`/`UPDATE`-Probe. Der `--write`-Pfad (`apply_migration()`, `BEGIN IMMEDIATE`/`INSERT ... ON CONFLICT`) ist unverändert.
8. ~~`candidate_decision.py` und `strategy_suggestion.py` sind noch nicht an das MCP-Tool `trading_sqlite` angebunden~~ — **gelöst 2026-09-25**: Beide sind nun als `evaluate_watchlist_candidates()` / `suggest_strategy_assignments()` verfügbar (Tool-Version `1.4.0`, siehe §5).
9. **Es wurde kein Widerspruch bei Pfad/Name/Wert** zwischen diesem Dokument und dem aktuellen Code für irgendeine der in §3 tabellierten Konstanten gefunden — jede wurde direkt aus der daneben genannten Quelldatei gelesen, nicht aus einem früheren Planungsstand oder einer früheren Unterhaltung übernommen.
10. **`FUNDAMENTALS_POINT_IN_TIME_DECISION_REQUIRED` — entschieden und umgesetzt**: `Backfill-TradingFundamentalsIR.py` (immer `filing_date = NULL`) und `Research-TradingFundamentals.py` (vom LLM geliefert, nicht validiert, oft NULL) lassen viele `fundamentals`-Zeilen ohne `filing_date`; der strikte Guard `filing_date IS NOT NULL` verbarg sie in jedem Snapshot mit explizitem `as_of` (z. B. Swing-Promotion: R3NK, BA., ATS, BAYN, HO nicht verfügbar). Sichtbarkeitsregel (`analysis_engine._FUNDAMENTAL_VISIBLE_SQL`, von `_latest_fundamental` und `_previous_comparable_fundamental` gemeinsam verwendet): `filing_date <= as_of` -> sichtbar, Qualität nach Feld-Vollständigkeit; `filing_date IS NULL` und `date(fetched_at) <= as_of` -> sichtbar, Detail `FUNDAMENTALS_PUBLICATION_DATE_UNVERIFIED`, Qualität höchstens `partial`; sonst für dieses Datum nicht sichtbar. `period_end` ist nie ein Veröffentlichungsdatum, und NULL wird nie bedingungslos freigegeben. `fetched_at` ist nur eine Obergrenze der Veröffentlichung (und wird bei einer Aktualisierung überschrieben), daher die Deckelung. Dieselbe Regel gilt auf dem aktuellen Pfad: `build_analysis_snapshot` löst `as_of=None` einmal zu heute auf (`evaluation_as_of`) und übergibt dieses Datum an die Loader, die nie die Systemuhr lesen; vor dieser Änderung wendete `as_of=None` überhaupt keinen zeitlichen Filter an, sodass Positionen und die Kandidaten-Snapshots des Planners undatierte Zeilen als `available` sahen, die Swing-Promotion (explizites `as_of`) aber nicht. `fetched_at IS NULL` ist nie sichtbar. Noch offen: echte Veröffentlichungsdaten ("Option A", z. B. die DART-Filing-ID für SK hynix, belegte Daten im Recherche-Tool) sowie die Tatsache, dass `Backfill-TradingFundamentalsIR.write_company` vor dem Neuschreiben alle IR-Zeilen eines Wertpapiers löscht.

Weitere veraltete Pfade, widersprüchliche Schwellenwerte oder doppelte/überlappende Dokumentationsdateien wurden in `docs/` zum Zeitpunkt des Schreibens nicht gefunden.
