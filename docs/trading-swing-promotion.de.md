# Promotion von der Watchlist zu Swing

English version: [trading-swing-promotion.en.md](trading-swing-promotion.en.md)

Der generische Status `watchlist.status = 'WATCH'` drückt nur Recherche-Interesse aus. Er
macht ein Wertpapier nicht zum Swing-Kandidaten und löst nie einen Trade aus.

`swing_promotion.evaluate_swing_promotion()` ist rein lesend und liefert genau
eines von `PROMOTE`, `KEEP_WATCHING`, `REJECT` oder `DATA_INSUFFICIENT`. Das
konstruktive Trend-Gate lautet: Kurs über SMA50 und SMA200, mit SMA50 über
SMA200. Recherche- und Fundamentaldaten sind nur Diagnose, bis es eine
maßgebliche Recherche-Schwelle gibt. Portfolio-Cash und -Allokation sind
bewusst ausgeschlossen; sie gehören zum Sizing des Ersteinstiegs.

Ein ausdrücklich freigegebener `PROMOTE`-Plan kann über seinen aktuellen
Plan-Token genau einmal angewendet werden. Die Operation prüft erneut den aktiven Watchlist-Status, die Position null,
keine offene Campaign, keine kollidierende Strategiezuordnung und den gesamten ausgewerteten
Markt-/Technik-Zustand, bevor sie genau eine `strategy_assignment`
mit `strategy_type = 'swing'` einfügt. `candidate_promotion` hält diese Freigabe
für das Audit fest. Es entstehen keine Order, Transaktion, Position, kein Capital-State und keine Campaign.

Die kontrollierte Abfolge bleibt:

`WATCH` → `promotion evaluation` → `explicit approval` → `swing strategy
assignment` → `Phase 3B.7 BUY recommendation` → `explicit execution` →
`authoritative transaction` → `explicit campaign opening`.
