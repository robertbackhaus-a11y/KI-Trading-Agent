# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Deterministic market intelligence: collect, classify and map news to the portfolio (read-only, no orders, no LLM).

Flow::

    SOURCE (official feeds, SEC filings, Yahoo headlines, company-wire RSS) -> NORMALIZED EVENT -> SECURITY/ALIAS RESOLUTION
        -> DEDUPLICATION -> CLASSIFY -> RELEVANCE -> PORTFOLIO / WATCHLIST / DISCOVERY MAPPING

``NEWS != SIGNAL``: nothing here changes SELL/TRIM/HOLD, PROMOTE, ENTRY_READY, the ranking or
any sizing, and nothing creates an order.  The output is context and warnings only.

Everything is rule based and reproducible:

* **Category** from the source and fixed rules (feed type, SEC form item, explicit keywords).
* **Importance** (HIGH/MEDIUM/LOW) from a fixed table.  Scheduled macro releases are MEDIUM
  because a surprise against a consensus cannot be assessed without consensus data.
* **Impact** is ``UNKNOWN`` unless the *structure* of a primary source states an adverse
  fact (SEC delisting notice, cybersecurity incident); it is never read out of a headline.
* **Mapping** by security (``security_id``; the symbol is only one of its aliases, see ``security_aliases``), by an explicitly
  sector-scoped event (static sector map) and, for macro events, by the region of the held symbols.
* **Deduplication** across sources, deterministic: same event id, then same normalized URL, then same normalized headline within
  ``DEDUPE_WINDOW_HOURS`` (6 hours for short headlines); the sources of the merged copies stay in ``duplicate_sources``.

Network access lives in the CLI; this module only parses and classifies what it is given.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html import unescape
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from security_aliases import (
    NAME_GENERIC as _NAME_GENERIC,
    NAME_NOISE as _NAME_NOISE,
    SECURITY_ID,
    TEXT_MATCHES,
    AliasIndex,
    Match,
    SecurityAlias,
)
from strategy_config import MarketIntelligenceConfig

MACRO = "MACRO"
MONETARY_POLICY = "MONETARY_POLICY"
INFLATION = "INFLATION"
LABOR_MARKET = "LABOR_MARKET"
ECONOMIC_GROWTH = "ECONOMIC_GROWTH"
GEOPOLITICS = "GEOPOLITICS"
ENERGY = "ENERGY"
SECTOR = "SECTOR"
COMPANY = "COMPANY"
EARNINGS = "EARNINGS"
GUIDANCE = "GUIDANCE"
REGULATORY = "REGULATORY"
M_AND_A = "M_AND_A"
# COMMODITIES is intentionally not implemented: no commodity source exists besides the energy agency.
CATEGORIES = (MACRO, MONETARY_POLICY, INFLATION, LABOR_MARKET, ECONOMIC_GROWTH, GEOPOLITICS, ENERGY, SECTOR, COMPANY, EARNINGS, GUIDANCE, REGULATORY, M_AND_A)
MACRO_CATEGORIES = (MACRO, MONETARY_POLICY, INFLATION, LABOR_MARKET, ECONOMIC_GROWTH, GEOPOLITICS)

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"
IMPORTANCES = (HIGH, MEDIUM, LOW)
POSITIVE, NEGATIVE, MIXED, NEUTRAL, UNKNOWN = "POSITIVE", "NEGATIVE", "MIXED", "NEUTRAL", "UNKNOWN"
IMPACTS = (POSITIVE, NEGATIVE, MIXED, NEUTRAL, UNKNOWN)
SOURCE_TYPES = ("OFFICIAL_FEED", "SEC_FILING", "NEWS")

# Official primary feeds (no key, no cost).  Eurostat's feed was examined and left out: it is a stream of
# ~2,400 dataset-update lines, not news.  BLS "latest" is a single pointer page.
FEEDS: dict[str, dict[str, Any]] = {
    "ECB_PRESS": {"url": "https://www.ecb.europa.eu/rss/press.html", "label": "European Central Bank press", "regions": ["EU"]},
    "ECB_STATS": {"url": "https://www.ecb.europa.eu/rss/statpress.html", "label": "European Central Bank statistical press", "regions": ["EU"]},
    "FED_MONETARY": {"url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "label": "Federal Reserve monetary policy", "regions": ["US"]},
    "FED_PRESS": {"url": "https://www.federalreserve.gov/feeds/press_all.xml", "label": "Federal Reserve press", "regions": ["US"]},
    "BLS_CPI": {"url": "https://www.bls.gov/feed/cpi.rss", "label": "BLS consumer price index", "regions": ["US"]},
    "BLS_EMPLOYMENT": {"url": "https://www.bls.gov/feed/empsit.rss", "label": "BLS employment situation", "regions": ["US"]},
    "BEA": {"url": "https://apps.bea.gov/rss/rss.xml", "label": "Bureau of Economic Analysis", "regions": ["US"]},
    "DESTATIS": {"url": "https://www.destatis.de/SiteGlobals/Functions/RSSFeed/DE/RSSNewsfeed/Aktuell.xml", "label": "Destatis press", "regions": ["DE"]},
    "EIA_TODAY": {"url": "https://www.eia.gov/rss/todayinenergy.xml", "label": "EIA Today in Energy", "regions": ["US"]},
    "EIA_PRESS": {"url": "https://www.eia.gov/rss/press_rss.xml", "label": "EIA press releases", "regions": ["US"]},
}
YAHOO_NEWS = "YAHOO_NEWS"
SEC_FILINGS = "SEC_FILINGS"
# Same headline on two sources within this window is one event (the wire release and the copy a news site publishes shortly after).
DEDUPE_WINDOW_HOURS = 48
_MIN_HEADLINE_KEY = 20  # shorter normalized headlines are too generic to identify one article
_SHORT_HEADLINE_KEY = 40  # ... and a short one (a recurring column title) is only the same article within a few hours
_SHORT_HEADLINE_WINDOW_HOURS = 6
_NEWS_FEED_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")

# Yahoo symbol suffix -> region of the listing (for macro region exposure); no suffix = US listing
_SUFFIX_REGION = {"DE": "DE", "F": "DE", "AS": "NL", "PA": "FR", "MI": "IT", "MC": "ES", "VI": "AT", "L": "GB", "AX": "AU", "KS": "KR", "SW": "CH", "CO": "DK", "ST": "SE"}
_REGION_GROUPS = {"DE": ("DE", "EU"), "NL": ("NL", "EU"), "FR": ("FR", "EU"), "IT": ("IT", "EU"), "ES": ("ES", "EU"), "AT": ("AT", "EU")}

# SEC form items as stored by Backfill-TradingEventsNews.py -> (category, importance, impact, reason code)
SEC_EVENT_RULES: dict[str, tuple[str, str, str, str]] = {
    "earnings": (EARNINGS, MEDIUM, UNKNOWN, "SEC_8K_ITEM_2_02_RESULTS"),
    "acquisition_disposition": (M_AND_A, HIGH, UNKNOWN, "SEC_8K_ITEM_2_01_ACQUISITION_DISPOSITION"),
    "control_change": (M_AND_A, HIGH, UNKNOWN, "SEC_8K_ITEM_5_01_CHANGE_IN_CONTROL"),
    "delisting_notice": (REGULATORY, HIGH, NEGATIVE, "SEC_8K_ITEM_3_01_DELISTING_NOTICE"),
    "sec_8k_1.05": (COMPANY, HIGH, NEGATIVE, "SEC_8K_ITEM_1_05_CYBERSECURITY_INCIDENT"),
    "sec_8k_4.02": (COMPANY, HIGH, NEGATIVE, "SEC_8K_ITEM_4_02_NON_RELIANCE"),
    "material_agreement": (COMPANY, MEDIUM, UNKNOWN, "SEC_8K_ITEM_1_01_MATERIAL_AGREEMENT"),
    "material_agreement_termination": (COMPANY, MEDIUM, UNKNOWN, "SEC_8K_ITEM_1_02_AGREEMENT_TERMINATION"),
    "exit_disposal_costs": (COMPANY, MEDIUM, UNKNOWN, "SEC_8K_ITEM_2_05_EXIT_DISPOSAL_COSTS"),
    "accountant_change": (COMPANY, MEDIUM, UNKNOWN, "SEC_8K_ITEM_4_01_ACCOUNTANT_CHANGE"),
    "financial_obligation": (COMPANY, LOW, UNKNOWN, "SEC_8K_ITEM_2_03_FINANCIAL_OBLIGATION"),
    "officer_director_change": (COMPANY, LOW, UNKNOWN, "SEC_8K_ITEM_5_02_OFFICER_DIRECTOR_CHANGE"),
    "bylaws_amendment": (COMPANY, LOW, UNKNOWN, "SEC_8K_ITEM_5_03_BYLAWS"),
    "shareholder_vote": (COMPANY, LOW, UNKNOWN, "SEC_8K_ITEM_5_07_SHAREHOLDER_VOTE"),
    "regulation_fd_disclosure": (COMPANY, LOW, UNKNOWN, "SEC_8K_ITEM_7_01_REGULATION_FD"),
    "other_material_event": (COMPANY, LOW, UNKNOWN, "SEC_8K_ITEM_8_01_OTHER_EVENTS"),
    "security_holder_rights_change": (COMPANY, LOW, UNKNOWN, "SEC_8K_ITEM_3_03_HOLDER_RIGHTS"),
    "sec_8k_3.02": (COMPANY, LOW, UNKNOWN, "SEC_8K_ITEM_3_02_UNREGISTERED_SALES"),
    "foreign_issuer_report": (COMPANY, LOW, UNKNOWN, "SEC_6K_CONTENT_UNKNOWN"),
}
SEC_SKIPPED = {"sec_8k_9.01"}  # exhibits only, never the reason of a filing

# explicit sector words for sector-scoped events (headline keyword -> sector of the static sector map)
SECTOR_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"\b(semiconductors?|chipmakers?|chip stocks?|foundry|foundries)\b", "SEMICONDUCTORS"),
    (r"\b(oil prices?|crude|opec|natural gas|refiner(?:y|ies)|energy stocks?)\b", "ENERGY"),
    (r"\b(defen[sc]e stocks?|defen[sc]e spending|nato)\b", "DEFENSE_AEROSPACE"),
    (r"\b(biotech|pharma(?:ceuticals?)?|drug pricing)\b", "HEALTHCARE_PHARMA"),
    (r"\b(bank stocks?|banks)\b", "FINANCIALS"),
    (r"\b(carmakers?|automakers?|auto tariffs?)\b", "AUTOMOTIVE"),
)

# company press-release wires: a headline there is a statement of the company itself (primary-like)
COMPANY_WIRES = frozenset({"GlobeNewswire", "PR Newswire", "Business Wire", "ACCESSWIRE", "Newsfile Corp.", "Newsfile"})

_YAHOO_RULES: tuple[tuple[str, str, str, str], ...] = (
    # (regex on the lower-case headline, category, importance, reason code)
    (r"\b(stocks? to buy|best (?:[\w&.\-]+ ){0,4}stocks?|buy now|buy, hold,? or sell|time to buy|better stock|top stocks?|stock picks?|should you buy|worth buying)\b", COMPANY, LOW, "STOCK_PICK_ARTICLE"),
    (r"\b(profit warning|warns of|cuts? (?:its )?(?:guidance|forecast|outlook)|raises? (?:its )?(?:guidance|forecast|outlook)|lowers? (?:its )?(?:guidance|forecast|outlook)|(?:reaffirms?|reiterates?|issues?|updates?) (?:its )?guidance|guidance|preannounc\w*)", GUIDANCE, HIGH, "GUIDANCE_KEYWORD"),
    (r"\b(acquire[sd]?|acquisition|merger|takeover|buyout|bids? for|spin-?off|divest\w*)\b", M_AND_A, HIGH, "M_AND_A_KEYWORD"),
    (r"\b(sec charges|investigation|probe|antitrust|fda|approval|approves|lawsuit|sues|injunction|ruling|fines?|fined|export controls?|regulators?|recall)\b", REGULATORY, HIGH, "REGULATORY_KEYWORD"),
    (r"\b(earnings|quarterly results|q[1-4] (?:results|earnings)|reports? (?:first|second|third|fourth) quarter|eps)\b", EARNINGS, MEDIUM, "EARNINGS_KEYWORD"),
    (r"\b(upgrades?|downgrades?|price targets?|initiates? coverage)\b", COMPANY, MEDIUM, "ANALYST_ACTION_KEYWORD"),
    (r"\b(fed|federal reserve|rate cuts?|rate hikes?|ecb|treasury yields?)\b", MONETARY_POLICY, MEDIUM, "MONETARY_KEYWORD"),
    (r"\b(inflation|cpi|pce)\b", INFLATION, MEDIUM, "INFLATION_KEYWORD"),
    (r"\b(jobs report|payrolls?|unemployment)\b", LABOR_MARKET, MEDIUM, "LABOR_KEYWORD"),
    (r"\b(gdp|recession)\b", ECONOMIC_GROWTH, MEDIUM, "GROWTH_KEYWORD"),
    (r"\b(tariffs?|sanctions?|geopolit\w*|war|ceasefire|missile|embargo)\b", GEOPOLITICS, MEDIUM, "GEOPOLITICS_KEYWORD"),
    (r"\b(oil prices?|crude|opec|natural gas)\b", ENERGY, MEDIUM, "ENERGY_KEYWORD"),
)


# --------------------------------------------------------------------------- helpers
def importance_rank(value: str) -> int:
    return IMPORTANCES.index(value)


def canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def event_id(source: str, canonical: str) -> str:
    return hashlib.sha256(f"{source}|{canonical}".encode("utf-8")).hexdigest()[:16]


def symbol_region(symbol: str) -> str:
    suffix = symbol.rsplit(".", 1)[1].upper() if "." in symbol else ""
    return _SUFFIX_REGION.get(suffix, "US") if suffix else "US"


def symbol_regions(symbol: str) -> tuple[str, ...]:
    region = symbol_region(symbol)
    return _REGION_GROUPS.get(region, (region,))


def _to_utc_iso(value: Any) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _clean(text: Optional[str], limit: int) -> Optional[str]:
    if not text:
        return None
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    return plain[:limit] if plain else None




def company_mentioned(headline: str, symbol: str, info: Mapping[str, Any], *, allow_ticker: bool = True) -> bool:
    """True if the headline names the company: its normalized legal name, a distinctive word of the name, or (``allow_ticker``) its ticker as a whole upper-case word."""
    text = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", headline.lower())) + " "
    tokens = [t for t in re.sub(r"[^a-z0-9 ]+", " ", str(info.get("name") or "").lower()).split() if t not in _NAME_NOISE]
    if tokens and f" {' '.join(tokens)} " in text:
        return True
    if any(len(t) >= 4 and t not in _NAME_GENERIC and f" {t} " in text.replace("'s ", " ") for t in tokens):
        return True
    if not allow_ticker:
        return False
    ticker = symbol.split(".")[0]
    return len(ticker) >= 3 and re.search(rf"\b{re.escape(ticker)}\b", headline) is not None


def load_sector_map(payload: Mapping[str, Any]) -> dict[str, str]:
    """``{"sectors": {SECTOR: [symbols]}}`` -> ``{SYMBOL: SECTOR}``; a symbol in two sectors is an error."""
    mapping: dict[str, str] = {}
    for sector, symbols in (payload.get("sectors") or {}).items():
        for symbol in symbols:
            if symbol in mapping and mapping[symbol] != sector:
                raise ValueError(f"symbol {symbol} is in two sectors")
            mapping[symbol] = sector
    if not mapping:
        raise ValueError("sector map is empty")
    return mapping


# --------------------------------------------------------------------------- feeds
def parse_feed(payload: bytes) -> list[dict[str, Any]]:
    """RSS 2.0 / Atom bytes -> ``[{"title", "link", "published_at", "summary", "guid", "lead"}]`` (no network).

    ``guid`` is the source's own item id (RSS ``guid`` / Atom ``id``) when present; ``lead`` is a longer plain-text start of the
    description used only to identify companies (it is never stored in a report)."""
    return parse_feed_detailed(payload)[0]


def parse_feed_detailed(payload: bytes) -> tuple[list[dict[str, Any]], int]:
    """Like :func:`parse_feed` plus the number of entries that were skipped because title, link or date was missing.

    A payload that is not well-formed XML raises ``xml.etree.ElementTree.ParseError`` (a feed-level failure for the caller)."""
    root = ET.fromstring(payload)
    atom = "{http://www.w3.org/2005/Atom}"
    dc = "{http://purl.org/dc/elements/1.1/}"

    def text(item: ET.Element, *tags: str) -> str:
        for tag in tags:
            node = item.find(tag)
            if node is not None:
                return (node.text or node.attrib.get("href") or "").strip()
        return ""

    items = []
    skipped = 0
    for item in root.findall(".//item") + root.findall(f".//{atom}entry"):
        title = text(item, "title", f"{atom}title")
        link = text(item, "link", f"{atom}link")
        published = _to_utc_iso(text(item, "pubDate", f"{atom}updated", f"{atom}published", f"{dc}date"))
        if title and link and published:
            description = text(item, "description", f"{atom}summary")
            items.append({"title": title, "link": link, "published_at": published, "summary": _clean(description, 200),
                          "guid": text(item, "guid", f"{atom}id") or None, "lead": _clean(description, 600)})
        else:
            skipped += 1
    return items, skipped


def load_news_feeds(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Company-wire RSS feed configuration (``universe/news_feeds_v1.json``) -> ``{KEY: {url, label, publisher, source}}`` of the enabled feeds.

    Only https URLs, unique upper-case keys; anything else is a configuration error (the collector then ignores the file and says so)."""
    if payload.get("version") != 1:
        raise ValueError("news feed configuration: unsupported version")
    feeds: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for entry in payload.get("feeds") or []:
        key = str(entry.get("key") or "")
        if not _NEWS_FEED_KEY.match(key) or key in seen:
            raise ValueError(f"news feed configuration: invalid or duplicate key {key!r}")
        seen.add(key)
        url, publisher, source = str(entry.get("url") or ""), str(entry.get("publisher") or "").strip(), str(entry.get("source") or "")
        if not url.startswith("https://") or not publisher or not _NEWS_FEED_KEY.match(source):
            raise ValueError(f"news feed configuration: {key} needs an https url, a publisher and an upper-case source name")
        if entry.get("enabled", True):
            feeds[key] = {"url": url, "label": str(entry.get("label") or key), "publisher": publisher, "source": source}
    return feeds


def classify_feed_item(source: str, title: str, link: str) -> tuple[str, str, list[str], list[str]]:
    """Fixed rules per official feed -> ``(category, importance, reason_codes, explicit_sectors)``."""
    t = title.lower()
    if source in ("ECB_PRESS", "ECB_STATS"):
        if re.search(r"monetary policy decisions", t):
            return MONETARY_POLICY, HIGH, ["CENTRAL_BANK_RATE_DECISION"], []
        if re.search(r"account of the monetary policy meeting|monetary policy (?:statement|account)", t):
            return MONETARY_POLICY, MEDIUM, ["CENTRAL_BANK_MEETING_RECORD"], []
        if re.search(r"inflation|hicp", t):
            return INFLATION, MEDIUM, ["MACRO_RELEASE_SURPRISE_NOT_ASSESSED"], []
        if "/press/key/" in link:
            if "supervis" in t:
                return REGULATORY, LOW, ["CENTRAL_BANK_SUPERVISION_COMMUNICATION"], []
            return MONETARY_POLICY, LOW, ["CENTRAL_BANK_COMMUNICATION"], []
        return MACRO, LOW, ["ECB_PRESS_RELEASE"], []
    if source == "FED_MONETARY":
        if re.search(r"fomc statement|monetary policy|federal funds rate", t):
            return MONETARY_POLICY, HIGH, ["CENTRAL_BANK_RATE_DECISION"], []
        if re.search(r"economic projections|minutes|implementation note", t):
            return MONETARY_POLICY, MEDIUM, ["CENTRAL_BANK_MEETING_RECORD"], []
        return MONETARY_POLICY, LOW, ["CENTRAL_BANK_COMMUNICATION"], []
    if source == "FED_PRESS":
        if re.search(r"approval of application|enforcement|consent order|reserve bank|stress test", t):
            return REGULATORY, LOW, ["CENTRAL_BANK_SUPERVISION_COMMUNICATION"], []
        return MACRO, LOW, ["FED_PRESS_RELEASE"], []
    if source == "BLS_CPI":
        return INFLATION, MEDIUM, ["MACRO_RELEASE_SURPRISE_NOT_ASSESSED"], []
    if source == "BLS_EMPLOYMENT":
        return LABOR_MARKET, MEDIUM, ["MACRO_RELEASE_SURPRISE_NOT_ASSESSED"], []
    if source == "BEA":
        if re.search(r"personal income and outlays|\bpce\b", t):
            return INFLATION, MEDIUM, ["MACRO_RELEASE_SURPRISE_NOT_ASSESSED"], []
        if re.search(r"\bgdp\b|gross domestic product", t):
            return ECONOMIC_GROWTH, MEDIUM, ["MACRO_RELEASE_SURPRISE_NOT_ASSESSED"], []
        return MACRO, LOW, ["BEA_RELEASE"], []
    if source == "DESTATIS":
        if re.search(r"inflationsrate|verbraucherpreise", t):
            return INFLATION, MEDIUM, ["MACRO_RELEASE_SURPRISE_NOT_ASSESSED"], []
        if re.search(r"bruttoinlandsprodukt|\bbip\b|produktion im|auftragseingang|verarbeitenden gewerbe|industrie", t):
            return ECONOMIC_GROWTH, MEDIUM, ["MACRO_RELEASE_SURPRISE_NOT_ASSESSED"], []
        if re.search(r"erwerbst\w+|arbeitslos\w*|arbeitsmarkt", t):
            return LABOR_MARKET, MEDIUM, ["MACRO_RELEASE_SURPRISE_NOT_ASSESSED"], []
        return MACRO, LOW, ["DESTATIS_PRESS_RELEASE"], []
    if source == "EIA_PRESS":
        return ENERGY, MEDIUM, ["ENERGY_AGENCY_OUTLOOK"], ["ENERGY"]
    if source == "EIA_TODAY":
        return ENERGY, LOW, ["ENERGY_AGENCY_ANALYSIS"], ["ENERGY"]
    return MACRO, LOW, ["UNCLASSIFIED_OFFICIAL_ITEM"], []


def classify_headline(title: str) -> tuple[str, str, str, list[str]]:
    """Explicit keyword rules for secondary-source headlines -> ``(category, importance, confidence, reasons)``.

    Keyword matches are only a category hint (confidence LOW); they never decide a direction."""
    lowered = title.lower()
    for pattern, category, importance, reason in _YAHOO_RULES:
        if re.search(pattern, lowered):
            return category, importance, LOW, [reason, "CLASSIFIED_BY_HEADLINE_KEYWORD"]
    return COMPANY, LOW, LOW, ["COMPANY_NEWS_UNCLASSIFIED"]


def headline_sectors(title: str) -> list[str]:
    lowered = title.lower()
    return sorted({sector for pattern, sector in SECTOR_KEYWORDS if re.search(pattern, lowered)})


# --------------------------------------------------------------------------- event construction
def _event(*, source: str, source_type: str, canonical: str, source_url: Optional[str], published_at: str, category: str, headline: str,
           summary: Optional[str], symbols: Sequence[str], explicit_sectors: Sequence[str], regions: Sequence[str], importance: str,
           impact: str, impact_basis: str, confidence: str, reasons: Sequence[str], fetched_at: str,
           source_event_id: Optional[str] = None, matches: Sequence[Match] = ()) -> dict[str, Any]:
    real = [m for m in matches if m.security_id >= 0]
    return {
        "source_event_id": source_event_id, "security_ids": sorted({m.security_id for m in real}), "matches": [m.primitive() for m in matches],
        "event_id": event_id(source_type, canonical), "published_at": published_at, "event_date": published_at[:10], "source": source,
        "source_type": source_type, "source_url": source_url, "category": category, "headline": headline, "summary": summary,
        "affected_symbols": sorted(set(symbols)), "explicit_sectors": sorted(set(explicit_sectors)), "affected_regions": sorted(set(regions)),
        "importance": importance, "impact": impact, "impact_basis": impact_basis, "confidence": confidence,
        "reason_codes": list(dict.fromkeys(reasons)), "fetched_at": fetched_at,
    }


def feed_events(source: str, items: Sequence[Mapping[str, Any]], *, fetched_at: str) -> list[dict[str, Any]]:
    spec = FEEDS[source]
    events = []
    for item in items:
        category, importance, reasons, sectors = classify_feed_item(source, item["title"], item["link"])
        link = item["link"]
        if link.startswith("/"):
            link = {"EIA_PRESS": "https://www.eia.gov", "EIA_TODAY": "https://www.eia.gov"}.get(source, "") + link
        events.append(_event(
            source=source, source_type="OFFICIAL_FEED", canonical=canonical_url(link), source_url=link, published_at=item["published_at"],
            category=category, headline=item["title"], summary=item.get("summary"), symbols=[], explicit_sectors=sectors, regions=spec["regions"],
            importance=importance, impact=UNKNOWN, impact_basis="NOT_ASSESSABLE_WITHOUT_CONSENSUS", confidence=MEDIUM, reasons=reasons, fetched_at=fetched_at))
    return events


def _classify_news(title: str, publisher: Optional[str]) -> tuple[str, str, str, list[str], list[str]]:
    """Headline-keyword classification of a news item; a company press-release wire is exempt from the secondary-source cap.

    The importance always comes from the same keyword table: a wire does not raise an item, it only is not capped, and an unclassified
    release stays LOW.  Returns ``(category, importance, confidence, reasons, sectors)``."""
    category, importance, confidence, reasons = classify_headline(title)
    if publisher in COMPANY_WIRES:
        confidence = MEDIUM
        reasons = [*reasons, "COMPANY_PRESS_RELEASE"]
    elif importance == HIGH:
        importance = MEDIUM
        reasons = [*reasons, "SECONDARY_SOURCE_CAPPED_AT_MEDIUM"]
    sectors = headline_sectors(title)
    if category == ENERGY and "ENERGY" not in sectors:
        sectors = [*sectors, "ENERGY"]
    return category, importance, confidence, reasons, sectors


def news_events(items: Sequence[Mapping[str, Any]], *, fetched_at: str, alias_index: Optional[AliasIndex] = None) -> list[dict[str, Any]]:
    """Yahoo headlines (cached DB rows or a fresh fetch); ``items`` carry ``symbol`` (Yahoo), ``title``, ``url``, ``published_at``, ``publisher``
    and, for cached rows, the ``security_id`` the provider attached them to (structured metadata; otherwise the Yahoo symbol is resolved as a source identifier)."""
    events = []
    for item in items:
        published = _to_utc_iso(item.get("published_at"))
        if not published or not item.get("title") or not item.get("url"):
            continue
        category, importance, confidence, reasons, sectors = _classify_news(item["title"], item.get("publisher"))
        matches: list[Match] = []
        if item.get("security_id") is not None:
            matches = [Match(int(item["security_id"]), SECURITY_ID, str(item.get("symbol") or item["security_id"]))]
        elif alias_index is not None and item.get("symbol"):
            resolved = alias_index.resolve(source="Yahoo Finance", source_symbol=item["symbol"])
            matches = [resolved] if resolved else []
        events.append(_event(
            source=YAHOO_NEWS, source_type="NEWS", canonical=canonical_url(item["url"]), source_url=item["url"], published_at=published, category=category,
            headline=item["title"], summary=None, symbols=[item["symbol"]] if item.get("symbol") else [], explicit_sectors=sectors, regions=[],
            importance=importance, impact=UNKNOWN, impact_basis="NOT_READ_FROM_HEADLINE", confidence=confidence,
            reasons=[*reasons, *([f"PUBLISHER:{item['publisher']}"] if item.get("publisher") else [])], fetched_at=fetched_at, matches=matches))
    return events


def rss_news_events(
    feed_key: str, spec: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *, fetched_at: str, alias_index: AliasIndex,
    relevant_keys: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """Company-wire RSS items -> events in the same model as every other news source -> ``(events, items without a relevant security)``.

    A wire carries no ticker metadata, so a release is attributed to a security only by explicit identification in its headline or lead
    (ISIN, ``EXCHANGE: TICKER``, full company name; see ``AliasIndex.resolve_text``).  Items that identify no portfolio / watchlist / discovery
    security are not events (a wire carries thousands of unrelated releases)."""
    events: list[dict[str, Any]] = []
    unrelated = 0
    for item in items:
        published = _to_utc_iso(item.get("published_at"))
        if not published or not item.get("title") or not item.get("link"):
            continue
        text = f"{item['title']} {item.get('lead') or item.get('summary') or ''}"
        matches = [m for m in alias_index.resolve_text(text) if (alias_index.key_for(m.security_id) in relevant_keys)]
        if not matches:
            unrelated += 1
            continue
        category, importance, confidence, reasons, sectors = _classify_news(item["title"], spec["publisher"])
        event = _event(
            source=spec["source"], source_type="NEWS", canonical=canonical_url(item["link"]), source_url=item["link"], published_at=published, category=category,
            headline=item["title"], summary=item.get("summary"), symbols=[alias_index.key_for(m.security_id) for m in matches], explicit_sectors=sectors, regions=[],
            importance=importance, impact=UNKNOWN, impact_basis="NOT_READ_FROM_HEADLINE", confidence=confidence,
            reasons=[*reasons, f"PUBLISHER:{spec['publisher']}", *(f"RSS_MATCH_{m.matched_by}" for m in matches)], fetched_at=fetched_at,
            source_event_id=item.get("guid"), matches=matches)
        event["_feed"] = feed_key
        events.append(event)
    return events, unrelated


def sec_events(rows: Sequence[Mapping[str, Any]], *, fetched_at: str) -> list[dict[str, Any]]:
    """Stored SEC 8-K / 6-K events (``event_type`` slug, ``event_date``, ``title``, ``notes``, ``symbol``)."""
    events = []
    for row in rows:
        slug = row.get("event_type")
        if slug in SEC_SKIPPED:
            continue
        category, importance, impact, reason = SEC_EVENT_RULES.get(slug, (COMPANY, LOW, UNKNOWN, "SEC_EVENT_TYPE_UNMAPPED"))
        match = re.search(r"accession=([0-9-]+)", row.get("notes") or "")
        canonical = f"sec:{row['symbol']}:{match.group(1)}:{slug}" if match else f"sec:{row['symbol']}:{slug}:{row['event_date']}:{row.get('title')}"
        events.append(_event(
            source=SEC_FILINGS, source_type="SEC_FILING", canonical=canonical, source_url=None, published_at=f"{row['event_date']}T00:00:00+00:00",
            category=category, headline=row.get("title") or slug, summary=_clean(row.get("notes"), 160), symbols=[row["symbol"]], explicit_sectors=[], regions=[],
            importance=importance, impact=impact, impact_basis="STRUCTURE_OF_PRIMARY_FILING" if impact != UNKNOWN else "NOT_ASSESSABLE", confidence=HIGH if slug in SEC_EVENT_RULES else MEDIUM,
            reasons=[reason], fetched_at=fetched_at,
            matches=[Match(int(row["security_id"]), SECURITY_ID, str(row["symbol"]))] if row.get("security_id") is not None else []))
    return events


def url_key(url: str) -> str:
    """Host (without ``www.``/``m.``/``amp.``) + path, lower case, no query or fragment: the same article under tracking-parameter variants."""
    parsed = urlparse(url.strip())
    return f"{re.sub(r'^(?:www|m|amp)[.]', '', parsed.netloc.lower())}{parsed.path.rstrip('/').lower()}"


def headline_key(headline: str) -> str:
    """Normalized headline for cross-source comparison: no HTML entities, no ticker parentheses, no trailing ``- Publisher``, only letters and digits."""
    text = unescape(headline).lower()
    text = re.sub(r"\(\s*(?:[a-z ]+:\s*)?[a-z0-9.\-]{1,8}\s*\)", " ", text)
    stripped = re.sub(r"\s+[-\u2013\u2014|]\s+[^-\u2013\u2014|]{2,30}$", "", text)
    if len(stripped) >= _MIN_HEADLINE_KEY:
        text = stripped
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _source_priority(event: Mapping[str, Any]) -> int:
    """Which copy represents a merged event: official feed, then SEC filing, then a company wire, then any other news site."""
    if event["source_type"] == "OFFICIAL_FEED":
        return 0
    if event["source_type"] == "SEC_FILING":
        return 1
    return 2 if "COMPANY_PRESS_RELEASE" in event["reason_codes"] else 3


def _hours_apart(a: str, b: str) -> float:
    return abs(datetime.fromisoformat(a).timestamp() - datetime.fromisoformat(b).timestamp()) / 3600.0


def _absorb(rep: dict[str, Any], other: Mapping[str, Any], stats: Optional[dict[str, dict[str, int]]]) -> None:
    """Fold a duplicate into the representative event: symbols, sectors, reasons, security matches united, earliest time, sources remembered."""
    rep["affected_symbols"] = sorted(set(rep["affected_symbols"]) | set(other["affected_symbols"]))
    rep["explicit_sectors"] = sorted(set(rep["explicit_sectors"]) | set(other["explicit_sectors"]))
    rep["reason_codes"] = list(dict.fromkeys([*rep["reason_codes"], *other["reason_codes"]]))
    rep["security_ids"] = sorted(set(rep.get("security_ids") or []) | set(other.get("security_ids") or []))
    known = {(m["security_id"], m["matched_by"], m["matched_alias"]) for m in rep.get("matches") or []}
    rep["matches"] = [*(rep.get("matches") or []), *(m for m in other.get("matches") or [] if (m["security_id"], m["matched_by"], m["matched_alias"]) not in known)]
    rep["published_at"] = min(rep["published_at"], other["published_at"])
    rep["event_date"] = rep["published_at"][:10]
    if other["source"] != rep["source"] or other.get("source_url") != rep.get("source_url"):
        copy = {"source": other["source"], "source_url": other.get("source_url"), "event_id": other["event_id"]}
        if copy not in rep.setdefault("duplicate_sources", []):
            rep["duplicate_sources"].append(copy)
    if stats is not None and other.get("_feed"):
        stats.setdefault(other["_feed"], {}).setdefault("deduplicated", 0)
        stats[other["_feed"]]["deduplicated"] += 1


def merge_duplicates(events: Iterable[dict[str, Any]], *, stats: Optional[dict[str, dict[str, int]]] = None) -> list[dict[str, Any]]:
    """One event per real-world message, deterministic (no embeddings, no LLM).  Order of the comparisons:

    1. the source's own event id (``source`` + ``source_event_id``), or the same ``event_id`` (source type + canonical URL / accession)
    2. the same normalized URL (news only)
    3. the same normalized headline within ``DEDUPE_WINDOW_HOURS`` - within 6 hours if the headline is short (a recurring column title) - (news only), and
       only if the two copies do not belong to different securities

    Symbols, sectors, reasons and security matches are united, the earliest publication time wins, the more important classification
    (e.g. the monetary-policy feed over the generic press feed; a wire over a secondary copy of equal importance) represents the event and
    the other sources stay in ``duplicate_sources``."""
    ordered = sorted(events, key=lambda e: (importance_rank(e["importance"]), _source_priority(e), e["source"], e["published_at"], e["event_id"]))
    reps: list[dict[str, Any]] = []
    by_event_id: dict[str, dict[str, Any]] = {}
    by_source_id: dict[tuple[str, str], dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    by_headline: dict[str, list[dict[str, Any]]] = {}
    for event in ordered:
        news = event["source_type"] == "NEWS"
        rep = by_event_id.get(event["event_id"])
        if rep is None and event.get("source_event_id"):
            rep = by_source_id.get((event["source"], event["source_event_id"]))
        if rep is None and news and event.get("source_url"):
            rep = by_url.get(url_key(event["source_url"]))
        key = headline_key(event["headline"]) if news else ""
        if rep is None and news and len(key) >= _MIN_HEADLINE_KEY:
            for candidate in by_headline.get(key, []):
                disjoint = candidate["affected_symbols"] and event["affected_symbols"] and not set(candidate["affected_symbols"]) & set(event["affected_symbols"])
                window = DEDUPE_WINDOW_HOURS if len(key) >= _SHORT_HEADLINE_KEY else _SHORT_HEADLINE_WINDOW_HOURS
                if not disjoint and _hours_apart(candidate["published_at"], event["published_at"]) <= window:
                    rep = candidate
                    break
        if rep is not None:
            _absorb(rep, event, stats)
            by_event_id.setdefault(event["event_id"], rep)
            continue
        rep = {**event}
        reps.append(rep)
        by_event_id[event["event_id"]] = rep
        if event.get("source_event_id"):
            by_source_id[(event["source"], event["source_event_id"])] = rep
        if news and event.get("source_url"):
            by_url.setdefault(url_key(event["source_url"]), rep)
        if news and len(key) >= _MIN_HEADLINE_KEY:
            by_headline.setdefault(key, []).append(rep)
    return reps


# --------------------------------------------------------------------------- mapping
def map_event(event: dict[str, Any], *, portfolio: Mapping[str, Any], watchlist: Mapping[str, Any], discovery: Mapping[str, Any], sector_map: Mapping[str, str]) -> dict[str, Any]:
    """Add the portfolio / watchlist / discovery mapping and the final importance (pure; returns a new dict)."""
    out = dict(event)
    symbols = set(out["affected_symbols"])
    reasons = list(out["reason_codes"])
    sectors = set(out.pop("explicit_sectors"))
    for symbol in out["affected_symbols"]:
        if symbol in sector_map:
            sectors.add(sector_map[symbol])
    if sectors:
        # only an explicitly sector-scoped event reaches other symbols of that sector; a company event stays with its company
        explicit = {s for s in event["explicit_sectors"]}
        if explicit:
            for universe in (portfolio, watchlist, discovery):
                symbols |= {s for s in universe if sector_map.get(s) in explicit}
            reasons.append("SECTOR_SCOPED_EVENT")
    affected_portfolio = sorted(s for s in symbols if s in portfolio)
    affected_watchlist = sorted(s for s in symbols if s in watchlist)
    affected_discovery = sorted(s for s in symbols if s in discovery)
    importance = out["importance"]
    # an explicit identification in the text (ISIN, "EXCHANGE: TICKER", full company name) is as strong as a headline that names the company
    explicitly_identified = any(m.get("matched_by") in TEXT_MATCHES for m in event.get("matches") or [])
    if out["source_type"] == "NEWS" and out["affected_symbols"] and explicitly_identified:
        reasons.append("HEADLINE_NAMES_COMPANY")
    elif out["source_type"] == "NEWS" and out["affected_symbols"]:
        known = {**discovery, **watchlist, **portfolio}
        linked = [s for s in event["affected_symbols"] if s in known]
        if any(company_mentioned(out["headline"], s, known[s], allow_ticker=False) for s in linked):
            reasons.append("HEADLINE_NAMES_COMPANY")
        elif any(company_mentioned(out["headline"], s, known[s]) for s in linked):
            # a bare ticker can be a homonym ("ACME Supply"); it never carries HIGH
            reasons.append("HEADLINE_NAMES_TICKER_ONLY")
            if importance == HIGH:
                importance = MEDIUM
                reasons.append("TICKER_ONLY_MATCH_CAPPED_AT_MEDIUM")
        elif importance == HIGH:
            importance = MEDIUM
            reasons.append("SYMBOL_LINK_NOT_CONFIRMED_BY_HEADLINE_CAPPED_AT_MEDIUM")
    held_regions = sorted({region for symbol in portfolio for region in symbol_regions(symbol)})
    exposure = sorted(set(out["affected_regions"]) & set(held_regions))
    security_ids = set(out.get("security_ids") or [])
    for symbol in event["affected_symbols"]:
        for universe in (portfolio, watchlist):
            known_id = (universe.get(symbol) or {}).get("security_id")
            if known_id is not None:
                security_ids.add(int(known_id))
    out.update(
        security_ids=sorted(security_ids),
        affected_symbols=sorted(symbols), affected_sectors=sorted(sectors), importance=importance, reason_codes=list(dict.fromkeys(reasons)),
        affected_portfolio_symbols=affected_portfolio, affected_watchlist_symbols=affected_watchlist, affected_discovery_symbols=affected_discovery,
        portfolio_region_exposure=exposure,
    )
    return out


def _sort_key(event: Mapping[str, Any]):
    return (importance_rank(event["importance"]), 0 if event["affected_portfolio_symbols"] else 1, 0 if event["affected_watchlist_symbols"] else 1, _neg_ts(event["published_at"]), event["event_id"])


def _neg_ts(value: str) -> float:
    return -datetime.fromisoformat(value).timestamp()


def build_market_intelligence(
    *,
    evaluation_as_of: str,
    fetched_at: str,
    feed_items: Mapping[str, Sequence[Mapping[str, Any]]],
    news_items: Sequence[Mapping[str, Any]],
    sec_rows: Sequence[Mapping[str, Any]],
    portfolio: Mapping[str, Any],
    watchlist: Mapping[str, Any],
    discovery: Mapping[str, Any],
    sector_map: Mapping[str, str],
    config: Optional[MarketIntelligenceConfig] = None,
    rss_items: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
    news_feeds: Optional[Mapping[str, Mapping[str, Any]]] = None,
    alias_index: Optional[AliasIndex] = None,
) -> dict[str, Any]:
    """Pure: classify and map everything that was handed in; the ``fetched_at`` stamp is passed in, no clock is read.

    ``rss_items`` (company-wire feeds, ``{feed key: parsed items}`` with their specs in ``news_feeds``) run through the same pipeline as every other
    source; ``alias_index`` is the Security-Master view that resolves symbols, ISINs and names to a ``security_id``."""
    cfg = config or MarketIntelligenceConfig()
    since = (date.fromisoformat(evaluation_as_of) - timedelta(days=cfg.lookback_days)).isoformat()
    base_index = alias_index or AliasIndex(())
    events: list[dict[str, Any]] = []
    for source, items in feed_items.items():
        events += feed_events(source, items, fetched_at=fetched_at)
    events += news_events(news_items, fetched_at=fetched_at, alias_index=base_index)
    events += sec_events(sec_rows, fetched_at=fetched_at)
    feed_stats: dict[str, dict[str, int]] = {}
    if rss_items:
        # discovery candidates are not in the Security Master: they take part in the matching as entries with negative ids
        pseudo = [SecurityAlias(-(n + 1), symbol, (symbol,), None, None, str(info.get("name") or ""), (("YAHOO", symbol),)) for n, (symbol, info) in enumerate(sorted(discovery.items()))
                  if base_index.resolve(symbol=symbol) is None]
        index = base_index.extended(pseudo)
        relevant = {**discovery, **watchlist, **portfolio}
        for key, items in rss_items.items():
            rss, unrelated = rss_news_events(key, (news_feeds or {})[key], items, fetched_at=fetched_at, alias_index=index, relevant_keys=relevant)
            events += rss
            feed_stats[key] = {"fetched": len(items), "unrelated": unrelated, "accepted": 0, "deduplicated": 0}
    events = [e for e in events if since <= e["event_date"] <= evaluation_as_of]
    for event in events:
        if event.get("_feed"):
            feed_stats[event["_feed"]]["accepted"] += 1
    merged = merge_duplicates(events, stats=feed_stats)
    for event in merged:
        event.pop("_feed", None)
    mapped = [map_event(e, portfolio=portfolio, watchlist=watchlist, discovery=discovery, sector_map=sector_map) for e in merged]
    mapped.sort(key=_sort_key)
    source_counts: dict[str, int] = {}
    for event in mapped:
        source_counts[event["source"]] = source_counts.get(event["source"], 0) + 1
    count = lambda importance: sum(1 for e in mapped if e["importance"] == importance)
    return {
        "status": "AVAILABLE",
        "read_only": True,
        "orders_created": False,
        "signals_changed": False,
        "evaluation_as_of": evaluation_as_of,
        "lookback_days": cfg.lookback_days,
        "events": mapped,
        "source_counts": dict(sorted(source_counts.items())),
        "event_count": len(mapped),
        "high_count": count(HIGH),
        "medium_count": count(MEDIUM),
        "low_count": count(LOW),
        "portfolio_relevant_count": sum(1 for e in mapped if e["affected_portfolio_symbols"]),
        "watchlist_relevant_count": sum(1 for e in mapped if e["affected_watchlist_symbols"]),
        "discovery_relevant_count": sum(1 for e in mapped if e["affected_discovery_symbols"]),
        "macro_event_count": sum(1 for e in mapped if e["category"] in MACRO_CATEGORIES),
        "methodology": {
            "chain": "NEWS != SIGNAL != BUY != SELL",
            "category": "fixed rules per source (official feed type, SEC form item, explicit headline keywords for secondary news)",
            "importance": "fixed table; scheduled macro releases are MEDIUM because a surprise cannot be assessed without consensus data; HIGH needs a primary source, a company press-release wire, or a headline-keyword class whose headline names the linked company; keyword-only secondary headlines are capped at MEDIUM; GEOPOLITICS is at most MEDIUM because no primary geopolitics source exists",
            "impact": "UNKNOWN unless the structure of a primary source states an adverse fact (SEC delisting notice, cybersecurity incident, non-reliance); never read from a headline; POSITIVE/MIXED/NEUTRAL are not produced in v1",
            "mapping": "by security (security_id); a symbol is only one alias of it; explicitly sector-scoped events reach the symbols of that sector (static sector map); macro events carry the regions of the held symbols (portfolio_region_exposure)",
            "security_identity": "security_id first (structured provider metadata), then source identifier, ISIN, WKN, exact alias; a company-wire release is attributed only by an explicit ISIN, 'EXCHANGE: TICKER' or full company name, never by a bare ticker word",
            "deduplication": "same source event id / event id, then same normalized URL, then same normalized headline within 48 h (not for different securities); deterministic, the other sources stay in duplicate_sources",
            "confidence": "HIGH structured primary filing, MEDIUM official feed with a title rule, LOW keyword on a secondary headline",
            "not_used": ["Eurostat dataset-update feed (not news)", "commodity prices (no source)", "LLM text"],
            "signal_changes": "none: news never changes SELL/TRIM/HOLD, PROMOTE, ENTRY_READY, ranking or sizing",
        },
        "lookback_since": since,
        "news_feed_stats": dict(sorted(feed_stats.items())),
        # the securities of the events plus every portfolio / watchlist security, so that any alias or ISIN of them resolves in the DB-free reader
        "aliases": base_index.report_table([*(i for e in mapped for i in e["security_ids"]),
                                            *(int(v["security_id"]) for universe in (portfolio, watchlist) for v in universe.values() if v.get("security_id") is not None)]),
    }


# --------------------------------------------------------------------------- read-only database context
def load_db_context(conn: sqlite3.Connection, *, since: str, until: str) -> dict[str, Any]:
    """Portfolio / watchlist membership and the cached company news / SEC events of the window (production DB: read only)."""
    index = AliasIndex.from_connection(conn)
    securities = {int(r["id"]): r for r in conn.execute("SELECT id, symbol, name, asset_type FROM security")}

    def key(security_id: int) -> Optional[str]:
        return index.key_for(security_id)  # the Yahoo symbol of the security, else its public symbol

    portfolio: dict[str, dict[str, Any]] = {}
    watchlist: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT security_id FROM positions WHERE shares > 0"):
        symbol = key(int(row[0]))
        if symbol:
            portfolio[symbol] = {"security_id": int(row[0]), "symbol": securities[int(row[0])]["symbol"], "name": securities[int(row[0])]["name"]}
    for row in conn.execute("SELECT security_id FROM watchlist"):
        symbol = key(int(row[0]))
        if symbol:
            watchlist[symbol] = {"security_id": int(row[0]), "symbol": securities[int(row[0])]["symbol"], "name": securities[int(row[0])]["name"]}
    news = []
    for row in conn.execute("SELECT security_id, published_at, title, url, publisher FROM news WHERE substr(published_at, 1, 10) >= ? AND substr(published_at, 1, 10) <= ?", (since, until)):
        symbol = key(int(row["security_id"]))
        if symbol:
            news.append({"symbol": symbol, "security_id": int(row["security_id"]), "published_at": row["published_at"], "title": row["title"], "url": row["url"], "publisher": row["publisher"]})
    sec = []
    for row in conn.execute("SELECT security_id, event_type, event_date, title, notes FROM events WHERE event_date >= ? AND event_date <= ?", (since, until)):
        symbol = key(int(row["security_id"]))
        if symbol:
            sec.append({"symbol": symbol, "security_id": int(row["security_id"]), "event_type": row["event_type"], "event_date": row["event_date"], "title": row["title"], "notes": row["notes"]})
    news.sort(key=lambda r: (r["published_at"], r["symbol"], r["url"]))
    sec.sort(key=lambda r: (r["event_date"], r["symbol"], r["event_type"], r["title"] or ""))
    return {"portfolio": portfolio, "watchlist": watchlist, "news": news, "sec_events": sec, "alias_index": index}
