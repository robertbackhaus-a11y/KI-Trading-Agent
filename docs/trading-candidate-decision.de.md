# Status der Watchlist-Kandidatenentscheidung

English version: [trading-candidate-decision.en.md](trading-candidate-decision.en.md)

`candidate_decision.evaluate_watchlist_candidates(connection, as_of=None)` ist
rein lesend und liefert je Eintrag mit `watchlist.status = 'WATCH'` genau ein
`CandidateDecision`: `security_id`, `symbol`, `analytics_score`, `analytics_quality`,
`strategy`, `campaign_status`, `portfolio_constraints`, `decision_status`,
`decision_reasons`.

Das Modul ersetzt `swing_promotion.py` nicht und dupliziert es nicht. Jenes Modul
beantwortet die Frage *"soll dieser Watchlist-Eintrag eine Swing-Strategiezuordnung
erhalten"* (siehe [trading-swing-promotion.de.md](trading-swing-promotion.de.md)). Dieses Modul
beantwortet eine engere, spätere Frage — *"ist angesichts der heutigen Analytics, der aktuellen
Strategiezuordnung, einer eventuell offenen Campaign und der aktuellen Portfolio-Allokation
ein neuer BUY derzeit gerechtfertigt"* — indem es die Ergebnisse bereits vorhandener,
bereits getesteter Funktionen zusammensetzt, statt sie neu zu berechnen:

- `trading_analytics.rank_watchlist()` → `analytics_score` / `analytics_quality`
- `swing_promotion._active_assignment()` → `strategy`
- `swing_promotion._open_campaign()` → `campaign_status`
- `analysis_engine.build_portfolio_context()` /
  `portfolio_context.derive_allocation_guardrails()` → `portfolio_constraints`

`decision_status` ist genau einer von `BUY`, `WATCH`, `DEFERRED`,
`INSUFFICIENT_DATA` und wird in dieser festen Reihenfolge entschieden:

1. `analytics_quality != "OK"` → `INSUFFICIENT_DATA`
2. keine aktive `strategy_assignment` → `DEFERRED`
3. es existiert bereits eine offene Swing-Campaign → `WATCH`
4. die aktive Zuordnung ist nicht `swing` → `WATCH`
5. der Zustand der Portfolio-Allokations-Guardrails ist nicht verfügbar → `DEFERRED`
6. der Guardrail `SWING_ALLOCATION_ABOVE_MAX` ist gesetzt → `DEFERRED`
7. der Analytics-Score ist nicht verfügbar → `WATCH`
8. der Analytics-Score ist `>= BUY_SCORE_THRESHOLD` (20.0, eine benannte Konstante in
   `candidate_decision.py`) → `BUY`
9. sonst → `WATCH`

Jeder Zweig trägt einen expliziten `decision_reasons`-Eintrag; nichts wird
stillschweigend abgeleitet. Fehlende oder unbestimmte Eingaben (begrenzte/unzureichende
Kurshistorie, ein nicht klassifizierbarer Portfolio-Nenner, ein fehlender Score)
führen zu `INSUFFICIENT_DATA` oder `DEFERRED` statt standardmäßig zu `BUY`.

Die Funktion erteilt keine Order, schreibt nichts und eröffnet nie eine Swing-
Campaign. Ein `BUY`-Status ist eine Eingabe für die weiterhin getrennte, weiterhin manuelle
Abfolge aus Promotion, Freigabe und Ausführung, die in
[trading-swing-promotion.de.md](trading-swing-promotion.de.md) beschrieben ist; er ist selbst kein
Ausführungsauslöser.

## MCP-Anbindung

Rein lesend verfügbar über das Tool `trading_sqlite` des Trading-MCP-Servers (genutzt von
einem MCP-Client; Repo-Quelle `mcp-tools/trading_sqlite.py`, Runtime
`C:\tools\trading\mcp`) als `evaluate_watchlist_candidates(as_of=None)`, das
diese Funktion unverändert aufruft — im Tool-Layer wird keine Entscheidungslogik
dupliziert. Siehe [trading-agent-architecture.de.md](trading-agent-architecture.de.md) §5.
