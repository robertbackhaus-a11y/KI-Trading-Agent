# Swing-Einstiegsempfehlungen

English version: [trading-entry-recommendations.en.md](trading-entry-recommendations.en.md)

Phase 3B.7 erzeugt eine `BUY`-Empfehlung nur für ein Wertpapier mit einer
aktiven, expliziten `strategy_assignment` vom Typ `swing`, ohne offene Position und ohne
offene Swing-Campaign. Die generische `watchlist` ist keine Autorität für Einstiegskandidaten,
weil ihr Status `WATCH` keine Strategie abbildet.

Die Empfehlung ist rein lesend. Sie erzeugt weder eine Order, eine Transaktion, einen
Capital-State-Eintrag noch eine Swing-Campaign. Der vorgesehene kontrollierte Ablauf ist:

`BUY recommendation` → `explicit user approval / execution` → `authoritative
transaction import or execution record` → `position exists` → `explicit
campaign-open operation` → `lifecycle begins`.

Das EUR-Sizing verwendet den FX-normalisierten Kandidatenkurs und die Portfolio-Nenner nach dem
Trade. Native Marktpreise bleiben für technische Vergleiche erhalten.

Die Rangfolge der Blocker lautet: zuerst Kandidatenumfang und aktuelle Position/Campaign, danach
vollständige Portfolio-Bewertung/-Allokation, die aktuelle Swing-Obergrenze, Cash-Verfügbarkeit/
-Reserve und zuletzt die technischen Einstiegseingaben. Dadurch führt eine aktuell
übergewichtete Swing-Allokation unbedingt zu `ENTRY_SWING_ALLOCATION_LIMIT`.
