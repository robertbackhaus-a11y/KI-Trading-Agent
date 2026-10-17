# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""ETF holdings for the effective single-name exposure: the official provider holdings of the held ETFs, read from one private file.  Reading only; this module never writes.

::

    etf_holdings_local.json   private (git-ignored, protected from the deploy, like sector_map_local.json and etf_lookthrough_local.json)

Per ETF ISIN: provider, as-of date, source (the provider file), ``kind`` and the holdings exactly as the provider publishes them (name, ticker, ISIN if the provider
has one, weight in %, asset class, and -- as additional information only -- sector, location and exchange).  Plus ``aliases``: explicit, reviewed links between a
provider listing (ticker + exchange) and a security of the portfolio for the cases no identifier can express (an ADR and the ordinary share of the same issuer), and
``issuer_groups``: reviewed lists of ISINs that are share classes of one issuer (Alphabet A and C), which count as one company.

Rules (nothing is guessed):

* ``kind`` ``physical``: the weights are the shares of the fund's net assets as the provider publishes them (equity, cash, futures, FX; they add up to about 100 %,
  the rest is reported as "other", never spread over the companies);
* ``kind`` ``synthetic_index``: the ETF holds a swap and a collateral basket; the economic exposure is the *index* the swap pays, so the file holds the provider's
  index composition (never the collateral basket).  Its weights are used **as published, not normalised**: Invesco publishes the S&P 500 composition with a resolution of
  0.1 % from 0.1 % upward and 0.01 % below (a floor of 0.01 %), and because the many small constituents have a strongly skewed weight distribution, rounding them up
  makes the list add up to more than 100 % (102.43 % on 28 Aug 2026: 503 rows, 503 distinct ISINs, no duplicate, no cash or other component; compared with the
  iShares Core S&P 500 holdings of 12 Oct 2026 the excess sits in the small names -- about +3.8 points for the 304 weights below 0.1 % and +2.6 points for the 180
  weights around 0.1 % -- while the large weights are rounded without a bias).  Scaling every weight by 100 / total would therefore push about 2.4 % relative of error
  into the large names, which are the ones that matter for concentration; the published weights are kept instead and the excess is shown as ``rounding drift``
  (a negative block that makes the sums reconcile);
* a file entry that is not plausible (no date, no source, negative equity weight, weights far from 100 %) is ignored and reported as ``ALLOCATION_INVALID``;
* an entry older than ``STALE_AFTER_DAYS`` is still used but flagged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

import runtime_paths

FILE_NAME = "etf_holdings_local.json"
DEFAULT_UNIVERSE = Path(__file__).resolve().parent / "universe"
PHYSICAL, SYNTHETIC_INDEX = "physical", "synthetic_index"
MIN_TOTAL, MAX_TOTAL = 97.0, 103.0  # plausible total of the published weights (providers round; derivatives and FX carry small or negative weights)
STALE_AFTER_DAYS = 120
EQUITY = "Equity"

APPLIED, STALE, NO_DATA, INVALID, NO_ISIN = "APPLIED", "STALE", "NO_DATA", "ALLOCATION_INVALID", "NO_ISIN"


class HoldingsError(ValueError):
    """A holdings entry is unreadable or implausible."""


@dataclass(frozen=True)
class Holding:
    name: str
    ticker: str
    isin: str
    weight: float  # % as published
    asset_class: str
    sector: str = ""
    location: str = ""
    exchange: str = ""

    @property
    def is_equity(self) -> bool:
        return self.asset_class == EQUITY


@dataclass(frozen=True)
class EtfHoldings:
    isin: str
    name: str
    provider: str
    as_of: str
    source: str
    kind: str
    holdings: tuple[Holding, ...]
    note: str = ""

    @property
    def total_weight(self) -> float:
        return sum(h.weight for h in self.holdings)


@dataclass(frozen=True)
class Alias:
    ticker: str
    exchange: str  # normalised (see ``norm``)
    isin: str  # the ISIN of the portfolio security the listing belongs to
    reason: str


@dataclass(frozen=True)
class Layers:
    etfs: Mapping[str, EtfHoldings]
    invalid: Mapping[str, str]
    aliases: tuple[Alias, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    issuer_groups: Mapping[str, str] = field(default_factory=dict)  # ISIN -> the name of the issuer whose share class it is


def norm(text: Any) -> str:
    """Lower-case letters and digits only: ``Euronext Amsterdam`` == ``EURONEXT_AMSTERDAM`` (an exact comparison after this, never a similarity)."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _holding(raw: Any) -> Holding:
    if not isinstance(raw, dict):
        raise HoldingsError("a holding is not an object")
    weight = raw.get("weight")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight != weight:
        raise HoldingsError(f"weight of {raw.get('name')!r} is not a number")
    asset = str(raw.get("asset_class") or EQUITY)
    if asset == EQUITY and weight < 0:
        raise HoldingsError(f"negative equity weight of {raw.get('name')!r}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise HoldingsError("a holding without a name")
    return Holding(name, str(raw.get("ticker") or "").strip(), str(raw.get("isin") or "").strip().upper(), float(weight), asset, str(raw.get("sector") or ""),
                   str(raw.get("location") or ""), str(raw.get("exchange") or ""))


def _etf(isin: str, entry: Any) -> EtfHoldings:
    if not isinstance(entry, dict):
        raise HoldingsError("entry is not an object")
    try:
        date.fromisoformat(str(entry.get("as_of")))
    except ValueError as exc:
        raise HoldingsError("as_of is not a date") from exc
    source = entry.get("source")
    if not isinstance(source, str) or not source.strip():
        raise HoldingsError("no source")
    kind = entry.get("kind")
    if kind not in (PHYSICAL, SYNTHETIC_INDEX):
        raise HoldingsError("kind must be physical or synthetic_index")
    rows = entry.get("holdings")
    if not isinstance(rows, list) or not rows:
        raise HoldingsError("no holdings")
    holdings = tuple(_holding(r) for r in rows)
    result = EtfHoldings(isin, str(entry.get("name") or ""), str(entry.get("provider") or ""), str(entry["as_of"]), source, kind, holdings, str(entry.get("note") or ""))
    if not MIN_TOTAL <= result.total_weight <= MAX_TOTAL and not (kind == SYNTHETIC_INDEX and 90.0 <= result.total_weight <= 110.0):
        raise HoldingsError(f"weights add up to {result.total_weight:.2f} %, not about 100 %")
    if not any(h.is_equity for h in holdings):
        raise HoldingsError("no equity holdings")
    return result


def read_layers(universe_dir: Optional[Path] = None) -> Layers:
    """The holdings file; a missing file means no holdings (every ETF stays one unresolved position), an unreadable one raises :class:`HoldingsError`."""
    path = runtime_paths.private_file(Path(universe_dir) if universe_dir is not None else DEFAULT_UNIVERSE, FILE_NAME)
    if not path.is_file():
        return Layers({}, {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldingsError(f"{FILE_NAME}: {exc}") from exc
    if not isinstance(payload, dict):
        raise HoldingsError(f"{FILE_NAME}: not an object")
    etfs: dict[str, EtfHoldings] = {}
    invalid: dict[str, str] = {}
    for isin, entry in (payload.get("etfs") or {}).items():
        try:
            etfs[str(isin).upper()] = _etf(str(isin).upper(), entry)
        except HoldingsError as exc:
            invalid[str(isin).upper()] = str(exc)
    aliases: list[Alias] = []
    warnings: list[str] = []
    for raw in payload.get("aliases") or []:
        if isinstance(raw, dict) and raw.get("ticker") and raw.get("exchange") and raw.get("isin") and raw.get("reason"):
            aliases.append(Alias(str(raw["ticker"]).strip().upper(), norm(raw["exchange"]), str(raw["isin"]).strip().upper(), str(raw["reason"])))
        else:
            warnings.append("ALIAS_INVALID: an alias needs ticker, exchange, isin and a reason; it was ignored")
    groups: dict[str, str] = {}
    for raw in payload.get("issuer_groups") or []:
        if isinstance(raw, dict) and raw.get("name") and isinstance(raw.get("isins"), list) and len(raw["isins"]) >= 2 and raw.get("reason"):
            for isin in raw["isins"]:
                groups[str(isin).strip().upper()] = str(raw["name"])
        else:
            warnings.append("ISSUER_GROUP_INVALID: a group needs a name, at least two ISINs and a reason; it was ignored")
    return Layers(etfs, invalid, tuple(aliases), tuple(warnings), groups)


def status_of(layers: Layers, isin: Optional[str], evaluation: str) -> tuple[str, Optional[EtfHoldings], str]:
    """``(status, holdings, detail)`` of one ETF: ``APPLIED``, ``STALE`` (used, but old), ``NO_DATA``, ``ALLOCATION_INVALID`` or ``NO_ISIN``."""
    if not isin:
        return NO_ISIN, None, "the security has no ISIN to match holdings"
    key = isin.strip().upper()
    if key in layers.invalid:
        return INVALID, None, layers.invalid[key]
    holdings = layers.etfs.get(key)
    if holdings is None:
        return NO_DATA, None, "no holdings for this ISIN"
    try:
        age = (date.fromisoformat(evaluation[:10]) - date.fromisoformat(holdings.as_of)).days
    except ValueError:
        age = 0
    return (STALE if age > STALE_AFTER_DAYS else APPLIED), holdings, f"as of {holdings.as_of} ({age} days old)"
