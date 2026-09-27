# OpenWebUI-Tool `trading_sqlite`

## Zweck

Stellt dem Trading Agent (OpenWebUI-Modell `trading-agent`) lesenden und schreibenden
SQLite-Zugriff auf die Trading-DB bereit, inklusive einer read-only Watchlist-Rangliste
(`rank_watchlist()`) auf Basis der technischen Analysebibliothek `trading_analytics.py`.

## Speicherorte

| Was | Wo |
|---|---|
| Produktive OpenWebUI-DB | `C:\KI-Stack\OpenWebUI\data\webui.db` |
| Tool-ID (Tabelle `tool`) | `trading_sqlite` |
| Quellcode im Repo | `openwebui-tools\trading_sqlite.py` |
| Analytics-Modul (dynamisch nachgeladen) | `C:\KI-Stack\Tools\trading\trading_analytics.py` |
| Parqet-Importmodule (dynamisch nachgeladen) | `C:\KI-Stack\Tools\trading\parqet_import.py`, `openwebui_upload_resolver.py`, `swing_lifecycle.py`, `analysis_contracts.py` |

Die Methode `rank_watchlist()` des Tools lädt `trading_analytics.py` zur Laufzeit
(gecacht, siehe unten) nach und ruft dessen `rank_watchlist(connection)` auf einer
read-only Verbindung auf. Keine eigene DB-Logik, keine Duplikation.

**Runtime-Modul-Caching (seit 2026-09-27):** `_load_runtime_trading_module()` cacht jedes
geladene Hilfsmodul (`trading_analytics`, `trading_orchestrator`, `swing_promotion`,
`candidate_decision`, `strategy_suggestion`, `parqet_import`, …) klassenweit für die
Lebensdauer der aktuell geladenen Tool-Version — ein Modul wird nicht mehr bei jedem
Toolcall neu von der Festplatte gelesen und `exec_module()`-ausgeführt, sondern nur beim
jeweils ersten Aufruf. **Wichtige Konsequenz für Deploys:** Wird eine Runtime-Datei unter
`C:\KI-Stack\Tools\trading\` (z. B. `trading_analytics.py`) aktualisiert, greift das
**nicht** mehr automatisch beim nächsten Toolcall — der Cache wird erst durch ein
Redeploy von `trading_sqlite.py`s eigenem `content` (das der zugehörigen Python-Modulinstanz
neu erzeugt) oder einen OpenWebUI-Prozessneustart zurückgesetzt. Der bisherige
Deploy-Ablauf unten bleibt dafür ausreichend, solange nach einer Runtime-Modul-Änderung
zusätzlich `trading_sqlite.py` selbst redeployt wird.

## Aktuelle Funktionsübersicht (Version 1.4.0)

| Methode | Read-only? | Bemerkung |
|---|---|---|
| `sql_execute(sql)` | **Nein — uneingeschränkt** | Tool-Beschreibung: "Direct unrestricted SQLite access". Öffnet eine normale Read-Write-Verbindung (`isolation_level="DEFERRED"`); Mehrfach-Statements in einer Transaktion, komplettes Rollback bei Fehler. Valves: `database_path` (Default `C:\KI-Stack\data\trading\trading.db`), `timeout_seconds=1.0`, `busy_timeout_ms=1000`, `max_rows_per_statement=500`. |
| `database_tables()` | Ja | Tabellenliste |
| `database_schema()` | Ja | Vollständiger Schema-Dump |
| `table_info(table)` | Ja | Spalteninfo je Tabelle |
| `database_status()` | Ja | Summary-Counts/Integrity |
| `rank_watchlist()` | Ja | Lädt `trading_analytics.py` über den gecachten `_load_runtime_trading_module()`-Pfad; nutzt seit 2026-09-27 dieselbe read-only Verbindung (`_readonly_import_connection`) wie die übrigen Analytics-Wrapper (vorher: Read-Write-Verbindung, Asymmetrie jetzt behoben) |
| `run_trading_orchestrator(as_of=None)` | Ja | Lädt `trading_orchestrator.py` samt Abhängigkeitskette dynamisch nach |
| `evaluate_swing_candidate(security_id)` / `evaluate_swing_candidates()` | Ja | Lädt `swing_promotion.py` dynamisch nach |
| `approve_swing_promotion(...)` | **Nein — schreibt** `strategy_assignment` + `candidate_promotion` | Benötigt ein noch aktuelles Plan-Token aus `evaluate_swing_candidate` |
| `preview_parqet_import(uploaded_file_id)` | Ja | Read-only SQLite-Verbindung |
| `apply_parqet_import(uploaded_file_id, plan_token, include_historical=False)` | **Nein — schreibt** `transactions` + `positions` | Legt vorher ein DB-Backup an; akzeptiert ausschließlich eine OpenWebUI-Datei-UUID |
| `suggest_strategy_assignments(as_of=None)` | Ja | **Neu (2026-09-25).** Lädt `strategy_suggestion.py` aus `C:\KI-Stack\Tools\trading\` dynamisch nach; ruft `suggest_strategy_assignments(connection, as_of)` unverändert auf. Vorschlag ≠ Zuordnung — keine Strategy-Assignment-Writes. |
| `evaluate_watchlist_candidates(as_of=None)` | Ja | **Neu (2026-09-25).** Lädt `candidate_decision.py` aus `C:\KI-Stack\Tools\trading\` dynamisch nach; ruft `evaluate_watchlist_candidates(connection, as_of)` unverändert auf. Keine Campaign-Eröffnung, keine Persistierung, keine Orders. |

Beide neuen Methoden folgen exakt dem bereits etablierten Muster von `run_trading_orchestrator()`/`evaluate_swing_candidates()`: `_load_runtime_trading_module()` lädt das produktive Modul aus `C:\KI-Stack\Tools\trading\`, `_readonly_import_connection()` öffnet eine strikte `PRAGMA query_only=ON`-Verbindung. Keine fachliche Logik wurde im Tool dupliziert.

Vollständige Architektur- und Konstanten-Referenz: [trading-agent-architecture.md](trading-agent-architecture.md).

## OpenWebUI-Besonderheit: `content` vs. `specs`

OpenWebUI speichert Tool-Code je Zeile in der Spalte `content`. Zusätzlich existiert die
Spalte `specs`: das daraus abgeleitete JSON-Funktionsschema, das dem LLM zur Toolauswahl
angezeigt wird.

**Wichtig:** Ein direktes SQL-Update von `content` aktualisiert `specs` NICHT automatisch.
Ohne neu generierte `specs` sieht das LLM neue/geänderte Tool-Funktionen nicht, auch wenn
der Code bereits korrekt ist. Nach jeder Änderung an Tool-Funktionen muss `specs` mit
OpenWebUIs eigener `get_tool_specs()`-Funktion (`open_webui.utils.tools`) neu generiert
werden. Danach muss OpenWebUI neu geladen/neu gestartet werden, damit die geänderte
Tool-Spezifikation sicher aktiv wird.

## Parqet-Inkrementalimport (Phase 3B.6)

`preview_parqet_import(uploaded_file_id)` und
`apply_parqet_import(uploaded_file_id, plan_token, include_historical=False)`
nehmen ausschließlich eine kanonische OpenWebUI-Datei-UUID entgegen. Sie nehmen
**keinen** lokalen Dateipfad, keine URL und keinen Dateinamen als Zugriffsschlüssel.

OpenWebUI injiziert für Toolaufrufe den versteckten Kontext `__files__` (die zum
Chat gehörenden Attachment-Objekte) und `__user__`. Der Resolver verlangt, dass die
UUID in `__files__` enthalten ist, liest den Datensatz ausschließlich über
`open_webui.models.files.Files`, prüft Eigentum bzw. OpenWebUIs reguläre
`has_access_to_file(..., "read", ...)`-Freigabe und materialisiert über den
konfigurierten `Storage`-Provider. Der endgültige Pfad muss nach Auflösung innerhalb
von `open_webui.config.UPLOAD_DIR` liegen, lesbar sein und eine UTF-8-Parqet-CSV
(`.csv`, CSV-Content-Type sowie erwartete Kopfzeile) darstellen. Somit können
modellgenerierte Windows-Pfade und Traversal nie den Importer erreichen.

Die Vorschau verwendet eine SQLite-Read-only-Verbindung, gibt keinen Upload-Pfad aus
und liefert Dateihash, DB-Fingerprint, Plan-Token, Zähler und alle nicht-duplizierten
Zeilen. Apply löst dieselbe UUID nochmals auf und lehnt ab mit `FILE_CHANGED`,
`DB_CHANGED` oder `PLAN_STALE`, sobald Datei, relevante Transaktionsdaten oder der
normalisierte Plan von der Vorschau abweichen. Ein OpenWebUI-Reload verwirft den
flüchtigen Preview-Cache ebenfalls sicher als `PLAN_STALE`.

Der Nutzer muss Apply nach einer Vorschau ausdrücklich anfordern. `NEW`-Zeilen können
geschrieben werden; `NEW_HISTORICAL` nur bei explizitem `include_historical=true`.
`CONFLICT`, `UNKNOWN_SECURITY` und `INVALID` werden nie geschrieben. Der Importkern
erstellt vor einem Write ein SQLite-Backup, arbeitet transaktional, baut Positionen
kanonisch neu auf und leitet keine Lifecycle-Ereignisse ab.

## Deploy-Ablauf

1. Backup von `C:\KI-Stack\OpenWebUI\data\webui.db` anlegen.
2. Die vier oben genannten Helper nach `C:\KI-Stack\Tools\trading` kopieren.
3. Ausschließlich die Tool-Zeile `id='trading_sqlite'` ändern und `content` aus
   `openwebui-tools\trading_sqlite.py` übernehmen.
4. `specs` mit OpenWebUIs eigener `get_tool_specs()`-Funktion neu generieren;
   versteckte `__...__`-Parameter bleiben aus dem öffentlichen Schema entfernt.
5. Prüfen, dass keine andere Tool-Zeile geändert wurde.
6. Den OpenWebUI-Serverprozess gezielt neu starten oder dessen Tool-Cache neu laden;
   ein Vollstack-Neustart ist nicht erforderlich.
7. `rank_watchlist()` und einen `sql_execute()`-Read testen, dann eine hochgeladene
   CSV ausschließlich per `preview_parqet_import()` prüfen.

## Verifizierter Teststand (21.09.2026)

- `rank_watchlist()`: `ok=True`, `count=53`, Sortierung absteigend korrekt
- Top 3: AMD 65.63, INGA 60.78, HALO 57.97
- Bottom 3: CPNG -83.64, GRAB -86.85, XRO -89.52
- DB unverändert: `security=81`, `watchlist=53`, `market_data=27699`, `fundamentals=445`,
  `integrity_check=ok`
- `sql_execute()`-Read: `SELECT COUNT(*) FROM watchlist WHERE status='WATCH'` → `53`

**Testhinweis:** Im echten Trading Agent wurde `rank_watchlist()` korrekt als Toolcall
ausgewählt. Der vollständige automatische serverseitige Tool-Loop (bis zur finalen
Chat-Antwort) konnte in der headless Testumgebung nicht beobachtet werden. Das von
OpenWebUI tatsächlich verwendete Callable wurde separat erfolgreich ausgeführt.

## Verifizierter Teststand (25.09.2026) — `suggest_strategy_assignments()` / `evaluate_watchlist_candidates()`

- `content`/`specs` aktualisiert auf Version `1.4.0`, 14 Funktionen (vorher 12).
- Andere Tool-Zeilen (`chroma1_hd_test_bildgenerierung`, `ki_stack_ballistics_calculator`,
  `ki_stack_generate_image`, `ki_stack_generate_video`, `trading_market_data`) per
  Fingerprint-Vergleich vor/nach unverändert bestätigt.
- Kein laufender OpenWebUI-Prozess/Port zum Zeitpunkt des Deploys gefunden — kein
  Neustart nötig; Content/Specs werden beim nächsten Start ohnehin frisch aus der DB
  gelesen (kein In-Process-Cache, siehe Abschnitt oben).
- Smoke-Test lud den tatsächlich in `webui.db` gespeicherten `content` (nicht die
  Quelldatei) und führte ihn exakt so aus, wie OpenWebUI es täte:
  - `suggest_strategy_assignments()`: `ok=True`, `count=46`,
    `counts={'swing': 13, 'long_term': 0, 'unknown': 33}`
  - `evaluate_watchlist_candidates()`: `ok=True`, `count=53`,
    `counts={'BUY': 0, 'WATCH': 7, 'DEFERRED': 46, 'INSUFFICIENT_DATA': 0}`
  - `sql_execute()` weiterhin funktionsfähig (`ok=True`)
- `trading.db`-Hash vor/nach dem Smoke-Test identisch → keine Schreibvorgänge.
- `webui.db`: `PRAGMA integrity_check` → `ok`.

## Performance-Optimierung (27.09.2026)

Drei anhand einer Baseline-Messung identifizierte Hotspots behoben (kein Architektur-,
kein DB-Schema-, kein Output-Format-Wechsel):

1. `trading_orchestrator.py`: `build_portfolio_context()` wird jetzt genau einmal pro
   Lauf berechnet statt einmal je offener Position und je Entry-Kandidat neu; die
   security-spezifischen Felder werden aus den bereits vorhandenen `exposures` abgeleitet
   (`_portfolio_context_for_security()`). **3973 → 1617 SQL-Queries** (−59 %),
   **135 ms → 80 ms** Ø-Laufzeit (direkt) bzw. **144 ms → 88 ms** (Wrapper).
2. `trading_analytics.py`: `_get_yahoo_source_id()` wird pro `analyze_security(_as_of)`-Aufruf
   nur noch einmal aufgelöst (vorher zweimal: einmal in `_load_price_series`, einmal in
   `_load_last_trade_date`) und lokal weitergereicht — kein globaler Cache.
3. `openwebui-tools/trading_sqlite.py`: `_load_runtime_trading_module()` cacht jedes
   geladene Modul jetzt klassenweit (siehe Abschnitt oben); `rank_watchlist()`s Wrapper
   nutzt jetzt dieselbe read-only Verbindung wie die übrigen Analytics-Wrapper.

Ergebnisvergleich (alte vs. neue Implementierung, identische Produktions-DB, read-only):
`run_trading_orchestrator()` und `rank_watchlist()` liefern **byte-identisches JSON** —
keine fachliche Änderung. Volle Regressionssuite (218/218) weiterhin grün.

## Performance-Optimierung, Runde 2 (27.09.2026)

Vier weitere, in Runde 1 gemessene Redundanzen behoben:

1. **`PRAGMA integrity_check` entfernt aus `_global_readiness_issues()`** — lief bisher bei
   *jedem* `run_trading_orchestrator()`-Lauf und war mit ~32 ms die teuerste Einzeloperation
   im gesamten Lauf. Integritätsprüfung bleibt weiterhin über die dafür vorgesehenen
   expliziten Pfade verfügbar (`Initialize-TradingDatabase.py`/`Reset-TradingDb.py`s eigene
   `validate_database()`, `parqet_import.py`s Post-Write-Check) — keine Ersatzprüfung
   im normalen Analyse-Lauf eingeführt, keine fachlich relevante Prüfung verloren.
2. **`swing_promotion.evaluate_swing_promotion()`**: `_open_campaign()` wird nur noch
   einmal je Security aufgerufen (Ergebnis lokal wiederverwendet, statt denselben
   Query zweimal für Entscheidung und Token-Payload abzusetzen) — **92 → 46 Aufrufe**.
3. **`analysis_engine.build_analysis_snapshot()`**: `build_portfolio_context()` erhält jetzt
   die vom Orchestrator bereits gebauten Snapshots der offenen Positionen
   (`position_snapshots`-Parameter) und baut sie nicht mehr selbst zusätzlich auf.
4. **Lauf-invariante `sqlite_master`-Checks** (`strategy_assignment`, `fx_rates`,
   `swing_campaign` vorhanden?): alle drei Module (`analysis_engine.py`, `fx_resolver.py`,
   `swing_promotion.py`) nehmen jetzt einen optionalen `table_exists_cache`-Parameter
   entgegen, den der Orchestrator einmal pro Lauf anlegt und durchreicht — **kein globaler,
   dauerhaft stale Cache**, nur Wiederverwendung innerhalb desselben Aufrufs; jeder
   Aufrufer ohne diesen Parameter verhält sich exakt wie zuvor (Default `None`).

Gemessen (identische Methode, Alt- und Neu-Code im selben Prozess gegen denselben
DB-Zustand, 5 Läufe, `run_trading_orchestrator()` direkt):

| Metrik | vorher | nachher |
|---|---|---|
| Ø Laufzeit | 83,54 ms | 45,44 ms (−45,6 %) |
| SQL-Queries | 1617 | 1168 (−27,8 %) |
| SQL-Gesamtzeit (Ø) | 49,81 ms | 16,87 ms (−66,1 %) |
| `build_analysis_snapshot()`-Aufrufe | 56 | 56 |
| `_open_campaign()`-Aufrufe | 92 | 46 |
| `sqlite_master`-Queries | 247 | 27 (−89,1 %) |
| `PRAGMA integrity_check`-Aufrufe | 1 | 0 |

Alt- vs. Neu-Code liefert gegen denselben DB-Zustand **byte-identisches JSON** für
`run_trading_orchestrator()` — keine fachliche Änderung. Regressionssuite weiterhin
218/218 grün.

## Performance-Optimierung, Runde 3 (27.09.2026)

Ein per Python-Profiling identifizierter, cross-cutting Hotspot behoben:

`trading_analytics.py::annualized_volatility_pct()` berechnete die Tages-Volatilität
bisher mit `statistics.pstdev()`. Diese Funktion konvertiert intern jeden Float exakt
in einen `fractions.Fraction` (inkl. `math.gcd`-Reduktion), um mathematisch exakte statt
Fließkomma-Ergebnisse zu liefern — für eine prozentuale Annäherung unnötiger Overhead,
der in jeder Funktion auftrat, die irgendeine Security technisch analysiert
(`rank_watchlist`, `suggest_strategy_assignments`, `evaluate_watchlist_candidates`,
`evaluate_swing_candidates`, `run_trading_orchestrator`, `analyze_security_as_of`).
Ersetzt durch eine einfache Float-basierte Populations-Standardabweichung
(`Varianz = Summe((x - Mittelwert)^2) / n`, `Standardabweichung = sqrt(Varianz)`) —
mathematisch identische Definition, nur ohne Exakt-Arithmetik. Keine Änderung an
Annualisierung oder Prozent-Semantik, keine neue Dependency.

Gemessen (identische cProfile-Methode, gemergt über alle sechs Zielfunktionen, je 5 Läufe):

| Metrik | vorher | nachher |
|---|---|---|
| Gemergte cProfile-Gesamtzeit | 2,246 s | 1,818 s (−19,1 %) |
| Gemergte Funktionsaufrufe | 3.360.891 | 2.416.977 (−28,1 %) |
| `annualized_volatility_pct()` kumulative Zeit | 0,403 s | 0,041 s (−89,8 %) |
| `math.gcd`-Aufrufe | 206.570 | 20.000* |
| `fractions.__new__`-Aufrufe | 76.430 | 7.400* |

\* Restliche Aufrufe stammen ausschließlich noch aus der zum Messzeitpunkt nicht
neu deployten Produktionskopie (`C:\KI-Stack\Tools\trading\trading_analytics.py`,
25 Wrapper-Aufrufe); im gefixten Dev-Repo-Pfad selbst (2470 Aufrufe von
`annualized_volatility_pct()`) tritt die Fraction-Kette nicht mehr auf.

Numerischer Vergleich (5 reale Securities + 6 Edge-Cases: leer, 1 Wert, konstant,
normale Serie, minimale Änderungen, große Werte): maximale absolute Abweichung
`1,4 × 10⁻¹⁴`, maximale relative Abweichung `1,4 × 10⁻¹⁶` — reines Gleitkomma-Rauschen
im letzten Bit, keine fachlich relevante Abweichung. Vollständiger Funktions-JSON-Vergleich
alt/neu zeigt ausschließlich Differenzen ab der 14.–15. signifikanten Nachkommastelle
in `volatility_60d_annualized_pct`-Feldern; alle übrigen Felder byte-identisch.
Regressionssuite weiterhin 218/218 grün.
