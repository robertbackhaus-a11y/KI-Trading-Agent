"""
Research-TradingFundamentals.py

    LLM   = web research + interpretation + mapping
    Python = read DB + build prompt + check JSON + write DB

This is NOT a parser. It does not fetch or parse any financial
document itself. Its whole job is three steps around the actual
research, which an LLM (Claude, run interactively against the prompt
this script produces) does:

  1. Read a security + its existing `fundamentals` rows from the
     trading DB and build a self-contained research prompt for it.
  2. Take the LLM's JSON answer to that prompt and run cheap,
     structural, non-semantic checks on it (valid JSON, allowed
     period_type, revenue > 0, assets ~= liabilities + equity, ...).
     No financial judgment happens in this file -- that already
     happened when the LLM decided what "revenue" means for this
     specific company and what to write in each field's
     "provenance.status".
  3. Upsert whatever passed validation into the existing
     `fundamentals` table (idempotent, same UNIQUE key as the other
     Backfill-TradingFundamentals* tools), only with --write.

Typical use from an interactive Claude Code session:

    python Research-TradingFundamentals.py --security-id 55 --years 3
    -> writes a prompt file, prints its path

    (the agent reads that prompt, does real web research with its own
    tools, and saves its JSON answer to a file)

    python Research-TradingFundamentals.py --security-id 55 --from-json answer.json
    -> validates, prints a dry-run table + diff against existing DB rows

    python Research-TradingFundamentals.py --security-id 55 --from-json answer.json --write
    -> same, and also upserts the periods that passed validation

Backfill-TradingFundamentalsIR.py / *SEC.py / *MarketData.py are left
untouched -- they remain the hand-validated ground truth for ASML,
ING, TSMC, SK hynix, BAE Systems and HENSOLDT, used here only as
regression fixtures (run --security-id 56 or 13 and compare).

Flags:
    --dry-run / --validate-only : validate the candidate and print the
        report, never write to the DB (this is also the default when
        --write is omitted).
    --write                     : upsert periods that passed validation.
    --force                     : with --write, also overwrite an existing
        period whose stored values differ from the candidate. Without
        --force such a period is SKIPped -- this is what protects the
        hand-validated reference rows above, since they share the same
        "Company IR" source_id and unique key that this tool writes to.
        A period whose stored values already match the candidate is
        always written (harmless refresh), --force or not.

This is the single, canonical fundamentals-research tool for this project.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")
AUDIT_DIR = Path(r"C:\KI-Stack\data\trading\fundamentals-audit")
PROMPT_DIR = AUDIT_DIR / "prompts"

VALID_PERIOD_TYPES = {"quarterly", "semiannual", "nine_month", "annual"}
VERIFICATION_STATUSES = {"verified_direct", "verified_derived", "verified_secondary", "not_verified"}
# Statuses whose value is actually kept; "not_verified" always becomes NULL.
KEEP_STATUSES = {"verified_direct", "verified_derived", "verified_secondary"}

FIELDS = [
    "revenue", "gross_profit", "operating_income", "ebit", "ebitda", "net_income",
    "eps_basic", "eps_diluted", "operating_cash_flow", "capex", "free_cash_flow",
    "cash", "total_debt", "total_assets", "total_liabilities", "total_equity",
    "shares_outstanding",
]


# ============================================================
# DB
# ============================================================

def connect():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Trading DB not found: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    return conn


def get_security(conn, security_id):
    row = conn.execute(
        """
        SELECT id, name, symbol, isin, wkn, exchange, currency, country, sector, industry
        FROM security WHERE id = ?
        """,
        (security_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"security_id {security_id} not found")
    return dict(row)


def get_existing_fundamentals(conn, security_id):
    rows = conn.execute(
        """
        SELECT f.period_end, f.period_type, f.fiscal_year, f.fiscal_quarter,
               f.currency, f.revenue, f.gross_profit, f.operating_income, f.ebit,
               f.ebitda, f.net_income, f.eps_basic, f.eps_diluted,
               f.operating_cash_flow, f.capex, f.free_cash_flow, f.cash,
               f.total_debt, f.total_assets, f.total_liabilities, f.total_equity,
               f.shares_outstanding, ds.name AS source_name
        FROM fundamentals f
        JOIN data_sources ds ON ds.id = f.source_id
        WHERE f.security_id = ?
        ORDER BY f.period_end
        """,
        (security_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_source_id(conn, name):
    row = conn.execute("SELECT id FROM data_sources WHERE name = ? LIMIT 1", (name,)).fetchone()
    if row is None:
        raise RuntimeError(f"Missing data source: {name}")
    return row["id"]


# ============================================================
# PROMPT
# ============================================================

PROMPT_TEMPLATE = """\
You are researching real, published fundamentals data for one company, to be
written into a trading database. Do genuine web research with your own tools
(browse the company's investor relations site, fetch actual documents,
consult regulatory filings) -- do not guess or estimate any number.

# Company

- security_id: {security_id}
- name: {name}
- symbol: {symbol}
- ISIN: {isin}
- WKN: {wkn}
- exchange: {exchange}
- currency: {currency}
- country: {country}
- sector: {sector}
- industry: {industry}

# Target coverage

The last {years} completed fiscal years, plus any interim periods published
since then. Do NOT assume quarterly reporting -- some companies only publish
half-year and annual results, some only publish a discrete standalone
quarter for Q1 (many EU issuers stopped mandatory quarterly reporting).
Include only periods the company actually published a report for. Never
invent a standalone Q2/Q3/Q4 the company itself never published as such.

# Already in the database for this security

{existing_summary}

Use this to see which periods are missing or might need a refresh -- it is
not necessarily correct or complete, treat it as a hint, not ground truth.

# Source strategy

Primary sources, in priority order:
1. Regulatory XBRL / ESEF / iXBRL filings
2. Official XLSX / CSV data
3. Official financial-statement PDFs (annual/half-year/quarterly reports)
4. Official investor-relations pages
5. Official regulatory publication platforms (SEC EDGAR, OpenDART,
   Bundesanzeiger/Unternehmensregister, etc.)

Secondary sources (Yahoo Finance, MarketScreener, StockAnalysis,
Macrotrends, TradingView, CompaniesMarketCap, etc.) are explicitly allowed
for: discovering what periods/documents exist, cross-checking a primary
value's plausibility, spotting an obvious extraction error, or bridging when
a primary source is technically unreachable (e.g. blocked by a WAF). Never
use a secondary source's number as a final value without at least one
independent corroborating check, and never silently prefer a secondary
number over an available primary one.

# Mapping rules (do not skip these)

- Prefer statutory / IFRS / GAAP figures. Do NOT map "Adjusted EBIT",
  "Adjusted EBITDA", "Underlying EBIT", "Adjusted Net Income" or "Adjusted
  Free Cash Flow" onto the statutory fields below unless no statutory
  equivalent exists AND you say so explicitly in that field's provenance.
- "Net income" must be verified per company, not assumed from precedent.
  Some companies report it as total profit for the period (before the
  non-controlling-interest split) -- e.g. SK hynix's own press materials
  define "Net Income" that way. Others report it as profit attributable to
  equity holders/shareholders of the parent -- e.g. BAE Systems and
  HENSOLDT. Check which one this company's own materials actually mean
  by "Net Income" (cross-check via reported EPS * shares if unsure), and
  say which basis you used in net_income's provenance.
- Revenue is never order intake, bookings, GMV, or backlog.
- EBIT/EBITDA margins are ratios (percentages), never the absolute EBIT or
  EBITDA figures themselves.
- capex: return it as a POSITIVE outflow magnitude (source often reports it
  as a negative cash-flow line, e.g. "Purchase of PP&E = -100" -> capex =
  100). free_cash_flow = operating_cash_flow - capex.
- total_debt: only set it when a complete, clearly-defined gross borrowings
  figure is available. Never substitute net debt, bonds-only, loans-only, or
  lease-liabilities-only for it. If unsure: null.
- cash: "Cash and cash equivalents" specifically -- do not fold in other
  short-term investments unless the source itself classifies them as cash
  equivalents.
- Never treat a cumulative year-to-date figure (H1, 9M, FY) as a standalone
  quarter. A derivation like "Q2 = H1_cumulative - Q1_cumulative" is only
  allowed when both operands are complete, use the same IFRS/GAAP
  definitions and the same consolidation scope, and the derivation is
  documented as "verified_derived" with the formula. Do not force this --
  if only H1 is published, just store it as period_type "semiannual".

# Period types

Use exactly one of: "quarterly", "semiannual", "nine_month", "annual".
fiscal_quarter is an integer 1-4 only for period_type "quarterly", else
null.

# Verification status (required on every non-null field)

- verified_direct: read straight from a primary source.
- verified_derived: computed from verified primary values via an exact,
  documented formula (state the formula).
- verified_secondary: primary source unreachable/unusable; corroborated by
  at least two independent secondary sources, or one high-quality secondary
  source plus a strong plausibility check. State which sources.
- not_verified: use this (and leave the value null) rather than guessing.
  A field with status not_verified will be discarded, not stored.

# Output format

Return ONLY a single JSON object, no prose before or after it, matching
exactly this structure (values below are structural placeholders, not real
data):

{{
  "security_id": {security_id},
  "company": "{name}",
  "periods": [
    {{
      "period_end": "YYYY-MM-DD",
      "period_type": "quarterly|semiannual|nine_month|annual",
      "fiscal_year": 2026,
      "fiscal_quarter": null,
      "filing_date": "YYYY-MM-DD or null",
      "currency": "EUR",
      "revenue": 0,
      "gross_profit": null,
      "operating_income": 0,
      "ebit": 0,
      "ebitda": null,
      "net_income": 0,
      "eps_basic": null,
      "eps_diluted": null,
      "operating_cash_flow": 0,
      "capex": 0,
      "free_cash_flow": 0,
      "cash": 0,
      "total_debt": null,
      "total_assets": 0,
      "total_liabilities": 0,
      "total_equity": 0,
      "shares_outstanding": null,
      "provenance": {{
        "revenue": {{
          "status": "verified_direct",
          "source_type": "primary",
          "document": "Half-Year Report 2026",
          "url": "https://...",
          "label": "Revenue"
        }}
      }}
    }}
  ]
}}

Every non-null field in a period object must have a matching entry in that
period's "provenance" object (field name -> {{status, source_type, document,
url, label}}, plus "formula" when status is verified_derived, and
"alternates" when you considered more than one candidate value, e.g. for
net_income). All monetary values are absolute amounts in the security's
own currency ({currency}), not millions/thousands -- e.g. revenue of
2.455 billion EUR is 2455000000, not 2455 or 2.455.
"""


def format_existing(existing):
    if not existing:
        return "(no fundamentals rows exist for this security yet)"
    lines = []
    for row in existing:
        lines.append(
            f"- {row['period_end']} ({row['period_type']}, source={row['source_name']}): "
            f"revenue={row['revenue']}, net_income={row['net_income']}, "
            f"total_assets={row['total_assets']}"
        )
    return "\n".join(lines)


def build_prompt(security, existing, years):
    return PROMPT_TEMPLATE.format(
        security_id=security["id"],
        name=security["name"],
        symbol=security["symbol"],
        isin=security["isin"] or "unknown",
        wkn=security["wkn"] or "unknown",
        exchange=security["exchange"] or "unknown",
        currency=security["currency"],
        country=security["country"] or "unknown",
        sector=security["sector"] or "unknown",
        industry=security["industry"] or "unknown",
        years=years,
        existing_summary=format_existing(existing),
    )


def save_prompt(security, prompt_text):
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", security["name"]).strip("_")
    path = PROMPT_DIR / f"{security['id']}_{safe_name}_{ts}.md"
    path.write_text(prompt_text, encoding="utf-8")
    return path


# ============================================================
# VALIDATION -- hard, structural checks only. No financial judgment.
# ============================================================

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_response(security_id, response):
    """Returns (accepted_periods, errors). accepted_periods have every
    field either kept (status in KEEP_STATUSES) or set to null (status
    not_verified / missing / failed a structural check), plus the
    checks that were run on that period."""
    errors = []

    if not isinstance(response, dict):
        return [], [{"error": "response is not a JSON object"}]

    if response.get("security_id") != security_id:
        errors.append({
            "error": "security_id mismatch",
            "expected": security_id,
            "got": response.get("security_id"),
        })

    periods = response.get("periods")
    if not isinstance(periods, list):
        errors.append({"error": "periods is not a list"})
        return [], errors

    accepted = []
    for i, period in enumerate(periods):
        checks = []

        def check(name, ok, detail=""):
            checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

        period_end = period.get("period_end")
        check("period_end is a valid date", isinstance(period_end, str) and bool(DATE_RE.match(period_end or "")))

        period_type = period.get("period_type")
        check("period_type is valid", period_type in VALID_PERIOD_TYPES, str(period_type))

        fiscal_year = period.get("fiscal_year")
        check("fiscal_year plausible", isinstance(fiscal_year, int) and 2000 <= fiscal_year <= 2100)

        fiscal_quarter = period.get("fiscal_quarter")
        if period_type == "quarterly":
            check("fiscal_quarter set for quarterly", fiscal_quarter in (1, 2, 3, 4))
        else:
            check("fiscal_quarter null for non-quarterly", fiscal_quarter is None)

        currency = period.get("currency")
        check("currency looks like a code", isinstance(currency, str) and len(currency) == 3)

        provenance = period.get("provenance") or {}
        if not isinstance(provenance, dict):
            provenance = {}

        clean = {}
        for field in FIELDS:
            raw_value = period.get(field)
            prov = provenance.get(field) or {}
            status = prov.get("status")
            if raw_value is None or status not in KEEP_STATUSES:
                clean[field] = None
                continue
            if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
                checks.append({"check": f"{field} is numeric", "status": "FAIL", "detail": repr(raw_value)})
                clean[field] = None
                continue
            clean[field] = float(raw_value)

        if clean.get("revenue") is not None:
            check("revenue > 0", clean["revenue"] > 0, str(clean["revenue"]))

        if clean.get("revenue") is not None and clean.get("gross_profit") is not None:
            check(
                "gross_profit <= revenue",
                clean["gross_profit"] <= clean["revenue"],
                f"{clean['gross_profit']} vs {clean['revenue']}",
            )

        a, l, e = clean.get("total_assets"), clean.get("total_liabilities"), clean.get("total_equity")
        if a is not None and l is not None and e is not None:
            diff = abs(a - (l + e))
            tolerance = max(abs(a) * 0.01, 1_000_000)
            check("assets ~= liabilities + equity", diff <= tolerance, f"diff={diff}")

        ocf, capex, fcf = clean.get("operating_cash_flow"), clean.get("capex"), clean.get("free_cash_flow")
        if ocf is not None and capex is not None and fcf is not None:
            check("fcf ~= ocf - capex", abs(fcf - (ocf - capex)) <= max(abs(ocf) * 0.01, 1000), f"{fcf} vs {ocf - capex}")

        if clean.get("capex") is not None and clean["capex"] < 0:
            check("capex stored as positive outflow magnitude", False, str(clean["capex"]))

        if clean.get("total_debt") is None:
            checks.append({"check": "total_debt", "status": "WARN", "detail": "not verified -> NULL"})

        accepted.append({
            "period_end": period_end,
            "period_type": period_type,
            "fiscal_year": fiscal_year,
            "fiscal_quarter": fiscal_quarter,
            "filing_date": period.get("filing_date"),
            "currency": currency,
            **clean,
            "_checks": checks,
            "_provenance": provenance,
            "_all_primary": all(
                (provenance.get(f) or {}).get("source_type") == "primary"
                for f in FIELDS
                if clean.get(f) is not None
            ),
        })

    return accepted, errors


def period_passed(period):
    return not any(c["status"] == "FAIL" for c in period["_checks"])


# ============================================================
# WRITE GATING -- protects hand-validated reference data
#
# Company IR data written by Backfill-TradingFundamentalsIR.py / *SEC.py
# for the reference companies (ASML, ING, TSMC, SK hynix, BAE Systems,
# HENSOLDT) uses the same source_id ("Company IR") and the same unique key
# (security_id, period_end, period_type, source_id) that this tool writes
# to. Without a guard, an LLM research answer could silently overwrite a
# hand-validated value on conflict. So: an existing row is only ever
# overwritten if its values already match the candidate (harmless refresh)
# or --force was passed explicitly.
# ============================================================

DIFF_TOLERANCE_REL = 0.001  # 0.1%
DIFF_TOLERANCE_ABS = 1.0    # smallest unit of the currency


def values_equal(existing, candidate):
    for field in FIELDS:
        old, new = existing.get(field), candidate.get(field)
        if old is None and new is None:
            continue
        if old is None or new is None:
            return False
        if abs(old - new) > max(abs(old) * DIFF_TOLERANCE_REL, DIFF_TOLERANCE_ABS):
            return False
    return True


def find_existing(existing_rows, period):
    for row in existing_rows:
        if row["period_end"] == period["period_end"] and row["period_type"] == period["period_type"]:
            return row
    return None


def decide_write_action(period, existing_rows, write_enabled, force):
    if not period_passed(period):
        return {"action": "SKIP", "reason": "failed validation"}
    if not write_enabled:
        return {"action": "DRY", "reason": "no --write / --dry-run / --validate-only"}
    match = find_existing(existing_rows, period)
    if match is None:
        return {"action": "WRITE", "reason": "new period"}
    if values_equal(match, period):
        return {"action": "WRITE", "reason": "refresh (unchanged)"}
    if force:
        return {"action": "WRITE", "reason": "overwrite (--force)"}
    return {
        "action": "SKIP",
        "reason": "existing row differs -- use --force to overwrite",
        "diff": {
            f: {"existing": match.get(f), "candidate": period.get(f)}
            for f in FIELDS
            if match.get(f) != period.get(f)
            and not (
                match.get(f) is not None and period.get(f) is not None
                and abs(match[f] - period[f]) <= max(abs(match[f]) * DIFF_TOLERANCE_REL, DIFF_TOLERANCE_ABS)
            )
        },
    }


# ============================================================
# AUDIT
# ============================================================

def write_audit(security, prompt_text, raw_response, accepted, errors, decisions=None):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", security["name"]).strip("_")
    path = AUDIT_DIR / f"{security['id']}_{safe_name}_{ts}.json"
    payload = {
        "security": security,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt_text,
        "raw_response": raw_response,
        "accepted_periods": accepted,
        "structural_errors": errors,
        "write_decisions": decisions or [],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    return path


# ============================================================
# DB WRITE
# ============================================================

def upsert_fundamental(conn, source_id, security_id, row):
    conn.execute(
        """
        INSERT INTO fundamentals (
            security_id, period_end, period_type, fiscal_year, fiscal_quarter,
            filing_date, currency, revenue, gross_profit, operating_income,
            ebit, ebitda, net_income, eps_basic, eps_diluted,
            operating_cash_flow, capex, free_cash_flow, cash, total_debt,
            total_assets, total_liabilities, total_equity, shares_outstanding,
            source_id, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (security_id, period_end, period_type, source_id) DO UPDATE SET
            fiscal_year = excluded.fiscal_year,
            fiscal_quarter = excluded.fiscal_quarter,
            filing_date = excluded.filing_date,
            currency = excluded.currency,
            revenue = excluded.revenue,
            gross_profit = excluded.gross_profit,
            operating_income = excluded.operating_income,
            ebit = excluded.ebit,
            ebitda = excluded.ebitda,
            net_income = excluded.net_income,
            eps_basic = excluded.eps_basic,
            eps_diluted = excluded.eps_diluted,
            operating_cash_flow = excluded.operating_cash_flow,
            capex = excluded.capex,
            free_cash_flow = excluded.free_cash_flow,
            cash = excluded.cash,
            total_debt = excluded.total_debt,
            total_assets = excluded.total_assets,
            total_liabilities = excluded.total_liabilities,
            total_equity = excluded.total_equity,
            shares_outstanding = excluded.shares_outstanding,
            fetched_at = excluded.fetched_at
        """,
        (
            security_id, row["period_end"], row["period_type"], row["fiscal_year"], row["fiscal_quarter"],
            row.get("filing_date"), row["currency"], row.get("revenue"), row.get("gross_profit"),
            row.get("operating_income"), row.get("ebit"), row.get("ebitda"), row.get("net_income"),
            row.get("eps_basic"), row.get("eps_diluted"), row.get("operating_cash_flow"), row.get("capex"),
            row.get("free_cash_flow"), row.get("cash"), row.get("total_debt"), row.get("total_assets"),
            row.get("total_liabilities"), row.get("total_equity"), row.get("shares_outstanding"),
            source_id, datetime.now(timezone.utc).isoformat(),
        ),
    )


# ============================================================
# CLI
# ============================================================

def format_amount(value):
    if value is None:
        return "-"
    return f"{value / 1_000_000:,.1f}m"


def print_dry_run(security, accepted, existing, decisions):
    by_period_end = {p["period_end"]: d for p, d in zip(accepted, decisions)}

    print()
    print(security["name"])
    print()
    print(f"{'Period':<12} {'Type':<12} {'Revenue':>12} {'EBIT':>12} {'Net Income':>12}  Action")
    for p in sorted(accepted, key=lambda x: x["period_end"]):
        d = by_period_end[p["period_end"]]
        print(
            f"{p['period_end']:<12} {p['period_type']:<12} "
            f"{format_amount(p.get('revenue')):>12} {format_amount(p.get('ebit')):>12} "
            f"{format_amount(p.get('net_income')):>12}  {d['action']} ({d['reason']})"
        )
    print()
    for p in sorted(accepted, key=lambda x: x["period_end"]):
        print(f"-- {p['period_end']} ({p['period_type']}) --")
        for c in p["_checks"]:
            detail = f" ({c['detail']})" if c.get("detail") else ""
            print(f"{c['status']:<5} {c['check']}{detail}")

    diffs_shown = [(p, d) for p, d in zip(accepted, decisions) if d.get("diff")]
    if diffs_shown:
        print()
        print("Conflicts with existing DB rows (SKIPped, use --force to overwrite):")
        for p, d in diffs_shown:
            print(f"  {p['period_end']} ({p['period_type']}):")
            for field, vals in d["diff"].items():
                print(f"    {field}: existing={vals['existing']} candidate={vals['candidate']}")

    if existing:
        print()
        print(f"Existing DB rows for this security: {len(existing)}")
        for row in existing:
            match = next(
                (p for p in accepted if p["period_end"] == row["period_end"] and p["period_type"] == row["period_type"]),
                None,
            )
            if match is None:
                continue
            d = by_period_end[row["period_end"]]
            label = "MATCH" if not d.get("diff") else "DIFF"
            print(f"  {label} {row['period_end']} ({row['period_type']}) -> {d['action']}")
    print()


def main():
    parser = argparse.ArgumentParser(description="LLM-driven fundamentals research workflow")
    parser.add_argument("--security-id", type=int, required=True)
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--from-json", type=str, help="path to the LLM's JSON research answer")
    parser.add_argument("--write", action="store_true", help="upsert accepted periods into fundamentals")
    parser.add_argument("--dry-run", action="store_true", help="validate + report only, never write (default without --write)")
    parser.add_argument("--validate-only", action="store_true", help="validate candidate, never write")
    parser.add_argument("--force", action="store_true", help="allow overwriting an existing period whose values differ from the candidate")
    args = parser.parse_args()

    write_enabled = args.write and not args.dry_run and not args.validate_only

    conn = connect()
    try:
        security = get_security(conn, args.security_id)
        existing = get_existing_fundamentals(conn, args.security_id)

        if not args.from_json:
            prompt_text = build_prompt(security, existing, args.years)
            path = save_prompt(security, prompt_text)
            print(f"Prompt written to: {path}")
            print("Have the agent research this company and save its JSON answer,")
            print(f"then re-run with: --security-id {args.security_id} --from-json <answer.json>")
            return

        with open(args.from_json, "r", encoding="utf-8") as f:
            raw_response = json.load(f)

        accepted, errors = validate_response(args.security_id, raw_response)
        if errors:
            print("Structural errors:")
            for e in errors:
                print(" ", e)

        decisions = [decide_write_action(p, existing, write_enabled, args.force) for p in accepted]

        prompt_text = build_prompt(security, existing, args.years)
        audit_path = write_audit(security, prompt_text, raw_response, accepted, errors, decisions)
        print(f"Audit written: {audit_path}")

        print_dry_run(security, accepted, existing, decisions)

        if not write_enabled:
            if args.validate_only:
                print("Validate-only -- no DB write.")
            elif args.dry_run:
                print("Dry run -- no DB write.")
            else:
                print("Dry run (default without --write) -- no DB write. Re-run with --write to persist.")
            return

        to_write = [p for p, d in zip(accepted, decisions) if d["action"] == "WRITE"]
        skipped_failed = sum(1 for d in decisions if d["action"] == "SKIP" and d["reason"] == "failed validation")
        skipped_conflict = sum(1 for d in decisions if d["action"] == "SKIP" and d["reason"] != "failed validation")
        if skipped_failed:
            print(f"Skipping {skipped_failed} period(s) that failed validation.")
        if skipped_conflict:
            print(f"Skipping {skipped_conflict} period(s) that already exist with different values (use --force to overwrite).")

        company_ir_id = get_source_id(conn, "Company IR")
        web_id = get_source_id(conn, "Web")

        conn.execute("BEGIN IMMEDIATE")
        try:
            for p in to_write:
                source_id = company_ir_id if p["_all_primary"] else web_id
                upsert_fundamental(conn, source_id, args.security_id, p)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        print(f"Wrote {len(to_write)} period(s) to fundamentals.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
