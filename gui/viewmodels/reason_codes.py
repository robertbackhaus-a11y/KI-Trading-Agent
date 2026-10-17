# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Central presentation of technical reason / status codes: short German text, tooltip description, original code.

Pure Python (no Qt, no business logic).  The backend codes, the MCP payloads and the database stay untouched; this module only
translates a code for display.  Every view model that shows a code uses :func:`format_reason_code` / :func:`format_reason_codes`;
the views read the tooltip from the returned :class:`Presented` text.  An unknown code is shown readable ("FOO_BAR" -> "Foo bar") with a
generic tooltip that still names the original code - never raw, never empty, never an exception.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Iterable

EMPTY = "—"  # shown for a missing code
UNKNOWN_DESCRIPTION = "Für diesen technischen Status ist noch keine spezifische Beschreibung hinterlegt."

# UPPER_SNAKE code, optionally with a detail after a colon (planner warnings such as ``RISK_SECTOR_UNKNOWN:3``)
_CODE = re.compile(r"^([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)(?::(.+))?$")

# code -> (short text, description).  Codes of the planner, the position engine, entry sizing, the risk guards and the orchestrator
# that reach the Dashboard / Planner pages; the three import / discovery codes at the end were named explicitly for the GUI.
_TEXTS: dict[str, tuple[str, str]] = {
    # -- planner status ---------------------------------------------------------------------------------------------
    "AVAILABLE": ("Verfügbar", "Die Auswertung liegt vor."),
    "UNAVAILABLE": ("Nicht verfügbar", "Die Daten oder die Auswertung stehen derzeit nicht zur Verfügung."),
    "GLOBAL_BLOCKING": ("Global blockiert", "Ein globales Problem verhindert die Auswertung; es wird kein belastbarer Plan erzeugt."),
    # -- entry status of a candidate --------------------------------------------------------------------------------
    "ENTRY_READY": ("Einstieg möglich", "Der Kandidat erfüllt aktuell die Voraussetzungen für einen geplanten Einstieg."),
    "WAIT_FOR_TRIGGER": ("Einstiegssignal fehlt", "Der Kandidat ist grundsätzlich interessant, erfüllt aber den aktuell benötigten Einstiegstrigger noch nicht."),
    "BLOCKED_BY_ALLOCATION": ("Allokationslimit erreicht", "Der Einstieg passt nicht in die zulässige Aufteilung (Swing-Anteil, Cash-Reserve oder Mindeststückzahl)."),
    "BLOCKED_BY_DATA": ("Daten unzureichend", "Für eine belastbare Bewertung des Kandidaten fehlen Daten."),
    "BLOCKED_BY_CONCENTRATION": ("Positionslimit erreicht", "Der Einstieg würde die zulässige Gewichtung einer einzelnen Position überschreiten."),
    "BLOCKED_EXISTING_POSITION": ("Position bereits vorhanden", "Ein neuer Einstieg wird nicht geplant, weil bereits eine Position in diesem Wertpapier besteht."),
    "BLOCKED_EXISTING_CAMPAIGN": ("Offene Kampagne vorhanden", "Für das Wertpapier läuft bereits eine offene Swing-Kampagne, daher wird kein neuer Einstieg geplant."),
    # -- risk guards of the planner ---------------------------------------------------------------------------------
    "BLOCKED_BY_SECTOR_CONCENTRATION": ("Sektorlimit erreicht", "Ein weiterer Kauf würde den Anteil dieses Sektors im Portfolio über das festgelegte Limit erhöhen."),
    "BLOCKED_BY_CORRELATION": ("Zu stark korreliert", "Der Kandidat würde zusammen mit bereits gehaltenen oder geplanten Positionen ein zu stark korreliertes Klumpenrisiko erzeugen."),
    "OK": ("OK", "Bei dieser Prüfung gibt es keine Beanstandung."),
    "BLOCKED": ("Blockiert", "Die Prüfung verhindert den Kauf."),
    "PARTIAL": ("Teilweise berechenbar", "Nicht für alle Positionen lagen die nötigen Daten vor; die Prüfung ist eingeschränkt."),
    "SECTOR_UNKNOWN": ("Sektor unbekannt", "Für das Wertpapier ist kein Sektor hinterlegt; die Sektorprüfung wird nicht angewendet."),
    "NO_OTHER_POSITION": ("Keine andere Position", "Es gibt keine andere Position, mit der sich eine Korrelation berechnen ließe."),
    # -- why a ready candidate is deferred / what limits the plan -----------------------------------------------------
    "STOPPED_SWING_MAX_REACHED": ("Swing-Limit erreicht", "Der maximal zulässige Anteil für Swing-Positionen ist erreicht, daher wird kein weiterer Einstieg geplant."),
    "STOPPED_CASH_RESERVE_REACHED": ("Cash-Reserve erreicht", "Die Mindest-Cash-Reserve ist erreicht, daher wird kein weiterer Einstieg geplant."),
    "STOPPED_CAPITAL_EXHAUSTED": ("Kapital aufgebraucht", "Das verfügbare Kapital reicht für den nächsten Kandidaten nicht mehr, daher stoppt die Planung in der Rangfolge."),
    "SWING_MAX_REACHED": ("Swing-Limit erreicht", "Der maximal zulässige Anteil für Swing-Positionen ist erreicht; der Kandidat wird zurückgestellt."),
    "CASH_RESERVE_REACHED": ("Cash-Reserve erreicht", "Der Einstieg würde die Mindest-Cash-Reserve unterschreiten; der Kandidat wird zurückgestellt."),
    "ALLOWED_CAPITAL_BELOW_ONE_SHARE_PRICE": ("Kapital unter einem Stückpreis", "Das zulässige Kapital reicht nicht für eine ganze Aktie; der Kandidat wird zurückgestellt."),
    "CONCENTRATION_LIMIT": ("Positionslimit erreicht", "Der Einstieg würde die zulässige Gewichtung dieser Position überschreiten; der Kandidat wird zurückgestellt."),
    "PORTFOLIO_STATE_UNAVAILABLE": ("Portfoliostand fehlt", "Der Kapital- und Portfoliostand ist nicht verfügbar, daher kann nicht geplant werden."),
    "ALL_READY_CANDIDATES_PLANNED": ("Alle Kandidaten eingeplant", "Alle einstiegsbereiten Kandidaten sind im Plan enthalten."),
    "NO_ENTRY_PLANNED": ("Kein Einstieg geplant", "Es wurde weder ein rangierter Entry noch ein BUY/ADD-Kauf in den Plan aufgenommen."),
    "NO_RANKED_ENTRY_PLANNED": ("Kein rangierter Entry (PROMOTE)", "Aus der Entry-Bewertung (PROMOTE) wurde kein Entry eingeplant. Die BUY/ADD-Käufe der Engine sind davon unabhängig und stehen in den geplanten Entries."),
    # -- entry sizing (block reasons of a candidate) ------------------------------------------------------------------
    "ENTRY_BLOCKED": ("Einstieg blockiert", "Der Einstieg ist blockiert, ohne dass ein genauerer Grund gemeldet wurde."),
    "ENTRY_EXISTING_POSITION": ("Position bereits vorhanden", "Ein neuer Einstieg wird nicht geplant, weil bereits eine Position in diesem Wertpapier besteht."),
    "ENTRY_OPEN_CAMPAIGN_EXISTS": ("Offene Kampagne vorhanden", "Für das Wertpapier läuft bereits eine offene Swing-Kampagne, daher wird kein neuer Einstieg geplant."),
    "ENTRY_NOT_SWING_CANDIDATE": ("Kein Swing-Kandidat", "Das Wertpapier ist nicht als Swing-Kandidat geführt."),
    "ENTRY_STRATEGY_CONFLICT": ("Strategiekonflikt", "Dem Wertpapier ist eine andere Strategie zugeordnet (z. B. Long-Term), daher gibt es keinen Swing-Einstieg."),
    "ENTRY_SECURITY_WEIGHT_LIMIT": ("Positionslimit erreicht", "Die Position würde das maximal zulässige Gewicht eines einzelnen Wertpapiers überschreiten."),
    "ENTRY_INITIAL_WEIGHT_LIMIT": ("Einstiegslimit erreicht", "Für den ersten Einstieg bleibt innerhalb des zulässigen Anfangsgewichts kein Spielraum."),
    "ENTRY_SWING_ALLOCATION_LIMIT": ("Swing-Limit erreicht", "Der maximal zulässige Anteil für Swing-Positionen ist erreicht, daher wird kein weiterer Einstieg geplant."),
    "ENTRY_CASH_RESERVE_LIMIT": ("Cash-Reserve erreicht", "Der Einstieg würde die Mindest-Cash-Reserve unterschreiten."),
    "ENTRY_QUANTITY_ZERO": ("Keine ganze Aktie möglich", "Mit dem zulässigen Kapital lässt sich keine ganze Aktie kaufen."),
    "ENTRY_PRICE_BELOW_OR_EQUAL_SMA50": ("Kurs nicht über SMA50", "Der Kurs liegt auf oder unter dem 50-Tage-Durchschnitt; der Trendfilter erlaubt noch keinen Einstieg."),
    "ENTRY_PRICE_BELOW_OR_EQUAL_SMA200": ("Kurs nicht über SMA200", "Der Kurs liegt auf oder unter dem 200-Tage-Durchschnitt; der Trendfilter erlaubt noch keinen Einstieg."),
    "ENTRY_SMA50_BELOW_OR_EQUAL_SMA200": ("SMA50 nicht über SMA200", "Der 50-Tage-Durchschnitt liegt auf oder unter dem 200-Tage-Durchschnitt; der Trendfilter erlaubt noch keinen Einstieg."),
    "ENTRY_PORTFOLIO_VALUATION_UNAVAILABLE": ("Portfoliobewertung fehlt", "Der Portfoliowert ist nicht verfügbar, daher lässt sich der Einstieg nicht dimensionieren."),
    "ENTRY_SWING_ALLOCATION_UNAVAILABLE": ("Swing-Anteil unbekannt", "Der aktuelle Swing-Anteil ist nicht verfügbar, daher lässt sich der Einstieg nicht dimensionieren."),
    "ENTRY_CASH_UNAVAILABLE": ("Cash-Stand fehlt", "Der Cash-Stand ist nicht verfügbar, daher lässt sich der Einstieg nicht dimensionieren."),
    "ENTRY_TECHNICAL_DATA_UNAVAILABLE": ("Technische Daten fehlen", "Die technischen Daten sind nicht verfügbar, daher gibt es keinen Einstieg."),
    "ENTRY_PRICE_UNAVAILABLE": ("Kurs fehlt", "Es liegt kein aktueller Kurs vor."),
    "ENTRY_SMA50_UNAVAILABLE": ("SMA50 fehlt", "Der 50-Tage-Durchschnitt ist nicht verfügbar."),
    "ENTRY_SMA200_UNAVAILABLE": ("SMA200 fehlt", "Der 200-Tage-Durchschnitt ist nicht verfügbar."),
    # -- data reasons of a candidate in the planner --------------------------------------------------------------------
    "CANDIDATE_SNAPSHOT_UNAVAILABLE": ("Kandidatendaten fehlen", "Für den Kandidaten liegt keine Datenbasis vor."),
    "TECHNICAL_DATA_UNAVAILABLE": ("Technische Daten fehlen", "Die technischen Daten sind nicht verfügbar, veraltet oder unzureichend."),
    "TECHNICAL_DATA_PARTIAL": ("Technische Daten unvollständig", "Die technischen Daten des Kandidaten sind nur teilweise verfügbar."),
    "TECHNICAL_DATA_INSUFFICIENT": ("Technische Daten unzureichend", "Die Kurshistorie des Kandidaten reicht für die Bewertung nicht aus."),
    "TECHNICAL_DATA_STALE": ("Technische Daten veraltet", "Die technischen Daten des Kandidaten sind nicht mehr aktuell."),
    "TECHNICAL_DATA_NOT_APPLICABLE": ("Technische Daten nicht anwendbar", "Für den Kandidaten sind technische Daten nicht anwendbar."),
    "PRICE_UNAVAILABLE": ("Kurs fehlt", "Es liegt kein aktueller Kurs vor."),
    "SMA_UNAVAILABLE": ("Durchschnitte fehlen", "Der 50- oder der 200-Tage-Durchschnitt ist nicht verfügbar."),
    "PRICE_EUR_UNAVAILABLE": ("Euro-Kurs fehlt", "Es liegt kein in Euro umgerechneter Kurs vor."),
    "FX_UNAVAILABLE": ("Wechselkurs fehlt", "Der Wechselkurs für die Bewertung ist veraltet oder nicht verfügbar."),
    "SCORE_UNAVAILABLE": ("Score fehlt", "Für den Kandidaten konnte kein Score berechnet werden."),
    "SCORE_BELOW_THRESHOLD": ("Score unter Schwelle", "Der Score des Kandidaten liegt unter der Schwelle für einen Einstieg."),
    # -- decision of the position engine ------------------------------------------------------------------------------
    "EXISTING_POSITION_DEFAULT_HOLD": ("Bestehende Position – halten", "Für die bereits vorhandene Position liegt aktuell kein stärkeres Kauf- oder Verkaufssignal vor."),
    "HARD_STOP_TRIGGERED": ("Stop unterschritten", "Der Kurs liegt unter dem festgelegten Hard-Stop; die Engine empfiehlt den vollständigen Ausstieg."),
    "HARD_STOP_EVALUATION_BLOCKED": ("Stop-Prüfung nicht möglich", "Der Hard-Stop konnte wegen fehlender Daten nicht geprüft werden."),
    "SWING_MAX_HORIZON_REACHED": ("Swing-Zeitraum abgelaufen", "Die Swing-Kampagne hat die maximale Haltedauer erreicht; der Rest der Position wird zum Verkauf empfohlen."),
    "RUNNER_BELOW_SMA200": ("Runner unter SMA200", "Der Kurs des verbleibenden Anteils liegt unter dem 200-Tage-Durchschnitt; der Rest der Position wird zum Verkauf empfohlen."),
    "RUNNER_BELOW_SMA50": ("Runner unter SMA50", "Der Kurs des verbleibenden Anteils liegt unter dem 50-Tage-Durchschnitt; Warnsignal, die Position wird vorerst gehalten."),
    "RUNNER_TECHNICAL_INPUT_UNAVAILABLE": ("Runner-Daten fehlen", "Für die Bewertung des verbleibenden Anteils fehlen technische Daten; die Position wird vorerst gehalten."),
    "TP1_REACHED": ("Kursziel 1 erreicht", "Der Kurs hat das erste Gewinnziel der Swing-Kampagne erreicht."),
    "TP2_REACHED": ("Kursziel 2 erreicht", "Der Kurs hat das zweite Gewinnziel der Swing-Kampagne erreicht."),
    "TP1_TRIM_RECOMMENDED": ("Teilverkauf bei Ziel 1", "Beim ersten Gewinnziel wird der Verkauf eines Teils der Position empfohlen."),
    "TP2_CUMULATIVE_TRIM_RECOMMENDED": ("Teilverkauf bei Ziel 2", "Beim zweiten Gewinnziel wird der Verkauf des kumulierten Anteils beider Ziele empfohlen; bereits Verkauftes wird angerechnet."),
    "ADD_RECOMMENDED": ("Aufstocken möglich", "Die Engine empfiehlt, die bestehende Position aufzustocken."),
    "NO_POSITION_DEFAULT_WATCH": ("Keine Position – beobachten", "Es besteht keine Position und kein Kaufsignal; das Wertpapier wird nur beobachtet."),
    "NEW_ENTRY_RECOMMENDED": ("Neuer Einstieg empfohlen", "Die Engine empfiehlt einen neuen Einstieg in das Wertpapier."),
    "POSITION_STATE_DATA_BLOCKER": ("Positionsdaten nicht auswertbar", "Der historische Positions- und Watchlist-Stand wird nicht unterstützt, daher erfolgt keine Bewertung."),
    "TECHNICAL_DATA_BLOCKER": ("Technische Daten unzureichend", "Die technischen Daten sind unvollständig, veraltet oder nicht verfügbar; das Wertpapier wird nur beobachtet."),
    # -- allocation guardrails ------------------------------------------------------------------------------------------
    "SWING_ALLOCATION_BELOW_MIN": ("Swing-Anteil unter Ziel", "Der Anteil der Swing-Positionen liegt unter dem Zielbereich."),
    "SWING_ALLOCATION_ABOVE_MAX": ("Swing-Anteil über Ziel", "Der Anteil der Swing-Positionen liegt über dem Zielbereich."),
    "LONG_TERM_ALLOCATION_BELOW_MIN": ("Long-Term-Anteil unter Ziel", "Der Anteil der Long-Term-Positionen liegt unter dem Zielbereich."),
    "LONG_TERM_ALLOCATION_ABOVE_MAX": ("Long-Term-Anteil über Ziel", "Der Anteil der Long-Term-Positionen liegt über dem Zielbereich."),
    "WITHIN_TARGET_RANGE": ("Im Zielbereich", "Die Aufteilung zwischen Swing und Long-Term liegt im Zielbereich."),
    # -- orchestrator issues ----------------------------------------------------------------------------------------------
    "ALLOCATION_OUTSIDE_TARGET": ("Aufteilung außerhalb Ziel", "Der Swing- oder Long-Term-Anteil liegt außerhalb des Zielbereichs (siehe Allokations-Hinweise)."),
    "CASH_RESERVE_LIMIT": ("Cash-Reserve erreicht", "Der verfügbare Cash liegt auf oder unter der Mindestreserve."),
    "CAPITAL_STATE_UNAVAILABLE": ("Kapitalstand fehlt", "Der Cash- und Kapitalstand ist nicht verfügbar; die Kaufplanung ist eingeschränkt."),
    "PORTFOLIO_VALUATION_UNAVAILABLE": ("Portfoliobewertung fehlt", "Der Portfoliowert kann nicht bestimmt werden; die Auswertung ist blockiert."),
    "PORTFOLIO_ALLOCATION_UNAVAILABLE": ("Aufteilung nicht berechenbar", "Die Aufteilung zwischen Swing und Long-Term kann nicht berechnet werden; die Auswertung ist blockiert."),
    "PORTFOLIO_CONTEXT_UNAVAILABLE": ("Portfoliokontext fehlt", "Der Portfoliokontext konnte nicht aufgebaut werden; die Auswertung ist blockiert."),
    "REQUIRED_SCHEMA_UNAVAILABLE": ("Datenbankschema unvollständig", "Erforderliche Tabellen fehlen in der Datenbank; die Auswertung ist blockiert."),
    "PROMOTION_APPROVAL_SCHEMA_UNAVAILABLE": ("Freigabe-Schema fehlt", "Die Tabelle für Promotion-Freigaben ist nicht vorhanden (Hinweis, nicht blockierend)."),
    "PROMOTION_FEATURE_VERSION_UNAVAILABLE": ("Promotion-Version unbekannt", "Die Schema-Version der Promotion-Freigaben fehlt oder stimmt nicht (Hinweis, nicht blockierend)."),
    "LIFECYCLE_RECONCILIATION_DELTA": ("Kampagnenmenge weicht ab", "Die Stückzahl der Swing-Kampagne stimmt nicht mit der Position überein; die Position ist blockiert."),
    "SWING_CAMPAIGN_UNAVAILABLE": ("Swing-Kampagne fehlt", "Die Position ist als Swing geführt, es gibt aber keine offene Kampagne; die Position ist blockiert."),
    "VALUATION_FX_UNAVAILABLE": ("Wechselkurs fehlt", "Der Wechselkurs für die Bewertung ist veraltet oder nicht verfügbar; die Position ist blockiert."),
    # -- planner notes --------------------------------------------------------------------------------------------------------
    "CONCENTRATION_SINGLE_SECURITY_LIMITS_ONLY": ("Nur Einzelwert-Limits aktiv", "Sektor- und Korrelationsprüfung sind nicht aktiv; es gelten nur die Limits je Einzelwert."),
    "EFFECTIVE_EXPOSURE_NOT_ASSESSED": ("Effective Exposure nicht beurteilt", "Die ETF-Holdings fehlen, sind veraltet oder unvollständig; das Einzelwert-Limit gilt nur für die direkte Position und ist nicht vollständig."),
    "SINGLE_NAME_EFFECTIVE_EXPOSURE_NOT_ASSESSED": ("Effective Exposure nicht beurteilt", "Die ETF-Holdings fehlen, sind veraltet oder unvollständig; das Einzelwert-Limit gilt nur für die direkte Position und ist nicht vollständig."),
    "SINGLE_NAME_LIMIT_EXCEEDED": ("Einzelwert-Limit überschritten", "Die in Klammern genannte Anzahl von Firmen liegt mit direkter Position plus ETF-Anteil über dem Einzelwert-Limit; bestehende Positionen werden dadurch nicht verkauft."),
    "RISK_SECTOR_MAP_UNAVAILABLE": ("Sektorzuordnung fehlt", "Die Sektorzuordnung ist nicht verfügbar, daher entfällt die Sektorprüfung."),
    "RISK_EXPOSURE_VALUE_INCOMPLETE": ("Positionswerte unvollständig", "Nicht für alle Positionen liegt ein Wert vor; die Risikoprüfung ist eingeschränkt."),
    "RISK_SECTOR_UNKNOWN": ("Sektor unbekannt", "Für die in Klammern genannte Anzahl von Wertpapieren ist kein Sektor hinterlegt; sie werden bei der Sektorprüfung nicht bewertet."),
    "RISK_CORRELATION_UNAVAILABLE": ("Korrelation nicht berechenbar", "Für die in Klammern genannte Anzahl von Wertpapieren ließ sich keine Korrelation berechnen."),
    "CASH_STATE_UNAVAILABLE": ("Cash-Stand fehlt", "Der Cash-Stand ist nicht verfügbar; die Kaufplanung ist eingeschränkt."),
    "BUYING_POWER_UNAVAILABLE_CASH_USED_AS_CAPITAL": ("Kaufkraft unbekannt", "Die Kaufkraft ist nicht hinterlegt; für die Planung gilt der Cash-Bestand als verfügbares Kapital."),
    "PROCEEDS_UNAVAILABLE": ("Verkaufserlös unbekannt", "Für das in Klammern genannte Wertpapier lässt sich der Verkaufserlös nicht schätzen."),
    "PURCHASE_VALUE_UNAVAILABLE": ("Kaufwert unbekannt", "Für das in Klammern genannte Wertpapier lässt sich der Kaufwert nicht bestimmen."),
    "TAX_ESTIMATED_FROM_AVERAGE_COST_NOT_FIFO": ("Steuer nur geschätzt", "Die Steuer wird mit dem Durchschnittskurs geschätzt, nicht nach FIFO-Lots."),
    "TAX_LOSS_OFFSET_DATA_UNKNOWN": ("Verlustverrechnung unbekannt", "Verlusttopf, Freibetrag und bereits realisierte Ergebnisse sind unbekannt und werden nicht angenommen."),
    "TAX_COST_BASIS_UNAVAILABLE_UPPER_BOUND_USED": ("Steuer mit Obergrenze geschätzt", "Für mindestens eine Position fehlt der Einstandskurs; für die Steuer wird eine Obergrenze angesetzt."),
    "FEES_NOT_MODELLED": ("Gebühren nicht berücksichtigt", "Orderkosten und Gebühren sind in der Planung nicht berücksichtigt."),
    "PROCEEDS_GROSS_TAXES_FEES_NOT_MODELLED": ("Erlöse brutto", "Steuern und Gebühren sind in den Verkaufserlösen nicht berücksichtigt."),
    "EXTERNAL_FUNDING_ASSUMED_AVAILABLE": ("External Funding angenommen", "Der Plan geht davon aus, dass externes Kapital zugeführt werden kann."),
    "EXTERNAL_FUNDING_REQUIRED_FOR_PLAN": ("External Funding nötig", "Der Plan benötigt zusätzliches externes Kapital (reiner Planungswert, nichts wird eingezahlt)."),
    # -- watchlist page: status, data quality, candidate decision, promotion, news ------------------------------------------------
    "WATCH": ("Beobachtung", "Das Wertpapier wird beobachtet (Watchlist-Status bzw. Einstufung ohne Kaufsignal)."),
    "LIMITED": ("Daten eingeschränkt", "Die Kurshistorie reicht für die meisten, aber nicht für alle Kennzahlen aus."),
    "INSUFFICIENT": ("Daten unzureichend", "Die Kurshistorie ist für eine belastbare Bewertung zu kurz."),
    "BUY": ("Einstieg prüfen", "Der Score des Kandidaten liegt über der Schwelle und die Portfolio-Grenzen stehen nicht entgegen (keine Order, nur eine Einstufung)."),
    "DEFERRED": ("Zurückgestellt", "Die Einstufung ist zurückgestellt, zum Beispiel weil keine Strategie zugeordnet ist oder eine Allokationsgrenze greift."),
    "INSUFFICIENT_DATA": ("Daten unzureichend", "Für eine Einstufung des Kandidaten fehlen Daten."),
    "PROMOTE": ("Promotion möglich", "Der Kandidat erfüllt die festen Promotion-Regeln; das ist nur ein Vorschlag, die Strategiezuordnung bleibt Sache des Nutzers."),
    "KEEP_WATCHING": ("Weiter beobachten", "Die Promotion-Regeln sind noch nicht erfüllt; der Kandidat bleibt auf der Watchlist."),
    "REJECT": ("Nicht geeignet", "Die Promotion-Regeln schließen den Kandidaten aus (zum Beispiel bestehende Position oder offene Kampagne)."),
    "DATA_INSUFFICIENT": ("Daten unzureichend", "Für die Promotion-Prüfung sind die Daten unvollständig oder veraltet."),
    "NEWS_CLEAR": ("Keine Auffälligkeit", "Der Markt-Intelligence-Bericht enthält keine direkten Meldungen mit erhöhter Wichtigkeit zu diesem Wertpapier."),
    "NEWS_ATTENTION": ("Beachten", "Es gibt eine direkte Meldung mittlerer Wichtigkeit oder eine hohe Meldung mit nur losem Bezug."),
    "NEWS_HIGH_ATTENTION": ("Hohe Aufmerksamkeit", "Es gibt mindestens eine direkte Meldung hoher Wichtigkeit zu diesem Wertpapier."),
    "NEWS_UNAVAILABLE": ("News nicht verfügbar", "Es liegt kein gültiger Markt-Intelligence-Bericht vor."),
    "RANKING_UNAVAILABLE": ("Ranking nicht verfügbar", "Das Ranking der Watchlist konnte nicht berechnet werden; Rang und Score fehlen."),
    "DECISIONS_UNAVAILABLE": ("Einstufung nicht verfügbar", "Die Einstufung der Kandidaten konnte nicht berechnet werden."),
    "ENGINE_UNAVAILABLE": ("Planner nicht verfügbar", "Das Ergebnis des Planners liegt nicht vor; der Einstiegsstatus fehlt."),
    "MARKET_INTELLIGENCE_REPORT_UNAVAILABLE": ("News-Bericht fehlt", "Es liegt kein gültiger Markt-Intelligence-Bericht vor; der News-Status ist nicht verfügbar."),
    # -- security onboarding (the answers of onboard_security) ---------------------------------------------------------------------
    "READY": ("Bereit zum Anlegen", "Die Prüfung hat keine Konflikte mit dem Security Master gefunden; es wurde noch nichts angelegt."),
    "CREATED": ("Angelegt", "Das Wertpapier wurde im Security Master angelegt, bei Bedarf samt Provider-Symbolen und Watchlist-Eintrag."),
    "ALREADY_EXISTS": ("Bereits vorhanden", "Das Wertpapier ist schon im Security Master; es wird weder ein zweites angelegt noch das vorhandene verändert."),
    "ISIN_CONFLICT": ("ISIN gehört einem anderen", "Die ISIN ist im Security Master bereits einem anderen Wertpapier zugeordnet."),
    "SYMBOL_CONFLICT": ("Symbol gehört einem anderen", "Das Symbol ist im Security Master bereits einem anderen Wertpapier zugeordnet (oder hat denselben Symbolstamm)."),
    "WKN_CONFLICT": ("WKN gehört einem anderen", "Die WKN ist im Security Master bereits einem anderen Wertpapier zugeordnet."),
    "ALIAS_CONFLICT": ("Provider-Symbol vergeben", "Das Provider-Symbol ist im Security Master bereits einem anderen Wertpapier zugeordnet."),
    "INVALID_INPUT": ("Eingabe ungültig", "Mindestens eine Eingabe fehlt oder hat ein ungültiges Format; es wurde nichts angelegt."),
    "ERROR": ("Fehler", "Die Aktion ist mit einem Fehler beendet worden; alle Änderungen wurden zurückgenommen."),
    # -- strategy edit (the answers of set_security_strategy) ----------------------------------------------------------------------
    "STRATEGY_SET": ("Strategie gespeichert", "Die Strategie des Wertpapiers wurde gesetzt; es wurde weder eine Campaign angelegt noch eine Order erzeugt."),
    "STRATEGY_REMOVED": ("Strategie entfernt", "Die aktive Strategiezuordnung des Wertpapiers wurde beendet; es wurde nichts sonst verändert."),
    "UNCHANGED": ("Unverändert", "Der gewünschte Wert war bereits gesetzt; es wurde nichts geschrieben."),
    "NOT_FOUND": ("Nicht gefunden", "Das Wertpapier oder der Eintrag wurde nicht gefunden."),
    "INVALID_IDENTIFIER": ("Ungültige Kennung", "Die übergebene Kennung des Wertpapiers ist ungültig; es wurde nichts geschrieben."),
    "INVALID_STRATEGY": ("Ungültige Strategie", "Erlaubt sind Swing, Long-Term oder keine Strategie; es wurde nichts geschrieben."),
    "BLOCKED_HELD_POSITION": ("Gehaltene Position", "Bei einer aktuell gehaltenen Position hängt die Strategie am Campaign-Ablauf und wird hier nicht isoliert geändert."),
    "BLOCKED_ACTIVE_CAMPAIGN": ("Aktive Campaign", "Für das Wertpapier läuft eine offene Swing-Campaign; ihre Strategie wird hier nicht geändert."),
    "ASSIGNMENT_HISTORY_UNCLEAR": ("Historie unklar", "Die Strategiezuordnungen des Wertpapiers sind nicht eindeutig (zum Beispiel mehrere aktive oder zukünftige); es wurde nichts geändert."),
    # -- sector edit (the answers of set_security_sector) ---------------------------------------------------------------------------
    "SECTOR_SET": ("Sektor gespeichert", "Die Sektor-Zuordnung wurde in der lokalen Sector-Map gespeichert; die öffentliche Sector-Map blieb unverändert."),
    "SECTOR_OVERRIDE_REMOVED": ("Lokale Zuordnung entfernt", "Der lokale Eintrag wurde entfernt; es gilt wieder der Wert der öffentlichen Sector-Map, falls es einen gibt."),
    "INVALID_SECTOR": ("Unbekannter Sektor", "Es sind nur vorhandene Sektor-Gruppen erlaubt; es wurde nichts geschrieben."),
    "NO_MAP_KEY": ("Kein Schlüssel", "Das Wertpapier hat weder ein Yahoo-Symbol noch ein Symbol und kann daher nicht in der Sector-Map geführt werden."),
    "SECTOR_MAP_UNAVAILABLE": ("Sector-Map fehlt", "Die öffentliche Sector-Map ist nicht lesbar; es wurde nichts geändert."),
    "LOCAL_MAP_INVALID": ("Lokale Map ungültig", "Die lokale Sector-Map ist ungültig (zum Beispiel kein gültiges JSON); es wurde nichts geändert."),
    # -- named explicitly for the GUI (not produced by the orchestrator / planner path) -----------------------------------------
    "BLOCKED_BY_EXISTING_POSITION": ("Position bereits vorhanden", "Ein neuer Einstieg wird nicht geplant, weil bereits eine Position in diesem Wertpapier besteht."),
    "STRATEGY_ASSIGNMENT_REQUIRED": ("Strategiezuordnung fehlt", "Für das Wertpapier ist noch nicht festgelegt, ob es als Swing- oder Long-Term-Position geführt werden soll."),
    "MARKET_DATA_MISSING": ("Marktdaten fehlen", "Für eine belastbare Bewertung fehlen aktuelle oder ausreichende Kursdaten."),
    # -- transaction import (open items of the Import page) ------------------------------------------------------------------------
    "MASTERDATA_CONFLICT": ("Stammdaten-Konflikt", "Die Datenbank hat für das Wertpapier einen anderen Wert als die Datei (zum Beispiel eine andere ISIN). Es wurde nichts überschrieben."),
    "IDENTIFIER_ALIAS_REFUSED": ("Zuordnung abgelehnt", "Die angeforderte Zuordnung der Kennung zu einem Wertpapier wurde abgelehnt (zum Beispiel fehlt die gleiche WKN als Beleg); ein Import wäre nicht möglich."),
    "ROWS_BLOCKED": ("Zeilen nicht importierbar", "Einzelne Zeilen der Datei sind ein Konflikt, ein unbekanntes Wertpapier oder ungültig und wurden nicht importiert."),
    "HISTORICAL_ROWS_NOT_IMPORTED": ("Historische Zeilen übersprungen", "Die Datei enthält Zeilen, die älter sind als die letzte gespeicherte Transaktion. Sie werden nur mit der entsprechenden Option importiert."),
    "STRATEGY_CONFLICT": ("Strategie widerspricht", "Die angeforderte Strategie weicht von der bestehenden Zuordnung ab; die Zuordnung wurde nicht geändert."),
    "STRATEGY_EFFECTIVE_FROM_UNCLEAR": ("Strategie-Beginn unklar", "Der Beginn der Strategiezuordnung ließ sich nicht eindeutig bestimmen; es wurde nichts geschrieben."),
    "CAMPAIGN_INITIALIZATION_REQUIRED": ("Kampagnenstart offen", "Der Start der Swing-Kampagne ist nicht eindeutig ableitbar und muss angegeben werden."),
    "CAMPAIGN_RECONCILIATION_REQUIRED": ("Kampagne abzugleichen", "Eine bestehende Kampagne stimmt nicht mit der Position überein und braucht eine manuelle Prüfung."),
    "CAMPAIGN_START_IGNORED": ("Kampagnenstart ignoriert", "Das angegebene Startdatum wurde nicht verwendet."),
}

KNOWN_REASON_CODES = frozenset(_TEXTS)

# The engine lists EXISTING_POSITION_DEFAULT_HOLD as the starting point of every decision, so it also stands next to a SELL / TRIM / ADD
# reason.  Next to such an action the text must not say "halten": code -> action (as in the payload) -> (short text, description).
_EXISTING = "Bestehende Position"
_WAS_EXISTING = "Die Position war im Ausgangszustand als bestehende Position markiert. Eine weitere Entscheidungsregel hat jedoch "
_AFTER_REDUCE = (_EXISTING, _WAS_EXISTING + "eine Verkaufs- oder Reduktionsaktion ausgelöst.")
_ACTION_TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "EXISTING_POSITION_DEFAULT_HOLD": {"SELL": _AFTER_REDUCE, "TRIM": _AFTER_REDUCE, "ADD": (_EXISTING, _WAS_EXISTING + "eine Aufstockung ausgelöst.")},
}


class Presented(str):
    """Display text that carries its tooltip (``tip``, rich text; empty = none).

    It is a ``str`` and compares equal to the plain text, so view models can put it into any table row or label unchanged; the views
    read ``tip`` to set the Qt tooltip.
    """

    tip: str

    def __new__(cls, text: str, tip: str = "") -> "Presented":
        obj = super().__new__(cls, text)
        obj.tip = tip
        return obj


@dataclass(frozen=True)
class ReasonPresentation:
    short_text: str
    description: str
    technical_code: str

    @property
    def tooltip(self) -> str:
        """Rich-text tooltip: the description, then ``Code: <original code>``.  Empty for text that is not a code."""
        if not self.description:
            return ""
        return f"<p>{html.escape(self.description, quote=False)}</p><p>Code: {html.escape(self.technical_code, quote=False)}</p>"


def _readable(code: str) -> str:
    """``FOO_BAR_BAZ`` -> ``Foo bar baz`` (the fallback for a code without a hand-written text)."""
    text = code.replace("_", " ").lower()
    return text[:1].upper() + text[1:]


def get_reason_presentation(code: Any, action: Any = None) -> ReasonPresentation:
    """Short text, description and original code of ``code``; never raises, never returns an empty short text.

    ``action`` is the action of the same payload row (``SELL``, ``TRIM``, ...), only used to word a few codes that read differently next to it.
    """
    text = "" if code is None else str(code).strip()
    if not text:
        return ReasonPresentation(EMPTY, "", "")
    match = _CODE.match(text)
    if match is None:  # free text (not a code): shown as it is, without a tooltip
        return ReasonPresentation(text, "", text)
    base, detail = match.groups()
    short, description = _TEXTS.get(base) or (_readable(base), UNKNOWN_DESCRIPTION)
    if action is not None:
        short, description = _ACTION_TEXTS.get(base, {}).get(str(action).strip().upper(), (short, description))
    return ReasonPresentation(f"{short} ({detail})" if detail else short, description, text)


def format_reason_code(code: Any, action: Any = None) -> Presented:
    """The visible short text of one code, carrying its tooltip."""
    p = get_reason_presentation(code, action)
    return Presented(p.short_text, p.tooltip)


def format_reason_codes(codes: Iterable[Any], separator: str = ", ", action: Any = None) -> Presented:
    """Several codes in one cell: the short texts joined, one tooltip block per code."""
    items = [get_reason_presentation(c, action) for c in codes if c is not None and str(c).strip()]
    if not items:
        return Presented(EMPTY)
    if len(items) == 1:
        return Presented(items[0].short_text, items[0].tooltip)
    blocks = "".join(
        f"<p><b>{html.escape(i.short_text, quote=False)}</b><br>{html.escape(i.description, quote=False)}<br>Code: {html.escape(i.technical_code, quote=False)}</p>"
        for i in items if i.description
    )
    return Presented(separator.join(i.short_text for i in items), blocks)


def format_status_and_reasons(status: Any, codes: Iterable[Any]) -> tuple[Presented, Presented]:
    """A status and its reason codes for two columns, without showing the same thing twice.

    A reason that reads exactly like the status (``ENTRY_READY`` as status and as reason, ``BLOCKED_EXISTING_POSITION`` next to
    ``ENTRY_EXISTING_POSITION``) is left out of the reasons; a differing original code is added to the tooltip of the status.
    """
    shown = get_reason_presentation(status)
    rest, merged = [], []
    for code in codes:
        item = get_reason_presentation(code)
        if shown.description and item.short_text == shown.short_text:
            if item.technical_code != shown.technical_code:
                merged.append(item.technical_code)
        else:
            rest.append(code)
    tip = shown.tooltip + "".join(f"<p>Code: {html.escape(c, quote=False)}</p>" for c in merged)
    return Presented(shown.short_text, tip), format_reason_codes(rest)
