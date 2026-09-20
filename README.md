# Trading Agent

Eigenständiger Trading Agent / Trading-Datenbestand.

## Runtime

Produktive Datenbank:

```
C:\KI-Stack\data\trading\trading.db
```

Dieses Repository enthält nur den Code. Die Datenbank, Backups, Audit-/Candidate-JSONs,
PDFs und Logs liegen außerhalb des Repos (siehe `.gitignore`).

## Hauptscripte (`tools/`)

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
