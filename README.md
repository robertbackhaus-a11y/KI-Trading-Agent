# Trading Agent

Eigenständiger Trading Agent / Trading-Datenbestand.

Version: v0.1.1 — siehe [CHANGELOG.md](CHANGELOG.md)

## Runtime

Produktive Datenbank:

```
C:\tools\trading\data\trading.db
```

Produktive Runtime: `C:\tools\trading` (`app\` Fachmodule/Skripte, `mcp\` MCP-Adapter, `data\`, `logs\`).
Der Agent ist MCP-basiert und an kein bestimmtes Frontend gekoppelt (jeder MCP-fähige LLM-Client kann ihn nutzen).
Im Repo versioniert: `mcp-tools\` (`server.py`, `trading_sqlite.py`, `smoke_test.py`) und
`scheduler\Manage-TradingTasks.ps1` (Verwaltung der geplanten Aufgaben).

Zugangsweg: MCP-Client (LLM-Frontend) → Trading MCP Server (`C:\tools\trading\mcp`, Repo-Quelle `mcp-tools\`, 15 Tools)
→ Runtime (`C:\tools\trading\app`) → SQLite (`C:\tools\trading\data\trading.db`).

Dieses Repository enthält nur den Code. Die Datenbank, Backups, Audit-/Candidate-JSONs,
PDFs und Logs liegen außerhalb des Repos (siehe `.gitignore`).

## Usage / Abfragen

Deutsch: [Trading Agent – Abfragen und Beispiele](docs/trading-agent-query-examples.de.md)

English: [Trading Agent – Queries and Examples](docs/trading-agent-query-examples.en.md)

## Deploy

Die Produktion (`-Root`, Standard `C:\tools\trading`) wird nur über das versionierte Skript `deploy\Deploy-TradingAgent.ps1` aktualisiert.
Es kopiert ausschließlich die Dateien aus `deploy\deploy-manifest.json` (explizite Liste, kein Wildcard-Kopieren; eine neue Runtime-Datei
muss dort eingetragen werden, ein Test erzwingt das). Tests, Doku, `.git`, DB, Logs und Daten werden nie angefasst.

```powershell
powershell -File deploy\Deploy-TradingAgent.ps1 -Action Check      # nur lesen: Pfade, Manifest, Syntax, DB integrity_check, Plan
powershell -File deploy\Deploy-TradingAgent.ps1 -Action Deploy     # Backup, kopieren, Hash-Vergleich, py_compile, integrity_check, MCP-Smoke-Test (-DryRun: nur Plan)
powershell -File deploy\Deploy-TradingAgent.ps1 -Action Rollback -BackupPath C:\tools\trading\backup\deploy-<Zeitstempel>
```

- **Backups:** ersetzte Dateien nach `backup\deploy-<Zeitstempel>\` (mit `backup-manifest.json`), Datenbank-Kopie über die SQLite-Backup-API nach `data\trading.db.bak-deploy-<Zeitstempel>`.
- **Rollback:** kopiert nur die dort gesicherten Dateien zurück; die Datenbank wird nur mit ausdrücklichem `-RestoreDb` zurückgespielt (vorher Sicherheitskopie `trading.db.bak-pre-rollback-<Zeitstempel>`).
- **Nach jedem Deploy** laufende MCP-Prozesse bzw. das Frontend neu starten (Python-Module werden pro Prozess gecacht). Der Scheduler wird nicht neu registriert, es wird kein Collector gestartet, es gibt keine Schema-Migration.
- **Privat:** `app\universe\sector_map_local.json` (git-ignoriert, nicht im Manifest) bleibt unangetastet; fehlt sie, gibt es nur eine Warnung.

## Architektur

```
Market/Fundamental Data -> Analytics -> Strategy Suggestion -> Strategy Assignment
  -> Portfolio Context/Guardrails -> Swing Campaign State -> Candidate Decision
  -> Decision Engine/Orchestrator -> Agent Output
```

Vollständige, code-verifizierte Referenz aller Tools/Module, fester Werte/Schwellenwerte,
des Entscheidungsflusses und des DB-Schemas:
[docs/trading-agent-architecture.md](docs/trading-agent-architecture.md).

## Hauptscripte (`tools/trading/`)

Auszug der wichtigsten Einstiegspunkte — vollständige Liste aller ~40 Module mit Zweck,
Inputs/Outputs, DB-Zugriff und Abhängigkeiten: siehe
[docs/trading-agent-architecture.md](docs/trading-agent-architecture.md#2-tool--module-inventory).

| Script | Zweck |
|---|---|
| `Initialize-TradingDatabase.py` | DB initialisieren (Tabellen, Indizes, Views, Basis-`data_sources`) |
| `Reset-TradingDb.py --db-path <Pfad>` (Pfad Pflicht, kein Default) | DB vollständig zurücksetzen (löscht bestehende DB-Datei) |
| `Backfill-TradingMarketData.py` | Market Data Backfill (OHLCV) — läuft täglich automatisiert um 17:15 Uhr via Windows-Task `Trading-MarketData-Backfill` (Log: `C:\tools\trading\logs\market-data-backfill.log`) |
| `Backfill-TradingFXRatesECB.py` | ECB-EUR-Referenzkurs-Backfill für USD, GBP, AUD und KRW in einem Lauf (`--quote-currency`, kommagetrennt, Default = diese vier; GBp-Pence-Kurse werden im Resolver nach GBP normalisiert) — läuft automatisiert via Windows-Task `Trading-FXRates-Backfill` (Logon + täglich 17:20 Uhr, `StartWhenAvailable=True`; Log: `C:\tools\trading\logs\fx-rates-backfill.log`) |
| `Discover-TradingSECIdentifiers.py` | SEC-CIK-Identifier-Discovery |
| `Resolve-TradingSecurities.py` | Security-Stammdaten-Auflösung (OpenFIGI) |
| `Backfill-TradingFundamentalsSEC.py` | Fundamentals-Bulk-Import aus SEC-EDGAR-XBRL — **weekly automated** via Windows-Task `Trading-Fundamentals-SEC` (Logon + Sonntag 10:00 Uhr, `StartWhenAvailable=True`; Log: `C:\tools\trading\logs\fundamentals-sec-backfill.log`) |
| `Backfill-TradingFundamentalsIR.py` | Fundamentals aus Company-IR-Referenzparsern — **manual/event-driven** (hartkodierte Quartals-URLs je Firma, keine Automatisierung) |
| `Research-TradingFundamentals.py` | LLM-Research-Fundamentals-Pipeline (kanonisches Tool) — **manual/LLM-assisted** (erfordert eine aktive Claude-Session, nicht automatisierbar) |
| `Backfill-TradingEventsNews.py` | `events` (SEC EDGAR 8-K/6-K, 2-Jahres-Lookback) + `news` (Yahoo Finance Search) Backfill — läuft täglich automatisiert via Windows-Task `Trading-EventsNews-Backfill` (Logon + täglich 17:25 Uhr, `StartWhenAvailable=True`; Log: `C:\tools\trading\logs\events-news-backfill.log`). Inkonsistenz #4 Teil A (events/news) damit resolved; `estimates`/`ratings`/`price_targets` bleiben weiterhin bewusst offen (kein kostenloser Datenpfad) |
| `Import-ParqetTransactions.py` | Inkrementeller Parqet-CSV-Import (`transactions` + `positions`; Vorschau ist Default, `--write` legt vorher ein Backup an). Siehe Abschnitt „Parqet-Import und Swing-Campaigns" |
| `trading_analytics.py` | Technische Scoring-Bibliothek (SMA/RSI/Momentum/Drawdown/Volatility), `rank_watchlist()` |
| `trading_orchestrator.py` | Read-only Gesamt-Portfolio-Report (komponiert Analytics/Decision/Promotion) |
| `candidate_discovery.py` / `Discover-TradingCandidates.py` | Read-only Watchlist-Kandidaten-Discovery über ein kontrolliertes Universum (`universe/swing_large_cap_v1.json`); kein Teil des Orchestrators, schreibt nichts in die Datenbank; läuft täglich automatisiert via Windows-Task `Trading-Candidate-Discovery` (17:35 Uhr, `StartWhenAvailable=True`, kein Anmelde-Auslöser, Zeitlimit 2 h; Log: `C:\tools\trading\logs\candidate-discovery.log`). Siehe Abschnitt „Watchlist Candidate Discovery" |
| `market_intelligence.py` / `intelligence_report.py` / `Collect-TradingMarketIntelligence.py` | Read-only Market Intelligence: Wirtschafts-/Markt-News aus offiziellen Quellen, SEC und Yahoo-Schlagzeilen, regelbasiert klassifiziert und auf Depot/Watchlist/Discovery abgebildet; der CLI-Lauf (täglich automatisiert via Windows-Task `Trading-Market-Intelligence`, 08:15 und 17:45 Uhr, `StartWhenAvailable=True`, kein Anmelde-Auslöser, Zeitlimit 2 h; Log: `C:\tools\trading\logs\market-intelligence.log`) schreibt einen Report, der MCP-Reader liest ihn nur. Kein Teil des Orchestrators, kein Signal. Siehe Abschnitt „Market Intelligence" |
| `opportunity_view.py` | Read-only Opportunity View: führt Engine-/Planner-Kandidaten, den letzten Discovery-Report und den letzten Market-Intelligence-Report zusammen (News nur als Kontext, kein neuer Score); gelesen über die MCP-Methode `get_opportunity_view`. Siehe Abschnitt „Unified Opportunity View" |
| `portfolio_action_planner.py` | Deterministischer Kapital-/Entry-Plan (`portfolio_action_plan`, Simulation ohne Orders); vom Orchestrator am Ende aufgerufen. Siehe Abschnitt „Portfolio Action Planner" |

## Parqet-Import und Swing-Campaigns

Der Import reconciliert eindeutig zuordenbare Trades **nach** `opened_at` einer offenen Swing-Campaign
in derselben DB-Transaktion mit transaktionsverknüpften Events: SELL → `manual_reduction`, BUY → `add`
(Menge/Preis exakt aus der Transaktion). Trades **vor** `opened_at` gehören zur Baseline und werden ignoriert.
Ein Trade am selben Kalendertag wie `opened_at` wird nur reconciliert, wenn er der einzige unverlinkte
Same-Day-Trade ist und die Mengenhistorie ihn eindeutig nach der Baseline einordnet (z. B. 143 − 45 = 98);
sonst `MANUAL_REVIEW_REQUIRED`. Es werden nie TP1/TP2-/Stop-Gründe abgeleitet, `original_quantity` bleibt unverändert, Campaigns werden nicht
automatisch geschlossen. Idempotent über die eindeutige `transaction_id`-Verknüpfung. Mehrdeutige oder nicht
exakt aufgehende Fälle (mehrere offene Campaigns, Transfers, Overselling, Split) schreiben nichts und werden
als `AMBIGUOUS` / `MANUAL_REVIEW_REQUIRED` gemeldet. Bereits importierte Trades:
`Import-ParqetTransactions.py --reconcile-campaigns [--write]`.

## Portfolio Action Planner

`tools/trading/portfolio_action_planner.py` erzeugt den `portfolio_action_plan` — rein deterministisch,
ohne LLM-Rangfolge, ohne Orders, ohne DB-Writes und ohne automatische Strategieänderung. Der Orchestrator
gibt ihn in `run_trading_orchestrator` mit aus (keine neue MCP-Methode).

**PROMOTE ist kein BUY.** PROMOTE ist nur die Eingabe der Entry-Bewertung:

```
PROMOTE  → ENTRY-EVALUATION → RANKING → POSITION SIZING → PLANNED ENTRY
SELL/TRIM → POST-ACTION CAPITAL → ENTRY PLAN
```

- **Post-Action-Kapital:** Erlös je SELL/TRIM (Menge × aktueller EUR-Kurs) abzüglich einer **geschätzten Steuer**
  → Cash, Swing-/Long-Term-Anteil und freie Swing-Kapazität nach den Aktionen. Die Steuer (25 % KESt + 5,5 % Soli =
  26,375 %, Kirchensteuer 0 %) fällt nur auf den realisierten Gewinn an, nie auf den Brutto-Erlös; ein Verlust ergibt
  keine negative Steuer und kein zusätzliches Cash. Basis ist `positions.avg_cost` (Durchschnitts-Einstand,
  **kein FIFO**; `tax_estimate_quality = estimated_from_average_cost`). Geplant wird mit der **konservativen** Sicht
  (keine Verlustverrechnung); die Sicht mit planinterner Verlustverrechnung wird nur zusätzlich angezeigt.
  Verlusttopf, Freistellungsauftrag und bereits realisierte Ergebnisse des Jahres sind unbekannt und werden nicht erfunden.
- **Entry-Status** (nur PROMOTE-Kandidaten): `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `BLOCKED_BY_ALLOCATION`,
  `BLOCKED_BY_DATA`, `BLOCKED_BY_CONCENTRATION`, `BLOCKED_EXISTING_POSITION`, `BLOCKED_EXISTING_CAMPAIGN`.
- **Ranking** nur für `ENTRY_READY`: `entry_score = momentum_score × confidence` (bestehender Analytics-Score ×
  bestehender Datenqualitäts-Wert der Decision Engine); Gleichstand: höhere Watchlist-Priorität, dann niedrigere `security_id`.
- **Sizing/Allokation:** bestehende Entry-Logik (Swing-Maximum 40 %, Cash-Reserve, max. Positionsgewicht, ganze
  Stück abgerundet), sequentiell nach Rang; Stopp bei Swing-Maximum, Cash-Reserve oder erschöpftem Kapital.
- **Nicht modelliert:** Gebühren, FIFO-Lots, Verlusttopf/Freibetrag, Sektor-Konzentration (keine Sektor-Daten für Bestandspositionen — es gelten
  nur die Einzeltitel-Limits).

## Watchlist Candidate Discovery

`DISCOVERY ≠ PROMOTE ≠ ENTRY_READY ≠ ORDER`: Die Discovery schlägt neue Titel nur **vor**. Sie legt keine Security, keinen
Watchlist-Eintrag, keine Strategy-Zuweisung, keine Campaign, keinen BUY und keinen Entry-Plan an; Version 1 ist read-only.

```
python tools\trading\Discover-TradingCandidates.py [--limit N] [--top N] [--json] [--output FILE] [--cache-dir DIR]
```

- **Universum:** versionierte, statische Liste liquider Large Caps (`tools/trading/universe/swing_large_cap_v1.json`, keine
  offizielle Indexzusammensetzung); Yahoo-Symbole wie in der bestehenden Market-Data-Pipeline. Keine freie Web-/LLM-Suche.
- **Pre-Filter** vor dem ersten Request: bereits im Depot / auf der Watchlist (Yahoo-Symbol), kein Aktien-Typ, Kurswährung nicht
  bewertbar (bestehender FX-Resolver). Danach: Mindestkurs 5 EUR, Median-Handelsvolumen 60 Tage ≥ 5 Mio. EUR (`DiscoveryConfig`,
  Vorschlag), Historie/Frische nach der bestehenden Technik-Regel.
- **Analyse:** unveränderte Engine (`build_analysis_snapshot`, `evaluate_swing_promotion`) auf einer flüchtigen In-Memory-Kopie
  des Produktions-Schemas; keine zweite Bewertungslogik. Fundamentals, Bewertung und Events werden für neue Titel **nicht** geholt.
- **Score:** `discovery_score = momentum_score × confidence` (wie der `entry_score` des Planners), Gleichstand: höherer Momentum-Score,
  dann Symbol. Status: `DISCOVERY_READY` (Trend intakt und Momentum ≥ 20), `DISCOVERY_WATCH`, `DISCOVERY_DATA_INSUFFICIENT`, `DISCOVERY_REJECTED`.
- **Ablauf für den MCP-Client:** `Universum → Scan (CLI) → Report → MCP-Reader → MCP-Client`. Der lange Lauf (mehrere Minuten, Yahoo-Requests) bleibt
  das explizite Skript; es schreibt nach erfolgreichem, vollständigem Lauf einen strukturierten Report nach
  `C:\tools\trading\data\candidate-discovery\` (`latest.json`, atomar ersetzt, plus optional `candidate-discovery-YYYYMMDD-HHMMSS.json`;
  `--output-dir`, `--no-history`). Ein fehlgeschlagener Lauf lässt den letzten gültigen Report unberührt.
- **MCP:** `get_candidate_discovery(status, limit, detail, symbol)` liest **nur** den letzten fertigen Report — **kein** Discovery-Lauf,
  kein Yahoo-Aufruf, keine DB- oder Watchlist-Zugriffe. Ohne Report: `UNAVAILABLE` / `NO_DISCOVERY_REPORT`. Ein alter Report wird nicht
  versteckt, sondern mit Alter und `REPORT_STALE` ausgewiesen (Grenze: `market_data_max_age_days`). Der MCP-Client liest also den letzten fertigen
  Report, die Discovery bleibt read-only und es gibt keine automatische Watchlist-Aufnahme. Der Report wird im Referenz-Setup täglich um 17:35
  vom Windows-Task `Trading-Candidate-Discovery` erzeugt (nicht vom MCP-Tool).

## Market Intelligence

`NEWS ≠ SIGNAL ≠ BUY ≠ SELL`: Die Market Intelligence ordnet Nachrichten **nur ein** (Kategorie, Wichtigkeit, betroffene Titel). Sie
ändert kein Signal, keine Empfehlung, kein Ranking, kein Sizing und legt keine Orders an. Version 1 ist read-only und deterministisch;
ein LLM entscheidet nichts und erzeugt keine Nachrichten.

```
python tools\trading\Collect-TradingMarketIntelligence.py [--top N] [--json] [--output-dir DIR] [--no-history] [--discovery-news-limit N]
```

- **Ablauf für den MCP-Client:** `Quellen → Collect-CLI → Report → MCP-Reader → MCP-Client`. Der Abruf der Quellen ist das explizite Skript; es schreibt nach
  einem erfolgreichen Lauf `C:\tools\trading\data\market-intelligence\latest.json` (atomar ersetzt: Temp-Datei → erneut lesen/validieren
  → `os.replace`; plus optional `market-intelligence-YYYYMMDD-HHMMSS.json`). Fehlgeschlagener Lauf (keine einzige Quelle antwortet)
  lässt den letzten gültigen Report unberührt; einzelne ausgefallene Feeds erscheinen als Warnung `FEED_FAILED:<feed>`.
- **Quellen:** offizielle Feeds ohne API-Key — EZB (Pressemitteilungen, Statistik), Fed (Geldpolitik, Pressemitteilungen), BLS (CPI,
  Beschäftigung), BEA, Destatis, EIA (Today in Energy, Pressemitteilungen); SEC-8-K-Ereignisse und gecachte Yahoo-Schlagzeilen aus der
  Datenbank (nur lesen); frische Yahoo-Schlagzeilen für `DISCOVERY_READY`-Kandidaten des Discovery-Reports. Eurostat ist in v1 nicht
  angebunden; `COMMODITIES` ist nicht implementiert.
- **Kategorien:** MACRO, MONETARY_POLICY, INFLATION, LABOR_MARKET, ECONOMIC_GROWTH, GEOPOLITICS, ENERGY, SECTOR, COMPANY, EARNINGS,
  GUIDANCE, REGULATORY, M_AND_A. Feste Regeltabellen je Quelle bzw. Schlagzeilen-Schlüsselwörter; `event_id` ist ein stabiler Hash
  aus Quellentyp und kanonischer URL/Accession (Duplikate werden zusammengeführt).
- **Relevanz (HIGH/MEDIUM/LOW):** feste Tabelle. HIGH nur für Primärquellen (z. B. EZB-/Fed-Zinsentscheid, SEC-Pflichtmeldungen wie
  Übernahme/Delisting), Unternehmens-Pressemitteilungs-Dienste oder Schlüsselwort-Klassen, deren Schlagzeile das verknüpfte Unternehmen
  **mit Namen** nennt (ein bloßer Ticker zählt nicht). Sekundärquellen mit reinen Schlüsselwörtern sind höchstens MEDIUM; Stock-Pick-Artikel
  sind LOW; geplante Makro-Veröffentlichungen sind MEDIUM (ob eine Überraschung vorliegt, ist ohne Konsensdaten nicht prüfbar);
  GEOPOLITICS ist höchstens MEDIUM.
- **Impact:** `UNKNOWN`, solange keine verlässliche Regel greift. Eine Richtung wird nie aus einer Schlagzeile gelesen; nur die Struktur einer
  Primär-Meldung (Delisting-Hinweis, Cybersecurity-Vorfall 1.05, Non-Reliance 4.02) ergibt `NEGATIVE`. Jedes Ereignis nennt `impact_basis`.
- **Abbildung:** `affected_portfolio_symbols`, `affected_watchlist_symbols`, `affected_discovery_symbols`; Sektor-Ereignisse (explizite
  Schlüsselwörter, EIA) erreichen die Titel dieses Sektors über die statische Zuordnung `universe/sector_map_v1.json`; Makro-Ereignisse tragen
  `portfolio_region_exposure` (Region je Yahoo-Suffix der Depot-Titel).
- **MCP:** `get_market_intelligence(scope, category, importance, symbol, sector, limit, detail)` liest **nur** den letzten fertigen Report —
  kein Web-Abruf, kein Scan, kein DB-Zugriff. `scope`: ALL, PORTFOLIO, WATCHLIST, DISCOVERY, MACRO. Ohne Report: `UNAVAILABLE` /
  `NO_MARKET_INTELLIGENCE_REPORT`; ein alter Report wird mit Alter und `REPORT_STALE` ausgewiesen (Grenze 24 h, `MarketIntelligenceConfig`).
  Die kompakte Antwort (höchstens 50 Ereignisse) enthält nur kurze Frische, den aktiven Filter, die Zähler je Wichtigkeit aller Treffer und die
  Ereignisse selbst (Zeit, Wichtigkeit, Kategorie, Schlagzeile, Quelle, Impact, bis zu 3 Reason Codes; bei `symbol=...` zusätzlich `link`
  DIRECT/SHARED/SECTOR/LOOSE, sonst die betroffenen Symbole/Sektoren/Regionen). Kein `rendered_de`, keine Quellenstatistik, kein Lauf-Protokoll,
  keine Methodik. `detail=true` (höchstens 25) liefert alles. Die Antwort bleibt unter dem MCP-Limit von 40.000 Zeichen (sonst
  `RESPONSE_TRIMMED_TO_SIZE_BUDGET`). Der Report wird im Referenz-Setup täglich um 08:15 und 17:45 vom Windows-Task `Trading-Market-Intelligence` erzeugt (nicht vom MCP-Tool).
- **Bekannte Grenzen:** Yahoo ordnet Schlagzeilen teils lose Symbolen zu (Ticker-Homonyme, Marktberichte); Stichwort-Klassifikation hat
  `confidence: LOW`; Impact ist meist `UNKNOWN`.

## Unified Opportunity View

`DISCOVERY + MARKET INTELLIGENCE + PORTFOLIO/PLANNER-KONTEXT → eine Opportunity-Ausgabe`. Die Ansicht **führt nur zusammen**, was schon
existiert; sie rechnet keinen Score, kein Ranking, keine Positionsgröße und keine Empfehlung und erzeugt keine Order. News bleiben Kontext
und Warnung: sie ändern weder `discovery_score` noch PROMOTE/ENTRY_READY, Rang, Sizing oder SELL/TRIM/HOLD. Es gibt **keinen**
kombinierten Score (kein „Score + News-Bonus/Malus").

- **MCP:** `get_opportunity_view(source, status, news_status, symbol, limit, detail)` (Tool Nr. 15). Es liest nur: den Orchestrator auf der
  gewohnten read-only Verbindung, den letzten Discovery-Report und den letzten Market-Intelligence-Report. Kein Web-Zugriff, kein
  Discovery-/Collector-Lauf, keine DB-Writes, keine neue Tabelle, keine neue Persistenz. Fehlt ein Report, ist das Ergebnis `PARTIAL`
  (der Rest wird gezeigt, `market_context` bzw. Discovery-Teil `UNAVAILABLE`); `UNAVAILABLE` nur, wenn gar nichts vorliegt. Beide Reports werden
  mit Stand, Alter und `stale` offen ausgewiesen (Discovery: `market_data_max_age_days`, Market Intelligence: 24 h).
- **Ergebnis kompakt (Standard):** nur das Angefragte — Status und Frische beider Reports, ein minimaler Kapitalrahmen (Cash, Cash nach den
  geplanten Verkäufen, konservativer Nettoerlös, geplante Entries, verbleibende Kaufkapazität, Swing-Anteil und -Korridor), die passenden
  `opportunities` (Entry-Status/-Rang/-Score, Momentum, Plan-Status mit geplantem Kapital, vom Engine-Sizing vorgeschlagenes Kapital
  `proposed_capital_eur`/`proposed_quantity`, Stop-Grund wie `STOPPED_CAPITAL_EXHAUSTED`, News-Status mit Zählern und dem wichtigsten Event) und
  `warnings` als Codes. Bei `symbol=...` kommen nur diese Symbole (und ggf. die so benannten Depotpositionen). Keine Depotliste, keine
  Discovery-Liste, kein Makro-Block, keine globale High-Attention-Liste, kein `rendered_de`. Leere Felder werden weggelassen.
- **Ergebnis `detail=true`:** zusätzlich `metadata`-Felder, `portfolio_context` (mit Depotpositionen), `watchlist_candidates`,
  `discovery_candidates`, `market_context` (Makro, High-Attention), Methodik und `rendered_de` (fertige Tabellen).
- **Kandidaten:** A) Watchlist-/PROMOTE-Kandidaten des Planners (alle `entry_status`), B) `DISCOVERY_READY` aus dem Discovery-Report. `source`
  = `WATCHLIST`, `DISCOVERY` oder `BOTH` (Symbol in beiden). Keine automatische Watchlist-Aufnahme, keine Strategy-Zuweisung.
- **Reihenfolge:** ENTRY_READY → WAIT_FOR_TRIGGER → DISCOVERY_READY → Sonstige; innerhalb der Gruppe der Engine-Rang (dann `entry_score`,
  Discovery-Rang, Watchlist-Priorität). Die bestehenden Scores (`discovery_score`, `entry_score`, `momentum_score`, `confidence`) bleiben getrennt sichtbar.
- **News-Kontext je Kandidat und Depotposition:** `NEWS_HIGH_ATTENTION` (mindestens ein *direkt* symbolbezogenes HIGH-Event: SEC-Meldung oder
  Schlagzeile, die das Unternehmen nennt, auch „(TICKER)"), `NEWS_ATTENTION` (direktes MEDIUM-Event oder ein nur über den Sektor bzw. lose
  zugeordnetes HIGH-Event), `NEWS_CLEAR` (nur LOW / lose MEDIUM / keine Meldung), `NEWS_UNAVAILABLE` (kein gültiger MI-Report). Mit Zählern
  (alle / direkt), den wichtigsten Events (Kategorie, Impact, `link` DIRECT/SECTOR/LOOSE, Reason Codes) und der Begründung `status_reasons`.
- **Kapital:** bei Kandidaten ohne Planner-Eintrag (Discovery) nur ein informativer Vergleich `capital_fit` (Preis einer Aktie gegen frei
  verfügbares Kapital nach den geplanten Verkäufen bzw. nach den geplanten Entries) — keine Menge, keine Order.
- **Typische Fragen an den Assistenten:** „beste neue Chancen" → `source=DISCOVERY`; „Discovery mit relevanten News" → `source=DISCOVERY, news_status=RELEVANT`;
  „ENTRY_READY mit High-Impact-News" → `status=ENTRY_READY, news_status=NEWS_HIGH_ATTENTION`; „technisch stark und news-unauffällig" →
  `news_status=NEWS_CLEAR`; „NVDA vor AAPL?" → `symbol=NVDA,AAPL` (Rang, Score, Plan-Status, Verzögerungsgrund); „Neues zu AAPL, NVDA, GOOGL" →
  `symbol=AAPL,NVDA,GOOGL`; „passt ins freie Kapital" → `source=DISCOVERY` (`capital_fit`, Abschnitt Kapital); „Discovery READY ohne HIGH-News" →
  `source=DISCOVERY, status=DISCOVERY_READY, news_status=NOT_HIGH`. Standard-`limit` 30 (höchstens 40 kompakt, 10 Detail): ein Statusfilter wie ENTRY_READY liefert alle Treffer in einem Aufruf. Die Antwort bleibt unter
  34.000 Zeichen (sonst `RESPONSE_TRIMMED_TO_SIZE_BUDGET`; im Detail zuerst Nebenabschnitte, dann die letzten Zeilen).
- **Grenzen:** Ein Unternehmen wird nur über seinen Namen oder einen geklammerten Ticker erkannt (Kürzel ohne Namensbestandteil zählen
  nicht als direkt); Yahoo ordnet Schlagzeilen teils lose zu; viele `NEWS_ATTENTION` sind Analysten-Kursziele (COMPANY/MEDIUM).

## Watchlist-Kandidaten-Status

`tools/trading/candidate_decision.py::evaluate_watchlist_candidates()` ist read-only und
verdichtet Analytics-Score (`trading_analytics.rank_watchlist`), aktive Strategiezuordnung,
offenen Swing-Campaign-Status und die Portfolio-Allokations-Guardrails zu einem strukturierten
`BUY` / `WATCH` / `DEFERRED` / `INSUFFICIENT_DATA` je Watchlist-Titel. Keine Orders, keine
DB-Writes, keine automatische Campaign-Eröffnung. Auch über den Trading MCP Server (MCP-Client) erreichbar
(`evaluate_watchlist_candidates()`). Details und Entscheidungsreihenfolge siehe
[docs/trading-candidate-decision.md](docs/trading-candidate-decision.md).

## Watchlist-Strategie-Vorschlag

`tools/trading/strategy_suggestion.py::suggest_strategy_assignments()` ist read-only und
schlägt für Watchlist-Titel **ohne aktive** `strategy_assignment` `swing` / `long_term` /
`unknown` vor. Ein Vorschlag ist **keine** Zuordnung — er schreibt nichts und ersetzt nicht
den bestehenden `swing_promotion.approve_swing_promotion`-Pfad. `long_term` wird nie allein
aus dem Momentum-Score abgeleitet (einzige Long-Term-Grundlage aktuell: `asset_type` ∈
`{etf, fund}`). Auch über den Trading MCP Server (MCP-Client) erreichbar (`suggest_strategy_assignments()`).
Details siehe [docs/trading-strategy-suggestion.md](docs/trading-strategy-suggestion.md).

## Prinzip

```
LLM    = Research + Interpretation + Mapping
Python = DB I/O + Prompt + Validation + Write
```

Die LLM-Recherche liefert Kandidaten-JSON; Python validiert strukturell und schreibt
kontrolliert (mit Overwrite-Schutz) in die Datenbank. Keine neuen firmenspezifischen
Parser ohne Notwendigkeit — bestehende Company-IR-Referenzparser bleiben Ausnahmen.

## Datenquellen

- Yahoo Finance
- SEC EDGAR
- Company Investor Relations
- Regulatorische Quellen (z. B. DART/OpenDART, ESEF)

## Contributing, Security and License

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md)
- [LICENSE](LICENSE)
