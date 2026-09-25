# Trading Agent

Eigenständiger Trading Agent / Trading-Datenbestand.

## Runtime

Produktive Datenbank:

```
C:\KI-Stack\data\trading\trading.db
```

Dieses Repository enthält nur den Code. Die Datenbank, Backups, Audit-/Candidate-JSONs,
PDFs und Logs liegen außerhalb des Repos (siehe `.gitignore`).

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
| `Reset-TradingDb.py` | DB vollständig zurücksetzen (löscht bestehende DB-Datei) |
| `Backfill-TradingMarketData.py` | Market Data Backfill (OHLCV) |
| `Discover-TradingSECIdentifiers.py` | SEC-CIK-Identifier-Discovery |
| `Resolve-TradingSecurities.py` | Security-Stammdaten-Auflösung (OpenFIGI) |
| `Canonicalize-TradingUniverse.py` | Verifizierte Security-Stammdaten (Symbol/ISIN/WKN/Exchange/Country) für bestehende Positionen/Watchlist |
| `Backfill-TradingFundamentalsSEC.py` | Fundamentals-Bulk-Import aus SEC-EDGAR-XBRL |
| `Backfill-TradingFundamentalsIR.py` | Fundamentals aus Company-IR-Referenzparsern |
| `Research-TradingFundamentals.py` | LLM-Research-Fundamentals-Pipeline (kanonisches Tool) |
| `trading_analytics.py` | Technische Scoring-Bibliothek (SMA/RSI/Momentum/Drawdown/Volatility), `rank_watchlist()` |
| `trading_orchestrator.py` | Read-only Gesamt-Portfolio-Report (komponiert Analytics/Decision/Promotion) |

## OpenWebUI-Integration

OpenWebUI nutzt das Tool `trading_sqlite` (aktuell Version 1.4.0) für DB-Zugriff auf die
Trading-DB. Neben `rank_watchlist()` und `run_trading_orchestrator()` sind seit 2026-09-25
auch `suggest_strategy_assignments()` und `evaluate_watchlist_candidates()` verfügbar — beide
read-only, beide rufen ausschließlich die unten beschriebenen, bestehenden Module auf.
Details siehe [docs/openwebui-trading-tool.md](docs/openwebui-trading-tool.md).

## Watchlist-Kandidaten-Status

`tools/trading/candidate_decision.py::evaluate_watchlist_candidates()` ist read-only und
verdichtet Analytics-Score (`trading_analytics.rank_watchlist`), aktive Strategiezuordnung,
offenen Swing-Campaign-Status und die Portfolio-Allokations-Guardrails zu einem strukturierten
`BUY` / `WATCH` / `DEFERRED` / `INSUFFICIENT_DATA` je Watchlist-Titel. Keine Orders, keine
DB-Writes, keine automatische Campaign-Eröffnung. Auch über das OpenWebUI-Tool erreichbar
(`evaluate_watchlist_candidates()`). Details und Entscheidungsreihenfolge siehe
[docs/trading-candidate-decision.md](docs/trading-candidate-decision.md).

## Watchlist-Strategie-Vorschlag

`tools/trading/strategy_suggestion.py::suggest_strategy_assignments()` ist read-only und
schlägt für Watchlist-Titel **ohne aktive** `strategy_assignment` `swing` / `long_term` /
`unknown` vor. Ein Vorschlag ist **keine** Zuordnung — er schreibt nichts und ersetzt nicht
den bestehenden `swing_promotion.approve_swing_promotion`-Pfad. `long_term` wird nie allein
aus dem Momentum-Score abgeleitet (einzige Long-Term-Grundlage aktuell: `asset_type` ∈
`{etf, fund}`). Auch über das OpenWebUI-Tool erreichbar (`suggest_strategy_assignments()`).
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
