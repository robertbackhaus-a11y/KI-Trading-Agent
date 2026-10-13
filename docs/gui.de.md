# Desktop-GUI

English version: [gui.en.md](gui.en.md)

Die Desktop-GUI ist ein überwiegend lesendes Cockpit für den Trading Agent. Sie zeigt, was der Trading-MCP-Server meldet, steuert die geplanten Tasks und bietet einen Chat mit einem lokalen Sprachmodell, das dieselben Daten lesen kann. Sie enthält keine Trading-Logik, keinen Datenbankzugriff und keine Orderfunktion. Geschrieben wird nur über sechs schmale, ausdrückliche MCP-Aktionen der Watchlist-Seite (Abschnitte 3 und 5); der Chat kann nur lesen. Entwicklungsstand: Die GUI hat keine eigene Versionsnummer; sie meldet `dev`.

## 1. Architektur

```
Desktop GUI (PySide6)
  |-- MCP client ----- stdio child process ----- Trading MCP server ----- SQLite
  |-- Chat ----------- Ollama HTTP API (local)   tool calls go through the MCP client
  |-- Task service --- PowerShell -------------- Windows Task Scheduler
  `-- settings file and log files
```

- Trading-Daten kommen ausschließlich aus den READ-Tools des Trading-MCP-Servers. Die ViewModels formatieren nur, was die Tools liefern, und rechnen nichts.
- Der MCP-Server läuft als eigener Kindprozess der GUI. Er wird nie importiert und kann neben einem anderen Client laufen, der seinen eigenen Serverprozess startet.
- Der Windows Task Scheduler wird direkt und nicht über MCP angesprochen, damit kein Sprachmodell einen Task starten kann.
- Der Chat spricht direkt mit dem lokalen Ollama-Server; die GUI startet oder stoppt Ollama nie.

## 2. Installation und Start

Die GUI hat eine eigene virtuelle Umgebung; die Laufzeitumgebung des Trading Agents wird nicht verändert. `requirements-gui.txt` pinnt `PySide6-Essentials` (nicht das Metapaket `PySide6`, das auch WebEngine und die Addons nachzieht) und `mcp` (dieselbe Version wie die Laufzeit).

```
py -3.12 -m venv .venv-gui
.venv-gui\Scripts\python.exe -m pip install -r requirements-gui.txt
.venv-gui\Scripts\python.exe -m gui
```

Die Befehle laufen im Repository-Stammordner. Die Standardwerte zeigen auf die Produktionsinstallation; sie lassen sich in den Einstellungen ändern.

## 3. Seiten

| Seite | Inhalt | Quelle |
|---|---|---|
| Dashboard | Portfolio, Allokation, Cash, External Funding, Planner-Status, geplante und zurückgestellte Entries, Blocker und Warnungen | `run_trading_orchestrator` (Detail), `database_status` |
| Planner / Actions | Engine-Aktionen je Position, geplante und zurückgestellte Entries mit Reason Codes, Sektor- und Korrelationsrisiko, `simulation_only`, `orders_created` | `run_trading_orchestrator` (Detail) |
| Watchlist | die gesamte Watchlist mit Strategie, Priorität, Rang, Score, Status, Datenqualität, News-Status und Bewertungsdatum; Filter und Sortierung; Hinzufügen, Entfernen sowie Strategie und Priorität direkt in der Tabelle bearbeiten | `get_watchlist`, `add_watchlist_candidate`, `remove_watchlist_candidate`, `set_watchlist_priority`, `onboard_security`, `set_security_strategy`, `set_security_sector` |
| Tasks | die sechs geplanten Trading-Tasks: Status, letzter und nächster Lauf, Ergebnis, Run now, Log-Ende | Windows Task Scheduler, Log-Dateien |
| Chat | Fragen an ein lokales Sprachmodell, das über die READ-Tools liest; gestreamte Antwort, optionales Thinking, Tool-Status, Stop, Neuer Chat | Ollama, MCP-Tools |
| Diagnostics / Settings | MCP-, Datenbank- und Ollama-Status, Pfade, Tool-Klassifizierung, Schema-Hash, letzter Chat-Fehler, Einstellungsformular, MCP-Neustart | `database_status`, Einstellungsdatei |

Die Watchlist-Seite ändert die Watchlist nur über schmale, ausdrückliche Aktionen: Hinzufügen (ein Wertpapier, das bereits im Security Master steht; die optionale Prüfung löst es nur auf), Entfernen (nach einer Bestätigung, deren Standard Abbrechen ist; eine gehaltene Position bleibt unberührt) sowie die Bearbeitung von Strategie und Priorität direkt in der Tabelle (unten; es gibt dafür keinen zweiten, sofort schreibenden Dialog). Ein Wertpapier, das nicht im Security Master steht, wird beim Hinzufügen nie angelegt; der Dialog sagt das und bietet dann die Schaltfläche „Neues Wertpapier aufnehmen“ an. Die Aktions-Tools stehen im Chat nicht zur Verfügung.

**Strategie und Priorität direkt in der Tabelle.** Genau zwei Spalten sind bearbeitbar: Strategie (Auswahl: Keine, Swing, Long-Term – nur die Werte, die es gibt) und Priorität (ganze Zahl, leer = keine; die bestehende Semantik). Ein Doppelklick öffnet den Editor; eine Auswahl schreibt **nichts**, sondern markiert die Zeile als „Geändert“ (fett, Markierung in der Spalte „Änderung“). Erst „Änderung übernehmen“ speichert die markierte Zeile: zuerst die Strategie über `set_security_strategy`, nur bei Erfolg danach die Priorität über `set_watchlist_priority`; bei einem Fehler wird nicht blind weitergeschrieben, der nicht gespeicherte Teil bleibt markiert, und die Liste wird neu geladen (das Backend ist die Wahrheit). „Änderung verwerfen“ setzt die Zeile zurück. Ein Neuladen mit ungespeicherten Änderungen fragt „Ungespeicherte Änderungen verwerfen?“ (Standard: Abbrechen); Filter, Sortierung und Seitenwechsel behalten sie. **Gehaltene Positionen** haben keinen Strategie-Editor und eine **aktive Campaign** blockiert die Änderung (lesbare Meldung; der Original-Code steht im Tooltip), denn die Strategie hängt dort am Campaign-Ablauf. Eine Strategie setzt nur die Zuordnung: Es wird keine Campaign angelegt und keine Order erzeugt; ein nicht gehaltenes Wertpapier mit Swing behandelt der Planner danach als Einstiegskandidat (nur Simulation). Das Tool steht im Chat nicht zur Verfügung.

**Sektor in der Tabelle.** Die Spalte „Sektor“ zeigt die Sektor-Gruppe aus der Sector-Map (die der Risk Guard und das News-Mapping verwenden, nicht eine Datenbankspalte). Ein Zusatz „(lokal)“ und der Tooltip zeigen die Herkunft: aus `sector_map_v1.json` (öffentlich), aus `sector_map_local.json` (lokaler Eintrag, der einen öffentlichen Wert ersetzen kann – der ersetzte Wert steht im Tooltip) oder „nicht zugeordnet“ (der Sector-Guard bewertet das Wertpapier dann nicht). Die Auswahl bietet nur vorhandene Gruppen; bei einem lokalen Eintrag zusätzlich „Lokale Zuordnung entfernen“ (es gilt wieder der öffentliche Wert, falls vorhanden). Es läuft derselbe Ablauf wie bei Strategie und Priorität: Auswahl, Markierung als „Geändert“, erst „Änderung übernehmen“ speichert (nach Strategie und Priorität) über `set_security_sector`. Geschrieben wird ausschließlich `sector_map_local.json` (die vorherige Fassung bleibt als `.bak`); `sector_map_v1.json` und die Datenbank bleiben unberührt. Gehaltene Positionen und offene Campaigns blockieren die Sektor-Änderung nicht, denn sie ist eine Klassifikation, kein Lifecycle-Wechsel. Das Tool steht im Chat nicht zur Verfügung.

**Neues Wertpapier aufnehmen.** Der eigene Dialog legt genau ein Wertpapier an, ausschließlich aus den Angaben, die dort stehen: Symbol, Name und Art (Aktie oder ETF) sind Pflicht; ISIN, WKN, Börse, Währung, Land sowie ein Yahoo- und ein FMP-Symbol sind optional. Es wird nichts recherchiert, ergänzt oder geraten (keine ISIN, keine Börsenendung, kein Yahoo-Symbol, kein Sector, keine CIK). „Prüfen“ ist ein Trockenlauf: Er normalisiert die Eingaben, prüft ISIN (mit Prüfziffer), Symbol, WKN und Provider-Symbole gegen den Security Master und zeigt entweder „Keine Konflikte gefunden.“ mit einer Zusammenfassung oder alle Konflikte in Klartext; die technischen Codes stehen nur im Tooltip. „Anlegen“ ist erst nach einer Prüfung ohne Konflikte aktiv und wird bei jeder Änderung der Eingaben wieder gesperrt. Das Anlegen läuft in einer einzigen Transaktion (Wertpapier, Provider-Symbole und auf Wunsch der Watchlist-Eintrag) und wird bei jedem Fehler vollständig zurückgenommen. Existiert das Wertpapier schon, wird kein zweites angelegt und das vorhandene nicht verändert; der Dialog zeigt Symbol, Name und `security_id` und bietet an, es über die gewöhnliche Aktion zur Watchlist hinzuzufügen. Der Sector bleibt „nicht zugeordnet“. Es werden keine Strategie, Kampagne, Position oder Order angelegt und keine Marktdaten, Fundamentaldaten oder News geholt; Marktdaten werden beim nächsten regulären Lauf ergänzt. Das Tool `onboard_security` steht im Chat nicht zur Verfügung.

Technische Reason Codes (z. B. `STOPPED_SWING_MAX_REACHED`) werden als kurzer deutscher Text angezeigt; der Tooltip enthält die Erklärung und den Originalcode. Die Backend-Codes bleiben unverändert, und ein unbekannter Code wird lesbar aufbereitet angezeigt.

## 4. MCP-Client

- Das offizielle `mcp`-SDK startet den Server als stdio-Kindprozess. Die Client-Session gehört einem eigenen QThread mit eigenem asyncio-Loop.
- Alle Pfade sind absolut, insbesondere `TRADING_DB_PATH`; ein relativer Datenbankpfad bricht die lesenden Tools des Servers.
- Jeder Aufruf hat ein Timeout. Etwa alle fünf Sekunden läuft eine billige Lebendprüfung; stirbt der Kindprozess, zeigt der Status einen Fehler und der nächste Aufruf verbindet neu. Restart MCP startet nur den Kindprozess neu und verändert keine Trading-Daten.
- Die stderr-Ausgabe des Servers geht in eine eigene Logdatei.
- Nichts hängt von `capabilities.experimental` ab.

## 5. Lese- und Aktions-Tools

| Klasse | Tools | GUI |
|---|---|---|
| READ | die 14 Lese-Tools, zum Beispiel `database_status`, `run_trading_orchestrator`, `get_watchlist` | erlaubt; die Seiten rufen nur `database_status`, `run_trading_orchestrator` und `get_watchlist` auf, der Chat bietet dem Modell alle 14 an |
| ACTION | `approve_swing_promotion`, `sql_execute` | verweigert und dem Modell nie angeboten |
| ACTION (Watchlist) | `add_watchlist_candidate`, `remove_watchlist_candidate`, `set_watchlist_priority` | nur über die ausdrücklichen Schaltflächen der Watchlist-Seite (`call_action`); dem Modell nie angeboten, von `call` verweigert |
| ACTION (Strategie) | `set_security_strategy` | nur über „Änderung übernehmen“ der Watchlist-Seite (`call_action`); dem Modell nie angeboten, von `call` verweigert |
| ACTION (Sektor) | `set_security_sector` | nur über „Änderung übernehmen“ der Watchlist-Seite (`call_action`); schreibt nur die lokale Sector-Map; dem Modell nie angeboten, von `call` verweigert |
| ACTION (Security Master) | `onboard_security` | nur über den Dialog „Neues Wertpapier aufnehmen“ der Watchlist-Seite (`call_action`, erst Trockenlauf, dann Anlegen); dem Modell nie angeboten, von `call` verweigert |
| unbekannt | jedes Tool, das nicht in der Tabelle steht | verweigert (Deny by default) |

Die Tabelle liegt in `gui/services/tool_policy.py`; ein Test verlangt, dass jedes Tool des Servers klassifiziert ist. Aktionen sind in der Oberfläche sichtbar von Lesezugriffen getrennt und stecken nie hinter einem Lesezugriff.

## 6. Chat

- Der Ollama-Client nutzt nur die Standardbibliothek. Er streamt `/api/chat` (Antworttext, Thinking-Text, Tool Calls), und Stop schließt die HTTP-Verbindung genau dieser einen Anfrage. Die GUI startet, stoppt, lädt oder entlädt weder Ollama noch ein Modell und setzt keine Kontextgröße, sodass sie dieselbe Ollama-Instanz mit anderen Clients teilen kann.
- Modell und Thinking-Schalter kommen aus den Einstellungen `ollama_model` und `think_enabled`; das Modell wird nur dort gewechselt. Das Öffnen der Seite fragt Ollama nur nach Version und Modellnamen; geladen wird das Modell erst durch die erste Frage.
- Der Tool-Loop ist eine kleine eigene Umsetzung ohne jedes Framework: Frage, `/api/chat`, Tool Calls, Prüfung, MCP-Aufruf, Ergebnis als Tool-Nachricht, wieder `/api/chat`, bis zur Antwort. Höchstens 6 Tool-Runden je Frage; danach endet die Anfrage mit einem Hinweis.
- Angeboten werden nur die READ-Tools der MCP-Discovery, mit Name, Beschreibung und Schema unverändert. Ein Aufruf von allem anderen wird mit `TOOL_NOT_ALLOWED` beantwortet und erreicht den MCP-Server nie. Argumente werden vorher validiert (`TOOL_ARGS_INVALID`). Die Parameter `detail` und `table` werden aus den angebotenen Schemas entfernt und auf false erzwungen (nur bei Tools, die sie haben), damit die kompakten Ansichten in den Kontext passen. Ein Ergebnis über 40.000 Zeichen (`TOOL_RESULT_TOO_LARGE`) oder jenseits von 80.000 Zeichen je Frage (`TOOL_RESULT_BUDGET`) wird dem Modell nicht übergeben.
- Der Verlauf zeigt Nutzer, Assistent, Thinking, Tool-Aufruf, Tool-Status und Fehler getrennt. Tool-Ergebnisse werden nie vollständig gezeigt: nur Tool-Name, Dauer und Größe.
- Es läuft nur eine Anfrage gleichzeitig. Der Verlauf bleibt im Arbeitsspeicher, Neuer Chat setzt ihn zurück, und nichts wird auf die Platte geschrieben; in Folgefragen werden ältere Tool-Ergebnisse durch einen einzeiligen Platzhalter ersetzt. Der Systemprompt ist kurz und steht im Code (`SYSTEM_PROMPT`).

## 7. Task-Steuerung

- Nur die sechs Trading-Tasks werden abgefragt und gestartet: `Trading-MarketData-Backfill`, `Trading-FXRates-Backfill`, `Trading-EventsNews-Backfill`, `Trading-Fundamentals-SEC`, `Trading-Candidate-Discovery` und `Trading-Market-Intelligence`.
- Der Status wird mit PowerShell als JSON gelesen, nie als lokalisierter Text.
- Run now fragt nach einer Bestätigung und startet dann den bestehenden Task mit `Start-ScheduledTask`. Es startet nie das Skript hinter dem Task und ändert nie eine Task-Definition. Ein laufender oder deaktivierter Task kann nicht gestartet werden; die Scheduler-Einstellung `IgnoreNew` bleibt in Kraft.
- Ein gestarteter Task darf wie geplant das Netz nutzen und in die Datenbank schreiben; deshalb ist Run now als Aktion gekennzeichnet.
- Die Log-Ansicht lädt nur das Ende der Logdatei aus dem Log-Ordner und verändert die Datei nie.

## 8. Einstellungen und Logging

- Die Einstellungsdatei ist `%LOCALAPPDATA%\TradingAgentGui\settings.json`. Eine fehlende Datei bedeutet Standardwerte; eine kaputte Datei oder ein ungültiger Wert fällt mit einer Warnung auf den Standardwert zurück. Es gibt keine Secrets.
- Einstellungen: `ollama_base_url`, `ollama_model`, `mcp_python_path`, `mcp_server_path`, `trading_db_path`, `trading_logs_dir`, `default_detail_mode`, `think_enabled`, `refresh_interval_s` und `window_geometry`.
- Das GUI-Log ist `%LOCALAPPDATA%\TradingAgentGui\gui.log` (rotierend). Es hält Start und Stopp, MCP-Verbindung und -Trennung, Tool-Name, Dauer und Ergebnisgröße, Ollama-Anfragen (Modell, Dauer, Tool-Runden, Abbruch, Fehler) und Task-Starts fest. Fragen, Antworten, Tool-Ergebnisse und Portfolio-Werte schreibt es nie.
- Die stderr-Ausgabe des MCP-Servers wird nach `%LOCALAPPDATA%\TradingAgentGui\mcp-stderr.log` geschrieben.

## 9. Threading

- Der GUI-Thread blockiert nie. MCP-Aufrufe laufen im asyncio-Thread; das Ergebnis kommt über ein gequeutes Signal an einen Callback im GUI-Thread.
- PowerShell, Log-Lesen und die Ollama-Prüfung laufen in einem Thread-Pool (`QThreadPool`).
- Der Ollama-Stream und der Tool-Loop laufen in einem eigenen Worker-Thread; ein Tool-Aufruf wird an den MCP-Thread übergeben und der Worker wartet darauf, sodass die Client-Session nur von ihrem eigenen Thread benutzt wird.
- Es gibt kein `qasync` und kein `QtAsyncio`.

## 10. Tests

```
.venv-gui\Scripts\python.exe -m unittest discover -s tests/gui -p "test_*.py"
```

- Die Qt-Tests laufen offscreen (`QT_QPA_PLATFORM=offscreen`). Ohne PySide6 werden sie übersprungen; die Tests von Policy, Einstellungen, ViewModels, Task-Service, Ollama-Client und Tool-Loop laufen in jeder Umgebung.
- Der MCP-Client wird gegen einen kleinen Fake-stdio-Server getestet, der dasselbe SDK nutzt: Start, Discovery, Aufruf, Timeout, Neustart, Absturz und Shutdown.
- Der Ollama-Client und der Chat werden gegen einen geskripteten Fake-Ollama-HTTP-Server getestet: Streaming, Thinking, Tool Calls, Runden, Limits, Fehler, Stop und die Folgeanfrage, nur mit synthetischen Daten.
- Die ViewModels werden mit Ergebnissen des echten Orchestrators und Planners auf synthetischen Daten getestet.

## 11. Bekannte Grenzen

- Nur lesend: In dieser Version gibt es keinen Import, keine Campaigns-Seite, keine Orderfunktion und keine Strategieänderung; der Chat kann kein Aktions-Tool aufrufen.
- Der Chat sieht nur die kompakten Ansichten; Drilldown-Ergebnisse (`detail`) sind für das Modell nicht verfügbar.
- Das detaillierte Orchestrator-Ergebnis ist nicht wie die kompakte Ansicht versioniert; die ViewModels lesen es defensiv und zeigen für Fehlendes einen Strich.
- Run now braucht PowerShell; ohne sie zeigt die Seite den Fehler und nichts wird gestartet.
- Die Aktualisierung ist manuell plus ein moderates Intervall; die Statusleiste zeigt den letzten erfolgreichen Refresh.

## 12. Deployment

Die GUI wird mit demselben Skript deployt wie die Laufzeit. Das Manifest hat die Gruppen `gui`, `gui-services`, `gui-viewmodels`, `gui-views` und `gui-requirements`; sie kopieren das Paket nach `gui\` im Produktionsstamm und `requirements-gui.txt` in den Produktionsstamm. Tests, Dokumentation und die Umgebung werden nie kopiert. Die Umgebung `.venv-gui` wird im Produktionsstamm aus `requirements-gui.txt` erzeugt (der Deploy legt sie nicht an); die Laufzeitumgebung bleibt unangetastet.

```
C:\tools\trading\.venv-gui\Scripts\python.exe -m gui
C:\tools\trading\.venv-gui\Scripts\pythonw.exe -m gui
```

Der erste Befehl startet die GUI mit Konsole, damit Fehler sichtbar bleiben, der zweite ohne Konsolenfenster. Es gibt keine Desktop-Verknüpfung, keinen Installer und keinen Autostart.

## 13. Desktop-Start

Die Desktop-Verknüpfung KI TA startet die GUI direkt, ohne Batch-Datei und ohne Skript dazwischen. Der Chat ist produktiv verfügbar und nur lesend.

- Ziel: `C:\tools\trading\.venv-gui\Scripts\pythonw.exe` mit dem Argument `-m gui`, sodass kein Konsolenfenster erscheint.
- Arbeitsverzeichnis: `C:\tools\trading`.
- Icon: `C:\tools\ki\ki-ta-icon.ico`, neben dem Icon der vorhandenen KI-Start- und Stopp-Verknüpfungen. Es folgt deren Look (türkiser Kreis, weiße fette Schrift) und zeigt KI und TA in zwei Zeilen, sodass es sich klar unterscheidet. Es ist ein Multi-Resolution-Icon (16 bis 256 Pixel, 32 Bit mit Transparenz).
- Die GUI startet oder stoppt Ollama nie; sie spricht nur mit einer laufenden Instanz, also Ollama wie gewohnt vorher starten.
- Der Chat speichert keinen Verlauf auf der Platte, und Stop bricht nur die aktuelle Anfrage ab.
