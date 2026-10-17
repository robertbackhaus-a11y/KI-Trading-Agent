# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""View model of the "Import" page: form values -> ``check_transaction_import`` / ``import_transactions`` arguments, and the answer -> readable German tables and sentences.

Pure Python (no Qt, no database).  The page never imports anything itself: a check (dry run) shows the difference to the database and the reconciliation of the file against
the simulated end state; "Importieren" is only possible after a check that would change something, and with the token of exactly that check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .portfolio import DASH
from .reason_codes import format_reason_code

FORMATS = {"Parqet-Export": "parqet", "Kanonisches CSV": "canonical"}  # shown text -> value of the tool
STRATEGY_CHOICES = {"Offen": None, "Swing": "swing", "Long-Term": "long_term", "Keine": "none"}  # shown text -> value; "none" is a decision (stay without a strategy), not sent to the tool
ALIAS_CHOICES = {"Offen": None, "Alias übernehmen": "accept"}
ALIAS_STATUS = {"NEW": "wird übernommen", "EXISTS": "bereits gespeichert", "REFUSED": "abgelehnt"}
TYPE_TEXT = {"BUY": "Kauf", "SELL": "Verkauf", "DIVIDEND": "Dividende", "COST": "Kosten", "TRANSFERIN": "Einlieferung", "TRANSFEROUT": "Auslieferung"}
KIND_TEXT = {"new": "neu", "changed": "geändert", "closed": "geschlossen"}
FIELD_TEXT = {"isin": "ISIN", "wkn": "WKN"}
CLASS_TEXT = {"CONFLICT": "Konflikt", "UNKNOWN_SECURITY": "Wertpapier unbekannt", "INVALID": "ungültig"}

SECURITY_HEADERS = ["Name", "ISIN", "WKN", "Art", "Hinweis"]
MASTERDATA_HEADERS = ["Wertpapier", "Feld", "Datenbank", "Datei", "Bewertung"]
POSITION_HEADERS = ["Wertpapier", "Änderung", "Stück vorher", "Stück nachher"]
PROBLEM_HEADERS = ["Zeile", "Wertpapier", "Problem", "Grund"]
OPEN_HEADERS = ["Wertpapier", "Offener Punkt", "Detail"]
DIFFERENCE_HEADERS = ["Wertpapier", "Datei (Stück)", "Datenbank (Stück)", "Hinweis"]
DECISION_HEADERS = ["Art", "Wertpapier", "Angaben", "Entscheidung", "Stand"]


@dataclass
class ImportView:
    ok: bool
    level: str  # "info" | "warning" | "blocker"
    headline: str
    can_import: bool = False
    plan_token: Optional[str] = None
    written: bool = False
    lines: list[str] = field(default_factory=list)
    securities: list[list[str]] = field(default_factory=list)
    masterdata: list[list[str]] = field(default_factory=list)
    positions: list[list[str]] = field(default_factory=list)
    problems: list[list[str]] = field(default_factory=list)
    open_items: list[list[str]] = field(default_factory=list)
    differences: list[list[str]] = field(default_factory=list)
    alias_infos: dict[str, dict[str, Any]] = field(default_factory=dict)  # Parqet identifier -> what the user needs to decide on it (this check)
    strategy_infos: dict[str, dict[str, Any]] = field(default_factory=dict)  # strategy key -> the newly held position without a strategy (this check)
    refused: list[str] = field(default_factory=list)  # identifiers whose alias the server refused (the import is then not possible)
    closing: list[str] = field(default_factory=list)
    tooltip: str = ""


def request_arguments(path: str, format_text: str, create_securities: bool, include_historical: bool, strategies: Optional[dict[str, str]] = None,
                      aliases: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Exactly what the user chose; nothing is filled in.  ``strategies`` maps a strategy key to ``swing`` / ``long_term`` / ``none`` (``none`` is not sent: the position simply
    stays without a strategy), ``aliases`` maps a Parqet identifier to the security key.  No campaign is ever started by the page (``start_campaigns`` false)."""
    return {"file_path": path.strip(), "format": FORMATS.get(format_text, "parqet"), "create_securities": bool(create_securities), "include_historical": bool(include_historical),
            "strategies": [f"{key}={value}" for key, value in sorted((strategies or {}).items()) if value in ("swing", "long_term")],
            "accept_aliases": [f"{identifier}={security}" for identifier, security in sorted((aliases or {}).items())], "start_campaigns": False}


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return DASH


def _shares(value: Any) -> str:
    try:
        text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return DASH
    return (text or "0").replace(".", ",")


def _types(by_type: dict[str, int]) -> str:
    return ", ".join(f"{count} {TYPE_TEXT.get(kind, kind)}" for kind, count in sorted(by_type.items()))


def failure(message: str) -> ImportView:
    return ImportView(False, "blocker", f"Nicht möglich: {message or 'unbekannter Fehler'}")


def build(res_ok: bool, data: Any, message: str = "") -> ImportView:
    """The view of one answer of ``check_transaction_import`` or ``import_transactions``."""
    if not res_ok or not isinstance(data, dict) or data.get("ok") is False or not isinstance(data.get("diff"), dict):
        return failure(message or (data.get("message") if isinstance(data, dict) else "") or "ungültige Antwort")
    diff, closing = data["diff"], data.get("closing") or {}
    written, mode = bool(data.get("written")), data.get("mode")
    new_tx = diff["new_transactions"]
    counts = diff.get("classification_counts") or {}
    created, updates, conflicts = diff["new_securities"], diff["masterdata_updates"], diff["masterdata_conflicts"]
    changes = diff["position_changes"]
    would_write = bool(data.get("would_write"))
    results = {a["identifier"]: a for a in diff.get("identifier_aliases", [])}
    refused = sorted(i for i, a in results.items() if a["status"] == "REFUSED")
    view = ImportView(True, "info", "", can_import=(mode == "check" and would_write and bool(data.get("plan_token")) and not refused), plan_token=data.get("plan_token"), written=written)
    view.refused = refused
    for candidate in diff.get("alias_candidates", []):
        view.alias_infos[candidate["identifier"]] = {"security": candidate["security"], "name": candidate.get("name"), "wkn": candidate.get("wkn"),
                                                       "database_isin": candidate.get("database_isin"), "same_wkn": bool(candidate.get("same_wkn")), "status": "OFFEN", "reason": ""}
    for identifier, alias in results.items():  # an alias already chosen: the result of the server (accepted, already stored or refused with the reason)
        info = view.alias_infos.setdefault(identifier, {"security": alias["security"], "name": alias.get("symbol"), "wkn": None, "database_isin": None, "same_wkn": None})
        info.update(status=alias["status"], reason=alias.get("reason") or "")
    for needed in diff.get("strategy_needed", []):
        view.strategy_infos[needed["key"]] = {"name": needed.get("name") or needed.get("symbol"), "symbol": needed.get("symbol"), "shares": needed.get("shares")}

    nothing = not (new_tx["count"] or created or updates)
    prefix = "Import abgeschlossen" if written else "Prüfung"
    if written:
        view.headline = f"Import abgeschlossen: {new_tx['count']} neue Transaktionen, {len(created)} neue Wertpapiere, {len(changes)} geänderte Positionen."
    elif nothing and not would_write:
        view.headline = "Keine Änderungen: Alles aus der Datei ist bereits in der Datenbank."
    else:
        view.headline = f"{prefix}: {new_tx['count']} neue Transaktionen, {len(created)} neue Wertpapiere, {len(updates)} ergänzte Stammdaten, {len(changes)} geänderte Positionen. Es wurde nichts geschrieben."
    problems = diff["problem_row_count"]
    view.lines = [
        f"Datei: {data.get('source_row_count', DASH)} Zeilen, {counts.get('duplicate', diff['existing_transactions'])} davon bereits in der Datenbank",
        f"Neue Transaktionen: {new_tx['count']}" + (f" ({_types(new_tx['by_type'])})" if new_tx["by_type"] else ""),
        f"Neue Wertpapiere: {len(created)}; ergänzte Stammdaten: {len(updates)}; Stammdaten-Konflikte: {len(conflicts)}",
        f"Historische Zeilen übersprungen: {diff['historical_skipped']}; Zeilen mit Problem: {problems}",
    ]
    if data.get("backup_path"):
        view.lines.append("Sicherung der Datenbank: " + str(data["backup_path"]))

    view.securities = [[c.get("name") or DASH, c.get("isin") or DASH, c.get("wkn") or DASH, "Aktie (Standard)" if c.get("asset_type_defaulted") else str(c.get("asset_type") or DASH),
                        "Parqet nennt keine Art; angelegt als Aktie" if c.get("asset_type_defaulted") else ""] for c in created]
    view.masterdata = [[u.get("symbol") or DASH, FIELD_TEXT.get(u["field"], u["field"]), DASH, str(u["to"]), "wird ergänzt"] for u in updates]
    view.masterdata += [[c.get("symbol") or DASH, FIELD_TEXT.get(c["field"], c["field"]), str(c.get("database") or DASH), str(c.get("file") or DASH), "Konflikt – nicht überschrieben"]
                        for c in conflicts]
    view.positions = [[c["symbol"], KIND_TEXT.get(c["kind"], c["kind"]), _shares(c["shares_before"]), _shares(c["shares_after"])] for c in changes]
    view.problems = [[str(p["row"]), str(p.get("holding") or p.get("identifier") or DASH), CLASS_TEXT.get(p["classification"], p["classification"]), str(p["reason"])] for p in diff["problem_rows"]]
    view.open_items = [[str(i.get("symbol") or DASH), str(format_reason_code(i["code"])), str(i.get("detail") or "")] for i in diff.get("open_items", [])]

    if closing:
        counts_c, value, fvd = closing["counts"], closing.get("portfolio_value") or {}, closing["file_vs_database"]
        where = "nach dem Import" if written else "für den simulierten Stand nach dem Import"
        view.closing = [
            f"Abgleich Datei ↔ Datenbank ({where}): " + ("Übereinstimmung" if closing["status"] == "MATCH" else "Abweichungen"),
            f"Wertpapiere {counts_c['securities']}, Transaktionen {counts_c['transactions']}, gehaltene Positionen {counts_c['positions_held']}, Zeilen der Datei {counts_c['file_rows']}",
            "Depotwert: " + (f"{_num(value['total_market_value_eur'])} EUR ({value.get('as_of') or DASH})" if value.get("total_market_value_eur") is not None else "nicht verfügbar"),
            f"Stückzahl-Unterschiede {fvd['share_difference_count']}, Dateizeilen nicht in der Datenbank {fvd['file_rows_not_in_database_count']}, "
            f"Datenbankzeilen nicht in der Datei {fvd['database_rows_not_in_file']}, nicht zugeordnet {closing['unresolved_count']}, "
            f"Positionen ungleich ihren Transaktionen {closing['positions_vs_transactions']['drift_count']}",
            "Datenbankprüfung: integrity_check " + ("ok" if closing["integrity_check"] == ["ok"] else "FEHLER") + ", foreign_key_check " + ("ok" if not closing["foreign_key_check"] else "FEHLER"),
        ]
        view.differences = [[d["security"], _shares(d["file_net_shares"]), _shares(d["database_shares"]),
                             "Die Datei verkauft mehr Stücke, als sie kauft; die Position wird nie negativ." if d.get("note") else ""] for d in fvd["share_differences"]]
        if closing["integrity_check"] != ["ok"] or closing["foreign_key_check"]:
            view.level = "blocker"
        elif closing["status"] != "MATCH" or conflicts or problems:
            view.level = "warning"
    elif conflicts or problems:
        view.level = "warning"
    if refused:
        view.level = "blocker" if view.level != "blocker" else view.level
        view.headline = "Zuordnung abgelehnt – bitte die Auswahl ändern. " + view.headline
    if view.open_items and view.level == "info":
        view.level = "warning"
    view.tooltip = "Prüfen schreibt nichts: Der Import wird auf einer Kopie im Speicher simuliert."
    return view
