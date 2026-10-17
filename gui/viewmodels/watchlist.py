# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""View model of the Watchlist page: ``get_watchlist`` payload -> table rows, filters, sort keys and the messages of the three watchlist actions.

Pure Python (no Qt, no database).  Nothing is computed here beyond formatting, filtering and the sort keys of the table; every figure comes from the
``get_watchlist`` result.  A missing or malformed value shows as "—".  Statuses and codes go through ``reason_codes`` (short text, tooltip with the
original code).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .portfolio import DASH, fmt_score, fmt_time
from .reason_codes import Presented, format_reason_code

HEADERS = ["Symbol", "Name", "Strategie", "Priorität", "Rang", "Score", "Status", "Einstufung", "Einstieg", "Daten", "News", "Bestand", "Bewertet am", "ISIN", "Sektor"]
NUMERIC_COLUMNS = (3, 4, 5)  # priority, rank, score sort by value
SPIN_MIN, SPIN_MAX = -(2**31), 2**31 - 1  # the range of a Qt number field (32-bit); not a business range: the schema has none
ALL = "Alle"
STRATEGY_TEXT = {"swing": "Swing", "long_term": "Long-Term", "tactical": "Taktisch"}
NO_STRATEGY = "Ohne Strategie"
MAX_NOTE_LINES = 6


@dataclass
class WatchRow:
    security_id: int
    symbol: str
    name: str
    held: bool
    annotated: bool
    priority: Optional[int]
    status: str  # filter keys: the presented texts
    strategy: str
    quality: str
    cells: list[str] = field(default_factory=list)
    sort_keys: list[Any] = field(default_factory=list)
    search: str = ""
    level: str = ""  # "", "warning" (needs a look) -- only a colour, never a decision
    strategy_value: Optional[str] = None  # the backend value (swing / long_term / ...), None = no active strategy; the table shows strategy_text of it
    sector_value: Optional[str] = None  # the effective sector group (sector map: the local entry overrides the public one), None = not assigned
    sector_source: str = "none"  # "public" (sector_map_v1.json), "local" (sector_map_local.json) or "none"
    sector_public: Optional[str] = None  # the public value, also when a local entry replaces it


@dataclass
class WatchlistVM:
    available: bool = False
    error: str = ""
    refreshed: str = DASH
    rows: list[WatchRow] = field(default_factory=list)
    summary: str = DASH
    notes: list[str] = field(default_factory=list)
    sector_groups: list[str] = field(default_factory=list)  # the existing sector groups a sector may be chosen from
    truncated: bool = False


@dataclass
class ActionMessage:
    level: str  # "info" | "warning" | "blocker"
    text: str
    done: bool  # the operation did what was asked (the list is refreshed)
    result: str = ""  # set where the dialog offers a follow-up: "SECURITY_NOT_FOUND" shows the button "Neues Wertpapier aufnehmen"
    tip: str = ""  # the original code and its explanation (tooltip only)


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return DASH if value in (None, "") else str(value)


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _day(value: Any) -> str:
    return DASH if not value else str(value)[:10]


def sector_text(value: Optional[str], source: str) -> str:
    """The sector cell: the group, marked "(lokal)" when it comes from the local map; "—" when not assigned."""
    if not value:
        return DASH
    return f"{value} (lokal)" if source == "local" else value


def strategy_text(value: Any) -> str:
    return NO_STRATEGY if not value else STRATEGY_TEXT.get(str(value), str(value).replace("_", " ").capitalize())


def _entry(row: dict) -> Presented:
    """The planner's entry status; for an entry without one the promotion verdict (both come from the engines, nothing is derived)."""
    status = row.get("entry_status") or row.get("promotion")
    return format_reason_code(status) if status else Presented(DASH)


def build_row(item: dict) -> WatchRow:
    sid = _int(item.get("security_id")) or 0
    symbol, name = _text(item.get("symbol")), _text(item.get("name"))
    priority, rank, score = _int(item.get("priority")), _int(item.get("rank")), _num(item.get("score"))
    status = format_reason_code(item.get("status"))
    quality = format_reason_code(item.get("quality")) if item.get("quality") else Presented(DASH)
    news = format_reason_code(item.get("news_status")) if item.get("news_status") else Presented(DASH)
    assessment = format_reason_code(item.get("assessment")) if item.get("assessment") else Presented(DASH)
    entry = _entry(item)
    strategy = strategy_text(item.get("strategy"))
    held = bool(item.get("held"))
    cells = [symbol, name, strategy, DASH if priority is None else str(priority), DASH if rank is None else str(rank), fmt_score(score), status, assessment, entry, quality, news,
             "Ja" if held else DASH, _day(item.get("as_of")), _text(item.get("isin"))]
    sort_keys: list[Any] = [c.lower() for c in cells]
    sort_keys[3], sort_keys[4], sort_keys[5] = priority, rank, score
    needs_a_look = item.get("news_status") in ("NEWS_ATTENTION", "NEWS_HIGH_ATTENTION")
    sector = item.get("sector") if isinstance(item.get("sector"), str) else None
    source = item.get("sector_source") if item.get("sector_source") in ("public", "local") and sector else "none"
    public = item.get("sector_public") if isinstance(item.get("sector_public"), str) else None
    cells.append(sector_text(sector, source))
    sort_keys.append(None if sector is None else sector.lower())
    return WatchRow(sid, symbol, name, held, bool(item.get("annotated")), priority, str(status), strategy, str(quality), cells, sort_keys,
                    " ".join(str(x) for x in (item.get("symbol"), item.get("name"), item.get("isin")) if x).lower(), "warning" if needs_a_look else "",
                    item.get("strategy") if isinstance(item.get("strategy"), str) else None, sector, source, public)


def _notes(payload: dict) -> list[str]:
    out = []
    for warning in _l(payload.get("warnings")):
        if isinstance(warning, dict) and warning.get("code"):
            shown = format_reason_code(warning["code"])
            out.append(f"{shown}")
    if payload.get("truncated"):
        out.append(f"Gekürzt: {_int(payload.get('returned')) or 0} von {_int(payload.get('matching')) or 0} Einträgen")
    return out[:MAX_NOTE_LINES]


def build_watchlist(envelope: Any, refreshed: Optional[datetime] = None) -> WatchlistVM:
    vm = WatchlistVM(refreshed=fmt_time(refreshed))
    env = _d(envelope)
    if not env:
        vm.error = "Keine Daten"
        return vm
    if env.get("ok") is False:
        vm.error = str(env.get("message") or env.get("error") or "get_watchlist meldet einen Fehler")
        return vm
    if not isinstance(env.get("rows"), list):
        vm.error = "Antwort ohne Watchlist-Zeilen"
        return vm
    vm.available = True
    vm.rows = [build_row(item) for item in env["rows"] if isinstance(item, dict)]
    vm.truncated = bool(env.get("truncated"))
    vm.sector_groups = [g for g in _l(env.get("sector_groups")) if isinstance(g, str)]
    counts = _d(env.get("counts"))
    held = _int(counts.get("held"))
    vm.summary = f"{len(vm.rows)} Einträge" + (f" · davon {held} im Bestand" if held else "")
    vm.notes = _notes(env)
    return vm


def filter_options(rows: list[WatchRow]) -> dict[str, list[str]]:
    """Distinct values of the filter combos (sorted, with 'Alle' first)."""
    return {
        "status": [ALL, *sorted({r.status for r in rows})],
        "strategy": [ALL, *sorted({r.strategy for r in rows})],
        "quality": [ALL, *sorted({r.quality for r in rows})],
    }


def apply_filter(rows: list[WatchRow], text: str = "", status: str = ALL, strategy: str = ALL, quality: str = ALL) -> list[WatchRow]:
    needle = (text or "").strip().lower()
    return [r for r in rows if (not needle or needle in r.search) and status in (ALL, r.status, "") and strategy in (ALL, r.strategy, "") and quality in (ALL, r.quality, "")]


def check_priority(value: Any) -> Optional[str]:
    """``None`` when ``value`` is a usable priority (a whole number in range, or empty for 'no priority'), else a short reason."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return "Die Priorität muss eine ganze Zahl sein."
    if not SPIN_MIN <= value <= SPIN_MAX:
        return "Die Zahl ist für die Eingabe zu groß."
    return None


def _who(data: dict) -> str:
    symbol, name = data.get("symbol"), data.get("name")
    return " – ".join(str(x) for x in (symbol, name) if x) or "Wertpapier"


def describe_action(res_ok: bool, data: Any, fallback_error: str = "") -> ActionMessage:
    """The German message of one action result.  ``res_ok`` is the technical outcome of the call (``McpResult.tool_ok``)."""
    d = _d(data)
    result = d.get("result")
    if not res_ok or not result:
        return ActionMessage("blocker", f"Aktion fehlgeschlagen: {d.get('message') or fallback_error or 'unbekannter Fehler'}", False)
    who = _who(d)
    if result == "ADDED":
        return ActionMessage("info", f"{who} wurde zur Watchlist hinzugefügt.", True)
    if result == "WOULD_ADD":
        isin = f" · ISIN {d['isin']}" if d.get("isin") else ""
        return ActionMessage("info", f"Gefunden: {who}{isin}", False)
    if result == "ALREADY_EXISTS":
        return ActionMessage("warning", f"{who} steht bereits auf der Watchlist – nichts geändert.", False)
    if result == "SECURITY_NOT_FOUND":
        if d.get("reason") == "SECURITY_INACTIVE":  # it exists: a second one must not be created
            return ActionMessage("blocker", "Das Wertpapier ist im Security Master deaktiviert.\nDie Aufnahme eines vollständig neuen Wertpapiers ist ein separater Workflow.", False)
        return ActionMessage("blocker", "Wertpapier ist noch nicht im Security Master.\nÜber „Neues Wertpapier aufnehmen“ kann es mit eigenen Angaben angelegt werden.", False, result)
    if result == "AMBIGUOUS_IDENTIFIER":
        names = "; ".join(_who(c) + (f" (ID {c['security_id']})" if isinstance(c, dict) and c.get("security_id") else "") for c in _l(d.get("candidates")) if isinstance(c, dict))
        return ActionMessage("warning", "Der Identifier ist mehrdeutig – bitte security_id oder ISIN verwenden." + (f"\nTreffer: {names}" if names else ""), False)
    if result == "INVALID_IDENTIFIER":
        return ActionMessage("warning", "Bitte einen gültigen Identifier angeben (Symbol, ISIN, WKN, security_id oder vollständiger Firmenname).", False)
    if result == "INVALID_PRIORITY":
        return ActionMessage("warning", str(d.get("message") or "Ungültige Priorität."), False)
    if result == "REMOVED":
        held = " Die Position bleibt unverändert." if d.get("still_held") else ""
        notes = " Einstiegsgrund, These oder Notizen des Eintrags wurden mit entfernt." if d.get("had_notes") else ""
        return ActionMessage("info", f"{who} wurde von der Watchlist entfernt.{held}{notes}", True)
    if result == "NOT_FOUND":
        return ActionMessage("warning", "Das Wertpapier steht nicht (mehr) auf der Watchlist.", True)
    if result == "PRIORITY_SET":
        changed = bool(d.get("changed"))
        value = d.get("priority")
        return ActionMessage("info", (f"Priorität gesetzt: {DASH if value is None else value}." if changed else "Die Priorität war bereits so gesetzt."), changed)
    return ActionMessage("blocker", f"Unerwartetes Ergebnis: {result}", False)
