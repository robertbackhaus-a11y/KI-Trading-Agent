# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""View model of the "Neues Wertpapier aufnehmen" dialog: form values -> ``onboard_security`` arguments, and the result -> readable German messages.

Pure Python (no Qt, no database).  Nothing is looked up or completed here: the arguments contain exactly what the user typed (empty fields are left
out, never filled).  Technical codes (``ISIN_CONFLICT``, ``BAD_ISIN`` ...) go into the tooltip only; the main message is a sentence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .portfolio import DASH
from .reason_codes import format_reason_code

ASSET_TYPES = {"Aktie": "stock", "ETF": "etf"}  # shown text -> value of the Security Master
ASSET_TEXT = {value: text for text, value in ASSET_TYPES.items()}
PROVIDER_FIELDS = {"yahoo": "Yahoo Finance", "fmp": "FMP"}  # form key -> data source name

FIELD_TEXT = {
    "symbol": "Symbol", "name": "Name", "asset_type": "Art", "isin": "ISIN", "wkn": "WKN", "exchange": "Börse", "currency": "Währung", "country": "Land",
    "provider_symbols": "Provider-Symbol", "provider_symbols.Yahoo Finance": "Yahoo-Symbol", "provider_symbols.FMP": "FMP-Symbol",
    "add_to_watchlist": "Watchlist", "watchlist_priority": "Watchlist-Priorität", "dry_run": "Prüfmodus",
}
REASON_TEXT = {
    "REQUIRED": "fehlt.", "TEXT_EXPECTED": "muss ein Text sein.", "CONTROL_CHARACTERS": "enthält Steuerzeichen.", "TOO_LONG": "ist zu lang.",
    "BAD_SYMBOL": "darf nur Buchstaben, Ziffern, Punkt und Bindestrich enthalten (höchstens 20 Zeichen).",
    "BAD_ISIN": "ist keine gültige ISIN (zwei Buchstaben, neun Zeichen, eine Prüfziffer).", "BAD_ISIN_CHECKSUM": "hat eine falsche Prüfziffer.",
    "BAD_WKN": "muss aus sechs Buchstaben oder Ziffern bestehen.", "BAD_CURRENCY": "muss ein dreistelliger Währungscode sein (zum Beispiel EUR).",
    "BAD_COUNTRY": "muss ein zweistelliger Ländercode sein (zum Beispiel DE).", "UNKNOWN_ASSET_TYPE": "ist unbekannt.",
    "UNKNOWN_PROVIDER": "ist keine unterstützte Quelle.", "DUPLICATE_PROVIDER": "ist doppelt angegeben.", "MAPPING_EXPECTED": "hat ein ungültiges Format.",
    "BAD_PROVIDER_SYMBOL": "enthält unzulässige Zeichen.", "BOOLEAN_EXPECTED": "hat einen ungültigen Wert.", "BAD_PRIORITY": "muss eine ganze Zahl sein.",
    "PRIORITY_WITHOUT_WATCHLIST": "ist nur zusammen mit „Nach Anlage zur Watchlist hinzufügen“ möglich.",
}
CONFLICT_FIELD_TEXT = {"isin": "Die ISIN", "symbol": "Das Symbol", "wkn": "Die WKN"}


@dataclass
class Outcome:
    result: str  # the code of the answer ("" when the call itself failed)
    level: str  # "info" | "warning" | "blocker"
    text: str  # the main message
    lines: list[str] = field(default_factory=list)  # the check summary, or one line per conflict / error
    tooltip: str = ""  # the technical codes (diagnosis only)
    can_create: bool = False  # the check found nothing in the way: "Anlegen" may be enabled
    created: bool = False
    existing: Optional[dict[str, Any]] = None  # an already existing security: security_id, symbol, name, active, on_watchlist


def form_arguments(
    symbol: str, name: str, asset_text: str, isin: str = "", wkn: str = "", exchange: str = "", currency: str = "", country: str = "", yahoo: str = "", fmp: str = "",
    add_to_watchlist: bool = False, priority: Optional[int] = None,
) -> dict[str, Any]:
    """Exactly what the user typed (trimmed); an empty optional field is left out and never filled in."""
    arguments: dict[str, Any] = {"symbol": symbol.strip(), "name": name.strip(), "asset_type": ASSET_TYPES.get(asset_text, "")}
    for key, value in (("isin", isin), ("wkn", wkn), ("exchange", exchange), ("currency", currency), ("country", country)):
        if value.strip():
            arguments[key] = value.strip()
    providers = {PROVIDER_FIELDS[key]: value.strip() for key, value in (("yahoo", yahoo), ("fmp", fmp)) if value.strip()}
    if providers:
        arguments["provider_symbols"] = providers
    if add_to_watchlist:
        arguments["add_to_watchlist"] = True
        if priority is not None:
            arguments["watchlist_priority"] = int(priority)
    return arguments


def missing_fields(arguments: Mapping[str, Any]) -> list[str]:
    """The required fields that are still empty (the dialog says so before it asks the server)."""
    return [FIELD_TEXT[key] for key in ("symbol", "name") if not arguments.get(key)] + ([FIELD_TEXT["asset_type"]] if not arguments.get("asset_type") else [])


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _who(item: Mapping[str, Any]) -> str:
    symbol, name = item.get("symbol"), item.get("name")
    text = " – ".join(str(x) for x in (symbol, name) if x) or "einem anderen Wertpapier"
    return f"{text} (ID {item['security_id']})" if item.get("security_id") is not None else text


def _summary(planned: Mapping[str, Any]) -> list[str]:
    security, watch = _d(planned.get("security")), planned.get("watchlist")
    providers = ", ".join(f"{_d(p).get('symbol')} ({FIELD_TEXT.get('provider_symbols.' + str(_d(p).get('source')), str(_d(p).get('source')))})" for p in _l(planned.get("provider_symbols")))
    lines = [
        f"Symbol: {security.get('symbol') or DASH}", f"Name: {security.get('name') or DASH}", f"Art: {ASSET_TEXT.get(security.get('asset_type'), DASH)}",
        f"ISIN: {security.get('isin') or DASH}", f"WKN: {security.get('wkn') or DASH}", f"Börse: {security.get('exchange') or DASH}", f"Währung: {security.get('currency') or DASH}",
        f"Land: {security.get('country') or DASH}", f"Provider-Symbole: {providers or DASH}", "Sector: nicht zugeordnet",
    ]
    if isinstance(watch, dict):
        priority = watch.get("priority")
        lines.append("Watchlist: ja" + ("" if priority is None else f" (Priorität {priority})"))
    else:
        lines.append("Watchlist: nein")
    return lines


def _conflict_line(conflict: Mapping[str, Any]) -> str:
    code, value, who = conflict.get("code"), conflict.get("value"), _who(conflict)
    if code == "ALREADY_EXISTS":
        return f"Bereits vorhanden: {who}."
    if code == "ALIAS_CONFLICT":
        return f"Das Provider-Symbol {value} gehört bereits zu {who}."
    stem = " (gleicher Symbolstamm mit anderer Börsenendung)" if conflict.get("match") == "STEM" else ""
    return f"{CONFLICT_FIELD_TEXT.get(str(conflict.get('field')), 'Der Wert')} {value} gehört bereits zu {who}{stem}."


def _warning_line(warning: Mapping[str, Any]) -> str:
    if warning.get("code") == "NAME_MATCH":
        return f"Hinweis: Ein Wertpapier mit gleichem Namen existiert bereits: {_who(warning)}. Bitte prüfen, ob es dasselbe ist."
    if warning.get("code") == "EXISTING_SECURITY_INACTIVE":
        return "Das vorhandene Wertpapier ist deaktiviert. Es wird hier nicht verändert."
    return ""


def _tooltip(codes: list[str]) -> str:
    seen = list(dict.fromkeys(c for c in codes if c))
    return "".join(format_reason_code(c).tip for c in seen)


def describe_onboarding(res_ok: bool, data: Any, fallback_error: str = "") -> Outcome:
    """The German message of one ``onboard_security`` answer.  ``res_ok`` is the technical outcome of the call (``McpResult.tool_ok``)."""
    d = _d(data)
    result = str(d.get("result") or "")
    if not res_ok or not result:
        return Outcome("", "blocker", f"Die Aktion ist fehlgeschlagen: {d.get('message') or fallback_error or 'unbekannter Fehler'} Es wurde nichts angelegt.", tooltip=_tooltip([str(d.get("error") or "")]))
    warnings = [line for line in (_warning_line(_d(w)) for w in _l(d.get("warnings"))) if line]
    if result == "READY":
        return Outcome(result, "info", "Keine Konflikte gefunden.", [*_summary(_d(d.get("planned"))), *warnings], _tooltip([result]), can_create=True)
    if result == "CREATED":
        who = _who({"symbol": d.get("symbol"), "name": d.get("name"), "security_id": d.get("security_id")})
        watch = " und zur Watchlist hinzugefügt" if isinstance(d.get("watchlist"), dict) else ""
        return Outcome(result, "info", f"{who} wurde im Security Master angelegt{watch}. Marktdaten werden beim nächsten regulären Lauf ergänzt.", warnings, _tooltip([result]), created=True)
    if result == "INVALID_INPUT":
        lines = [f"{FIELD_TEXT.get(str(e.get('field')), str(e.get('field')))} {REASON_TEXT.get(str(e.get('reason')), 'ist ungültig.')}" for e in _l(d.get("errors")) if isinstance(e, dict)]
        return Outcome(result, "warning", "Bitte die Eingaben korrigieren.", lines, _tooltip([result, *[str(_d(e).get("reason")) for e in _l(d.get("errors"))]]))
    if result == "ERROR":
        return Outcome(result, "blocker", f"Das Wertpapier konnte nicht angelegt werden: {d.get('message') or fallback_error or 'unbekannter Fehler'} Es wurde nichts angelegt.", tooltip=_tooltip([str(d.get("error") or "")]))
    conflicts = [_d(c) for c in _l(d.get("conflicts"))]
    codes = [result, *[str(c.get("code")) for c in conflicts]]
    if result == "ALREADY_EXISTS":
        existing = {**_d(d.get("existing")), "on_watchlist": bool(d.get("on_watchlist"))}
        lines = [f"Symbol: {existing.get('symbol') or DASH}", f"Name: {existing.get('name') or DASH}", f"security_id: {existing.get('security_id', DASH)}"]
        lines.append("Auf der Watchlist: " + ("ja" if existing["on_watchlist"] else "nein"))
        lines += [line for line in (_conflict_line(c) for c in conflicts if c.get("code") != "ALREADY_EXISTS") if line] + warnings
        return Outcome(result, "warning", "Wertpapier ist bereits im Security Master.", lines, _tooltip(codes), existing=existing)
    if result in ("ISIN_CONFLICT", "SYMBOL_CONFLICT", "WKN_CONFLICT", "ALIAS_CONFLICT"):
        return Outcome(result, "blocker", "Das Wertpapier kann so nicht angelegt werden:", [*[_conflict_line(c) for c in conflicts], *warnings], _tooltip(codes))
    return Outcome(result, "blocker", f"Unerwartetes Ergebnis: {format_reason_code(result)}", tooltip=_tooltip([result]))
