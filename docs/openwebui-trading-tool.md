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
| Analytics-Modul (dynamisch nachgeladen) | `C:\KI-Stack\tools\trading\trading_analytics.py` |

Die Methode `rank_watchlist()` des Tools lädt `trading_analytics.py` zur Laufzeit per
`importlib.util` nach und ruft dessen `rank_watchlist(connection)` auf derselben,
bereits offenen DB-Verbindung auf. Keine eigene DB-Logik, keine Duplikation.

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

## Deploy-Ablauf

1. Backup von `webui.db` anlegen
2. ausschließlich die Tool-Zeile `id='trading_sqlite'` ändern
3. `content` aktualisieren (aus `openwebui-tools\trading_sqlite.py`)
4. `specs` mit OpenWebUIs eigener `get_tool_specs()`-Funktion neu generieren
5. prüfen, dass keine anderen Tool-Zeilen verändert wurden
6. OpenWebUI gezielt neu starten (nur die OpenWebUI-Serverprozesse, kein Full-Stack-Restart)
7. `rank_watchlist()` testen
8. DB vor/nach vergleichen (keine ungewollten Writes)
9. bestehenden `sql_execute()`-Read testen

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
