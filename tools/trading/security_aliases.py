# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Security alias resolution: one security (``security_id``), many source-specific names (read-only, deterministic, no fuzzy matching).

``security_id`` of the security master is the canonical identity.  Symbols are aliases of it: the public symbol (``security.symbol``),
the symbol every data source uses (``source_symbols``: Yahoo Finance, SEC EDGAR, FMP, ...), the ISIN and the WKN.  Market intelligence,
the opportunity view and the discovery use this one index instead of comparing symbol strings.

Resolution order (``AliasIndex.resolve``), the first unambiguous hit wins and nothing is guessed::

    1. security_id given          2. unique source identifier (source + its symbol)     3. ISIN       4. WKN
    5. exact known alias          6. controlled symbol-stem fallback (opt-in)           7. exact company name

Free text (a wire headline) is never searched for a bare ticker.  ``resolve_text`` accepts only explicit identification: an ISIN, an
exchange-qualified ticker such as ``(NASDAQ: ABCD)`` or the full company-name phrase.  A short symbol like ``AI``/``ON``/``IT`` can
therefore never match an ordinary word.

The Security-Master data is read with SELECT only; a missing table or column degrades to fewer aliases, never to an exception.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

# matched_by values
SECURITY_ID = "SECURITY_ID"
SOURCE_IDENTIFIER = "SOURCE_IDENTIFIER"
ISIN = "ISIN"
WKN = "WKN"
ALIAS = "ALIAS"
SYMBOL_STEM = "SYMBOL_STEM"
COMPANY_NAME = "COMPANY_NAME"
TEXT_ISIN = "TEXT_ISIN"
TEXT_EXCHANGE_TICKER = "TEXT_EXCHANGE_TICKER"
TEXT_COMPANY_NAME = "TEXT_COMPANY_NAME"
TEXT_MATCHES = frozenset({TEXT_ISIN, TEXT_EXCHANGE_TICKER, TEXT_COMPANY_NAME})

# Listing suffixes of Yahoo-style symbols ("SAP.DE").  market_intelligence._SUFFIX_REGION has the same keys (a test enforces it).
EXCHANGE_SUFFIXES = frozenset({"DE", "F", "AS", "PA", "MI", "MC", "VI", "L", "AX", "KS", "SW", "CO", "ST"})
# data_sources.name -> short source key
SOURCE_KEYS = {"Yahoo Finance": "YAHOO", "SEC EDGAR": "SEC", "FMP": "FMP"}

NAME_NOISE = frozenset({"inc", "corp", "corporation", "holdings", "holding", "ltd", "limited", "plc", "ag", "se", "nv", "n", "v", "co", "company", "group", "the", "class",
                        "adr", "a", "b", "c", "sa", "spa", "incorporated"})
# words that occur in many company names and therefore do not identify one company on their own
NAME_GENERIC = frozenset({"advanced", "applied", "general", "micro", "materials", "technology", "technologies", "systems", "therapeutics", "pharma", "pharmaceuticals",
                          "biosciences", "bio", "networks", "international", "global", "energy", "semiconductor", "manufacturing", "devices", "american", "united", "first",
                          "national", "digital", "capital", "financial"})

_ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")
# explicit "EXCHANGE: TICKER" as wires write it; the exchange is matched case-insensitively, the ticker must be upper case
_EXCHANGE_TICKER_RE = re.compile(
    r"(?i:\b(NASDAQ(?:\s+(?:Copenhagen|Stockholm|Helsinki))?|NYSE(?:\s+(?:American|Arca|MKT))?|AMEX|OTC(?:QX|QB|MKTS|PK)?|XETRA|Frankfurt|FWB|"
    r"Euronext(?:\s+(?:Paris|Amsterdam))?|LSE|LON|ASX|KRX|KOSDAQ|SIX|SWX|BME|Borsa\s+Italiana|MIL|STO|CPH|EPA|AMS)\s*:)\s*([A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z0-9]{1,2})?)\b")
_US_EXCHANGES = frozenset({"NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEARCA", "NYSEMKT", "AMEX", "OTC", "OTCQX", "OTCQB", "OTCMKTS", "OTCPK"})
_EXCHANGE_SUFFIX = {
    "XETRA": "DE", "FRANKFURT": "F", "FWB": "F", "EURONEXTPARIS": "PA", "EPA": "PA", "EURONEXTAMSTERDAM": "AS", "AMS": "AS", "BORSAITALIANA": "MI", "MIL": "MI",
    "BME": "MC", "LSE": "L", "LON": "L", "ASX": "AX", "KRX": "KS", "KOSDAQ": "KS", "SIX": "SW", "SWX": "SW", "NASDAQCOPENHAGEN": "CO", "CPH": "CO",
    "NASDAQSTOCKHOLM": "ST", "STO": "ST",
}


# --------------------------------------------------------------------------- normalization
def _class_key(stem: str) -> str:
    return stem.replace("-", ".").replace("/", ".")  # BRK-B == BRK.B


def split_symbol(value: Any) -> tuple[str, Optional[str]]:
    """``"SAP.DE"`` -> ``("SAP", "DE")``; ``"BRK-B"`` -> ``("BRK.B", None)``.  Only a known listing suffix is split off."""
    text = re.sub(r"\s+", "", str(value or "").upper())
    if "." in text:
        stem, _, suffix = text.rpartition(".")
        if stem and suffix in EXCHANGE_SUFFIXES:
            return _class_key(stem), suffix
    return _class_key(text), None


def symbol_key(value: Any) -> str:
    stem, suffix = split_symbol(value)
    return f"{stem}.{suffix}" if suffix else stem


def source_key(name: Any) -> str:
    text = str(name or "").strip()
    return SOURCE_KEYS.get(text, re.sub(r"[^A-Z0-9]", "", text.upper()))


def name_tokens(name: Any) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower()).split() if t not in NAME_NOISE]


def _normalized_text(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())) + " "


# --------------------------------------------------------------------------- model
@dataclass(frozen=True)
class Match:
    security_id: int
    matched_by: str
    matched_alias: str

    def primitive(self) -> dict[str, Any]:
        return {"security_id": self.security_id, "matched_by": self.matched_by, "matched_alias": self.matched_alias}


@dataclass(frozen=True)
class SecurityAlias:
    """One security with all of its names.  ``aliases`` holds the public symbol and every source symbol (raw, as stored)."""

    security_id: int
    key: Optional[str]
    aliases: tuple[str, ...] = ()
    isin: Optional[str] = None
    wkn: Optional[str] = None
    name: str = ""
    source_symbols: tuple[tuple[str, str], ...] = ()  # (source key, symbol)


def _unique(ids: Optional[set]) -> Optional[int]:
    return next(iter(ids)) if ids is not None and len(ids) == 1 else None


class AliasIndex:
    """Immutable in-memory alias index (build once per run)."""

    def __init__(self, securities: Iterable[SecurityAlias]):
        self._by_id: dict[int, SecurityAlias] = {}
        self._by_isin: dict[str, set[int]] = {}
        self._by_wkn: dict[str, set[int]] = {}
        self._by_alias: dict[str, set[int]] = {}
        self._by_listing: dict[str, set[int]] = {}  # the listing symbol (key, with its exchange suffix) of each security
        self._by_stem: dict[str, set[int]] = {}
        self._by_source: dict[tuple[str, str], set[int]] = {}
        self._by_name: dict[str, set[int]] = {}
        for security in securities:
            sid = int(security.security_id)
            self._by_id[sid] = security
            if security.isin:
                self._by_isin.setdefault(security.isin.upper(), set()).add(sid)
            if security.wkn:
                self._by_wkn.setdefault(security.wkn.upper(), set()).add(sid)
            if security.key:
                self._by_listing.setdefault(symbol_key(security.key), set()).add(sid)
            for alias in security.aliases:
                if alias:
                    self._by_alias.setdefault(symbol_key(alias), set()).add(sid)
                    self._by_stem.setdefault(split_symbol(alias)[0], set()).add(sid)
            for source, symbol in security.source_symbols:
                self._by_source.setdefault((source, symbol_key(symbol)), set()).add(sid)
            tokens = name_tokens(security.name)
            if tokens:
                self._by_name.setdefault(" ".join(tokens), set()).add(sid)

    def __len__(self) -> int:
        return len(self._by_id)

    def extended(self, extra: Iterable[SecurityAlias]) -> "AliasIndex":
        """A new index with additional entries (for example discovery candidates that are not in the Security Master and carry negative ids)."""
        return AliasIndex([*self._by_id.values(), *extra])

    def __contains__(self, security_id: Any) -> bool:
        return security_id is not None and int(security_id) in self._by_id

    # -- construction -------------------------------------------------------
    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "AliasIndex":
        """Read-only SELECTs on ``security``, ``source_symbols`` and ``data_sources``; missing pieces mean fewer aliases."""
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(security)")}
            select = ", ".join(c if c in columns else "NULL" for c in ("id", "symbol", "isin", "wkn", "name"))
            securities = {int(r[0]): r for r in conn.execute(f"SELECT {select} FROM security")} if "id" in columns else {}
        except sqlite3.Error:
            return cls(())
        by_source: dict[int, list[tuple[str, str]]] = {}
        try:
            for row in conn.execute("SELECT s.security_id, d.name, s.symbol FROM source_symbols s JOIN data_sources d ON d.id = s.source_id WHERE s.symbol IS NOT NULL ORDER BY s.security_id, d.name"):
                by_source.setdefault(int(row[0]), []).append((source_key(row[1]), str(row[2])))
        except sqlite3.Error:
            pass
        records = []
        for sid, row in securities.items():
            sources = tuple(by_source.get(sid, ()))
            yahoo = next((symbol for source, symbol in sources if source == "YAHOO"), None)
            symbol = row[1]
            aliases = tuple(dict.fromkeys([a for a in ([symbol] if symbol else []) + [s for _, s in sources] if a]))
            records.append(SecurityAlias(sid, yahoo or symbol, aliases, row[2], row[3], row[4] or "", sources))
        return cls(records)

    @classmethod
    def from_table(cls, table: Mapping[str, Mapping[str, Any]]) -> "AliasIndex":
        """Rebuild from the ``aliases`` table of a market-intelligence report (the reader has no database)."""
        return cls(SecurityAlias(int(sid), entry.get("key"), tuple(entry.get("aliases") or ()), entry.get("isin"), entry.get("wkn")) for sid, entry in (table or {}).items())

    # -- lookups -------------------------------------------------------------
    def key_for(self, security_id: Any) -> Optional[str]:
        record = self._by_id.get(int(security_id)) if security_id is not None else None
        return record.key if record else None

    def aliases_of(self, security_id: Any) -> list[str]:
        """Upper-case lookup names of a security: public/source symbols (full and without listing suffix), ISIN and WKN."""
        record = self._by_id.get(int(security_id)) if security_id is not None else None
        if record is None:
            return []
        names: list[str] = []
        for alias in ([record.key] if record.key else []) + list(record.aliases):
            names += [symbol_key(alias), split_symbol(alias)[0]]
        names += [x.upper() for x in (record.isin, record.wkn) if x]
        return sorted(dict.fromkeys(n for n in names if n))

    def report_table(self, security_ids: Iterable[int]) -> dict[str, dict[str, Any]]:
        """Compact alias table for a report (only the securities that appear in it)."""
        table: dict[str, dict[str, Any]] = {}
        for sid in sorted({int(i) for i in security_ids if i is not None and int(i) in self._by_id}):
            record = self._by_id[sid]
            table[str(sid)] = {"key": record.key, "aliases": list(record.aliases), "isin": record.isin, "wkn": record.wkn}
        return table

    # -- resolution ----------------------------------------------------------
    def resolve(
        self,
        *,
        security_id: Any = None,
        source: Optional[str] = None,
        source_symbol: Optional[str] = None,
        isin: Optional[str] = None,
        wkn: Optional[str] = None,
        symbol: Optional[str] = None,
        name: Optional[str] = None,
        allow_stem: bool = False,
    ) -> Optional[Match]:
        if security_id is not None and int(security_id) in self._by_id:
            return Match(int(security_id), SECURITY_ID, str(int(security_id)))
        if source and source_symbol:
            sid = _unique(self._by_source.get((source_key(source), symbol_key(source_symbol))))
            if sid is not None:
                return Match(sid, SOURCE_IDENTIFIER, str(source_symbol))
        if isin:
            sid = _unique(self._by_isin.get(str(isin).strip().upper()))
            if sid is not None:
                return Match(sid, ISIN, str(isin).strip().upper())
        if wkn:
            sid = _unique(self._by_wkn.get(str(wkn).strip().upper()))
            if sid is not None:
                return Match(sid, WKN, str(wkn).strip().upper())
        if symbol:
            sid = _unique(self._by_alias.get(symbol_key(symbol)))
            if sid is not None:
                return Match(sid, ALIAS, str(symbol))
            if allow_stem:
                sid = _unique(self._by_stem.get(split_symbol(symbol)[0]))
                if sid is not None:
                    return Match(sid, SYMBOL_STEM, str(symbol))
        if name:
            tokens = name_tokens(name)
            sid = _unique(self._by_name.get(" ".join(tokens))) if tokens else None
            if sid is not None:
                return Match(sid, COMPANY_NAME, str(name))
        return None

    def candidates(self, kind: str, value: Any) -> frozenset[int]:
        """Every security that one exact lookup of :meth:`resolve` finds (``resolve`` itself returns only a unique hit).

        For callers that must tell *unknown* (no id) from *ambiguous* (several ids).  ``kind`` is ``SECURITY_ID``, ``ISIN``, ``WKN``, ``ALIAS``,
        ``SYMBOL_STEM`` or ``COMPANY_NAME``; the normalization is the one of ``resolve``.  Read-only, never raises for a plain value.
        """
        text = str(value if value is not None else "").strip()
        if not text:
            return frozenset()
        if kind == SECURITY_ID:
            return frozenset({int(text)}) if text.isdigit() and int(text) in self._by_id else frozenset()
        if kind == ISIN:
            return frozenset(self._by_isin.get(text.upper(), ()))
        if kind == WKN:
            return frozenset(self._by_wkn.get(text.upper(), ()))
        if kind == ALIAS:
            return frozenset(self._by_alias.get(symbol_key(text), ()))
        if kind == SYMBOL_STEM:
            return frozenset(self._by_stem.get(split_symbol(text)[0], ()))
        if kind == COMPANY_NAME:
            tokens = name_tokens(text)
            return frozenset(self._by_name.get(" ".join(tokens), ())) if tokens else frozenset()
        raise ValueError(f"unknown lookup kind: {kind}")

    def resolve_text(self, text: str, *, allow_name: bool = True) -> list[Match]:
        """Securities a free text identifies *explicitly* (ISIN, ``EXCHANGE: TICKER``, full company-name phrase).  Never a bare ticker word."""
        found: dict[int, Match] = {}
        for token in dict.fromkeys(_ISIN_RE.findall(text or "")):
            sid = _unique(self._by_isin.get(token))
            if sid is not None:
                found.setdefault(sid, Match(sid, TEXT_ISIN, token))
        for exchange, ticker in _EXCHANGE_TICKER_RE.findall(text or ""):
            exchange_key = re.sub(r"[^A-Z0-9]", "", exchange.upper())
            if exchange_key in _US_EXCHANGES:
                lookup = ticker
            elif exchange_key in _EXCHANGE_SUFFIX:
                lookup = f"{ticker}.{_EXCHANGE_SUFFIX[exchange_key]}"
            else:
                continue  # an exchange without a known listing suffix cannot be mapped to a stored symbol
            sid = _unique(self._by_listing.get(symbol_key(lookup)))  # the exchange must fit the listing, not just the bare ticker
            if sid is not None:
                found.setdefault(sid, Match(sid, TEXT_EXCHANGE_TICKER, f"{exchange.strip()} {ticker}"))
        if allow_name:
            normalized = _normalized_text(text or "")
            for phrase, ids in self._by_name.items():
                sid = _unique(ids)
                if sid is None or sid in found:
                    continue
                tokens = phrase.split()
                if len(tokens) == 1 and (len(tokens[0]) < 6 or tokens[0] in NAME_GENERIC):
                    continue  # a single common word does not identify a company
                if f" {phrase} " in normalized and re.search(rf"\b{re.escape(tokens[0].capitalize())}|\b{re.escape(tokens[0].upper())}\b", text or ""):
                    found[sid] = Match(sid, TEXT_COMPANY_NAME, self._by_id[sid].name)
        return [found[sid] for sid in sorted(found)]

    def security_ids_for_query(self, text: Any) -> set[int]:
        """Securities a user query (canonical symbol, any alias, ISIN, WKN) refers to; the stem fallback applies only if nothing else matches."""
        query = str(text or "").strip().upper()
        if not query:
            return set()
        ids = set(self._by_isin.get(query, ())) | set(self._by_wkn.get(query, ())) | set(self._by_alias.get(symbol_key(query), ()))
        return ids or set(self._by_stem.get(split_symbol(query)[0], ()))


def load_alias_index(conn: sqlite3.Connection) -> AliasIndex:
    return AliasIndex.from_connection(conn)
