# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The sector map in two layers, and the narrow write of the local layer (the ``set_security_sector`` MCP action).

The sector of a security is NOT stored in the database.  It comes from the sector map, keyed by the security's listing key (the Yahoo symbol of the
security, else its public symbol -- the same key the risk guards and the market-intelligence mapping use)::

    public  universe/sector_map_v1.json     versioned, never changed here
    local   universe/sector_map_local.json  private (git-ignored), the only file this module writes

Resolution (``resolve``): the local entry of a symbol wins over the public one; a symbol without an entry in either layer is not assigned (the sector
guard then reports it as "not assessable" and never blocks).  A symbol listed in two different sectors *inside the same file* is an error, as before; a
symbol that is in both files is simply overridden.

Writing (``set_security_sector``) touches nothing but ``sector_map_local.json``:

* only an EXISTING group may be chosen (a group of either file); no new group name is invented;
* a sector equal to the public one removes the local entry instead of duplicating it; ``None`` removes the local entry (a public sector then applies again);
* the file is read strictly first (invalid JSON or a duplicate inside it: nothing is written), changed in memory, written to a temporary file and replaced
  atomically, the previous content is kept as ``sector_map_local.json.bak``, and the result is read back and checked (otherwise the previous content is restored);
* no strategy, campaign, position, watchlist row or order is touched, and a held position or an open campaign does not block it: the sector is a
  classification, not a lifecycle state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import runtime_paths

OPERATION = "set_security_sector"
PUBLIC_NAME = "sector_map_v1.json"
LOCAL_NAME = "sector_map_local.json"
BACKUP_NAME = "sector_map_local.json.bak"
DEFAULT_UNIVERSE = Path(__file__).resolve().parent / "universe"
LOCAL_DESCRIPTION = "Private additions and overrides to sector_map_v1.json (own portfolio/watchlist symbols). Not versioned (git-ignored); merged by the risk guards and Collect-TradingMarketIntelligence.py if present."

# results
SECTOR_SET = "SECTOR_SET"
SECTOR_OVERRIDE_REMOVED = "SECTOR_OVERRIDE_REMOVED"
UNCHANGED = "UNCHANGED"
NOT_FOUND = "NOT_FOUND"
INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
INVALID_SECTOR = "INVALID_SECTOR"
NO_MAP_KEY = "NO_MAP_KEY"
SECTOR_MAP_UNAVAILABLE = "SECTOR_MAP_UNAVAILABLE"
LOCAL_MAP_INVALID = "LOCAL_MAP_INVALID"
ERROR = "ERROR"

SOURCE_PUBLIC, SOURCE_LOCAL, SOURCE_NONE = "public", "local", "none"

MESSAGES = {
    SECTOR_SET: "Sektor gespeichert.",
    SECTOR_OVERRIDE_REMOVED: "Lokale Zuordnung entfernt.",
    UNCHANGED: "Die Sektor-Zuordnung war bereits so gesetzt.",
    NOT_FOUND: "Wertpapier nicht gefunden.",
    INVALID_IDENTIFIER: "Ungültige security_id.",
    INVALID_SECTOR: "Unbekannter Sektor: erlaubt sind nur vorhandene Sektor-Gruppen.",
    NO_MAP_KEY: "Das Wertpapier hat weder ein Yahoo-Symbol noch ein Symbol; es kann nicht in der Sector-Map geführt werden.",
    SECTOR_MAP_UNAVAILABLE: "Die öffentliche Sector-Map ist nicht lesbar; es wurde nichts geändert.",
    LOCAL_MAP_INVALID: "Die lokale Sector-Map ist ungültig; es wurde nichts geändert.",
}


class SectorMapError(ValueError):
    """A sector map file is missing, unreadable or inconsistent."""


@dataclass(frozen=True)
class Layers:
    public: Mapping[str, str]
    local: Mapping[str, str]
    groups: tuple[str, ...]  # every existing group of either file, in a stable order
    local_exists: bool


# --------------------------------------------------------------------------- reading
def _payload(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SectorMapError(f"{path.name}: {exc}") from exc
    except ValueError as exc:
        raise SectorMapError(f"{path.name}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sectors"), dict):
        raise SectorMapError(f"{path.name}: no 'sectors' object")
    return data


def _layer(payload: Mapping[str, Any], name: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for sector, symbols in payload["sectors"].items():
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            raise SectorMapError(f"{name}: the group {sector} is not a list of symbols")
        for symbol in symbols:
            if symbol in mapping and mapping[symbol] != sector:
                raise SectorMapError(f"{name}: symbol {symbol} is in two sectors")
            mapping[symbol] = sector
    return mapping


def read_layers(public_path: Optional[Path] = None) -> Layers:
    """Both layers of the map.  The local file is optional; the public one is required."""
    public_path = Path(public_path) if public_path is not None else DEFAULT_UNIVERSE / PUBLIC_NAME
    public_payload = _payload(public_path)
    local_path = runtime_paths.private_file(public_path.parent, LOCAL_NAME)
    local_payload = _payload(local_path) if local_path.is_file() else {"sectors": {}}
    groups = tuple(dict.fromkeys([*public_payload["sectors"], *local_payload["sectors"]]))
    return Layers(_layer(public_payload, PUBLIC_NAME), _layer(local_payload, LOCAL_NAME), groups, local_path.is_file())


def effective(layers: Layers) -> dict[str, str]:
    """``{SYMBOL: SECTOR}``: the local layer overrides the public one."""
    return {**layers.public, **layers.local}


def merged_payload(public_payload: Mapping[str, Any], local_payload: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """The ``{"sectors": {SECTOR: [symbols]}}`` payload for ``market_intelligence.load_sector_map`` with the local overrides applied."""
    sectors = {sector: list(symbols) for sector, symbols in (public_payload.get("sectors") or {}).items()}
    local = (local_payload or {}).get("sectors") or {}
    overridden = {symbol for symbols in local.values() for symbol in symbols}
    for sector in sectors:
        sectors[sector] = [symbol for symbol in sectors[sector] if symbol not in overridden]
    for sector, symbols in local.items():
        sectors.setdefault(sector, []).extend(symbols)
    return {**public_payload, "sectors": sectors}


def resolve(layers: Layers, key: Optional[str]) -> dict[str, Any]:
    """The sector of one listing key and where it comes from (``public`` / ``local`` / ``none``); ``public`` is the public value even if overridden."""
    public, local = layers.public.get(key) if key else None, layers.local.get(key) if key else None
    sector = local or public
    return {"sector": sector, "source": SOURCE_LOCAL if local else SOURCE_PUBLIC if public else SOURCE_NONE, "public": public}


def listing_key(conn: sqlite3.Connection, security_id: int) -> Optional[str]:
    """The key the guards use: the Yahoo symbol of the security, else its public symbol."""
    import security_aliases as sa

    key = sa.AliasIndex.from_connection(conn).key_for(security_id)
    if key:
        return key
    row = conn.execute("SELECT symbol FROM security WHERE id = ?", (int(security_id),)).fetchone()
    return row[0] if row and row[0] else None


# --------------------------------------------------------------------------- writing
def _dump(payload: Mapping[str, Any]) -> str:
    """The format of the hand-maintained file: header keys one per line, one line per group."""
    lines = ["{"]
    items = [(k, v) for k, v in payload.items() if k != "sectors"]
    for key, value in items:
        lines.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},")
    lines.append('  "sectors": {')
    groups = list(payload["sectors"].items())
    for index, (sector, symbols) in enumerate(groups):
        comma = "," if index < len(groups) - 1 else ""
        lines.append(f"    {json.dumps(sector)}: {json.dumps(symbols, ensure_ascii=False)}{comma}")
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def _write_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, path)


def _result(result: str, *, changed: bool = False, ok: bool = True, **fields: Any) -> dict[str, Any]:
    out = {"ok": ok, "operation": OPERATION, "result": result, "changed": changed, **fields}
    out.setdefault("message", MESSAGES.get(result, ""))
    return out


def check_sector(sector: Any) -> Optional[str]:
    """The wanted group name, or ``None`` (= no local entry); raises ``ValueError`` for anything that is not text."""
    if sector is None:
        return None
    if isinstance(sector, str):
        text = sector.strip()
        return None if text.lower() in ("", "none", "null") else text
    raise ValueError("sector must be text or null")


def set_security_sector(conn: sqlite3.Connection, security_id: Any, sector: Any, *, universe_dir: Optional[Path] = None) -> dict[str, Any]:
    """Set (an existing group) or remove (``None``) the local sector entry of one security.  Never raises for bad input; writes only ``sector_map_local.json``."""
    try:
        if isinstance(security_id, bool) or not isinstance(security_id, (int, str)) or not str(security_id).strip().lstrip("-").isdigit():
            return _result(INVALID_IDENTIFIER)
        sid = int(str(security_id).strip())
        row = conn.execute("SELECT id, symbol, name FROM security WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return _result(NOT_FOUND, security_id=sid)
        info = {"security_id": int(row[0]), "symbol": row[1], "name": row[2]}
        folder = Path(universe_dir) if universe_dir is not None else DEFAULT_UNIVERSE
        public_path, local_path = folder / PUBLIC_NAME, runtime_paths.private_file(folder, LOCAL_NAME)
        try:
            wanted = check_sector(sector)
        except ValueError:
            return _result(INVALID_SECTOR, **info)
        try:
            layers = read_layers(public_path)
        except SectorMapError as exc:
            kind = LOCAL_MAP_INVALID if str(exc).startswith(LOCAL_NAME) else SECTOR_MAP_UNAVAILABLE
            return _result(kind, detail=str(exc)[:200], **info)
        if wanted is not None and wanted not in layers.groups:
            return _result(INVALID_SECTOR, valid=list(layers.groups), **info)
        key = listing_key(conn, sid)
        if not key:
            return _result(NO_MAP_KEY, **info)
        before = resolve(layers, key)
        desired_local = None if wanted is None or wanted == before["public"] else wanted  # equal to the public value: no redundant local entry
        if desired_local == layers.local.get(key):
            return _result(UNCHANGED, key=key, sector=before["sector"], source=before["source"], public_sector=before["public"], **info)
        previous_text = local_path.read_text(encoding="utf-8") if local_path.is_file() else None
        payload = _payload(local_path) if previous_text is not None else {"map_id": "sector_map_local", "version": 1, "description": LOCAL_DESCRIPTION, "sectors": {}}
        sectors = {name: [s for s in symbols if s != key] for name, symbols in payload["sectors"].items()}
        if desired_local is not None:
            sectors.setdefault(desired_local, []).append(key)
        payload = {**payload, "sectors": sectors}
        if previous_text is not None:
            _write_atomically(local_path.with_name(BACKUP_NAME), previous_text)
        _write_atomically(local_path, _dump(payload))
        try:
            after = resolve(read_layers(public_path), key)
            if after["sector"] != (desired_local or before["public"]):
                raise SectorMapError("the written map does not give the expected sector")
        except SectorMapError:
            if previous_text is None:
                local_path.unlink(missing_ok=True)
            else:
                _write_atomically(local_path, previous_text)
            raise
        return _result(SECTOR_SET if desired_local else SECTOR_OVERRIDE_REMOVED, changed=True, key=key, sector=after["sector"], source=after["source"], public_sector=after["public"],
                       previous_sector=before["sector"], previous_source=before["source"], **info)
    except Exception as exc:  # noqa: BLE001 - every failure is reported; the previous file was restored where it had been replaced
        return _result(ERROR, ok=False, error=type(exc).__name__, message=str(exc)[:200])
