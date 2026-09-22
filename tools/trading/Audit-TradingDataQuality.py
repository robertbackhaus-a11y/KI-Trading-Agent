from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")

CORE_FIELDS = [
    "revenue",
    "net_income",
    "operating_cash_flow",
    "cash",
]

NOT_APPLICABLE_ASSET_TYPES = {
    "etf",
    "fund",
}


# ============================================================
# DB
# ============================================================

def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Trading DB not found: {DB_PATH}")

    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=10.0,
        isolation_level=None,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")

    return conn


def validate_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT value
        FROM metadata
        WHERE key = 'schema_version'
        """
    ).fetchone()

    if row is None:
        raise RuntimeError("metadata.schema_version missing")

    if row["value"] != "2.0":
        raise RuntimeError(
            f"Expected schema 2.0, got {row['value']}"
        )


# ============================================================
# RELEVANT SECURITIES
# ============================================================

def get_securities(
    conn: sqlite3.Connection,
    scope: str,
) -> list[sqlite3.Row]:

    if scope == "all":
        where_clause = "s.active = 1"
    else:
        where_clause = """
        s.active = 1
        AND (
            EXISTS (
                SELECT 1
                FROM positions p
                WHERE p.security_id = s.id
                  AND COALESCE(p.shares, 0) <> 0
            )
            OR EXISTS (
                SELECT 1
                FROM watchlist w
                WHERE w.security_id = s.id
            )
        )
        """

    return conn.execute(
        f"""
        SELECT
            s.id,
            s.symbol,
            s.name,
            s.asset_type,
            s.exchange,
            s.currency,
            CASE WHEN EXISTS (
                SELECT 1
                FROM positions p
                WHERE p.security_id = s.id
                  AND COALESCE(p.shares, 0) <> 0
            ) THEN 1 ELSE 0 END AS in_portfolio,
            CASE WHEN EXISTS (
                SELECT 1
                FROM watchlist w
                WHERE w.security_id = s.id
            ) THEN 1 ELSE 0 END AS in_watchlist
        FROM security s
        WHERE {where_clause}
        ORDER BY s.name
        """
    ).fetchall()


# ============================================================
# DATA STATUS
# ============================================================

def exists_for_security(
    conn: sqlite3.Connection,
    table: str,
    security_id: int,
) -> bool:
    return conn.execute(
        f"""
        SELECT 1
        FROM {table}
        WHERE security_id = ?
        LIMIT 1
        """,
        (security_id,),
    ).fetchone() is not None


def get_fundamentals_stats(
    conn: sqlite3.Connection,
    security_id: int,
) -> dict:

    row = conn.execute(
        """
        SELECT
            COUNT(*) AS periods,
            MAX(period_end) AS latest_period,

            SUM(CASE WHEN revenue IS NOT NULL THEN 1 ELSE 0 END)
                AS revenue_count,

            SUM(CASE WHEN net_income IS NOT NULL THEN 1 ELSE 0 END)
                AS net_income_count,

            SUM(CASE WHEN operating_cash_flow IS NOT NULL THEN 1 ELSE 0 END)
                AS operating_cash_flow_count,

            SUM(CASE WHEN capex IS NOT NULL THEN 1 ELSE 0 END)
                AS capex_count,

            SUM(CASE WHEN free_cash_flow IS NOT NULL THEN 1 ELSE 0 END)
                AS free_cash_flow_count,

            SUM(CASE WHEN cash IS NOT NULL THEN 1 ELSE 0 END)
                AS cash_count,

            SUM(CASE WHEN total_debt IS NOT NULL THEN 1 ELSE 0 END)
                AS total_debt_count

        FROM fundamentals
        WHERE security_id = ?
        """,
        (security_id,),
    ).fetchone()

    return dict(row)


def age_days(period_end: str | None) -> int | None:
    if not period_end:
        return None

    d = datetime.strptime(period_end, "%Y-%m-%d").date()
    return (date.today() - d).days


def classify(
    security: sqlite3.Row,
    stats: dict,
    stale_days: int,
) -> tuple[str, list[str]]:

    reasons: list[str] = []

    asset_type = (security["asset_type"] or "").lower()

    if asset_type in NOT_APPLICABLE_ASSET_TYPES:
        return "NOT_APPLICABLE", []

    periods = stats["periods"] or 0

    if periods == 0:
        return "MISSING", ["no fundamentals rows"]

    latest_period = stats["latest_period"]
    age = age_days(latest_period)

    if age is not None and age > stale_days:
        reasons.append(
            f"latest fundamentals {latest_period} ({age} days old)"
        )

    missing_fields = []

    for field in CORE_FIELDS:
        count = stats[f"{field}_count"] or 0
        if count == 0:
            missing_fields.append(field)

    if missing_fields:
        reasons.append(
            "never populated: " + ", ".join(missing_fields)
        )

    ocf_count = stats["operating_cash_flow_count"] or 0
    capex_count = stats["capex_count"] or 0
    fcf_count = stats["free_cash_flow_count"] or 0

    if (
        ocf_count > 0
        and capex_count > 0
        and fcf_count == 0
    ):
        reasons.append(
            "free_cash_flow missing although OCF and capex are populated"
        )

    if reasons:
        if age is not None and age > stale_days:
            return "STALE", reasons

        return "PARTIAL", reasons

    return "COMPLETE", []


# ============================================================
# AUDIT
# ============================================================

def audit(
    conn: sqlite3.Connection,
    scope: str,
    stale_days: int,
) -> list[dict]:

    results = []

    for security in get_securities(conn, scope):

        security_id = security["id"]

        stats = get_fundamentals_stats(
            conn,
            security_id,
        )

        status, reasons = classify(
            security,
            stats,
            stale_days,
        )

        results.append(
            {
                "security_id": security_id,
                "symbol": security["symbol"],
                "name": security["name"],
                "asset_type": security["asset_type"],
                "in_portfolio": bool(security["in_portfolio"]),
                "in_watchlist": bool(security["in_watchlist"]),
                "has_source_symbols": exists_for_security(
                    conn,
                    "source_symbols",
                    security_id,
                ),
                "has_market_data": exists_for_security(
                    conn,
                    "market_data",
                    security_id,
                ),
                "fundamentals_status": status,
                "fundamentals_periods": stats["periods"] or 0,
                "latest_period": stats["latest_period"],
                "reasons": reasons,
            }
        )

    return results


# ============================================================
# OUTPUT
# ============================================================

def filter_results(
    results: list[dict],
    only_problems: bool,
) -> list[dict]:

    if not only_problems:
        return results

    return [
        r for r in results
        if r["fundamentals_status"]
        not in {"COMPLETE", "NOT_APPLICABLE"}
        or not r["has_source_symbols"]
        or not r["has_market_data"]
    ]


def print_table(
    results: list[dict],
) -> None:

    print(
        f"{'ID':>4} "
        f"{'SYM':<10} "
        f"{'P':>1} "
        f"{'W':>1} "
        f"{'SRC':>3} "
        f"{'MKT':>3} "
        f"{'FUND':<15} "
        f"{'N':>3} "
        f"{'LATEST':<10} "
        f"NAME"
    )

    print("-" * 120)

    for r in results:
        print(
            f"{r['security_id']:>4} "
            f"{(r['symbol'] or '-'):10} "
            f"{int(r['in_portfolio']):>1} "
            f"{int(r['in_watchlist']):>1} "
            f"{int(r['has_source_symbols']):>3} "
            f"{int(r['has_market_data']):>3} "
            f"{r['fundamentals_status']:<15} "
            f"{r['fundamentals_periods']:>3} "
            f"{(r['latest_period'] or '-'):10} "
            f"{r['name']}"
        )

        for reason in r["reasons"]:
            print(f"     -> {reason}")

    print()
    print("SUMMARY")
    print("-------")
    print("Securities          :", len(results))

    for status in [
        "COMPLETE",
        "PARTIAL",
        "STALE",
        "MISSING",
        "NOT_APPLICABLE",
    ]:
        print(
            f"{status:<20}:",
            sum(
                r["fundamentals_status"] == status
                for r in results
            ),
        )

    print(
        "Missing source       :",
        sum(not r["has_source_symbols"] for r in results),
    )

    print(
        "Missing market data  :",
        sum(not r["has_market_data"] for r in results),
    )


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description="Audit Trading DB data quality"
    )

    parser.add_argument(
        "--scope",
        choices=["relevant", "all"],
        default="relevant",
        help="relevant = active positions + watchlist",
    )

    parser.add_argument(
        "--stale-days",
        type=int,
        default=180,
        help="maximum age of latest fundamentals period",
    )

    parser.add_argument(
        "--only-problems",
        action="store_true",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of table output",
    )

    return parser.parse_args()


def main() -> None:

    args = parse_args()

    conn = connect()

    try:
        validate_schema(conn)

        results = audit(
            conn,
            scope=args.scope,
            stale_days=args.stale_days,
        )

        results = filter_results(
            results,
            only_problems=args.only_problems,
        )

    finally:
        conn.close()

    if args.json:
        print(
            json.dumps(
                results,
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print_table(results)


if __name__ == "__main__":
    main()
