# Transaktionsimport

English version: [trading-import.en.md](trading-import.en.md)

Dieses Dokument beschreibt, wie Transaktionen in den Trading Agent gelangen. Es gilt für alle: Der Agent hängt von keinem Broker und
keinem Portfolio-Tool ab. Transaktionen kommen aus einem dokumentierten kanonischen CSV; Parqet ist nur eine optionale Quelle, die in
dasselbe Modell umgewandelt wird.

## 1. Überblick

```
source file -> adapter -> canonical transaction -> validation -> security resolution
  -> transactions -> positions -> strategy assignment -> Swing campaign -> reconciliation -> validation
```

- Alles nach dem Adapter ist anbieterneutral (`transaction_import.py`). Der Parqet-Adapter (`parqet_import.py`) bildet nur seine Spalten ab.
- Der Befehl ist `Import-TradingTransactions.py`. Ohne `--write` ist er ein vollständiger Dry-Run; mit `--write` sichert er die Datenbank und schreibt alles in einer SQLite-Transaktion.
- Eine gehaltene Position ist für den Orchestrator nur nutzbar, wenn sie eine aktive Strategie-Zuordnung hat, eine Swing-Position zusätzlich eine passende offene Campaign, und ein aktueller Kurs vorhanden ist. Der Import legt an, was sich ohne Raten ableiten lässt, und meldet alles andere als offenen Punkt.
- Der Agent erzeugt nie Orders und ändert nie den Cash: Der Cash liegt in `portfolio_capital_state`.

## 2. Unterstützte Formate

| Format | Option | Datei |
|---|---|---|
| Kanonisches Trading-CSV | `--format canonical` | UTF-8, Komma oder Semikolon getrennt, Dezimalpunkt, ISO-8601-Daten |
| Parqet-Export | `--format parqet` | Semikolon-CSV mit Dezimalkomma (siehe Abschnitt 21) |

`Import-ParqetTransactions.py` gibt es weiterhin und verhält sich wie `Import-TradingTransactions.py --format parqet`.

## 3. Kanonisches CSV

- Eine Kopfzeile, eine Transaktion pro Zeile, Spalten in beliebiger Reihenfolge.
- Das Trennzeichen ist ein Komma, oder ein Semikolon, wenn die Kopfzeile mehr Semikolons als Kommas enthält. Dezimalzahlen haben immer einen Dezimalpunkt.
- Spaltennamen sind nicht von der Groß-/Kleinschreibung abhängig. Unbekannte Spalten werden abgelehnt, damit eine falsch geschriebene Spalte nie stillschweigend eine Gebühr verschluckt.
- Eine UTF-8-Byte-Order-Mark wird akzeptiert.
- Die Spalten stehen in Abschnitt 4; eine vollständige Datei ist [examples/trading-import-example.csv](../examples/trading-import-example.csv).

## 4. Feldreferenz

| Feld | Pflicht | Typ / Format | Bedeutung | Erlaubte Werte | Beispiel |
|---|---|---|---|---|---|
| `transaction_date` | ja | ISO-8601-Datum oder -Zeitpunkt | Zeitpunkt der Transaktion; ein Datum ohne Uhrzeit bedeutet 00:00 UTC, ein Zeitpunkt ohne Offset gilt als UTC | `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SS[.fff][Z or +HH:MM]` | `2026-03-02T10:15:00Z` |
| `transaction_type` | ja | Text | Art der Transaktion | `BUY`, `SELL`, `TRANSFERIN`, `TRANSFEROUT`, `DIVIDEND`, `COST` (ohne Beachtung der Groß-/Kleinschreibung) | `BUY` |
| `isin` | eines von `isin` / `symbol` | 12 Zeichen | ISIN des Wertpapiers | `CC` + 9 Buchstaben oder Ziffern + Ziffer | `US0000000001` |
| `symbol` | eines von `isin` / `symbol` | Text | Ticker des Wertpapiers | beliebig | `EXA` |
| `wkn` | nein | Text | Deutsche Wertpapierkennnummer, dient nur zum Finden des Wertpapiers | beliebig | `A0ABCD` |
| `name` | für neue Wertpapiere | Text | Name des Wertpapiers; dient auch zum Finden | beliebig | `Example Corp A` |
| `shares` | bei BUY, SELL, TRANSFERIN, TRANSFEROUT | Zahl größer 0 | Stückzahl | Dezimalpunkt | `40` |
| `price` | bei BUY, SELL | Zahl, 0 oder mehr | Preis je Stück | Dezimalpunkt | `10.05` |
| `amount` | bei TRANSFERIN, DIVIDEND, COST | Zahl, 0 oder mehr | Bruttobetrag; Vorgabe bei BUY und SELL ist `shares` mal `price` | Dezimalpunkt | `402.00` |
| `fees` | nein | Zahl, 0 oder mehr | Transaktionsgebühren, Vorgabe 0 | Dezimalpunkt | `2.00` |
| `taxes` | nein | Zahl, 0 oder mehr | Einbehaltene Steuern, Vorgabe 0 | Dezimalpunkt | `0` |
| `currency` | ja | 3 Buchstaben | Währung aller Beträge der Zeile | `EUR`, `USD`, ... | `EUR` |
| `broker` | nein | Text | Konto- oder Brokername; Teil der Duplikat-Identität, zwischen den Läufen konstant halten | beliebig | `demo-broker` |
| `external_id` | nein | Text | Eigene eindeutige Zeilen-ID; eine geänderte Zeile wird so zum Konflikt statt zur zweiten Transaktion | eindeutig in Datei und Datenbank | `2026-0001` |
| `realized_gain` | nein | Zahl, darf negativ sein | Vom Broker gemeldeter realisierter Gewinn eines SELL (nur Information) | Dezimalpunkt | `-12.5` |
| `asset_type` | nein | Text | Wird nur beim Anlegen eines Wertpapiers verwendet, Vorgabe `stock` | beliebig | `stock` |
| `notes` | nein | Text | Freie Notiz, wird mit der Transaktion gespeichert | beliebig | `opening position` |

Es gibt weder einen FX-Kurs je Zeile noch eine Spalte `source`: Fremdwährungspositionen werden mit den EZB-Kursen der Datenbank bewertet, und die Quelle eines Imports wird einmal in der Tabelle `imports` festgehalten.

## 5. Transaktionstypen

| Typ | Pflichtfelder | Position | Cost Basis | Cash | Campaign-Abgleich |
|---|---|---|---|---|---|
| `BUY` | `shares`, `price` | erhöht um die Stücke | erhöht um `amount` + `fees` + `taxes`; zählt in `invested_amount` | keine | nach dem Campaign-Start: Event `add` |
| `SELL` | `shares`, `price` | verringert um die Stücke (höchstens die gehaltene Menge) | verringert `remaining_cost_basis` zum Durchschnittskurs `avg_cost` (keine FIFO-Lots) | keine | nach dem Campaign-Start: Event `manual_reduction` |
| `TRANSFERIN` | `shares`, `amount` | erhöht um die Stücke | erhöht um `amount` + `fees` + `taxes`; zählt nicht in `invested_amount` | keine | nie automatisch: manuelle Prüfung (Abschnitt 14) |
| `TRANSFEROUT` | `shares` | verringert um die Stücke | verringert `remaining_cost_basis` zum Durchschnittskurs | keine | nie automatisch: manuelle Prüfung |
| `DIVIDEND` | `amount` | keine | keine | keine | keiner |
| `COST` | `amount` | keine | keine | keine | keiner |

- Die Typen `INTEREST`, `TAX`, `SPLIT` und `FEE` gibt es im Positionsmodell nicht; sie werden mit einer klaren Meldung abgelehnt. Eine einzeln gebuchte Gebühr ist ein `COST`. Aktiensplits werden mit `Manage-TradingCorporateActions.py` erfasst (Tabelle `corporate_action`), nie als Transaktion.
- Der Import ändert nie den Cash. Den Cash erfasst man mit `Manage-TradingCapitalState.py`.

## 6. Datum-, Dezimal- und Währungsregeln

- Kanonisches CSV: nur ISO 8601 (`02.03.2026` wird abgelehnt), nur Dezimalpunkt, kein Tausendertrenner (`1,000.50` und `1,5` werden abgelehnt), keine negativen Zahlen außer `realized_gain`.
- Alle Zeiten werden in UTC mit Millisekunden gespeichert.
- Die Währung muss aus drei Buchstaben bestehen; die Beträge einer Zeile sind in dieser Währung. Man bleibt bei einer Währung je Wertpapier.
- Der Parqet-Adapter behält seine eigenen, nachsichtigeren Zahl- und Datumsregeln.

## 7. Wertpapier-Identifikation

- Eine Zeile wird einem vorhandenen Wertpapier in dieser Reihenfolge zugeordnet, immer exakt und nie unscharf: ISIN, Source-Symbol, Symbol, WKN, normalisierter Name. Ein Symbol, das zu einem Wertpapier mit anderer ISIN gehört, wird als mehrdeutig abgelehnt.
- Nicht gefunden: Die Zeile ist `UNKNOWN_SECURITY`, und es wird nichts geraten. Mit `--create-securities` legt der Import das Wertpapier aus der Datei an (braucht `name` sowie `isin` oder `symbol`; Währung und Asset-Typ kommen aus der Zeile). Ohne Yahoo-Zuordnung hat ein neues Wertpapier noch keine Marktdaten; siehe Abschnitt 18.
- Ein Wertpapier wird pro Datei nur einmal angelegt, auch wenn es viele Zeilen hat.

## 8. Duplikatbehandlung

- Eine Zeile ist ein `DUPLICATE`, wenn die Datenbank schon eine Transaktion mit demselben Wertpapier, Typ, Zeitpunkt, denselben Stücken, demselben Preis, Betrag, denselben Gebühren, Steuern, derselben Währung und demselben Broker enthält. Das Format spielt keine Rolle: Ein aus Parqet importierter Trade ist im kanonischen CSV ein Duplikat und umgekehrt.
- Eine Zeile, die in Wertpapier, Typ und Zeitpunkt zu einer vorhandenen Transaktion passt, aber in einem Wert abweicht, oder deren `external_id` mit anderen Werten schon existiert, ist ein `CONFLICT`. Sie wird nie geschrieben und wird gemeldet.
- Dieselbe Zeile zweimal in einer Datei ist für die zweite ein `CONFLICT`.
- Dieselbe Datei erneut zu importieren fügt daher nichts ein, legt keine Position, Zuordnung, Campaign oder kein Event an und schreibt nicht einmal eine Zeile in `imports`.

## 9. Historische Transaktionen

- Eine neue Zeile mit Datum vor der jüngsten Transaktion der Datenbank ist `NEW_HISTORICAL`. Sie wird übersprungen, außer `--include-historical` ist gesetzt, und das Überspringen wird als offener Punkt gemeldet.
- Die erste vollständige Historie in eine leere Datenbank braucht kein Flag. Alte Historie eines neuen Wertpapiers in eine gefüllte Datenbank zu laden braucht es.

## 10. Neue Positionen

- Nach den Transaktionen wird jedes Wertpapier der Datei mit gehaltener Position geprüft. Eine Position ist neu, wenn sie vor dem Import nicht existierte (`new_positions` im Ergebnis).
- Positionen werden aus allen Transaktionen des Wertpapiers neu aufgebaut, Split-bewusst und mit Durchschnittskosten, genau wie bei jedem anderen Import.

## 11. Strategie-Zuordnung

- Eine gehaltene Position ohne aktive Strategie-Zuordnung wird als `STRATEGY_ASSIGNMENT_REQUIRED` gemeldet. Die Strategie wird nie geraten.
- `--strategy SYMBOL=swing` oder `--strategy SYMBOL=long_term` (wiederholbar) setzt sie. `SYMBOL` ist das Symbol oder die ISIN eines Wertpapiers der Datei.
- `effective_from` ist der Tag, an dem die aktuell gehaltene Position begann: die erste erhöhende Transaktion, nachdem die Position zuletzt flach war. Es ist nie das heutige Datum. Überlappt eine frühere Zuordnung diesen Tag, wird nichts geschrieben und `STRATEGY_EFFECTIVE_FROM_UNCLEAR` gemeldet.
- Eine vorhandene Zuordnung wird nie geändert. Verlangt `--strategy` eine andere Strategie als die aktive, wird `STRATEGY_CONFLICT` gemeldet.
- Eine `long_term`-Position bekommt ihre Zuordnung und nichts weiter: keine Campaign. Sie ist für den Orchestrator vollständig, sobald ein Kurs vorhanden ist.

## 12. Initialisierung der Swing-Campaign

- Eine `swing`-Position ohne offene Campaign bekommt eine, wenn der Start eindeutig ist. Sonst wird `CAMPAIGN_INITIALIZATION_REQUIRED` mit dem Grund gemeldet, und nichts wird erfunden.
- Automatischer Start: Die Transaktion, die die gehaltene Position eröffnet hat, ist Teil der importierten Datei und ein einzelner `BUY` an ihrem Tag. Dann ist `opened_at` dieser Tag, `original_quantity` die Stücke dieses `BUY`, `reference_avg_cost` seine Kosten je Stück inklusive Gebühren, `reference_currency` seine Währung, und das Baseline-Event wird mit dieser Transaktion verknüpft.
- Expliziter Start: `--campaign-opened-at SYMBOL=YYYY-MM-DD`. Die Baseline ist die vor diesem Tag gehaltene Menge mit ihrem Durchschnittskurs; alle Trades an oder nach diesem Tag werden als Events abgeglichen. Das Datum darf weder vor dem Beginn der Position noch in der Zukunft liegen.
- Die Campaign wird in einem Savepoint angelegt, abgeglichen und geprüft. Geht sie danach nicht exakt auf, wird sie nicht angelegt und der Grund gemeldet, während die Transaktionen importiert bleiben.
- `source` der Campaign ist das Importformat mit dem Suffix `_import`, `rationale` nennt, wie der Start abgeleitet wurde.

## 13. Campaign-Abgleich

- Nach dem Start einer offenen Campaign wird jeder noch nicht verknüpfte `BUY` zu einem `add`-Event und jeder `SELL` zu einem `manual_reduction`-Event, mit exakter Menge und exaktem Preis und mit der Transaktion verknüpft. TP1-, TP2- und Stop-Gründe werden nie aus einem Broker-Trade abgeleitet.
- Mehrere Trades am Starttag, Transfers, Überverkauf oder ein Split nach dem Trade werden nicht abgeglichen; das Ergebnis ist `MANUAL_REVIEW_REQUIRED`, und für diese Campaign wird nichts geschrieben.
- Der Abgleich läuft für jedes Wertpapier der Datei und ist idempotent; ein zweiter Lauf schreibt kein Event.
- Bereits importierte Trades lassen sich später mit `--reconcile-campaigns` abgleichen.

## 14. Semantik von TransferIn

- Ein `TRANSFERIN` bringt nur Stücke samt Cost Basis in das Konto. Sein Datum ist nicht der Start einer Swing-Campaign, denn die Entscheidung, die Position als Swing zu handeln, fällt später.
- Ohne explizites Datum bekommt eine Position, die durch einen `TRANSFERIN` eröffnet wurde, ihre Strategie, die Campaign wird aber als `CAMPAIGN_INITIALIZATION_REQUIRED` gemeldet (Grund `TRANSFERIN_IS_NOT_A_CAMPAIGN_START`).
- Man nutzt `--campaign-opened-at SYMBOL=YYYY-MM-DD` mit dem Tag, an dem der Swing begann. Die Baseline ist die vor diesem Tag gehaltene Menge. Gibt man den Transfertag selbst an, ist die Baseline die übertragene Menge, verknüpft mit dem Transfer.
- Ein `TRANSFERIN` oder `TRANSFEROUT` nach dem Start einer Campaign braucht eine manuelle Prüfung.

## 15. Dry-Run

- Ohne `--write` simuliert der Import den vollständigen Schreibpfad auf einer In-Memory-Kopie der Datenbank. Die echte Datenbank wird nur gelesen und bleibt Byte für Byte unverändert; weder ein Backup noch eine Datei entsteht.
- Die Vorschau zeigt die Klassifikation jeder Zeile und den geplanten Endzustand: eingefügte Transaktionen, neue Wertpapiere und Positionen, Strategie-Zuordnungen, Campaigns, Abgleich und alle offenen Punkte. Ein Write mit denselben Argumenten erzeugt genau diesen Zustand.

```
python tools\trading\Import-TradingTransactions.py --format canonical --csv examples\trading-import-example.csv --db-path <db> --create-securities --strategy EXA=swing --strategy EXB=long_term --strategy EXC=long_term --strategy EXD=long_term
```

## 16. Schreiben und Importieren

- `--write` baut den Plan neu auf, prüft, dass sich seither nichts geändert hat, und läuft dann in einer `BEGIN IMMEDIATE`-Transaktion: Wertpapiere, Transaktionen, Positionen, Strategie-Zuordnungen, Campaigns, Abgleich, Duplikat-Audit, Integritätsprüfung, Zeile in `imports`. Jeder Fehler rollt alles zurück.
- Ändert sich nichts, wird die Transaktion zurückgerollt; es gibt weder ein Backup noch eine Zeile in `imports`.
- Zeilen, die nicht importierbar sind (Konflikt, unbekanntes Wertpapier, ungültig), blockieren die gültigen Zeilen nie; sie stehen in `blocked_row_numbers`.

```
python tools\trading\Import-TradingTransactions.py --format canonical --csv my-transactions.csv --db-path <db> --write
```

## 17. Backups

- Ein Write, der etwas ändert, sichert zuerst die Datenbank als `<db>.<format>-import-<timestamp>.bak` neben der Datenbankdatei. Ein vorhandenes Backup wird nie überschrieben.
- Das Backup enthält den Zustand vor dem Import. Zum Zurückgehen stellt man diese Datei wieder her.

## 18. Validierung

- Nach dem Write prüft der Import die ganze Datenbank: `PRAGMA integrity_check`, Fremdschlüssel, keine doppelte wirtschaftliche Identität und keine doppelte `external_id`.
- Je gehaltener Position wird die Bereitschaft für den Orchestrator geprüft und alles Fehlende in `open_items` aufgelistet: Strategie-Zuordnung, Campaign-Start, Campaign-Abgleich und `MARKET_DATA_MISSING` (kein aktueller Kurs in `market_snapshot` oder keine Historie in `market_data`; `Resolve-TradingSecurities.py` und `Backfill-TradingMarketData.py` ausführen).
- `validation_status` ist `COMPLETE`, wenn kein offener Punkt bleibt, sonst `INCOMPLETE`. Vollständig heißt: Strategie bekannt, Swing-Campaign mit Delta 0 abgeglichen, Kurse vorhanden.
- Sind die Transaktionen gültig, lassen sich Strategie oder Campaign aber nicht eindeutig vollständig anlegen, bleiben die Transaktionen importiert, `validation_status` ist `INCOMPLETE`, und `open_items` nennt den genauen Grund. Nichts wird stillschweigend ignoriert, und ein falsches `COMPLETE` erscheint nie. Typische Punkte: `STRATEGY_ASSIGNMENT_REQUIRED` (keine Strategie angegeben), `CAMPAIGN_INITIALIZATION_REQUIRED` (der Start ist nicht ableitbar, zum Beispiel nach einem `TRANSFERIN`, oder die neue Campaign ginge nicht exakt auf) und `CAMPAIGN_RECONCILIATION_REQUIRED` mit dem Status `MANUAL_REVIEW_REQUIRED` (eine bestehende Campaign trifft auf einen Transfer, einen Überverkauf oder mehrere Trades desselben Tages). Nicht importierte Zeilen erscheinen als `ROWS_BLOCKED` und `HISTORICAL_ROWS_NOT_IMPORTED`.
- `INCOMPLETE` ist kein Fehler, und der Exit-Code bleibt 0. Man löst den Punkt, zum Beispiel mit `--strategy` oder `--campaign-opened-at`, und führt denselben Befehl erneut aus: Alles bereits Erledigte wird als Duplikat oder als vorhanden erkannt, daher wird nichts doppelt geschrieben.

## 19. Felder des Importergebnisses

| Feld | Bedeutung |
|---|---|
| `records_total` | Zeilen der Datei |
| `inserted` | geschriebene Transaktionen |
| `duplicates` | Zeilen, die schon in der Datenbank sind |
| `historical_skipped` | übersprungene historische Zeilen |
| `historical_inserted` | mit `--include-historical` geschriebene historische Zeilen |
| `conflicts` | als Konflikt abgelehnte Zeilen |
| `failed` | Zeilen, die nicht verarbeitet werden konnten: ungültige Zeilen plus Zeilen mit unbekanntem Wertpapier. Duplikate, übersprungene historische Zeilen, Konflikte, `strategy_required` und `campaign_initialization_required` werden in eigenen Feldern gezählt und nie hier |
| `new_securities` | angelegte Wertpapiere |
| `new_positions` | Positionen, die vorher nicht existierten |
| `strategy_assignments_created` | geschriebene Zuordnungen |
| `strategy_required` | Positionen, die noch `--strategy` brauchen |
| `campaigns_created` | gestartete Swing-Campaigns |
| `campaigns_reconciled` | Campaigns, die Abgleich-Events bekamen |
| `campaign_initialization_required` | Campaigns, die `--campaign-opened-at` oder Handarbeit brauchen |
| `validation_status` | `COMPLETE` oder `INCOMPLETE` |

- Das Ergebnis enthält außerdem `open_items` (Code, Wertpapier, Detail), `created_securities`, `strategy_assignments`, `campaigns_created`, `campaign_reconciliation`, `lifecycle`, `validation`, `backup_path` und `blocked_row_numbers`. Mit `--json` wird es als JSON ausgegeben. Die Spalte `records_failed` der Tabelle `imports` enthält dieselbe Zahl wie `failed`.

## 20. Fehler und Konflikte

| Meldung | Ursache | Was zu tun ist |
|---|---|---|
| `CSV has unknown columns` | ein Spaltenname gehört nicht zum kanonischen Format | Spalte umbenennen oder entfernen |
| `CSV missing required columns` | `transaction_date`, `transaction_type`, `currency` oder `isin`/`symbol` fehlt | Spalte ergänzen |
| `unsupported transaction type` | zum Beispiel `TAX`, `SPLIT`, `INTEREST` oder `FEE` | einen unterstützten Typ verwenden; einzelne Gebühren sind `COST` |
| `UNKNOWN_SECURITY` | das Wertpapier ist nicht in der Datenbank | `--create-securities` (mit Namen) ergänzen oder es vorher anlegen |
| `CONFLICT` | derselbe Trade mit anderen Werten, oder wiederholte Zeile | Datei oder alte Transaktion bewusst korrigieren |
| `import plan is stale` | Datei oder Datenbank haben sich zwischen Vorschau und Write geändert | den Befehl erneut ausführen |
| `STRATEGY_ASSIGNMENT_REQUIRED` | gehaltene Position ohne Strategie | `--strategy` angeben |
| `CAMPAIGN_INITIALIZATION_REQUIRED` | der Campaign-Start ist nicht ableitbar | `--campaign-opened-at` angeben |

## 21. Parqet-Adapter

- `--format parqet` liest das Parqet-Semikolon-CSV mit seinen deutschen oder englischen Spaltennamen und Dezimalkomma und bildet jede Zeile auf das kanonische Modell ab. Bestehende Parqet-Abläufe bleiben unverändert: kumulative Exporte, Duplikat- und Historical-Erkennung, Dry-Run, Write mit Backup, Konflikterkennung und `--reconcile-campaigns`.
- Parqet-Zeilen behalten die Identität `parqet:` + Fingerprint in `external_id`, den Importtyp `PARQET_CSV_INCREMENTAL` in `imports` und die Event-Quelle `parqet_reconciliation`.
- `--strategy`, `--campaign-opened-at`, `--create-securities` und die automatische Campaign-Initialisierung funktionieren für Parqet-Dateien genau wie für das kanonische CSV.

## 22. Import ohne Parqet

- Die Transaktionen als kanonisches CSV exportieren oder schreiben (Abschnitt 4), zum Beispiel aus einer Tabellenkalkulation; ein weiteres Werkzeug ist nicht nötig.
- Den Dry-Run mit `--create-securities` und einer `--strategy` für jedes gehaltene Wertpapier ausführen, den geplanten Zustand lesen und dann mit `--write` erneut ausführen.
- Zum Schluss die offenen Punkte füllen: Kurse mit `Resolve-TradingSecurities.py` und `Backfill-TradingMarketData.py`, Cash mit `Manage-TradingCapitalState.py`.

## 23. Beispiel-CSV

- [examples/trading-import-example.csv](../examples/trading-import-example.csv) enthält nur synthetische Daten: vier erfundene Unternehmen, `BUY`, `SELL`, `TRANSFERIN`, `DIVIDEND`, `COST`, Euro und eine US-Dollar-Position.
- Ein Test importiert diese Datei wirklich, daher ist das Beispiel immer gültig.

## 24. Durchgängige Beispiele

```
python tools\trading\Import-TradingTransactions.py --format canonical --csv examples\trading-import-example.csv --db-path <db> --create-securities --strategy EXA=swing --strategy EXB=long_term --strategy EXC=long_term --strategy EXD=long_term --write
python tools\trading\Import-TradingTransactions.py --format canonical --csv more.csv --db-path <db> --campaign-opened-at EXD=2026-03-15 --strategy EXD=swing
python tools\trading\Import-ParqetTransactions.py --csv parqet-export.csv --db-path <db> --include-historical
```

- Erster Befehl: neue Wertpapiere und Positionen, die vier Zuordnungen, die Swing-Campaign von `EXA` mit ihrer Baseline und den späteren Trades als Events.
- Zweiter Befehl: Die Position `EXD` kam per Transfer; die Campaign startet am angegebenen Tag mit der davor gehaltenen Menge.
- Dritter Befehl: die übliche Parqet-Vorschau, jetzt einschließlich historischer Zeilen.
