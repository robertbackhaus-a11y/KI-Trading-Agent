"""Backfill the `events` and `news` tables that back EventRiskAnalysis.

Two independent, already-registered sources, one per table:
- events: SEC EDGAR company-submissions JSON (8-K filings only), the same
  `data.sec.gov` host/User-Agent-identification convention already used by
  Backfill-TradingFundamentalsSEC.py. Only securities with a resolved SEC CIK
  (source_symbols) are covered.
- news: Yahoo Finance's public search endpoint (`v1/finance/search`), the
  same unauthenticated query1.finance.yahoo.com host already used by
  Backfill-TradingMarketData.py. Covers every relevant security (any
  asset_type) that has a resolved Yahoo symbol.

Both are read-only against the source, dry-run by default, and require
--write to persist. No estimates/ratings/price_targets here -- those need a
paid data plan or browser-authenticated session, neither of which exists in
this repo (see docs/trading-agent-architecture.md, known inconsistency #4).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DB_PATH = Path(r"C:\tools\trading\data\trading.db")

# SEC fair access: set SEC_USER_AGENT to "<name> <your e-mail>"; the default below is only a placeholder.
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "Trading-Agent contact@example.com")
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

REQUEST_TIMEOUT_SECONDS = 10
REQUEST_DELAY_SECONDS = 0.20
MAX_RETRIES = 1

NEWS_COUNT_PER_SECURITY = 10

# Same 2-year lookback convention already used for market_data
# (Backfill-TradingMarketData.py's HISTORY_RANGE) -- old filings still exist
# in the source but are not useful "event risk" signal today.
EVENTS_LOOKBACK_DAYS = 730

# Official SEC Form 8-K item codes -> a short event_type slug and the SEC's
# own item title (https://www.sec.gov/files/form8-k.pdf). Only items that
# have actually been observed to matter for the covered securities are
# mapped; anything else keeps its raw item code as event_type rather than
# guessing a category.
SEC_8K_ITEM_EVENT_TYPES = {
    "1.01": "material_agreement",
    "1.02": "material_agreement_termination",
    "2.01": "acquisition_disposition",
    "2.02": "earnings",
    "2.03": "financial_obligation",
    "2.05": "exit_disposal_costs",
    "3.01": "delisting_notice",
    "3.03": "security_holder_rights_change",
    "4.01": "accountant_change",
    "5.01": "control_change",
    "5.02": "officer_director_change",
    "5.03": "bylaws_amendment",
    "5.07": "shareholder_vote",
    "7.01": "regulation_fd_disclosure",
    "8.01": "other_material_event",
}
SEC_8K_ITEM_TITLES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "3.01": "Notice of Delisting",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}
# 9.01 ("Financial Statements and Exhibits") almost always accompanies a
# substantive item and is never itself the primary reason for a filing.
SEC_8K_NON_PRIMARY_ITEMS = {"9.01"}


# ============================================================
# DATABASE
# ============================================================

def connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"Trading DB not found: {path}")
    if write:
        conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    else:
        conn = sqlite3.connect(f"file:///{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def validate_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if row is None or row["value"] != "2.0":
        raise RuntimeError(f"Expected schema 2.0, got {row['value'] if row else None}")


def get_source_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM data_sources WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise RuntimeError(f"{name} data source missing")
    return row["id"]


def get_sec_covered_securities(conn: sqlite3.Connection, sec_source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT DISTINCT s.id, s.name, s.symbol, ss.symbol AS cik
        FROM security s
        JOIN source_symbols ss ON ss.security_id = s.id AND ss.source_id = ?
        LEFT JOIN positions p ON p.security_id = s.id
        LEFT JOIN watchlist w ON w.security_id = s.id
        WHERE s.active = 1
          AND LOWER(s.asset_type) = 'stock'
          AND (p.shares > 0 OR w.security_id IS NOT NULL)
        ORDER BY s.name
        """,
        (sec_source_id,),
    ).fetchall()


def get_yahoo_covered_securities(conn: sqlite3.Connection, yahoo_source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT DISTINCT s.id, s.name, s.symbol, ss.symbol AS yahoo_symbol
        FROM security s
        JOIN source_symbols ss ON ss.security_id = s.id AND ss.source_id = ?
        LEFT JOIN positions p ON p.security_id = s.id
        LEFT JOIN watchlist w ON w.security_id = s.id
        WHERE s.active = 1
          AND (p.shares > 0 OR w.security_id IS NOT NULL)
        ORDER BY s.name
        """,
        (yahoo_source_id,),
    ).fetchall()


# ============================================================
# HTTP
# ============================================================

def get_json(url: str, headers: dict) -> Optional[dict]:
    attempts = MAX_RETRIES + 1
    last_exc: Optional[Exception] = None
    for _ in range(attempts):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last_exc = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    return None


# ============================================================
# EVENTS (SEC EDGAR 8-K)
# ============================================================

def fetch_sec_events(cik: str) -> list[dict]:
    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    data = get_json(url, SEC_HEADERS)
    if data is None:
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    items_list = recent.get("items", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    cutoff = (date.today() - timedelta(days=EVENTS_LOOKBACK_DAYS)).isoformat()

    events: list[dict] = []
    for i, form in enumerate(forms):
        if form not in ("8-K", "6-K"):
            continue
        filing_date_str = filing_dates[i] if i < len(filing_dates) else None
        if not filing_date_str or filing_date_str < cutoff:
            continue
        accession = accession_numbers[i] if i < len(accession_numbers) else None
        doc = primary_docs[i] if i < len(primary_docs) else None
        event_date = filing_date_str
        period_end = report_dates[i] if i < len(report_dates) and report_dates[i] else None

        if form == "8-K":
            raw_items = (items_list[i] if i < len(items_list) else "") or ""
            item_codes = [c.strip() for c in raw_items.split(",") if c.strip()]
            primary_items = [c for c in item_codes if c not in SEC_8K_NON_PRIMARY_ITEMS] or item_codes
            primary_item = primary_items[0] if primary_items else None
            event_type = SEC_8K_ITEM_EVENT_TYPES.get(primary_item, f"sec_8k_{primary_item}" if primary_item else "sec_8k_filing")
            titles = [SEC_8K_ITEM_TITLES.get(c, c) for c in item_codes] or ["8-K Filing"]
            title = "8-K: " + "; ".join(titles)
            notes = f"accession={accession}; doc={doc}; items={raw_items}"
        else:
            # 6-K = "Report of Foreign Private Issuer", the FPI equivalent of
            # an 8-K. It carries no SEC item codes, so unlike 8-K this is
            # deliberately not sub-classified by content (that would require
            # guessing from the filename) -- SEC's own form-type label is the
            # only thing taken from the source.
            event_type = "foreign_issuer_report"
            title = "6-K: Report of Foreign Private Issuer"
            notes = f"accession={accession}; doc={doc}"

        events.append(
            {
                "event_type": event_type,
                "event_date": event_date,
                "period_end": period_end,
                "title": title,
                "actual_value": None,
                "estimated_value": None,
                "surprise_percent": None,
                "currency": None,
                "notes": notes,
            }
        )
    return events


def store_events(conn: sqlite3.Connection, security_id: int, source_id: int, events: list[dict]) -> int:
    conn.execute("DELETE FROM events WHERE security_id = ? AND source_id = ?", (security_id, source_id))
    conn.executemany(
        """
        INSERT INTO events(
            security_id, event_type, event_date, period_end, title,
            actual_value, estimated_value, surprise_percent, currency, notes, source_id
        ) VALUES (
            :security_id, :event_type, :event_date, :period_end, :title,
            :actual_value, :estimated_value, :surprise_percent, :currency, :notes, :source_id
        )
        """,
        [{**e, "security_id": security_id, "source_id": source_id} for e in events],
    )
    return len(events)


# ============================================================
# NEWS (Yahoo Finance search)
# ============================================================

def _search_yahoo_news(query: str) -> list[dict]:
    params = {"q": query, "newsCount": str(NEWS_COUNT_PER_SECURITY), "quotesCount": "0"}
    url = f"https://query1.finance.yahoo.com/v1/finance/search?{urllib.parse.urlencode(params)}"
    data = get_json(url, YAHOO_HEADERS)
    if data is None:
        return []
    return data.get("news", []) or []


def fetch_yahoo_news(symbol: str, *, name: Optional[str] = None) -> list[dict]:
    # Some tickers (mostly non-US listings) return nothing when searched by
    # their exact Yahoo symbol; falling back to the security's own
    # registered company name is still the same source/endpoint, just a
    # second real query against it -- not a different data source.
    items = _search_yahoo_news(symbol)
    if not items and name:
        items = _search_yahoo_news(name)
    news: list[dict] = []
    for item in items:
        publish_ts = item.get("providerPublishTime")
        published_at = (
            datetime.fromtimestamp(publish_ts, tz=timezone.utc).isoformat()
            if publish_ts is not None
            else None
        )
        title = item.get("title")
        url_ = item.get("link")
        if not published_at or not title or not url_:
            continue
        news.append(
            {
                "published_at": published_at,
                "title": title,
                "summary": None,
                "url": url_,
                "publisher": item.get("publisher"),
                "category": None,
                "sentiment": None,
                "is_material": 0,
                "expires_at": None,
            }
        )
    return news


def store_news(conn: sqlite3.Connection, security_id: int, source_id: int, news_items: list[dict]) -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    written = 0
    for item in news_items:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO news(
                security_id, published_at, title, summary, url, publisher,
                category, sentiment, is_material, expires_at, source_id, fetched_at
            ) VALUES (
                :security_id, :published_at, :title, :summary, :url, :publisher,
                :category, :sentiment, :is_material, :expires_at, :source_id, :fetched_at
            )
            """,
            {**item, "security_id": security_id, "source_id": source_id, "fetched_at": fetched_at},
        )
        if cur.rowcount > 0:
            written += 1
    return written


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill events (SEC 8-K) and news (Yahoo search), dry-run by default")
    parser.add_argument("--db-path", "--db", dest="db_path", type=Path, default=DB_PATH)
    parser.add_argument("--write", action="store_true", help="apply the writes")
    parser.add_argument("--skip-events", action="store_true")
    parser.add_argument("--skip-news", action="store_true")
    args = parser.parse_args()

    conn = connect(args.db_path, write=args.write)
    try:
        validate_schema(conn)
        sec_source_id = get_source_id(conn, "SEC EDGAR")
        yahoo_source_id = get_source_id(conn, "Yahoo Finance")

        print("=" * 60)
        print(" Trading Events/News Backfill v1")
        print("=" * 60)
        print(f"Mode: {'WRITE' if args.write else 'DRY-RUN'}")
        print()

        event_totals = {"securities": 0, "events_found": 0, "events_written": 0, "failed": []}
        if not args.skip_events:
            print("--- EVENTS (SEC EDGAR 8-K) ---")
            for security in get_sec_covered_securities(conn, sec_source_id):
                print(f"[{security['symbol']}] {security['name']} (CIK {security['cik']})")
                try:
                    events = fetch_sec_events(security["cik"])
                except Exception as exc:  # noqa: BLE001
                    print(f"    -> ERROR: {exc}")
                    event_totals["failed"].append({"id": security["id"], "name": security["name"], "error": str(exc)})
                    time.sleep(REQUEST_DELAY_SECONDS)
                    continue
                print(f"    -> {len(events)} 8-K filing(s) found")
                event_totals["securities"] += 1
                event_totals["events_found"] += len(events)
                if args.write:
                    written = store_events(conn, security["id"], sec_source_id, events)
                    event_totals["events_written"] += written
                time.sleep(REQUEST_DELAY_SECONDS)
            print()

        news_totals = {"securities": 0, "news_found": 0, "news_written": 0, "failed": []}
        if not args.skip_news:
            print("--- NEWS (Yahoo Finance search) ---")
            for security in get_yahoo_covered_securities(conn, yahoo_source_id):
                print(f"[{security['symbol']}] {security['name']} (Yahoo {security['yahoo_symbol']})")
                try:
                    news_items = fetch_yahoo_news(security["yahoo_symbol"], name=security["name"])
                except Exception as exc:  # noqa: BLE001
                    print(f"    -> ERROR: {exc}")
                    news_totals["failed"].append({"id": security["id"], "name": security["name"], "error": str(exc)})
                    time.sleep(REQUEST_DELAY_SECONDS)
                    continue
                print(f"    -> {len(news_items)} article(s) found")
                news_totals["securities"] += 1
                news_totals["news_found"] += len(news_items)
                if args.write:
                    written = store_news(conn, security["id"], yahoo_source_id, news_items)
                    news_totals["news_written"] += written
                    print(f"    -> {written} new row(s) written (rest already present)")
                time.sleep(REQUEST_DELAY_SECONDS)
            print()

        print("=" * 60)
        print("Summary")
        print("=" * 60)
        print(f"Events: {event_totals['securities']} securities, {event_totals['events_found']} filings found"
              + (f", {event_totals['events_written']} rows written" if args.write else " (dry-run, nothing written)"))
        if event_totals["failed"]:
            print(f"  Failed: {event_totals['failed']}")
        print(f"News:   {news_totals['securities']} securities, {news_totals['news_found']} articles found"
              + (f", {news_totals['news_written']} new rows written" if args.write else " (dry-run, nothing written)"))
        if news_totals["failed"]:
            print(f"  Failed: {news_totals['failed']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
