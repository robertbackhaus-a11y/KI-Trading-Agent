# Strategievorschlag für die Watchlist

English version: [trading-strategy-suggestion.en.md](trading-strategy-suggestion.en.md)

`strategy_suggestion.suggest_strategy_assignments(connection, as_of=None)` ist
rein lesend und liefert je `watchlist.status = 'WATCH'`-Eintrag, der derzeit **keine aktive**
`strategy_assignment` hat, genau einen `StrategySuggestion` (`security_id`, `symbol`,
`suggested_strategy`, `confidence`, `reasons`). Einträge, die bereits eine haben, werden vollständig
übersprungen -- nie überschrieben, nie erneut vorgeschlagen.

**Ein Vorschlag ist keine Zuordnung.** Diese Funktion schreibt nichts. Um einen
`swing`-Vorschlag in eine echte `strategy_assignment`-Zeile zu überführen, ist weiterhin der
separate, bestehende, explizite
[`swing_promotion.approve_swing_promotion`](trading-swing-promotion.de.md)-Pfad
mit eigenem Plan-Token und eigenen Eligibility-Prüfungen nötig. Dieses Modul ruft
diesen Pfad nicht auf und eröffnet keine Swing-Campaign. Für `long_term` gibt es derzeit
überhaupt keinen Promotion-Pfad; ein `long_term`-Vorschlag ist rein informativ.

Wiederverwendet statt neu implementiert wird:

- `trading_analytics.rank_watchlist()` für Analytics-Score/-Qualität.
- `swing_promotion._active_assignment()` für den Filter auf bestehende Zuordnungen.
- `swing_promotion._open_campaign()` für die Prüfung auf eine offene Campaign.

`suggested_strategy` ist genau einer von `swing`, `long_term`, `unknown` und wird
in dieser festen Reihenfolge entschieden:

1. `analytics_quality != "OK"` → `unknown` (Confidence `0.0`)
2. für das Wertpapier existiert bereits eine offene Swing-Campaign → `swing`
   (Confidence `0.9` -- es wird bereits aktiv als Swing gehandelt)
3. `security.asset_type` ist `etf` oder `fund` → `long_term` (Confidence `0.7`)
4. der Momentum-/Trend-Score ist `>= SWING_SUGGESTION_SCORE_THRESHOLD` (`20.0`,
   eine benannte Konstante) → `swing` (Confidence `0.6`)
5. sonst → `unknown` (Confidence `0.0`)

**`long_term` wird nie aus dem Momentum-/Trend-Score abgeleitet.** Das einzige
nicht auf Momentum beruhende Long-Term-Signal in dieser Codebasis ist heute
`asset_type` (`analysis_engine.py` behandelt `{"etf", "fund"}` für Fundamentals bereits als
Nicht-Aktien-Kategorie; dieses Modul verwendet dieselbe Konstante, `LONG_TERM_ASSET_TYPES`). Eine Aktie kann hier
daher nur zu `swing` oder `unknown` aufgelöst werden, nie zu `long_term`, bis an anderer Stelle in dieser
Codebasis ein echtes fundamentalbasiertes Long-Term-Signal existiert. Alle
Schwellenwerte und Confidence-Werte sind benannte Konstanten auf Modulebene in
`strategy_suggestion.py`, keine Inline-Heuristiken.

## MCP-Anbindung

Rein lesend verfügbar über das Tool `trading_sqlite` des Trading-MCP-Servers (genutzt von
einem MCP-Client; Repo-Quelle `mcp-tools/trading_sqlite.py`, Runtime
`C:\tools\trading\mcp`) als `suggest_strategy_assignments(as_of=None)`, das
diese Funktion unverändert aufruft — im Tool-Layer wird keine Vorschlagslogik
dupliziert. Siehe [trading-agent-architecture.de.md](trading-agent-architecture.de.md) §5.
