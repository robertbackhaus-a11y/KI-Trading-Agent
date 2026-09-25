"""Backfill ECB euro foreign-exchange reference rates.

The tool uses only the official ECB EXR API. Database mutation is deliberately
opt-in through ``--write``; otherwise fetched rows are printed as a dry-run.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import ssl
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
ECB_SOURCE = "ECB"
ECB_API_ROOT = "https://data-api.ecb.europa.eu/service/data/EXR"


def _trusted_ssl_context() -> ssl.SSLContext:
    """Use normal CA validation, augmenting it with Windows trusted roots.

    The bundled Python runtime may not ship a populated CA bundle even though
    Windows does.  This never disables certificate verification.
    """

    context = ssl.create_default_context()
    if hasattr(ssl, "enum_certificates"):
        try:
            certificates = []
            for store_name in ("ROOT", "CA"):
                certificates.extend(ssl.enum_certificates(store_name))
            pem_roots = "\n".join(
                ssl.DER_cert_to_PEM_cert(entry[0])
                for entry in certificates
                if len(entry) >= 3 and entry[2]
            )
            if pem_roots:
                context.load_verify_locations(cadata=pem_roots)
        except (OSError, ssl.SSLError):
            pass
    return context


def _currency(value: str) -> str:
    result = value.strip().upper()
    if len(result) != 3 or not result.isalpha():
        raise ValueError("quote currency must be a three-letter ISO-style code")
    if result == "EUR":
        raise ValueError("EUR is the ECB base currency and cannot be a quote currency")
    return result


def _date(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def build_ecb_url(
    quote_currency: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    latest: bool = False,
) -> str:
    quote = _currency(quote_currency)
    params: dict[str, str] = {"format": "csvdata"}
    if latest:
        params["lastNObservations"] = "1"
    else:
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start and end and end < start:
            raise ValueError("end_date must be on or after start_date")
        if start:
            params["startPeriod"] = start
        if end:
            params["endPeriod"] = end
    return f"{ECB_API_ROOT}/D.{quote}.EUR.SP00.A?{urlencode(params)}"


def parse_ecb_csv(payload: str, quote_currency: str) -> list[dict]:
    """Parse only daily ECB ``EUR -> quote`` reference-rate rows."""

    quote = _currency(quote_currency)
    rows: list[dict] = []
    for row in csv.DictReader(io.StringIO(payload)):
        if row.get("FREQ") != "D":
            continue
        if row.get("CURRENCY") != quote or row.get("CURRENCY_DENOM") != "EUR":
            continue
        rate_date = _date(row.get("TIME_PERIOD"), "ECB TIME_PERIOD")
        value = row.get("OBS_VALUE")
        if rate_date is None or value is None:
            continue
        rate = float(value)
        if rate <= 0:
            raise ValueError("ECB returned a non-positive FX rate")
        rows.append(
            {
                "rate_date": rate_date,
                "base_currency": "EUR",
                "quote_currency": quote,
                "rate": rate,
                "source": ECB_SOURCE,
            }
        )
    return sorted(rows, key=lambda item: item["rate_date"])


def fetch_ecb_rates(
    quote_currency: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    latest: bool = False,
) -> tuple[str, list[dict]]:
    url = build_ecb_url(
        quote_currency, start_date=start_date, end_date=end_date, latest=latest
    )
    request = Request(url, headers={"Accept": "text/csv", "User-Agent": "Trading-Agent/Phase-3A.5"})
    with urlopen(request, timeout=30, context=_trusted_ssl_context()) as response:  # nosec B310 - fixed official ECB URL
        payload = response.read().decode("utf-8")
    return url, parse_ecb_csv(payload, quote_currency)


def _require_fx_feature(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fx_rates'"
    ).fetchone()
    marker = conn.execute(
        "SELECT value FROM metadata WHERE key = 'fx_rates_schema_version'"
    ).fetchone()
    if table is None or marker is None or marker[0] != "1":
        raise RuntimeError("fx_rates migration feature version 1 is required")


def upsert_rates(conn: sqlite3.Connection, rows: Iterable[dict], *, fetched_at: Optional[str] = None) -> int:
    _require_fx_feature(conn)
    timestamp = fetched_at or datetime.now(timezone.utc).isoformat()
    values = list(rows)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            """
            INSERT INTO fx_rates(rate_date, base_currency, quote_currency, rate, source, fetched_at)
            VALUES (:rate_date, :base_currency, :quote_currency, :rate, :source, :fetched_at)
            ON CONFLICT(rate_date, base_currency, quote_currency, source) DO UPDATE SET
                rate = excluded.rate,
                fetched_at = excluded.fetched_at
            """,
            [{**row, "fetched_at": timestamp} for row in values],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(values)


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        return sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    return sqlite3.connect(f"file:///{path.as_posix()}?mode=ro", uri=True, timeout=10.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill official ECB FX rates (dry-run by default)")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--quote-currency", default="USD")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if not args.db_path.exists():
        raise FileNotFoundError(f"Trading DB not found: {args.db_path}")
    if args.latest and (args.start_date or args.end_date):
        raise ValueError("--latest cannot be combined with --start-date or --end-date")
    url, rows = fetch_ecb_rates(
        args.quote_currency,
        start_date=args.start_date,
        end_date=args.end_date,
        latest=args.latest or not (args.start_date or args.end_date),
    )
    conn = _connect(args.db_path, write=args.write)
    try:
        try:
            _require_fx_feature(conn)
            schema_status: dict[str, object] = {"database_ready": True}
        except RuntimeError as exc:
            if args.write:
                raise
            schema_status = {"database_ready": False, "database_note": str(exc)}
        report = {
            "source": ECB_SOURCE,
            "url": url,
            "row_count": len(rows),
            "would_upsert": rows,
            **schema_status,
        }
        if args.write:
            report["written"] = upsert_rates(conn, rows)
        else:
            report["dry_run"] = True
        print(json.dumps(report, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
