# Desktop-GUI

English version: [gui.en.md](gui.en.md)

Die Desktop-GUI ist ein lesendes Cockpit für den Trading Agent. Sie zeigt, was der Trading-MCP-Server meldet, und steuert die geplanten Tasks. Sie enthält keine Trading-Logik, keinen Datenbankzugriff und keine Orderfunktion. Entwicklungsstand: Es gibt noch keine Produktversion; die GUI meldet `dev`.

## 1. Architektur

```
Desktop GUI (PySide6)
  |-- MCP client ----- stdio child process ----- Trading MCP server ----- SQLite
  |-- Task service --- PowerShell -------------- Windows Task Scheduler
  `-- settings file and log files
```

- Trading-Daten kommen ausschließlich aus den READ-Tools des Trading-MCP-Servers. Die ViewModels formatieren nur, was die Tools liefern, und rechnen nichts.
- Der MCP-Server läuft als eigener Kindprozess der GUI. Er wird nie importiert und kann neben einem anderen Client laufen, der seinen eigenen Serverprozess startet.
- Der Windows Task Scheduler wird direkt und nicht über MCP angesprochen, damit kein Sprachmodell einen Task starten kann.
- Der Chat mit dem lokalen Sprachmodell gehört nicht zu dieser Version; die Diagnose zeigt nur, ob Ollama erreichbar ist.

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
| Tasks | die sechs geplanten Trading-Tasks: Status, letzter und nächster Lauf, Ergebnis, Run now, Log-Ende | Windows Task Scheduler, Log-Dateien |
| Diagnostics / Settings | MCP- und Datenbank-Status, Pfade, Tool-Klassifizierung, Ollama-Erreichbarkeit, Einstellungsformular, MCP-Neustart | `database_status`, Einstellungsdatei |

## 4. MCP-Client

- Das offizielle `mcp`-SDK startet den Server als stdio-Kindprozess. Die Client-Session gehört einem eigenen QThread mit eigenem asyncio-Loop.
- Alle Pfade sind absolut, insbesondere `TRADING_DB_PATH`; ein relativer Datenbankpfad bricht die lesenden Tools des Servers.
- Jeder Aufruf hat ein Timeout. Etwa alle fünf Sekunden läuft eine billige Lebendprüfung; stirbt der Kindprozess, zeigt der Status einen Fehler und der nächste Aufruf verbindet neu. Restart MCP startet nur den Kindprozess neu und verändert keine Trading-Daten.
- Die stderr-Ausgabe des Servers geht in eine eigene Logdatei.
- Nichts hängt von `capabilities.experimental` ab.

## 5. Lese- und Aktions-Tools

| Klasse | Tools | GUI |
|---|---|---|
| READ | die 13 Lese-Tools, zum Beispiel `database_status`, `run_trading_orchestrator`, `get_opportunity_view` | erlaubt; diese Version ruft nur `database_status` und `run_trading_orchestrator` auf |
| ACTION | `approve_swing_promotion`, `sql_execute` | verweigert |
| unbekannt | jedes Tool, das nicht in der Tabelle steht | verweigert (Deny by default) |

Die Tabelle liegt in `gui/services/tool_policy.py`; ein Test verlangt, dass jedes Tool des Servers klassifiziert ist. Aktionen sind in der Oberfläche sichtbar von Lesezugriffen getrennt und stecken nie hinter einem Lesezugriff.

## 6. Task-Steuerung

- Nur die sechs Trading-Tasks werden abgefragt und gestartet: `Trading-MarketData-Backfill`, `Trading-FXRates-Backfill`, `Trading-EventsNews-Backfill`, `Trading-Fundamentals-SEC`, `Trading-Candidate-Discovery` und `Trading-Market-Intelligence`.
- Der Status wird mit PowerShell als JSON gelesen, nie als lokalisierter Text.
- Run now fragt nach einer Bestätigung und startet dann den bestehenden Task mit `Start-ScheduledTask`. Es startet nie das Skript hinter dem Task und ändert nie eine Task-Definition. Ein laufender oder deaktivierter Task kann nicht gestartet werden; die Scheduler-Einstellung `IgnoreNew` bleibt in Kraft.
- Ein gestarteter Task darf wie geplant das Netz nutzen und in die Datenbank schreiben; deshalb ist Run now als Aktion gekennzeichnet.
- Die Log-Ansicht lädt nur das Ende der Logdatei aus dem Log-Ordner und verändert die Datei nie.

## 7. Einstellungen und Logging

- Die Einstellungsdatei ist `%LOCALAPPDATA%\TradingAgentGui\settings.json`. Eine fehlende Datei bedeutet Standardwerte; eine kaputte Datei oder ein ungültiger Wert fällt mit einer Warnung auf den Standardwert zurück. Es gibt keine Secrets.
- Einstellungen: `ollama_base_url`, `ollama_model`, `mcp_python_path`, `mcp_server_path`, `trading_db_path`, `trading_logs_dir`, `default_detail_mode`, `think_enabled`, `refresh_interval_s` und `window_geometry`.
- Das GUI-Log ist `%LOCALAPPDATA%\TradingAgentGui\gui.log` (rotierend). Es hält Start und Stopp, MCP-Verbindung und -Trennung, Tool-Name, Dauer und Ergebnisgröße, Fehler und Task-Starts fest. Tool-Ergebnisse, Portfolio-Werte und Prompts schreibt es nie.
- Die stderr-Ausgabe des MCP-Servers wird nach `%LOCALAPPDATA%\TradingAgentGui\mcp-stderr.log` geschrieben.

## 8. Threading

- Der GUI-Thread blockiert nie. MCP-Aufrufe laufen im asyncio-Thread; das Ergebnis kommt über ein gequeutes Signal an einen Callback im GUI-Thread.
- PowerShell, Log-Lesen und die Ollama-Prüfung laufen in einem Thread-Pool (`QThreadPool`).
- Es gibt kein `qasync` und kein `QtAsyncio`.

## 9. Tests

```
.venv-gui\Scripts\python.exe -m unittest discover -s tests/gui -p "test_*.py"
```

- Die Qt-Tests laufen offscreen (`QT_QPA_PLATFORM=offscreen`). Ohne PySide6 werden sie übersprungen; die Tests von Policy, Einstellungen, ViewModels und Task-Service laufen in jeder Umgebung.
- Der MCP-Client wird gegen einen kleinen Fake-stdio-Server getestet, der dasselbe SDK nutzt: Start, Discovery, Aufruf, Timeout, Neustart, Absturz und Shutdown.
- Die ViewModels werden mit Ergebnissen des echten Orchestrators und Planners auf synthetischen Daten getestet.

## 10. Bekannte Grenzen

- Nur lesend: In dieser Version gibt es keinen Chat, keinen Import, keine Campaigns-Seite, keine Orderfunktion und keine Strategieänderung.
- Das detaillierte Orchestrator-Ergebnis ist nicht wie die kompakte Ansicht versioniert; die ViewModels lesen es defensiv und zeigen für Fehlendes einen Strich.
- Run now braucht PowerShell; ohne sie zeigt die Seite den Fehler und nichts wird gestartet.
- Die Aktualisierung ist manuell plus ein moderates Intervall; die Statusleiste zeigt den letzten erfolgreichen Refresh.

## 11. Deployment

Die GUI wird mit demselben Skript deployt wie die Laufzeit. Das Manifest hat die Gruppen `gui`, `gui-services`, `gui-viewmodels`, `gui-views` und `gui-requirements`; sie kopieren das Paket nach `gui\` im Produktionsstamm und `requirements-gui.txt` in den Produktionsstamm. Tests, Dokumentation und die Umgebung werden nie kopiert. Die Umgebung `.venv-gui` wird im Produktionsstamm aus `requirements-gui.txt` erzeugt (der Deploy legt sie nicht an); die Laufzeitumgebung bleibt unangetastet.

```
C:\tools\trading\.venv-gui\Scripts\python.exe -m gui
C:\tools\trading\.venv-gui\Scripts\pythonw.exe -m gui
```

Der erste Befehl startet die GUI mit Konsole, damit Fehler sichtbar bleiben, der zweite ohne Konsolenfenster. Es gibt keine Desktop-Verknüpfung, keinen Installer und keinen Autostart.
