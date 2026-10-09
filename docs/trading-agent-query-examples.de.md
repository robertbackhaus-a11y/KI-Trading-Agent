# Trading Agent – Abfragen und Beispiele

English version: [trading-agent-query-examples.en.md](trading-agent-query-examples.en.md)

Dieses Dokument zeigt, welche Fragen der Trading Agent beantwortet und welches MCP-Tool dahintersteht. Es ist frontend-neutral: Jeder MCP-fähige Client kann die Tools mit natürlicher Sprache nutzen. Die Beispiele verwenden neutrale Ticker und sind keine Anlageempfehlung. Die Architektur steht in [trading-agent-architecture.md](trading-agent-architecture.md), der Überblick in der [README](../README.md).

**Grundsätze:** `PROMOTE` ist kein `BUY`. `DISCOVERY` ist kein `PROMOTE`. `NEWS` ist kein Signal. Keine Orders: Die Ausführung erfolgt außerhalb des Trading Agents.

**Inhalt**

- [1. Depotstatus](#1-depotstatus)
- [2. Einstieg](#2-einstieg)
- [3. Watchlist](#3-watchlist)
- [4. Candidate Discovery](#4-candidate-discovery)
- [5. News und Market Intelligence](#5-news-und-market-intelligence)
- [6. Chancen mit News](#6-chancen-mit-news)
- [7. Einzelaktie](#7-einzelaktie)
- [8. Aktien vergleichen](#8-aktien-vergleichen)
- [9. Kapital und Planner](#9-kapital-und-planner)
- [10. Steuer und Netto-Erlös](#10-steuer-und-netto-erlös)
- [11. Swing Campaigns](#11-swing-campaigns)
- [12. Datenbank und Diagnose](#12-datenbank-und-diagnose)
- [13. Strategiezuordnung und Promotion](#13-strategiezuordnung-und-promotion)
- [14. Datenqualität und Aktualität](#14-datenqualität-und-aktualität)
- [15. Was der Agent nicht macht](#15-was-der-agent-nicht-macht)
- [16. Empfohlene kurze Fragen](#16-empfohlene-kurze-fragen)
- [A. Technische Zuordnung](#a-technische-zuordnung)
- [B. Grenzen und Semantik](#b-grenzen-und-semantik)

## 1. Depotstatus

Der Depotstatus ist ein vollständiger, rein lesender Gesamtbericht: Positionen mit ihrer Aktion (SELL, TRIM, HOLD, ADD), Prioritäten, Allokation, Cash und am Ende der Kapital- und Entry-Plan. Er wird deterministisch berechnet; das Frontend zeigt ihn nur an.

**Tools:** `run_trading_orchestrator`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Depotstatus und Handlungsbedarf“ | `run_trading_orchestrator()` |
| 2 | „Welche Positionen soll ich verkaufen, reduzieren oder halten?“ | `run_trading_orchestrator()` |
| 3 | „Welche Aktion hat die höchste Priorität?“ | `run_trading_orchestrator()` (`highest_priority_actions`) |
| 4 | „Wie hoch ist der Swing-Anteil im Depot?“ | `run_trading_orchestrator()` |
| 5 | „Gibt es Allokations-Warnungen?“ | `run_trading_orchestrator()` |
| 6 | „Wie viel Cash ist verfügbar?“ | `run_trading_orchestrator()` |
| 7 | „Was soll ich als Nächstes prüfen?“ | `run_trading_orchestrator()` (`next_review_items`) |
| 8 | „Rechne den Depotstatus für den Stichtag 2026-01-31.“ | `run_trading_orchestrator(as_of="2026-01-31")` |

**Hinweise:**

- Die Antwort enthält `presentation_summary` (strukturierte Felder und Zähler) und `rendered_summary_de` (fertiger Text, immer auf Deutsch). Für Standardfragen wird das direkt angezeigt; nichts wird neu gezählt, neu summiert oder neu bewertet.
- Standardmäßig kommt die kompakte Fassung ohne die großen Ergebnislisten (`existing_position_results`, `entry_candidate_results`, `promotion_results`). `detail=true` liefert alles (rund 100.000 Zeichen) und gehört nur auf ausdrücklichen Wunsch angefragt.
- Ohne `as_of` gilt der aktuelle Stand. Aktionen sind Empfehlungen des Agents; die Orderausführung erfolgt außerhalb des Trading Agents.

## 2. Einstieg

Einstiegskandidaten entstehen aus der Promotion-Bewertung (PROMOTE). Der Planner bewertet, rankt und dimensioniert sie (ENTRY_READY, WAIT_FOR_TRIGGER, BLOCKED_*). PROMOTE ist kein BUY.

**Tools:** `get_opportunity_view`, `run_trading_orchestrator`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Gibt es aktuell Einstiegskandidaten?“ | `get_opportunity_view(status="ENTRY_READY")` |
| 2 | „Welche Kandidaten warten noch auf einen Trigger?“ | `get_opportunity_view(status="WAIT_FOR_TRIGGER")` |
| 3 | „Warum ist AAPL nicht einstiegsbereit?“ | `get_opportunity_view(symbol="AAPL")` |
| 4 | „Welche Einstiege sind mit dem freien Kapital geplant?“ | `run_trading_orchestrator()` |
| 5 | „Welcher Kandidat hat den besten Entry-Rang?“ | `get_opportunity_view(status="ENTRY_READY")` |
| 6 | „Warum wurde NVDA nicht mehr geplant?“ | `get_opportunity_view(symbol="NVDA")` (`STOPPED_CAPITAL_EXHAUSTED`) |

**Hinweise:**

- Entry-Status (nur PROMOTE-Kandidaten): `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `BLOCKED_BY_ALLOCATION`, `BLOCKED_BY_DATA`, `BLOCKED_BY_CONCENTRATION`, `BLOCKED_EXISTING_POSITION`, `BLOCKED_EXISTING_CAMPAIGN`.
- Das Ranking gilt nur für `ENTRY_READY`: `entry_score` = Momentum-Score × Confidence. Bei Gleichstand entscheidet die Watchlist-Priorität.
- Die Positionsgröße kommt aus der bestehenden Sizing-Logik (Swing-Maximum 40 %, Cash-Reserve, maximales Positionsgewicht, ganze Stück abgerundet), sequentiell nach Rang. Der Plan stoppt zum Beispiel mit `STOPPED_CAPITAL_EXHAUSTED`.
- Das Ergebnis ist eine Simulation ohne Order. Keine Antwort ist eine Kaufanweisung.

## 3. Watchlist

Die Watchlist wird auf drei Wegen bewertet: technischer Momentum-Rang, Status je Titel (BUY, WATCH, DEFERRED, INSUFFICIENT_DATA) und Prüfung der Promotion-Regeln für Swing.

**Tools:** `rank_watchlist`, `evaluate_watchlist_candidates`, `evaluate_swing_candidates`, `evaluate_swing_candidate`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Rangliste der Watchlist nach Momentum“ | `rank_watchlist()` |
| 2 | „Welche Watchlist-Titel haben den Status BUY, WATCH oder DEFERRED?“ | `evaluate_watchlist_candidates()` |
| 3 | „Bei welchen Watchlist-Titeln reichen die Daten nicht aus?“ | `evaluate_watchlist_candidates()` (`INSUFFICIENT_DATA`) |
| 4 | „Welche Watchlist-Titel erfüllen die Promotion-Regeln?“ | `evaluate_swing_candidates()` |
| 5 | „Prüfe den Titel mit der security_id 12 auf Promotion.“ | `evaluate_swing_candidate(security_id=12)` |

**Hinweise:**

- `rank_watchlist` bewertet technisch: SMA50/SMA200, RSI14, Performance über 1, 3 und 6 Monate, 52-Wochen-Drawdown, annualisierte Volatilität. Kein Schreibzugriff, kein Web.
- `BUY` in `evaluate_watchlist_candidates` ist ein Status-Label und keine Order. Es ist die Eingabe für den weiterhin getrennten, manuellen Ablauf aus Promotion, Freigabe und Ausführung.
- Fehlende oder unklare Eingaben führen zu `INSUFFICIENT_DATA` oder `DEFERRED`, nie stillschweigend zu `BUY`.
- Die security_id lässt sich mit `sql_execute` nachschlagen (SELECT auf die Tabelle `security`). Die Promotion-Prüfung legt keine Zuordnung und keine Campaign an.

## 4. Candidate Discovery

Die Discovery sucht in einem festen Universum liquider Large Caps nach neuen Titeln, die weder im Depot noch auf der Watchlist stehen. Sie schlägt nur vor. DISCOVERY ist nicht PROMOTE.

**Tools:** `get_candidate_discovery`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Gibt es neue Kandidaten aus der Discovery?“ | `get_candidate_discovery(status="DISCOVERY_READY")` |
| 2 | „Zeig mir auch die beobachtenswerten Kandidaten.“ | `get_candidate_discovery(status="DISCOVERY_WATCH")` |
| 3 | „Was sagt die Discovery zu NVDA?“ | `get_candidate_discovery(symbol="NVDA")` |
| 4 | „Wie aktuell ist der Discovery-Report?“ | `get_candidate_discovery()` (`REPORT_STALE`) |
| 5 | „Welche Titel hat der Vorfilter ausgeschlossen?“ | `get_candidate_discovery(status="EXCLUDED")` |
| 6 | „Zeig die 40 besten Kandidaten mit allen gespeicherten Feldern.“ | `get_candidate_discovery(detail=true, limit=40)` |

**Hinweise:**

- Status: `DISCOVERY_READY` (Trend intakt und Momentum ≥ 20), `DISCOVERY_WATCH`, `DISCOVERY_DATA_INSUFFICIENT`, `DISCOVERY_REJECTED`; `EXCLUDED` steht für Titel, die der Vorfilter aussortiert hat.
- Der Score ist `discovery_score` = Momentum-Score × Confidence. Fundamentals, Bewertung und Events werden für neue Titel nicht geholt.
- Das Tool liest nur den letzten fertigen Report: kein Scan, kein Web, keine Datenbank. Ohne Report lautet die Antwort `UNAVAILABLE` (`NO_DISCOVERY_REPORT`), ein alter Report wird mit `REPORT_STALE` gekennzeichnet.
- Der Report entsteht außerhalb des MCP durch das Skript `Discover-TradingCandidates.py`; im Referenz-Setup läuft es täglich um 17:35 als geplanter Task. Standard `limit` 20 (höchstens 150, mit `detail` 40).
- Es gibt keine automatische Aufnahme in die Watchlist und keine Strategiezuordnung.

## 5. News und Market Intelligence

Market Intelligence ordnet Nachrichten nur ein: Kategorie, Wichtigkeit, betroffene Titel, Sektoren und Regionen. News sind Kontext, kein Signal.

**Tools:** `get_market_intelligence`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Welche News betreffen AAPL?“ | `get_market_intelligence(symbol="AAPL")` |
| 2 | „Gibt es wichtige Nachrichten für mein Depot?“ | `get_market_intelligence(scope="PORTFOLIO", importance="HIGH")` |
| 3 | „Was passiert makroökonomisch?“ | `get_market_intelligence(scope="MACRO")` |
| 4 | „Gab es Zinsentscheidungen?“ | `get_market_intelligence(category="MONETARY_POLICY")` |
| 5 | „Was gibt es Neues im Halbleitersektor?“ | `get_market_intelligence(sector="SEMICONDUCTORS")` |
| 6 | „Gibt es Nachrichten zu Unternehmen auf meiner Watchlist?“ | `get_market_intelligence(scope="WATCHLIST")` |
| 7 | „Wie frisch ist der News-Report?“ | `get_market_intelligence()` (`REPORT_STALE`) |

**Hinweise:**

- `scope`: `ALL` (Standard), `PORTFOLIO`, `WATCHLIST`, `DISCOVERY`, `MACRO`. `importance`: `HIGH`, `MEDIUM`, `LOW`.
- Kategorien: `MACRO`, `MONETARY_POLICY`, `INFLATION`, `LABOR_MARKET`, `ECONOMIC_GROWTH`, `GEOPOLITICS`, `ENERGY`, `SECTOR`, `COMPANY`, `EARNINGS`, `GUIDANCE`, `REGULATORY`, `M_AND_A`.
- Der Impact ist `UNKNOWN`, solange keine Primärquelle eine belastende Tatsache nennt; eine Richtung wird nie aus einer Schlagzeile gelesen. HIGH vergeben nur Primärquellen oder Schlagzeilen, die das Unternehmen mit Namen nennen (ein bloßer Ticker zählt nicht).
- Quellen: offizielle Feeds (EZB, Fed, BLS, BEA, Destatis, EIA), SEC-Meldungen und Yahoo-Schlagzeilen. Stock-Pick-Artikel gelten höchstens als LOW.
- Das Tool liest nur den letzten fertigen Report (kein Web, kein Scan, keine Datenbank). Ohne Report: `UNAVAILABLE` (`NO_MARKET_INTELLIGENCE_REPORT`); nach 24 Stunden `REPORT_STALE`. Im Referenz-Setup läuft der Collector täglich um 08:15 und 17:45.
- Kompakt ist der Standard (`limit` 20, höchstens 50). `detail=true` liefert das Rohformat mit höchstens 25 Ereignissen und nur auf ausdrücklichen Wunsch. Bei `symbol` passt auch das Basissymbol (SAP findet SAP.DE).

## 6. Chancen mit News

Die Opportunity View führt Engine-/Planner-Kandidaten, den Discovery-Report und den News-Report in einer Ausgabe zusammen. Sie rechnet nichts Neues: News bleiben Kontext und Warnung.

**Tools:** `get_opportunity_view`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Was sind die besten neuen Chancen?“ | `get_opportunity_view(source="DISCOVERY")` |
| 2 | „Discovery-Kandidaten mit relevanten News“ | `get_opportunity_view(source="DISCOVERY", news_status="RELEVANT")` |
| 3 | „ENTRY_READY-Kandidaten mit High-Impact-News“ | `get_opportunity_view(status="ENTRY_READY", news_status="NEWS_HIGH_ATTENTION")` |
| 4 | „Technisch stark und news-unauffällig“ | `get_opportunity_view(news_status="NEWS_CLEAR")` |
| 5 | „Discovery-READY-Kandidaten ohne HIGH-News“ | `get_opportunity_view(source="DISCOVERY", status="DISCOVERY_READY", news_status="NOT_HIGH")` |
| 6 | „Passt der Kandidat ins freie Kapital?“ | `get_opportunity_view(source="DISCOVERY")` (`capital_fit`) |

**Hinweise:**

- `source`: `ALL`, `WATCHLIST`, `DISCOVERY`, `BOTH`. `status`: `ALL`, `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `DISCOVERY_READY`, `OTHER`. `news_status`: `NEWS_HIGH_ATTENTION`, `NEWS_ATTENTION`, `NEWS_CLEAR`, `NEWS_UNAVAILABLE`, `RELEVANT` (Attention oder High), `NOT_HIGH`.
- `NEWS_HIGH_ATTENTION`: mindestens ein direkt symbolbezogenes HIGH-Event. `NEWS_ATTENTION`: direktes MEDIUM-Event oder ein nur über den Sektor bzw. lose zugeordnetes HIGH-Event. `NEWS_CLEAR`: nur LOW, lose MEDIUM oder keine Meldung. `NEWS_UNAVAILABLE`: kein gültiger News-Report.
- News ändern nie Score, Rang, Positionsgröße oder SELL/TRIM/HOLD. Es gibt keinen kombinierten Score; `discovery_score`, `entry_score`, `momentum_score` und `confidence` bleiben getrennt sichtbar.
- Reihenfolge: `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `DISCOVERY_READY`, Sonstige; innerhalb der Gruppe nach Engine-Rang. Fehlt ein Report, ist das Ergebnis `PARTIAL`; der Rest wird trotzdem gezeigt.
- Standard `limit` 30 (höchstens 40, mit `detail` 10). Ein gefilterter Aufruf genügt normalerweise; `detail=true` nur auf ausdrücklichen Wunsch. Die Antwort bleibt unter 34.000 Zeichen (sonst `RESPONSE_TRIMMED_TO_SIZE_BUDGET`).

## 7. Einzelaktie

Für einen einzelnen Titel gibt es keine eigene Abfrage; er wird über den Filter `symbol` in den lesenden Tools abgefragt. So lässt sich Engine-Sicht, Discovery und News getrennt oder gemeinsam betrachten.

**Tools:** `get_opportunity_view`, `get_market_intelligence`, `get_candidate_discovery`, `sql_execute`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Was weiß der Agent über AAPL?“ | `get_opportunity_view(symbol="AAPL")` |
| 2 | „Gibt es aktuelle News zu GOOGL?“ | `get_market_intelligence(symbol="GOOGL")` |
| 3 | „Wie bewertet die Discovery GOOGL?“ | `get_candidate_discovery(symbol="GOOGL")` |
| 4 | „Halte ich AAPL, und was sagt die Engine dazu?“ | `get_opportunity_view(symbol="AAPL")` |
| 5 | „Zeig mir die gespeicherten Transaktionen zu AAPL.“ | `sql_execute` (SELECT) |

**Hinweise:**

- `get_opportunity_view(symbol=...)` zeigt neben Kandidaten auch die so benannten Depotpositionen mit Aktion und News-Kontext.
- Symbole werden in Yahoo-Schreibweise angegeben (zum Beispiel SAP.DE). Die News-Abfrage akzeptiert auch das Basissymbol.
- `sql_execute` ist ein uneingeschränkter SQL-Zugang. Er gehört nur für bewusste lesende Abfragen genutzt; für alles andere gibt es gezielte Tools.

## 8. Aktien vergleichen

Mehrere Titel werden mit einer kommagetrennten `symbol`-Liste nebeneinander gestellt: Rang, Score, Plan-Status, Verzögerungsgrund und News-Status.

**Tools:** `get_opportunity_view`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Warum NVDA vor AAPL?“ | `get_opportunity_view(symbol="NVDA,AAPL")` |
| 2 | „Vergleiche NVDA, AAPL und GOOGL.“ | `get_opportunity_view(symbol="NVDA,AAPL,GOOGL")` |
| 3 | „Welcher der beiden wurde mangels Kapital nicht geplant?“ | `get_opportunity_view(symbol="NVDA,AAPL")` (`STOPPED_CAPITAL_EXHAUSTED`) |
| 4 | „Bei welchem der beiden ist das News-Bild ruhiger?“ | `get_opportunity_view(symbol="NVDA,AAPL")` |

**Hinweise:**

- Die Gegenüberstellung ist sachlich: Sie zeigt Rang, `entry_score`, Plan-Status und Stopp-Grund nebeneinander und erklärt damit die Reihenfolge der Engine. Sie ist keine Empfehlung.
- Es gibt keinen kombinierten Score aus technischer Bewertung und News.
- Ein Titel, der in keiner Quelle vorkommt, erscheint nicht in der Antwort.

## 9. Kapital und Planner

Der Portfolio Action Planner rechnet deterministisch, wie viel Kapital nach den geplanten Verkäufen frei ist und welche Einstiege sich damit finanzieren lassen. Er erzeugt keine Orders.

**Tools:** `run_trading_orchestrator`, `get_opportunity_view`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Wie viel Kapital steht nach den geplanten Verkäufen zur Verfügung?“ | `run_trading_orchestrator()` |
| 2 | „Wie viel Cash bleibt nach den geplanten Entries?“ | `run_trading_orchestrator()` |
| 3 | „Wie viel Swing-Kapazität ist noch frei?“ | `run_trading_orchestrator()` |
| 4 | „Warum stoppt der Plan?“ | `get_opportunity_view(status="ENTRY_READY")` (`STOPPED_CAPITAL_EXHAUSTED`) |
| 5 | „Wie hoch ist die Summe der geplanten Entries?“ | `get_opportunity_view()` |

**Hinweise:**

- Post-Action-Kapital = Erlös aus SELL/TRIM abzüglich geschätzter Steuer. Daraus ergeben sich Cash, Swing-Anteil und freie Swing-Kapazität nach den Aktionen.
- Grenzen des Sizings: Swing-Maximum 40 %, Cash-Reserve (Standard im Code 10.000 EUR), maximales Positionsgewicht, ganze Stück abgerundet. Es wird sequentiell nach Rang vergeben.
- Nicht modelliert: Gebühren, FIFO-Lots, Verlusttopf und Freibetrag, Sektor-Konzentration. Es gelten nur die Einzeltitel-Limits.
- `get_opportunity_view` zeigt in der kompakten Antwort einen minimalen Kapitalrahmen (Cash, Cash nach Verkäufen, konservativer Nettoerlös, geplante Entries, verbleibende Kaufkapazität, Swing-Anteil).

## 10. Steuer und Netto-Erlös

Für geplante Verkäufe wird eine Steuer geschätzt, damit der Planner mit Netto-Erlösen rechnet. Das ist eine Schätzung, keine Steuerberatung.

**Tools:** `run_trading_orchestrator`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Wie hoch ist die geschätzte Steuer auf die geplanten Verkäufe?“ | `run_trading_orchestrator()` |
| 2 | „Was ist der konservative Netto-Erlös?“ | `run_trading_orchestrator()` |
| 3 | „Wie unterscheiden sich die konservative Sicht und die Sicht mit Verlustverrechnung?“ | `run_trading_orchestrator()` |
| 4 | „Auf welcher Basis wird die Steuer geschätzt?“ | `run_trading_orchestrator()` (`tax_estimate_quality`) |

**Hinweise:**

- Satz: 25 % Abgeltungsteuer plus 5,5 % Solidaritätszuschlag auf die Steuer = 26,375 %, Kirchensteuer 0 %. Sie fällt nur auf den realisierten Gewinn an, nie auf den Brutto-Erlös.
- Ein Verlust ergibt keine negative Steuer und kein zusätzliches Cash.
- Basis ist der Durchschnitts-Einstand (`positions.avg_cost`), nicht FIFO; `tax_estimate_quality` = `estimated_from_average_cost`.
- Geplant wird mit der konservativen Sicht (keine Verlustverrechnung). Verlusttopf, Freistellungsauftrag und bereits realisierte Ergebnisse des Jahres sind unbekannt und werden nicht erfunden.

## 11. Swing Campaigns

Eine Swing Campaign hält den Lebenszyklus einer Swing-Position fest (Baseline, Ergänzungen, Reduktionen, Abschluss). Es gibt dafür kein eigenes MCP-Tool: Der Lebenszyklus fließt in die Entscheidungen des Depotstatus ein und lässt sich über die Datenbank-Tools lesen.

**Tools:** `run_trading_orchestrator`, `table_info`, `sql_execute`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Welche Swing-Aktionen schlägt der Agent für meine Positionen vor?“ | `run_trading_orchestrator()` |
| 2 | „Welche Swing-Campaigns sind aktuell offen?“ | `sql_execute` (SELECT auf `swing_campaign`) |
| 3 | „Welche Ereignisse sind zur Campaign von AAPL erfasst?“ | `sql_execute` (SELECT auf `swing_campaign_event`) |
| 4 | „Welche Spalten hat die Tabelle swing_campaign?“ | `table_info(table_name="swing_campaign")` |

**Hinweise:**

- Campaigns werden außerhalb von MCP geführt: mit dem Skript `Manage-TradingSwingCampaigns.py` (`open`, `event`, `close`, schreibend nur mit `--write`).
- Der Parqet-Import ordnet eindeutig zuordenbare Trades nach dem Start einer Campaign zu (SELL als `manual_reduction`, BUY als `add`). Mehrdeutige Fälle schreiben nichts und werden als `MANUAL_REVIEW_REQUIRED` oder `AMBIGUOUS` gemeldet.
- Der Agent leitet aus Trades nie Take-Profit- oder Stop-Gründe ab, und Campaigns werden nicht automatisch geschlossen.

## 12. Datenbank und Diagnose

Die Datenbank-Tools zeigen Zustand, Struktur und Inhalte der lokalen SQLite-Datenbank. Vier davon lesen nur; `sql_execute` ist ein uneingeschränkter SQL-Zugang.

**Tools:** `database_status`, `database_tables`, `database_schema`, `table_info`, `sql_execute`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Wie ist der Status der Datenbank?“ | `database_status()` |
| 2 | „Wie viele Zeilen hat jede Tabelle?“ | `database_tables()` |
| 3 | „Zeig mir das komplette Schema.“ | `database_schema()` |
| 4 | „Welche Spalten und Indizes hat die Tabelle positions?“ | `table_info(table_name="positions")` |
| 5 | „Zeig die zehn jüngsten Transaktionen.“ | `sql_execute` (SELECT) |

**Hinweise:**

- `database_status` liefert Pfad, Dateigröße, SQLite-Version, Journal-Modus, Foreign-Key-Modus und Busy-Timeout. `table_info` liefert Spalten, Indizes und Foreign Keys einer Tabelle.
- `sql_execute` führt beliebiges SQL aus (SELECT, WITH, PRAGMA, EXPLAIN, aber auch INSERT, UPDATE, DELETE, CREATE, ALTER, DROP). Alle Statements laufen in einer Transaktion und werden bei einem Fehler komplett zurückgerollt.
- Weil `sql_execute` Daten ändern und löschen kann, nur gezielte, bewusste Abfragen verwenden und Änderungen ausdrücklich bestätigen lassen. Für fachliche Fragen sind die anderen Tools vorgesehen.
- Keines dieser Tools aktualisiert Marktdaten oder Reports.

## 13. Strategiezuordnung und Promotion

Titel können `swing`, `long_term` oder `unknown` zugeordnet werden. Der Agent schlägt vor und prüft; zugeordnet wird nur nach ausdrücklicher Freigabe eines konkreten Plans.

**Tools:** `suggest_strategy_assignments`, `evaluate_swing_candidates`, `evaluate_swing_candidate`, `approve_swing_promotion`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Welche Strategie wird für meine Watchlist-Titel vorgeschlagen?“ | `suggest_strategy_assignments()` |
| 2 | „Welche Watchlist-Titel kommen für Swing in Frage?“ | `evaluate_swing_candidates()` |
| 3 | „Ist der Titel mit der security_id 12 ein PROMOTE-Kandidat?“ | `evaluate_swing_candidate(security_id=12)` |
| 4 | „Ich genehmige die Promotion mit dem Plan-Token aus der Prüfung.“ | `approve_swing_promotion(security_id=12, plan_token="<token>", effective_from="2026-01-31")` |

**Hinweise:**

- Ein Vorschlag ist keine Zuordnung: `suggest_strategy_assignments` schreibt nichts und betrachtet nur Watchlist-Titel ohne aktive Zuordnung. `long_term` wird nie allein aus dem Momentum abgeleitet (einzige Grundlage derzeit: `asset_type` ETF oder Fonds).
- Ergebnis der Promotion-Prüfung ist `PROMOTE` oder `KEEP_WATCHING`. `PROMOTE` ist kein `BUY`, sondern die Eingabe der Entry-Bewertung.
- `approve_swing_promotion` ist das einzige fachliche Schreib-Tool. Es wird erst nach ausdrücklicher Freigabe des zurückgegebenen Tokens aufgerufen, nimmt keine Strategie-, Mengen-, Preis-, SQL- oder Campaign-Parameter an, wertet den aktuellen Stand neu aus und legt genau eine `swing`-Zuordnung an.
- Es entstehen weder eine Campaign noch eine Order.

## 14. Datenqualität und Aktualität

Reports und Marktdaten werden außerhalb des MCP erzeugt. Die lesenden Tools zeigen deshalb immer Stand und Alter; veraltete oder fehlende Daten werden offen gekennzeichnet statt verdeckt.

**Tools:** `get_opportunity_view`, `get_market_intelligence`, `get_candidate_discovery`, `run_trading_orchestrator`, `database_status`

| # | Frage | Tool-Aufruf |
|---|---|---|
| 1 | „Sind die Reports aktuell?“ | `get_opportunity_view()` (Stand, Alter und `stale` beider Reports) |
| 2 | „Wie alt ist der News-Report?“ | `get_market_intelligence()` |
| 3 | „Wie alt ist der Discovery-Report?“ | `get_candidate_discovery()` |
| 4 | „Sind die Daten für den Depotstatus vollständig?“ | `run_trading_orchestrator()` (`global_status`) |
| 5 | „Warum ist ein Kandidat BLOCKED_BY_DATA?“ | `get_opportunity_view(symbol="AAPL")` |
| 6 | „Wie groß ist die Datenbank und welche SQLite-Version läuft?“ | `database_status()` |

**Hinweise:**

- `REPORT_STALE`: Der Discovery-Report ist älter als `market_data_max_age_days` (Standard 5 Tage), der News-Report älter als 24 Stunden.
- Ohne Report antworten die Tools mit `UNAVAILABLE` (`NO_DISCOVERY_REPORT` bzw. `NO_MARKET_INTELLIGENCE_REPORT`). `get_opportunity_view` liefert dann `PARTIAL`, und der News-Kontext wird zu `NEWS_UNAVAILABLE`.
- Veraltete Marktdaten (älter als `market_data_max_age_days`) meldet die Analyse als Datenqualitätsproblem; fehlende oder unzureichende Daten führen bei Kandidaten zu `BLOCKED_BY_DATA` bzw. `INSUFFICIENT_DATA`.
- Die lesenden Tools holen nie selbst Daten. Frischere Daten entstehen nur durch die geplanten Backfills und Collector-Läufe außerhalb des MCP.

## 15. Was der Agent nicht macht

Der Agent analysiert, rankt und plant. Er handelt nicht. Die folgenden Anfragen werden bewusst nicht so ausgeführt, wie sie gestellt sind.

**Tools:** –

| # | Anfrage | Verhalten |
|---|---|---|
| 1 | „Kaufe 10 AAPL.“ | Es gibt keine Order. Die Ausführung erfolgt außerhalb des Trading Agents. |
| 2 | „Nimm NVDA automatisch in die Watchlist auf.“ | Die Discovery schlägt nur vor; es gibt keine automatische Aufnahme. |
| 3 | „Bewerte NVDA wegen der News besser.“ | News ändern nie Score, Rang, Positionsgröße oder Entscheidung. |
| 4 | „Hol jetzt die neuesten Nachrichten aus dem Web.“ | Die Reader lesen nur den letzten Report; der Abruf läuft im Collector außerhalb des MCP. |
| 5 | „Starte die Discovery neu.“ | Dafür gibt es kein MCP-Tool; sie läuft als Skript bzw. geplanter Task. |
| 6 | „Sag mir verbindlich, wie viel Steuer ich zahle.“ | Es gibt nur eine Schätzung auf Basis des Durchschnitts-Einstands; keine Steuerberatung. |

**Hinweise:**

- `PROMOTE` ist kein `BUY`. `DISCOVERY` ist kein `PROMOTE`. `NEWS` ist kein Signal.
- Keine Order, keine automatische Strategiezuordnung, keine automatische Campaign-Eröffnung; die einzige fachliche Schreiboperation ist `approve_swing_promotion` nach ausdrücklicher Freigabe.
- Keine individuelle Anlageberatung: Der Agent liefert Fakten, Regeln und Pläne, die Entscheidung bleibt beim Nutzer.
- Nicht modelliert: Gebühren, FIFO-Lots, Verlusttopf, Freibetrag, Sektor-Konzentration der Bestandspositionen.
- Der Agent startet keine Scheduled Tasks, führt keine Schema-Migration aus und startet kein Frontend neu.

## 16. Empfohlene kurze Fragen

Kurze Formulierungen, die zuverlässig auf das passende Tool führen.

**Tools:** –

| # | Kurzfrage | Tool-Aufruf |
|---|---|---|
| 1 | „Depotstatus“ | `run_trading_orchestrator()` |
| 2 | „Handlungsbedarf“ | `run_trading_orchestrator()` |
| 3 | „Beste neue Chancen“ | `get_opportunity_view(source="DISCOVERY")` |
| 4 | „ENTRY_READY“ | `get_opportunity_view(status="ENTRY_READY")` |
| 5 | „Neue Kandidaten“ | `get_candidate_discovery()` |
| 6 | „News zu AAPL“ | `get_market_intelligence(symbol="AAPL")` |
| 7 | „Makro-News“ | `get_market_intelligence(scope="MACRO")` |
| 8 | „NVDA gegen AAPL“ | `get_opportunity_view(symbol="NVDA,AAPL")` |
| 9 | „Freies Kapital“ | `run_trading_orchestrator()` |
| 10 | „Watchlist-Rang“ | `rank_watchlist()` |
| 11 | „Strategievorschläge“ | `suggest_strategy_assignments()` |
| 12 | „Datenbankstatus“ | `database_status()` |

**Hinweise:**

- Ein Filter wie `ENTRY_READY` liefert alle Treffer in einem Aufruf; weitere Aufrufe sind selten nötig.
- Optionale Parameter nur setzen, wenn sie die Frage wirklich verändern; Standardwerte reichen meist.

## A. Technische Zuordnung

Alle 15 MCP-Tools mit Zugriffsart. Namen und Parameter stammen aus dem aktuellen Code.

| Tool | Zugriff | Zweck | Wichtige Parameter | Kapitel |
|---|---|---|---|---|
| `approve_swing_promotion` | Schreiben (nur nach Freigabe) | Genehmigten, noch aktuellen PROMOTE-Plan anwenden: genau eine swing-Zuordnung | `security_id`, `plan_token`, `effective_from` | 13 |
| `database_schema` | Lesen | Vollständiges SQLite-Schema (Tabellen, Views, Indizes, Trigger) | – | 12 |
| `database_status` | Lesen | Datenbankstatus: Pfad, Größe, SQLite-Version, Journal-Modus | – | 12, 14 |
| `database_tables` | Lesen | Alle Tabellen mit exakten Zeilenzahlen | – | 12 |
| `evaluate_swing_candidate` | Lesen | Promotion-Prüfung für einen Watchlist-Titel | `security_id` | 3, 13 |
| `evaluate_swing_candidates` | Lesen | Promotion-Prüfung aller Watchlist-Titel ohne Position | – | 3, 13 |
| `evaluate_watchlist_candidates` | Lesen | Status BUY, WATCH, DEFERRED oder INSUFFICIENT_DATA je Watchlist-Titel | `as_of` | 3 |
| `get_candidate_discovery` | Lesen | Letzter Discovery-Report: neue Kandidaten, nicht PROMOTE/BUY | `status`, `limit`, `detail`, `symbol` | 4, 7, 14 |
| `get_market_intelligence` | Lesen | Letzter News-Report: klassifizierte Makro- und Unternehmensnachrichten | `scope`, `category`, `importance`, `symbol`, `sector`, `limit`, `detail` | 5, 7, 14 |
| `get_opportunity_view` | Lesen | Kandidaten aus Engine und Discovery mit News-Kontext, Plan-Status und Kapital | `source`, `status`, `news_status`, `symbol`, `limit`, `detail` | 2, 6, 7, 8, 9, 14 |
| `rank_watchlist` | Lesen | Technischer Momentum-/Trend-Rang der Watchlist | – | 3 |
| `run_trading_orchestrator` | Lesen | Depotstatus mit Aktionen, Allokation, Kapital- und Entry-Plan | `as_of`, `detail` | 1, 2, 9, 10, 11, 14 |
| `sql_execute` | Schreiben möglich (uneingeschränkt) | Beliebiges SQL auf der lokalen Datenbank | `sql` | 7, 11, 12 |
| `suggest_strategy_assignments` | Lesen | Vorschlag swing, long_term oder unknown je Watchlist-Titel ohne Zuordnung | `as_of` | 13 |
| `table_info` | Lesen | Spalten, Indizes und Foreign Keys einer Tabelle | `table_name` | 11, 12 |

## B. Grenzen und Semantik

Statuswerte und Tool-Namen werden nicht übersetzt.

- `PROMOTE` ist kein `BUY`; `DISCOVERY` ist kein `PROMOTE`; `NEWS` ist kein Signal. Es werden keine Orders erzeugt.
- Nur `approve_swing_promotion` (nach ausdrücklicher Freigabe) und `sql_execute` können Daten schreiben; alle anderen Tools lesen nur.
- Discovery- und News-Reader lesen den letzten fertigen Report und führen weder Scan noch Webabruf aus. Stand und Alter werden immer mit ausgegeben.
- Kompakt ist der Standard. `detail=true` liefert Rohdaten in großem Umfang und gehört nur auf ausdrücklichen Wunsch angefragt. Antworten bleiben unter dem Größenlimit des MCP (40.000 Zeichen).
- Die Engine rechnet, das Frontend zeigt an: Summen, Zähler und Rangfolgen werden nicht neu berechnet oder geschätzt.
- Nach einem Deploy müssen laufende MCP-Prozesse bzw. das Frontend neu gestartet werden, weil Python-Module pro Prozess gecacht werden.

| Statuswert | Bedeutung |
|---|---|
| `SELL`, `TRIM`, `HOLD`, `ADD` | Aktionen des Depotstatus für bestehende Positionen: verkaufen, reduzieren, halten, aufstocken. |
| `PROMOTE`, `KEEP_WATCHING` | Ergebnis der Promotion-Prüfung: geeignet für eine Swing-Zuordnung bzw. weiter beobachten. |
| `ENTRY_READY` | PROMOTE-Kandidat, der alle Entry-Prüfungen besteht und gerankt wird. |
| `WAIT_FOR_TRIGGER` | PROMOTE-Kandidat, der noch auf einen Auslöser wartet. |
| `BLOCKED_BY_ALLOCATION`, `BLOCKED_BY_DATA`, `BLOCKED_BY_CONCENTRATION` | Entry blockiert durch Allokationsgrenze, fehlende Daten bzw. Konzentrationslimit. |
| `BLOCKED_EXISTING_POSITION`, `BLOCKED_EXISTING_CAMPAIGN` | Entry blockiert, weil bereits eine Position bzw. eine offene Campaign besteht. |
| `DISCOVERY_READY`, `DISCOVERY_WATCH` | Neuer Kandidat der Discovery: bereit bzw. beobachtenswert. |
| `NEWS_HIGH_ATTENTION`, `NEWS_ATTENTION`, `NEWS_CLEAR`, `NEWS_UNAVAILABLE` | News-Kontext je Kandidat und Position; er ist Warnung und Kontext, nie ein Signal. |
| `STOPPED_CAPITAL_EXHAUSTED` | Der Entry-Plan hat den Kandidaten nicht mehr geplant, weil das Kapital erschöpft war. |
