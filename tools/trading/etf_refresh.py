# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Refresh of the private ETF files (holdings for the effective exposure, sector allocations for the look-through) from the providers' own published data.

Files (``universe`` next to ``sector_map_v1.json``; git-ignored, protected from the deploy)::

    etf_holdings_local.json      per ETF ISIN: the provider's holdings exactly as published (read by ``effective_exposure``)
    etf_lookthrough_local.json   per ETF ISIN: the provider's sector allocation exactly as published (read by ``sector_overview``)

Sources, one strict parser per provider format (no generic heuristic; a changed format is an error, never a guess):

* iShares (XUSE, IS3N): ``<product page>/latest-holdings.csv`` -- the published CSV with the 12 columns ``Ticker, Name, Sector, Asset Class, Market Value, Weight (%),
  Notional Value, Shares, Price, Location, Exchange, Market Currency``; the sector allocation is the ``exposureBreakdowns.sector`` block of the product page (its JSON).
* Invesco (SPXS): ``dng-api.invesco.com .../holdings/index?idType=isin`` -- the benchmark composition of the swap (S&P 500), NOT ``holdings/fund`` (the collateral basket).
  Its weights add up to more than 100 % (coarse rounding); they are stored as published, never normalised (the excess is the rounding drift of ``effective_exposure``: a
  reconciliation item, never a company).  Invesco publishes no structured sector allocation (only the factsheet PDF), so the look-through entry of SPXS is not refreshed here.

Rows that cannot carry exposure are not stored: a NON-equity row (FX forward, cash, future, money market ...) whose published weight is exactly 0 contributes nothing to the
effective exposure, to the "other" part of the fund or to the sum of the published weights, so dropping it changes no figure (the provider's raw row count, the stored count and the
omitted rows per asset class are reported and kept in the entry as ``provider_rows`` / ``omitted_zero_weight_rows``).  Everything else is stored exactly as published: every
equity row (also one whose rounded weight is 0.00 -- it still names a company and its identifiers), every row with a weight other than 0 (also a tiny one, also a negative cash line).

Rules:

* everything is validated BEFORE anything is written: the ETF is the expected one, data is not empty, the date is present and not older than the stored one, the loaders' own
  checks (``etf_holdings._etf`` / ``etf_lookthrough._allocation``) pass, no duplicate rows, the identifiers the matching needs are still there, the number of holdings did not
  collapse and the new list overlaps the stored one;
* an ETF that fails is skipped and reported, the stored entry stays; the holdings file and the look-through file are independent (one succeeding never touches the other);
* only the ``etfs`` entries of refreshed ETFs are replaced; ``aliases``, ``issuer_groups``, ``description`` and every other ETF are kept as they are;
* the file is replaced atomically after the candidate was written next to it and read back through the real loaders; the previous file is kept as ``.bak-refresh-<time>``;
  any error leaves the old file untouched.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import etf_holdings as eh
import etf_lookthrough as lt
import runtime_paths

HOLDINGS_FILE = eh.FILE_NAME
LOOKTHROUGH_FILE = lt.ALLOCATION_NAME
USER_AGENT = "Mozilla/5.0 (compatible; TradingAgent-ETF-Refresh/1.0)"
MAX_BYTES = 40 * 1024 * 1024
TIMEOUT_S = 90
MIN_COUNT_RATIO = 0.80  # fewer holdings than this share of the stored list is a broken answer, not a rebalancing
MIN_OVERLAP = 0.70  # share of the stored holdings that must still be in the new list (same fund)
MAX_UNLISTED_RATIO = 0.02  # equity rows without ticker / exchange: at most this much more than before
FUTURE_TOLERANCE_DAYS = 2

ISHARES_HEADER = ["Ticker", "Name", "Sector", "Asset Class", "Market Value", "Weight (%)", "Notional Value", "Shares", "Price", "Location", "Exchange", "Market Currency"]
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
HOLDINGS, LOOKTHROUGH = "holdings", "lookthrough"


class RefreshError(ValueError):
    """A provider answer is unusable (the code names why: ``FORMAT_CHANGED``, ``EMPTY``, ``NOT_THE_FUND`` ...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class EtfSpec:
    isin: str
    symbol: str
    name: str
    provider: str  # as stored in the files
    kind: str
    format: str  # "ishares" | "invesco"
    holdings_url: str
    holdings_source: str  # the text stored as ``source`` (the file the data came from)
    page_url: Optional[str] = None  # iShares: the product page (sector allocation)
    page_source: Optional[str] = None
    page_tickers: tuple[str, ...] = ()  # the tickers the product page names in its title (the UK page of IS3N says EIMI, the London listing)

    @property
    def sector_automated(self) -> bool:
        """Only iShares publishes the sector allocation in a structured form; Invesco's exists as a factsheet PDF only (maintained by hand)."""
        return self.format == "ishares"


SPECS: tuple[EtfSpec, ...] = (
    EtfSpec("IE000R4ZNTN3", "XUSE", "iShares MSCI World ex-USA UCITS ETF USD Acc", "iShares (BlackRock)", eh.PHYSICAL, "ishares",
            "https://www.ishares.com/uk/individual/en/products/340748/ishares-msci-world-ex-usa-ucits-etf/latest-holdings.csv",
            "https://www.ishares.com/uk/individual/en/products/340748/ishares-msci-world-ex-usa-ucits-etf/latest-holdings.csv (full holdings file)",
            "https://www.ishares.com/uk/individual/en/products/340748/ishares-msci-world-ex-usa-ucits-etf", "https://www.ishares.com/uk/individual/en/products/340748/ishares-msci-world-ex-usa-ucits-etf", ("XUSE",)),
    EtfSpec("IE00BKM4GZ66", "IS3N", "iShares Core MSCI EM IMI UCITS ETF USD Acc", "iShares (BlackRock)", eh.PHYSICAL, "ishares",
            "https://www.ishares.com/uk/individual/en/products/264659/ishares-msci-emerging-markets-imi-ucits-etf/latest-holdings.csv",
            "https://www.ishares.com/uk/individual/en/products/264659/ishares-msci-emerging-markets-imi-ucits-etf/latest-holdings.csv (full holdings file)",
            "https://www.ishares.com/uk/individual/en/products/264659/ishares-msci-emerging-markets-imi-ucits-etf", "https://www.ishares.com/uk/individual/en/products/264659/ishares-msci-emerging-markets-imi-ucits-etf", ("EIMI", "IS3N")),
    EtfSpec("IE00B3YCGJ38", "SPXS", "Invesco S&P 500 UCITS ETF Acc", "Invesco", eh.SYNTHETIC_INDEX, "invesco",
            "https://dng-api.invesco.com/cache/v1/accounts/en_IE/shareclasses/IE00B3YCGJ38/holdings/index?idType=isin",
            "https://dng-api.invesco.com/cache/v1/accounts/en_IE/shareclasses/IE00B3YCGJ38/holdings/index?idType=isin (benchmark composition of the swap, S&P 500; NOT the collateral basket holdings/fund)"),
)
INVESCO_FUND_URL = "https://dng-api.invesco.com/cache/v1/accounts/en_IE/shareclasses/{isin}?idType=isin"
INVESCO_NAME_PART = "S&P 500"

Fetcher = Callable[[str], bytes]


def is_zero_noise(holding: dict[str, Any]) -> bool:
    """A row that can never carry exposure: not an equity and a published weight of exactly 0."""
    return holding.get("asset_class", eh.EQUITY) != eh.EQUITY and holding["weight"] == 0


def split_zero_noise(holdings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``(rows to store, {asset class: number of omitted rows})``.  The order of the stored rows is the provider's."""
    kept = [h for h in holdings if not is_zero_noise(h)]
    omitted: dict[str, int] = {}
    for h in holdings:
        if is_zero_noise(h):
            omitted[h.get("asset_class", "?")] = omitted.get(h.get("asset_class", "?"), 0) + 1
    return kept, dict(sorted(omitted.items()))


# ------------------------------------------------------------------------------------------------------------------------ download
def fetch(url: str, timeout: float = TIMEOUT_S) -> bytes:
    """One HTTPS GET (follows redirects).  Raises :class:`RefreshError` with a code for every failure; nothing is retried."""
    if not url.startswith("https://"):
        raise RefreshError("BAD_URL", "only https sources are used")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv,application/json,text/html;q=0.8,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https provider URLs
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RefreshError("HTTP_ERROR", f"{exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RefreshError("DOWNLOAD_FAILED", f"{type(exc).__name__} for {url}") from exc
    if len(data) > MAX_BYTES:
        raise RefreshError("TOO_LARGE", f"answer larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not data.strip():
        raise RefreshError("EMPTY", "empty answer")
    return data


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RefreshError("FORMAT_CHANGED", "the answer is not UTF-8 text") from exc


# ------------------------------------------------------------------------------------------------------------------------ parsers
def _ishares_date(text: str) -> str:
    match = re.fullmatch(r"\s*(\d{1,2})/([A-Za-z]{3})/(\d{4})\s*", text or "")
    if not match or match.group(2).lower() not in _MONTHS:
        raise RefreshError("FORMAT_CHANGED", f"unreadable date {text!r}")
    try:
        return date(int(match.group(3)), _MONTHS[match.group(2).lower()], int(match.group(1))).isoformat()
    except ValueError as exc:
        raise RefreshError("FORMAT_CHANGED", f"impossible date {text!r}") from exc


def _number(text: str, what: str) -> float:
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError as exc:
        raise RefreshError("FORMAT_CHANGED", f"{what} is not a number: {text!r}") from exc


def parse_ishares_holdings(raw: bytes) -> tuple[str, list[dict[str, Any]]]:
    """``(as_of, holdings)`` of an iShares ``latest-holdings.csv``.  The header must be exactly the known one; HTML (a page instead of the file) is a format error."""
    text = _text(raw)
    if text.lstrip()[:1] == "<":
        raise RefreshError("FORMAT_CHANGED", "the answer is a web page, not the holdings CSV")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows[0]) != 2 or rows[0][0].strip() != "Fund Holdings as of":
        raise RefreshError("FORMAT_CHANGED", "the first line is not 'Fund Holdings as of,<date>'")
    as_of = _ishares_date(rows[0][1])
    start = next((i for i, r in enumerate(rows) if r and r[0] == "Ticker"), None)
    if start is None:
        raise RefreshError("FORMAT_CHANGED", "no header line starting with 'Ticker'")
    if [c.strip() for c in rows[start]] != ISHARES_HEADER:
        raise RefreshError("FORMAT_CHANGED", f"the columns changed: {[c.strip() for c in rows[start]]}")
    holdings: list[dict[str, Any]] = []
    for row in rows[start + 1:]:
        if not row or not any(c.strip() for c in row):
            break  # the table ends at the first blank line; what follows is the provider's footer
        if len(row) != len(ISHARES_HEADER):
            raise RefreshError("FORMAT_CHANGED", f"a row has {len(row)} columns instead of {len(ISHARES_HEADER)}")
        ticker, name, sector, asset, _mv, weight, _nv, _shares, _price, location, exchange, _cur = [c.strip() for c in row]
        holdings.append({"name": name, "ticker": ticker, "weight": _number(weight, f"weight of {name!r}"), "asset_class": asset, "sector": sector, "location": location, "exchange": exchange})
    if not holdings:
        raise RefreshError("EMPTY", "the table has a header but no rows")
    return as_of, holdings


def parse_invesco_index(raw: bytes) -> tuple[str, list[dict[str, Any]]]:
    """``(as_of, holdings)`` of the Invesco ``holdings/index`` answer: ``{"effectiveDate", "holdings": [{name, isin, cusip, weight}]}``."""
    try:
        payload = json.loads(_text(raw))
    except ValueError as exc:
        raise RefreshError("FORMAT_CHANGED", "the answer is not JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("holdings"), list) or not isinstance(payload.get("effectiveDate"), str):
        raise RefreshError("FORMAT_CHANGED", "expected an object with 'effectiveDate' and 'holdings'")
    try:
        as_of = date.fromisoformat(payload["effectiveDate"][:10]).isoformat()
    except ValueError as exc:
        raise RefreshError("FORMAT_CHANGED", f"unreadable effectiveDate {payload['effectiveDate']!r}") from exc
    holdings: list[dict[str, Any]] = []
    for item in payload["holdings"]:
        if not isinstance(item, dict) or not {"name", "isin", "weight"} <= set(item):
            raise RefreshError("FORMAT_CHANGED", "a holding lacks name, isin or weight")
        weight = item["weight"]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise RefreshError("FORMAT_CHANGED", f"weight of {item.get('name')!r} is not a number")
        holdings.append({"name": str(item["name"]).strip(), "ticker": "", "isin": str(item["isin"]).strip().upper(), "weight": float(weight), "asset_class": eh.EQUITY})
    if not holdings:
        raise RefreshError("EMPTY", "the index list is empty")
    return as_of, holdings


def parse_invesco_identity(raw: bytes, isin: str) -> str:
    """The full name of the share class from the Invesco share class answer (identity check of the fund behind the ISIN)."""
    try:
        payload = json.loads(_text(raw))
        name = html.unescape(str(payload["shareClassFullName"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise RefreshError("FORMAT_CHANGED", "the share class answer has no shareClassFullName") from exc
    return name


def parse_ishares_sectors(raw: bytes, tickers: tuple[str, ...]) -> tuple[str, dict[str, float]]:
    """``(as_of, {sector: %})`` from the ``exposureBreakdowns.sector`` block of an iShares product page; the page title must name one of ``tickers``."""
    text = html.unescape(_text(raw))
    title = re.search(r"<title>([^<]*)</title>", text)
    if not title or not any(re.search(rf"\|\s*{re.escape(t)}\s*\|", title.group(1)) for t in tickers):
        raise RefreshError("NOT_THE_FUND", f"the page title {title.group(1) if title else None!r} does not name {' / '.join(tickers)}")
    key = '"sector":{"name":"sector","fullName":"exposureBreakdowns.sector"'
    if text.count(key) != 1:
        raise RefreshError("FORMAT_CHANGED", f"the sector block occurs {text.count(key)} times, expected once")
    start = text.index("{", text.index(key) + len('"sector":'))
    depth = 0
    for end in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[end], 0)
        if depth == 0:
            break
    else:
        raise RefreshError("FORMAT_CHANGED", "the sector block is not closed")
    try:
        points = json.loads(text[start:end + 1])["dataPointsByNameMap"]
        as_of_number, names, values = points["asOf"]["value"], points["type"]["value"], points["fund"]["value"]
        as_of = datetime.strptime(str(as_of_number), "%Y%m%d").date().isoformat()
    except (ValueError, KeyError, TypeError) as exc:
        raise RefreshError("FORMAT_CHANGED", "the sector block lacks asOf, type or fund") from exc
    if not isinstance(names, list) or not isinstance(values, list) or len(names) != len(values) or not names:
        raise RefreshError("FORMAT_CHANGED", "sector names and weights do not pair up")
    sectors: dict[str, float] = {}
    for name, value in zip(names, values):
        if not isinstance(name, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RefreshError("FORMAT_CHANGED", "a sector row is not (name, number)")
        if name in sectors:
            raise RefreshError("DUPLICATE", f"sector {name!r} occurs twice")
        sectors[name] = float(value)
    return as_of, sectors


# ------------------------------------------------------------------------------------------------------------------------ validation
def _key(h: dict[str, Any]) -> tuple[str, ...]:
    return ("isin", h["isin"]) if h.get("isin") else ("listing", str(h.get("ticker", "")).upper(), eh.norm(h.get("exchange")), eh.norm(h.get("name")))


def _unlisted(holdings: Iterable[dict[str, Any]]) -> float:
    equities = [h for h in holdings if h.get("asset_class") == eh.EQUITY]
    if not equities:
        return 0.0
    return sum(1 for h in equities if not h.get("isin") and (not h.get("ticker") or h.get("ticker") == "-" or not h.get("exchange") or h.get("exchange") == "-")) / len(equities)


def validate_holdings(spec: EtfSpec, as_of: str, holdings: list[dict[str, Any]], old: Optional[dict[str, Any]], today: date) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` of a new holdings entry.  No error means the entry may be written."""
    errors: list[str] = []
    warnings: list[str] = []
    if not holdings:
        return ["EMPTY: no holdings"], warnings
    try:
        when = date.fromisoformat(as_of)
    except ValueError:
        return ["FORMAT_CHANGED: no valid date"], warnings
    if (when - today).days > FUTURE_TOLERANCE_DAYS:
        errors.append(f"DATE_IN_FUTURE: {as_of}")
    if old is not None and as_of < str(old.get("as_of")):
        errors.append(f"OLDER_THAN_STORED: {as_of} is older than the stored {old.get('as_of')}")
    age = (today - when).days
    if age > eh.STALE_AFTER_DAYS:
        warnings.append(f"STALE_SOURCE: the provider's data is {age} days old")
    entry = {"as_of": as_of, "source": spec.holdings_source, "kind": spec.kind, "holdings": holdings}
    try:
        eh._etf(spec.isin, entry)  # the loader's own validation: names, numbers, no negative equity, the total of the weights, equity present
    except eh.HoldingsError as exc:
        errors.append(f"LOADER_REJECTS: {exc}")
    keys = [_key(h) for h in holdings if h.get("asset_class") == eh.EQUITY]
    duplicates = len(keys) - len(set(keys))
    if duplicates:
        errors.append(f"DUPLICATE_ROWS: {duplicates} equity row(s) occur more than once")
    if spec.kind == eh.SYNTHETIC_INDEX:
        if any(not _ISIN.match(h.get("isin", "")) for h in holdings):
            errors.append("IDENTIFIER_MISSING: every index constituent needs a valid ISIN")
        if any(h.get("asset_class") != eh.EQUITY for h in holdings):
            errors.append("NOT_THE_INDEX: the index list contains non-equity rows")
        if len(holdings) < 450:
            errors.append(f"TOO_FEW_HOLDINGS: {len(holdings)} index constituents")
        total = sum(h["weight"] for h in holdings)
        if total > 100.0:
            warnings.append(f"NOT_NORMALISED: the published weights add up to {total:.2f} % and are stored as published (the excess is the rounding drift, never a company)")
    else:
        if sum(1 for h in holdings if h.get("asset_class") == eh.EQUITY) < 50:
            errors.append("TOO_FEW_HOLDINGS: fewer than 50 equity rows")
    unlisted = _unlisted(holdings)
    old_unlisted = _unlisted(old["holdings"]) if old else 0.0
    if unlisted > max(old_unlisted, 0.0) + MAX_UNLISTED_RATIO and spec.kind == eh.PHYSICAL:
        errors.append(f"IDENTIFIER_LOST: {unlisted:.1%} of the equity rows have no ticker / exchange (before: {old_unlisted:.1%})")
    if old and old.get("holdings"):
        old_rows = split_zero_noise(old["holdings"])[0]  # an older file may still hold the zero rows; they do not count for the comparison
        if len(holdings) < MIN_COUNT_RATIO * len(old_rows):
            errors.append(f"HOLDINGS_COUNT_DROPPED: {len(holdings)} rows instead of {len(old_rows)}")
        old_keys = {_key(h) for h in old_rows if h.get("asset_class", eh.EQUITY) == eh.EQUITY}
        if old_keys and len(old_keys & set(keys)) < MIN_OVERLAP * len(old_keys):
            errors.append(f"NOT_THE_FUND: only {len(old_keys & set(keys))} of {len(old_keys)} stored equity rows are in the new list")
    return errors, warnings


def validate_sectors(spec: EtfSpec, as_of: str, sectors: dict[str, float], old: Optional[dict[str, Any]], today: date) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not sectors:
        return ["EMPTY: no sector weights"], warnings
    try:
        when = date.fromisoformat(as_of)
    except ValueError:
        return ["FORMAT_CHANGED: no valid date"], warnings
    if (when - today).days > FUTURE_TOLERANCE_DAYS:
        errors.append(f"DATE_IN_FUTURE: {as_of}")
    if old is not None and as_of < str(old.get("as_of")):
        errors.append(f"OLDER_THAN_STORED: {as_of} is older than the stored {old.get('as_of')}")
    if (today - when).days > lt.STALE_AFTER_DAYS:
        warnings.append(f"STALE_SOURCE: the provider's data is {(today - when).days} days old")
    try:
        lt._allocation(spec.isin, {"as_of": as_of, "source": spec.page_source or "x", "sectors": sectors})
    except lt.LookthroughError as exc:
        errors.append(f"LOADER_REJECTS: {exc}")
    if old and old.get("sectors"):
        if not set(sectors) & set(old["sectors"]):
            errors.append("NOT_THE_FUND: no sector name of the stored allocation is in the new one")
        for name in sorted(set(sectors) - set(old["sectors"])):
            warnings.append(f"NEW_SECTOR: {name!r} was not in the stored allocation (it stays its own unmapped category unless the mapping file knows it)")
    return errors, warnings


# ------------------------------------------------------------------------------------------------------------------------ comparison
def _holding_summary(entry: dict[str, Any]) -> dict[str, Any]:
    rows = entry["holdings"]
    return {"count": len(rows), "total_weight": round(sum(h["weight"] for h in rows), 4), "equity_count": sum(1 for h in rows if h.get("asset_class", eh.EQUITY) == eh.EQUITY),
            "zero_weight_equity_rows": sum(1 for h in rows if h.get("asset_class", eh.EQUITY) == eh.EQUITY and h["weight"] == 0)}


def holdings_changes(old: Optional[dict[str, Any]], new: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"new": _holding_summary(new), "old": _holding_summary(old) if old else None, "as_of_old": old.get("as_of") if old else None, "as_of_new": new["as_of"],
                           "provider_rows": new.get("provider_rows"), "omitted_zero_weight_rows": new.get("omitted_zero_weight_rows", {})}
    if not old:
        return out
    before = {_key(h): h for h in split_zero_noise(old["holdings"])[0]}
    after = {_key(h): h for h in new["holdings"]}
    changed = [(abs(after[k]["weight"] - before[k]["weight"]), k, before[k], after[k]) for k in before.keys() & after.keys() if after[k]["weight"] != before[k]["weight"]]
    changed.sort(key=lambda t: -t[0])
    out.update(added=len(after.keys() - before.keys()), removed=len(before.keys() - after.keys()), weight_changed=len(changed), unchanged=len(before.keys() & after.keys()) - len(changed),
               added_names=[after[k]["name"] for k in list(after.keys() - before.keys())[:8]], removed_names=[before[k]["name"] for k in list(before.keys() - after.keys())[:8]],
               biggest_changes=[{"name": a["name"], "old": b["weight"], "new": a["weight"]} for _d, _k, b, a in changed[:5]])
    return out


def sector_changes(old: Optional[dict[str, Any]], new: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"as_of_old": old.get("as_of") if old else None, "as_of_new": new["as_of"], "total_new": round(sum(new["sectors"].values()), 4), "sectors_new": len(new["sectors"])}
    if old:
        before, after = old["sectors"], new["sectors"]
        out["changes"] = [{"sector": s, "old": before.get(s), "new": after.get(s)} for s in sorted(set(before) | set(after), key=lambda s: -abs((after.get(s) or 0) - (before.get(s) or 0))) if before.get(s) != after.get(s)][:12]
    return out


# ------------------------------------------------------------------------------------------------------------------------ files
def _style(text: str) -> dict[str, Any]:
    """The JSON layout of the stored file, so a refresh does not reformat what it does not change."""
    if "\n  " in text:
        return {"indent": 2, "ensure_ascii": "\\u" in text}
    return {"separators": (",", ":"), "ensure_ascii": "\\u" in text}


def _dump(payload: dict[str, Any], style: dict[str, Any]) -> str:
    return json.dumps(payload, **style) + ("\n" if style.get("indent") else "")


def _read_json(path: Path) -> tuple[Optional[dict[str, Any]], str]:
    if not path.is_file():
        return None, ""
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("etfs", {}), dict):
        raise RefreshError("STORED_FILE_INVALID", f"{path.name} is not a file of the expected shape")
    return payload, text


def _replace_atomically(path: Path, payload: dict[str, Any], style: dict[str, Any], verify: Callable[[Path], None], stamp: str) -> Optional[Path]:
    """Candidate next to the file, read back through the real loader, then ``os.replace``; the previous file stays as a backup.  Any error leaves the old file untouched and
    removes what this call created."""
    candidate = path.with_name(path.name + f".new-{stamp}")
    backup = path.with_name(path.name + f".bak-refresh-{stamp}")
    saved: Optional[Path] = None
    try:
        candidate.write_text(_dump(payload, style), encoding="utf-8")
        verify(candidate)
        if path.is_file():
            backup.write_bytes(path.read_bytes())
            saved = backup
        os.replace(candidate, path)
        return saved
    except BaseException:
        if saved is not None and saved.exists():
            saved.unlink()
        raise
    finally:
        if candidate.exists():
            candidate.unlink()


def _verify_holdings(candidate: Path, refreshed: set[str], before: dict[str, Any]) -> None:
    """Read the candidate back through the real loader: every refreshed ETF is usable, no stored ETF is gone, aliases and issuer groups are exactly the stored ones."""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / HOLDINGS_FILE).write_bytes(candidate.read_bytes())
        layers = eh.read_layers(Path(folder))
    raw = json.loads(candidate.read_text(encoding="utf-8"))
    for isin in refreshed:
        if isin not in layers.etfs:
            raise RefreshError("VERIFY_FAILED", f"{isin} is not readable from the candidate file ({layers.invalid.get(isin)})")
    if not set(before.get("etfs", {})) <= set(raw.get("etfs", {})):
        raise RefreshError("VERIFY_FAILED", "a stored ETF is missing in the candidate file")
    if raw.get("aliases") != before.get("aliases") or raw.get("issuer_groups") != before.get("issuer_groups"):
        raise RefreshError("VERIFY_FAILED", "aliases or issuer groups differ from the stored file")


def _verify_lookthrough(candidate: Path, refreshed: set[str], before: dict[str, Any]) -> None:
    raw = json.loads(candidate.read_text(encoding="utf-8"))
    for isin in refreshed:
        lt._allocation(isin, raw["etfs"][isin])  # the loader's own check on the written entry
    if not set(before.get("etfs", {})) <= set(raw.get("etfs", {})):
        raise RefreshError("VERIFY_FAILED", "a stored ETF is missing in the candidate file")


# ------------------------------------------------------------------------------------------------------------------------ the refresh
def refresh(universe_dir: Path, *, write: bool = False, what: Iterable[str] = (HOLDINGS, LOOKTHROUGH), only: Iterable[str] = (), fetcher: Fetcher = fetch,
            now: Optional[datetime] = None) -> dict[str, Any]:
    """Download, validate and (with ``write``) store.  Returns the report; never raises for a provider problem (it is reported per ETF)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    wanted = {w for w in what}
    chosen = [s for s in SPECS if not list(only) or s.symbol in {x.upper() for x in only} or s.isin in {x.upper() for x in only}]
    folder = Path(universe_dir)
    report: dict[str, Any] = {"write": bool(write), "universe_dir": str(folder), "checked_at": now.isoformat(timespec="seconds"), "etfs": [], "files": {}, "warnings": []}
    for kind, file_name in ((HOLDINGS, HOLDINGS_FILE), (LOOKTHROUGH, LOOKTHROUGH_FILE)):
        if kind not in wanted:
            continue
        path = runtime_paths.private_file(folder, file_name)
        try:
            stored, text = _read_json(path)
        except (OSError, ValueError, RefreshError) as exc:
            report["files"][file_name] = {"written": False, "error": f"STORED_FILE_UNREADABLE: {exc}"}
            for spec in chosen:
                report["etfs"].append({"etf": spec.symbol, "isin": spec.isin, "dataset": kind, "status": "SKIPPED", "errors": ["STORED_FILE_UNREADABLE"], "warnings": [], "written": False})
            continue
        payload = json.loads(json.dumps(stored)) if stored is not None else {"map_id": file_name.rsplit(".", 1)[0], "version": 1, "etfs": {}}
        style = _style(text) if text else ({"separators": (",", ":"), "ensure_ascii": False} if kind == HOLDINGS else {"indent": 2, "ensure_ascii": False})
        refreshed: set[str] = set()
        for spec in chosen:
            if kind == LOOKTHROUGH and spec.format != "ishares":
                continue  # reported below as NOT_AUTOMATED
            old = (stored or {}).get("etfs", {}).get(spec.isin)
            row: dict[str, Any] = {"etf": spec.symbol, "isin": spec.isin, "dataset": kind, "provider": spec.provider, "source": spec.holdings_source if kind == HOLDINGS else spec.page_source,
                                   "status": "", "errors": [], "warnings": [], "written": False}
            report["etfs"].append(row)
            try:
                entry = _build_entry(spec, kind, old, fetcher, today)
            except RefreshError as exc:
                row.update(status="SKIPPED", errors=[str(exc)])
                continue
            errors, warnings = entry.pop("_errors"), entry.pop("_warnings")
            row["warnings"], row["errors"] = warnings, errors
            row["changes"] = holdings_changes(old, entry) if kind == HOLDINGS else sector_changes(old, entry)
            row["as_of"] = entry["as_of"]
            if kind == HOLDINGS:
                row["holdings"], row["weights_total"] = len(entry["holdings"]), round(sum(h["weight"] for h in entry["holdings"]), 4)
                row["provider_rows"] = entry.get("provider_rows", len(entry["holdings"]))
                row["omitted_zero_weight_rows"] = entry.get("omitted_zero_weight_rows", {})
                row["zero_weight_equity_rows"] = sum(1 for h in entry["holdings"] if h.get("asset_class", eh.EQUITY) == eh.EQUITY and h["weight"] == 0)
            else:
                row["sectors"], row["weights_total"] = len(entry["sectors"]), round(sum(entry["sectors"].values()), 4)
            if errors:
                row["status"] = "REJECTED"
                continue
            if old is not None and _same(old, entry, kind):
                row["status"] = "UNCHANGED"
                continue
            row["status"] = "READY"
            payload.setdefault("etfs", {})[spec.isin] = entry
            refreshed.add(spec.isin)
        if kind == LOOKTHROUGH:
            for spec in chosen:
                if spec.format == "invesco":
                    old = (stored or {}).get("etfs", {}).get(spec.isin)
                    age = (today - date.fromisoformat(old["as_of"])).days if old and old.get("as_of") else None
                    report["etfs"].append({"etf": spec.symbol, "isin": spec.isin, "dataset": kind, "provider": spec.provider, "status": "NOT_AUTOMATED", "errors": [], "written": False,
                                           "warnings": ["NO_STRUCTURED_SOURCE: Invesco publishes the sector allocation only in the factsheet PDF; the stored entry is kept"
                                                        + (f" (as of {old['as_of']}, {age} days old)" if old else " (there is none)")], "as_of": old.get("as_of") if old else None})
        info: dict[str, Any] = {"written": False, "refreshed": sorted(refreshed), "backup": None, "error": None, "bytes_before": len(text.encode("utf-8")) if text else 0,
                                "bytes_after": len(_dump(payload, style).encode("utf-8")) if refreshed else (len(text.encode("utf-8")) if text else 0)}
        report["files"][file_name] = info
        if refreshed and write:
            try:
                if kind == HOLDINGS:
                    info["backup"] = _backup_name(_replace_atomically(path, payload, style, lambda c: _verify_holdings(c, refreshed, stored or {}), stamp))
                else:
                    info["backup"] = _backup_name(_replace_atomically(path, payload, style, lambda c: _verify_lookthrough(c, refreshed, stored or {}), stamp))
                info["written"] = True
                for row in report["etfs"]:
                    if row["dataset"] == kind and row["isin"] in refreshed:
                        row["written"] = True
                        row["status"] = "WRITTEN"
            except (OSError, ValueError, RefreshError, eh.HoldingsError, lt.LookthroughError) as exc:
                info["error"] = f"WRITE_FAILED: {type(exc).__name__}: {exc}"  # the old file is untouched (the candidate is removed)
                for row in report["etfs"]:
                    if row["dataset"] == kind and row["isin"] in refreshed:
                        row["status"] = "NOT_WRITTEN"
                        row["errors"].append(info["error"])
    report["warnings"] = sorted({w for row in report["etfs"] for w in row.get("warnings", [])})
    report["summary"] = {s: sum(1 for r in report["etfs"] if r["status"] == s) for s in ("WRITTEN", "READY", "UNCHANGED", "REJECTED", "SKIPPED", "NOT_AUTOMATED", "NOT_WRITTEN")}
    report["outcome"] = outcome_of(report)
    return report


FAILING = ("REJECTED", "SKIPPED", "NOT_WRITTEN")
EXIT_OK, EXIT_FAILED, EXIT_PARTIAL = 0, 2, 3


def outcome_of(report: dict[str, Any]) -> str:
    """``OK`` (nothing failed; unchanged data and the manual SPXS sector allocation are no failure), ``PARTIAL`` (some datasets were refreshed or are current, some failed: a provider
    or parser error, a rejected validation, a failed write) or ``FAILED`` (nothing could be refreshed).  The stored files are never damaged by any of them."""
    rows = report["etfs"]
    failed = [r for r in rows if r["status"] in FAILING] or [name for name, info in report["files"].items() if info.get("error")]
    if not failed:
        return "OK"
    good = [r for r in rows if r["status"] in ("WRITTEN", "READY", "UNCHANGED")]
    return "PARTIAL" if good else "FAILED"


def exit_code(report: dict[str, Any]) -> int:
    """0 only for ``OK``; a task scheduler sees anything else as a failed run, so a provider or parser problem is never silent."""
    return {"OK": EXIT_OK, "PARTIAL": EXIT_PARTIAL}.get(report["outcome"], EXIT_FAILED)


def _long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.month - 1]} {d.year}"


def _backup_name(path: Optional[Path]) -> Optional[str]:
    return path.name if path else None


def _same(old: dict[str, Any], new: dict[str, Any], kind: str) -> bool:
    return old.get("as_of") == new.get("as_of") and (old.get("holdings") == new.get("holdings") if kind == HOLDINGS else old.get("sectors") == new.get("sectors"))


def _note(total: float, as_of: str) -> str:
    return ("Synthetic ETF: economic exposure = the S&P 500 index (the swap's benchmark composition, not the collateral basket). The index list is published with coarse rounding "
            f"and adds up to {total:.2f} % on {as_of}; the weights are used as published, NOT scaled; the excess is reported as a negative rounding drift (reconciliation only, "
            "never attributed to a company).")


def _build_entry(spec: EtfSpec, kind: str, old: Optional[dict[str, Any]], fetcher: Fetcher, today: date) -> dict[str, Any]:
    name = (old or {}).get("name") or spec.name
    if kind == HOLDINGS:
        if spec.format == "ishares":
            as_of, holdings = parse_ishares_holdings(fetcher(spec.holdings_url))
        else:
            identity = parse_invesco_identity(fetcher(INVESCO_FUND_URL.format(isin=spec.isin)), spec.isin)
            if INVESCO_NAME_PART not in identity:
                raise RefreshError("NOT_THE_FUND", f"the share class behind {spec.isin} is {identity!r}")
            as_of, holdings = parse_invesco_index(fetcher(spec.holdings_url))
        provider_rows, provider_total = len(holdings), sum(h["weight"] for h in holdings)
        holdings, omitted = split_zero_noise(holdings)
        errors, warnings = validate_holdings(spec, as_of, holdings, old, today)
        if abs(sum(h["weight"] for h in holdings) - provider_total) > 1e-9:
            errors.append("SUM_CHANGED: the omitted rows changed the sum of the published weights")  # cannot happen for weight-0 rows; a guard, not an expectation
        entry: dict[str, Any] = {"name": name, "provider": spec.provider, "as_of": as_of, "kind": spec.kind, "source": spec.holdings_source}
        if spec.kind == eh.SYNTHETIC_INDEX:
            entry["note"] = _note(sum(h["weight"] for h in holdings), as_of)
        if omitted:
            entry["provider_rows"] = provider_rows
            entry["omitted_zero_weight_rows"] = omitted
        entry["holdings"] = holdings
    else:
        if spec.format != "ishares" or not spec.page_url:
            raise RefreshError("NOT_AUTOMATED", "no structured source")
        as_of, sectors = parse_ishares_sectors(fetcher(spec.page_url), spec.page_tickers or (spec.symbol,))
        errors, warnings = validate_sectors(spec, as_of, sectors, old, today)
        entry = {"name": name, "provider": spec.provider, "as_of": as_of,
                 "source": f"{spec.page_source} (exposure breakdown, sector, as of {_long_date(as_of)})", "sectors": sectors}
    entry["_errors"], entry["_warnings"] = errors, warnings
    return entry
