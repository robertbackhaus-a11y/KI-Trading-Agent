# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""ETF sector look-through data for the sector overview: official provider sector allocations of the held ETFs and their mapping to the sector groups.

Two small files next to ``sector_map_v1.json`` (``app/universe`` in production); nothing is stored in the database and ``sector_map_local.json`` is not touched::

    etf_sector_mapping_v1.json   versioned   provider sector name (GICS, as the providers publish it) -> sector group, or null with a reason
    etf_lookthrough_local.json   private     (git-ignored, like sector_map_local.json) per ETF ISIN: provider, as-of date, source, sector weights in %

Rules (nothing is guessed):

* an allocation is only used for the ISIN of the security it was published for; it needs an as-of date, a source and weights that add up to about 100 %
  (``MIN_TOTAL``..``MAX_TOTAL``), otherwise it is ignored and the ETF stays one unresolved position (``ALLOCATION_INVALID``);
* a provider category is mapped to a sector group only where the mapping file says so; a category that fits more than one group (for example the GICS sector
  "Information Technology", which spans semiconductors, software and hardware), fits none, or is not a sector (cash, derivatives, "other") stays its own
  *unmapped* category -- never put into a group by name or by size;
* an allocation older than ``STALE_AFTER_DAYS`` is still used but flagged;
* the contribution of an ETF to a category is ``market value x weight / total weight`` (the total is normalised to 100 % so a rounded 100.1 % cannot create value).

Reading only; this module never writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

import runtime_paths

MAPPING_NAME = "etf_sector_mapping_v1.json"
ALLOCATION_NAME = "etf_lookthrough_local.json"
DEFAULT_UNIVERSE = Path(__file__).resolve().parent / "universe"
MIN_TOTAL, MAX_TOTAL = 98.0, 102.0  # an allocation outside this range is not plausible (providers round, and some hold a little cash)
STALE_AFTER_DAYS = 120

APPLIED = "APPLIED"
STALE = "STALE"
NO_DATA = "NO_DATA"
INVALID = "ALLOCATION_INVALID"
NO_ISIN = "NO_ISIN"


class LookthroughError(ValueError):
    """A look-through file is unreadable or inconsistent."""


@dataclass(frozen=True)
class Allocation:
    isin: str
    name: str
    provider: str
    as_of: str
    source: str
    weights: Mapping[str, float]  # provider category -> % of the fund

    @property
    def total(self) -> float:
        return sum(self.weights.values())


@dataclass(frozen=True)
class Mapping_:
    groups: Mapping[str, Optional[str]]  # lower-case provider category -> group (None = not mappable)
    reasons: Mapping[str, str]  # lower-case provider category -> why it is not mapped

    def group_of(self, category: str) -> tuple[Optional[str], str]:
        key = category.strip().lower()
        if key in self.groups:
            group = self.groups[key]
            return group, ("" if group else self.reasons.get(key, "NOT_MAPPABLE"))
        return None, "UNKNOWN_CATEGORY"


@dataclass(frozen=True)
class Layers:
    allocations: Mapping[str, Allocation]
    invalid: Mapping[str, str]  # ISIN -> why the allocation is not usable
    mapping: Mapping_
    warnings: tuple[str, ...] = ()


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LookthroughError(f"{path.name}: {exc}") from exc
    except ValueError as exc:
        raise LookthroughError(f"{path.name}: invalid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise LookthroughError(f"{path.name}: not an object")
    return payload


def read_mapping(path: Path, valid_groups: Optional[set[str]] = None) -> tuple[Mapping_, list[str]]:
    return read_mapping_from(_read(path), valid_groups)


def read_mapping_from(payload: Mapping[str, Any], valid_groups: Optional[set[str]] = None) -> tuple[Mapping_, list[str]]:
    groups: dict[str, Optional[str]] = {}
    reasons: dict[str, str] = {}
    warnings: list[str] = []
    for name, group in (payload.get("mapping") or {}).items():
        if isinstance(group, str) and group:
            if valid_groups is not None and group not in valid_groups:
                warnings.append(f"MAPPING_GROUP_UNKNOWN: {name} -> {group} is not a sector group; treated as not mapped")
                groups[str(name).strip().lower()] = None
                reasons[str(name).strip().lower()] = "GROUP_UNKNOWN"
            else:
                groups[str(name).strip().lower()] = group
    for name, reason in (payload.get("not_mapped") or {}).items():
        groups.setdefault(str(name).strip().lower(), None)
        reasons[str(name).strip().lower()] = str(reason)
    return Mapping_(groups, reasons), warnings


def _allocation(isin: str, entry: Any) -> Allocation:
    if not isinstance(entry, dict):
        raise LookthroughError("entry is not an object")
    weights = entry.get("sectors")
    if not isinstance(weights, dict) or not weights:
        raise LookthroughError("no sector weights")
    clean: dict[str, float] = {}
    for name, value in weights.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value != value:
            raise LookthroughError(f"weight of {name!r} is not a non-negative number")
        clean[str(name)] = float(value)
    as_of, source = entry.get("as_of"), entry.get("source")
    try:
        date.fromisoformat(str(as_of))
    except ValueError as exc:
        raise LookthroughError("as_of is not a date") from exc
    if not isinstance(source, str) or not source.strip():
        raise LookthroughError("no source")
    allocation = Allocation(isin, str(entry.get("name") or ""), str(entry.get("provider") or ""), str(as_of), source, clean)
    if not MIN_TOTAL <= allocation.total <= MAX_TOTAL:
        raise LookthroughError(f"weights add up to {allocation.total:.2f} %, not about 100 %")
    return allocation


def read_layers(universe_dir: Optional[Path] = None, valid_groups: Optional[set[str]] = None) -> Layers:
    """Both files.  The mapping is required; the allocation file is optional (without it every ETF stays one unresolved position)."""
    directory = Path(universe_dir) if universe_dir is not None else DEFAULT_UNIVERSE
    mapping, warnings = read_mapping(directory / MAPPING_NAME, valid_groups)
    allocations: dict[str, Allocation] = {}
    invalid: dict[str, str] = {}
    path = runtime_paths.private_file(directory, ALLOCATION_NAME)
    if path.is_file():
        for isin, entry in (_read(path).get("etfs") or {}).items():
            try:
                allocations[str(isin).upper()] = _allocation(str(isin).upper(), entry)
            except LookthroughError as exc:
                invalid[str(isin).upper()] = str(exc)
    return Layers(allocations, invalid, mapping, tuple(warnings))


def status_of(layers: Layers, isin: Optional[str], evaluation: str) -> tuple[str, Optional[Allocation], str]:
    """``(status, allocation, detail)`` for one ETF: ``APPLIED``, ``STALE`` (used, but old), ``NO_DATA``, ``ALLOCATION_INVALID`` or ``NO_ISIN``."""
    if not isin:
        return NO_ISIN, None, "the security has no ISIN to match an allocation"
    key = isin.strip().upper()
    if key in layers.invalid:
        return INVALID, None, layers.invalid[key]
    allocation = layers.allocations.get(key)
    if allocation is None:
        return NO_DATA, None, "no allocation for this ISIN"
    try:
        age = (date.fromisoformat(evaluation[:10]) - date.fromisoformat(allocation.as_of)).days
    except ValueError:
        age = 0
    return (STALE if age > STALE_AFTER_DAYS else APPLIED), allocation, f"as of {allocation.as_of} ({age} days old)"


def contributions(allocation: Allocation, value_eur: float, mapping: Mapping_) -> list[dict[str, Any]]:
    """The categories of one ETF with their EUR value (``value x weight / total``), largest first; ``group`` is None for an unmapped category."""
    total = allocation.total
    out = []
    for category, weight in allocation.weights.items():
        if weight <= 0:
            continue
        group, reason = mapping.group_of(category)
        out.append({"category": category, "group": group, "reason": reason, "provider_weight_pct": weight, "share": weight / total, "value_eur": value_eur * weight / total})
    out.sort(key=lambda c: (-c["value_eur"], c["category"]))
    return out
