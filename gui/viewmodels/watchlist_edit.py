# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""View model of the inline editing on the Watchlist page: the strategy options, which rows may be edited, the pending (unsaved) changes and the messages.

Pure Python (no Qt, no database).  Three fields are editable: the strategy, the priority and the sector (each is written by its own narrow MCP action,
called by the app).  A change stays local ("pending") until the user presses "Änderung übernehmen"; nothing is written by selecting a value.
The backend decides: the GUI only hides the strategy editor for rows it already knows are blocked (held, or a value it does not offer) and shows the
backend's answer for the rest.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .reason_codes import format_reason_code
from .watchlist import ActionMessage, WatchRow, sector_text, strategy_text

# the strategies that exist and may be chosen: (shown text, backend value); None = no strategy
STRATEGY_OPTIONS: list[tuple[str, Optional[str]]] = [("Keine", None), ("Swing", "swing"), ("Long-Term", "long_term")]
EDITABLE_VALUES = {None, "swing", "long_term"}
STRATEGY_SUCCESS = {"STRATEGY_SET", "STRATEGY_REMOVED", "UNCHANGED"}
HELD_HINT = "Bei einer gehaltenen Position kann die Strategie hier nicht geändert werden."
OTHER_HINT = "Dieser Strategiewert wird hier nicht bearbeitet."
PENDING_TEXT = "Geändert"
PENDING_TIP = "Noch nicht gespeichert: „Änderung übernehmen“ speichert, „Änderung verwerfen“ macht die Änderung rückgängig."

MESSAGE_TEXT = {
    "STRATEGY_SET": "Strategie gespeichert.",
    "STRATEGY_REMOVED": "Strategie entfernt.",
    "UNCHANGED": "Die Strategie war bereits so gesetzt.",
    "BLOCKED_HELD_POSITION": "Strategie kann bei einer gehaltenen Position nicht geändert werden.",
    "BLOCKED_ACTIVE_CAMPAIGN": "Strategie kann bei einer aktiven Campaign nicht geändert werden.",
    "NOT_FOUND": "Das Wertpapier wurde nicht gefunden.",
    "INVALID_IDENTIFIER": "Das Wertpapier konnte nicht eindeutig bestimmt werden.",
    "INVALID_STRATEGY": "Ungültige Strategie.",
    "ASSIGNMENT_HISTORY_UNCLEAR": "Die Strategie-Historie dieses Wertpapiers ist nicht eindeutig; es wurde nichts geändert.",
}


def strategy_editable(row: WatchRow) -> tuple[bool, str]:
    """``(editable, hint)``: the strategy cell of a held row, or one holding a value that is not offered, has no editor."""
    if row.held:
        return False, HELD_HINT
    if row.strategy_value not in EDITABLE_VALUES:
        return False, OTHER_HINT
    return True, ""


def option_index(value: Optional[str]) -> int:
    return next((i for i, (_, v) in enumerate(STRATEGY_OPTIONS) if v == value), 0)


def prune(pending: Mapping[int, Mapping[str, Any]], rows: Mapping[int, WatchRow]) -> dict[int, dict[str, Any]]:
    """The pending changes that still differ from the backend (a row that vanished, or a value that now equals the pending one, drops out)."""
    out: dict[int, dict[str, Any]] = {}
    for sid, edits in pending.items():
        row = rows.get(sid)
        if row is None:
            continue
        kept = {}
        if "strategy" in edits and edits["strategy"] != row.strategy_value and strategy_editable(row)[0]:
            kept["strategy"] = edits["strategy"]
        if "priority" in edits and edits["priority"] != row.priority:
            kept["priority"] = edits["priority"]
        if "sector" in edits and edits["sector"] != row.sector_value:
            kept["sector"] = edits["sector"]
        if kept:
            out[sid] = kept
    return out


def display(row: WatchRow, edits: Optional[Mapping[str, Any]]) -> tuple[str, str]:
    """The strategy and the priority text of a row including its pending change."""
    edits = edits or {}
    strategy = strategy_text(edits["strategy"]) if "strategy" in edits else row.cells[2]
    priority = ("—" if edits["priority"] is None else str(edits["priority"])) if "priority" in edits else row.cells[3]
    return strategy, priority


def describe_strategy(res_ok: bool, data: Any, fallback_error: str = "") -> ActionMessage:
    """The German message of one ``set_security_strategy`` answer.  ``res_ok`` is the technical outcome of the call (``McpResult.tool_ok``)."""
    d = data if isinstance(data, dict) else {}
    result = str(d.get("result") or "")
    if not res_ok or not result:
        return ActionMessage("blocker", f"Die Strategie wurde nicht gespeichert: {d.get('message') or fallback_error or 'unbekannter Fehler'}", False, result, format_reason_code(d.get("error")).tip if d.get("error") else "")
    tip = format_reason_code(result).tip
    text = MESSAGE_TEXT.get(result)
    if result in STRATEGY_SUCCESS:
        return ActionMessage("info", text or "", result != "UNCHANGED", result, tip)
    if result == "ERROR":
        return ActionMessage("blocker", f"Die Strategie wurde nicht gespeichert: {d.get('message') or 'unbekannter Fehler'}", False, result, tip)
    level = "warning" if result in ("BLOCKED_HELD_POSITION", "BLOCKED_ACTIVE_CAMPAIGN", "ASSIGNMENT_HISTORY_UNCLEAR", "INVALID_STRATEGY", "NOT_FOUND", "INVALID_IDENTIFIER") else "blocker"
    return ActionMessage(level, text or f"Die Strategie wurde nicht gespeichert ({format_reason_code(result)}).", False, result, tip)


def priority_saved(res_ok: bool, data: Any) -> bool:
    """The priority write succeeded (also when the value was already set)."""
    return bool(res_ok and isinstance(data, dict) and data.get("result") == "PRIORITY_SET")


def strategy_saved(res_ok: bool, data: Any) -> bool:
    return bool(res_ok and isinstance(data, dict) and data.get("result") in STRATEGY_SUCCESS)


# -- sector ---------------------------------------------------------------------------------------------------------------------
NO_SECTOR_LABEL = "Nicht zugeordnet"
REMOVE_OVERRIDE_LABEL = "Lokale Zuordnung entfernen"
SECTOR_SUCCESS = {"SECTOR_SET", "SECTOR_OVERRIDE_REMOVED", "UNCHANGED"}
SECTOR_MESSAGE_TEXT = {
    "SECTOR_SET": "Sektor gespeichert.",
    "SECTOR_OVERRIDE_REMOVED": "Lokale Sektor-Zuordnung entfernt.",
    "UNCHANGED": "Die Sektor-Zuordnung war bereits so gesetzt.",
    "INVALID_SECTOR": "Unbekannter Sektor: erlaubt sind nur vorhandene Sektor-Gruppen.",
    "NOT_FOUND": "Das Wertpapier wurde nicht gefunden.",
    "INVALID_IDENTIFIER": "Das Wertpapier konnte nicht eindeutig bestimmt werden.",
    "NO_MAP_KEY": "Das Wertpapier hat weder ein Yahoo-Symbol noch ein Symbol und kann nicht in der Sector-Map geführt werden.",
    "SECTOR_MAP_UNAVAILABLE": "Die öffentliche Sector-Map ist nicht lesbar; es wurde nichts geändert.",
    "LOCAL_MAP_INVALID": "Die lokale Sector-Map ist ungültig; es wurde nichts geändert.",
}


def sector_options(row: WatchRow, groups: list[str]) -> list[tuple[str, Optional[str]]]:
    """``(shown text, group or None)``.  ``None`` means "no local entry": for a row with a local entry it removes it (the public value applies again), for an
    unassigned row it is the current state, and for a row that has only a public value it is not offered (there is nothing local to remove)."""
    options: list[tuple[str, Optional[str]]] = []
    if row.sector_source == "local":
        options.append((REMOVE_OVERRIDE_LABEL, None))
    elif row.sector_source == "none":
        options.append((NO_SECTOR_LABEL, None))
    options.extend((group, group) for group in groups)
    return options


def sector_editable(groups: list[str]) -> tuple[bool, str]:
    """A sector can be chosen when the sector map has groups; a held position or an open campaign does not matter (a classification, not a lifecycle state)."""
    if not groups:
        return False, "Die Sector-Map ist nicht verfügbar; der Sektor kann hier nicht gewählt werden."
    return True, ""


def sector_tooltip(row: WatchRow, editable_hint: str = "") -> str:
    if row.sector_source == "local":
        replaced = f" Sie ersetzt den öffentlichen Wert „{row.sector_public}“ der sector_map_v1.json." if row.sector_public and row.sector_public != row.sector_value else " Sie entspricht dem öffentlichen Wert."
        text = f"Lokale Zuordnung aus sector_map_local.json.{replaced}"
    elif row.sector_source == "public":
        text = "Zuordnung aus der öffentlichen sector_map_v1.json."
    else:
        text = "Nicht zugeordnet: Der Sector-Guard bewertet dieses Wertpapier nicht (er blockiert nie wegen eines fehlenden Sektors)."
    return text + (" " + editable_hint if editable_hint else "")


def display_sector(row: WatchRow, edits: Optional[Mapping[str, Any]]) -> str:
    """The sector cell including a pending change (a pending local choice is shown as local, ``None`` as "Nicht zugeordnet" or the public value)."""
    if not edits or "sector" not in edits:
        return row.cells[-1]
    value = edits["sector"]
    if value is None:
        return sector_text(row.sector_public, "public") if row.sector_source == "local" else sector_text(None, "none")
    return sector_text(value, "local" if value != row.sector_public else "public")


def describe_sector(res_ok: bool, data: Any, fallback_error: str = "") -> ActionMessage:
    """The German message of one ``set_security_sector`` answer.  ``res_ok`` is the technical outcome of the call (``McpResult.tool_ok``)."""
    d = data if isinstance(data, dict) else {}
    result = str(d.get("result") or "")
    if not res_ok or not result:
        return ActionMessage("blocker", f"Der Sektor wurde nicht gespeichert: {d.get('message') or fallback_error or 'unbekannter Fehler'}", False, result, format_reason_code(d.get("error")).tip if d.get("error") else "")
    tip = format_reason_code(result).tip
    if result in SECTOR_SUCCESS:
        return ActionMessage("info", SECTOR_MESSAGE_TEXT[result], result != "UNCHANGED", result, tip)
    if result == "ERROR":
        return ActionMessage("blocker", f"Der Sektor wurde nicht gespeichert: {d.get('message') or 'unbekannter Fehler'}", False, result, tip)
    level = "warning" if result in ("INVALID_SECTOR", "NOT_FOUND", "INVALID_IDENTIFIER", "NO_MAP_KEY") else "blocker"
    return ActionMessage(level, SECTOR_MESSAGE_TEXT.get(result) or f"Der Sektor wurde nicht gespeichert ({format_reason_code(result)}).", False, result, tip)


def sector_saved(res_ok: bool, data: Any) -> bool:
    return bool(res_ok and isinstance(data, dict) and data.get("result") in SECTOR_SUCCESS)
