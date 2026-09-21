from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional


# ============================================================
# CONFIG
# ============================================================

DB_PATH = Path(
    r"C:\KI-Stack\data\trading\trading.db"
)

SEC_CACHE_DIR = Path(
    r"C:\KI-Stack\data\trading\sec-cache"
)

CACHE_MAX_AGE_SECONDS = 6 * 3600

SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "okami.de robert@okami.de",
)

SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/json",
}

REQUEST_TIMEOUT_SECONDS = 10

REQUEST_DELAY_SECONDS = 0.20

MAX_RETRIES = 1

MAX_ANNUAL_PERIODS = 6
MAX_QUARTERLY_PERIODS = 12


# ============================================================
# STANDARD XBRL CONCEPT MAPPING
# ============================================================
#
# Order matters:
# preferred concepts come first.
#
# We intentionally use only standardized taxonomy concepts
# returned by SEC CompanyFacts.
#
# EBITDA is intentionally NOT approximated from other values.
# EBIT is also only populated if an explicit standardized
# concept exists.
# ============================================================

US_GAAP = {

    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],

    "gross_profit": [
        "GrossProfit",
    ],

    # Duration helper, not written directly to any fundamentals column.
    # Used only as a fallback to derive gross_profit (= revenue - cost_of_revenue)
    # when a filer doesn't tag GrossProfit at all -- the same pure arithmetic
    # identity (Revenue - Cost of sales) already applied by hand for several
    # Company-IR-researched companies in this project (e.g. Thales, RENK).
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ],

    "operating_income": [
        "OperatingIncomeLoss",
    ],

    "ebit": [
        "EarningsBeforeInterestAndTaxes",
    ],

    "ebitda": [
        "EarningsBeforeInterestTaxesDepreciationAndAmortization",
    ],

    # Duration helper, not written directly to any fundamentals column.
    # Used only as a fallback to derive ebitda (= operating_income + D&A)
    # when a filer doesn't tag a standardized EBITDA concept -- the same
    # verified_derived formula already applied by hand throughout this
    # project (e.g. HENSOLDT, AIXTRON, RENK: EBIT + D&A from the cash flow
    # statement). Never used to approximate operating_income or ebit
    # themselves, only as the D&A add-back for this one identity.
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
    ],

    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
    ],

    "eps_basic": [
        "EarningsPerShareBasic",
    ],

    "eps_diluted": [
        "EarningsPerShareDiluted",
    ],

    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
    ],

    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
    ],

    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],

    "total_assets": [
        "Assets",
    ],

    "total_liabilities": [
        "Liabilities",
    ],

    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],

    # Prefer an aggregated long-term debt concept including
    # current maturities where the filer provides one.
    #
    # ShortTermBorrowings is handled separately because it is
    # normally NOT included in these long-term debt concepts.
    "total_debt_direct": [
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        "LongTermDebtAndFinanceLeaseObligationsIncludingCurrentMaturities",
        "LongTermDebtAndFinanceLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebt",
    ],

    "debt_current": [
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
        "LongTermDebtCurrent",
    ],

    "debt_noncurrent": [
        "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "LongTermDebtNoncurrent",
    ],

    "short_term_borrowings": [
        "ShortTermBorrowings",
        "ShortTermDebt",
    ],
}


IFRS = {

    "revenue": [
        "Revenue",
    ],

    "gross_profit": [
        "GrossProfit",
    ],

    "operating_income": [
        "ProfitLossFromOperatingActivities",
        "OperatingProfitLoss",
    ],

    "ebit": [
        "ProfitLossFromOperatingActivities",
    ],

    "ebitda": [
        "EarningsBeforeInterestTaxesDepreciationAndAmortisation",
    ],

    "net_income": [
        "ProfitLoss",
        "ProfitLossAttributableToOwnersOfParent",
    ],

    "eps_basic": [
        "BasicEarningsLossPerShare",
    ],

    "eps_diluted": [
        "DilutedEarningsLossPerShare",
    ],

    "operating_cash_flow": [
        "CashFlowsFromUsedInOperatingActivities",
    ],

    "capex": [
        "PurchaseOfPropertyPlantAndEquipment",
        "PaymentsToAcquirePropertyPlantAndEquipment",
    ],

    "cash": [
        "CashAndCashEquivalents",
    ],

    "total_assets": [
        "Assets",
    ],

    "total_liabilities": [
        "Liabilities",
    ],

    "total_equity": [
        "Equity",
        "EquityAttributableToOwnersOfParent",
    ],

    "cost_of_revenue": [
        "CostOfSales",
    ],

    "depreciation_amortization": [
        "DepreciationAmortisationExpense",
        "DepreciationAndAmortisationExpense",
    ],

    "total_debt_direct": [
        "Borrowings",
    ],

    "debt_current": [
        "CurrentBorrowings",
        "BorrowingsCurrent",
    ],

    "debt_noncurrent": [
        "NoncurrentBorrowings",
        "BorrowingsNoncurrent",
    ],
}


DEI_SHARES = [
    "EntityCommonStockSharesOutstanding",
]


# ============================================================
# PERIOD CLASSIFICATION
# ============================================================

DURATION_FIELDS = {
    "revenue",
    "gross_profit",
    "cost_of_revenue",
    "operating_income",
    "ebit",
    "ebitda",
    "depreciation_amortization",
    "net_income",
    "eps_basic",
    "eps_diluted",
    "operating_cash_flow",
    "capex",
}

INSTANT_FIELDS = {
    "cash",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "total_debt_direct",
    "debt_current",
    "debt_noncurrent",
    "short_term_borrowings",
    "shares_outstanding",
}

ANNUAL_FORMS = {
    "10-K",
    "10-K/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
}

QUARTER_FORMS = {
    "10-Q",
    "10-Q/A",
    "6-K",
    "6-K/A",
}


# ============================================================
# DATABASE
# ============================================================

def connect() -> sqlite3.Connection:

    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Trading DB not found: {DB_PATH}"
        )

    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=10.0,
        isolation_level=None,
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA foreign_keys = ON;"
    )

    conn.execute(
        "PRAGMA journal_mode = WAL;"
    )

    conn.execute(
        "PRAGMA busy_timeout = 10000;"
    )

    return conn


def validate_schema(
    conn: sqlite3.Connection,
) -> None:

    row = conn.execute(
        """
        SELECT value
        FROM metadata
        WHERE key = 'schema_version'
        """
    ).fetchone()

    if row is None:
        raise RuntimeError(
            "metadata.schema_version missing"
        )

    if row["value"] != "2.0":
        raise RuntimeError(
            f"Expected schema 2.0, got {row['value']}"
        )


def get_sec_source_id(
    conn: sqlite3.Connection,
) -> int:

    row = conn.execute(
        """
        SELECT id
        FROM data_sources
        WHERE name = 'SEC EDGAR'
        LIMIT 1
        """
    ).fetchone()

    if row is None:
        raise RuntimeError(
            "SEC EDGAR data source missing"
        )

    return row["id"]


def get_relevant_sec_securities(
    conn: sqlite3.Connection,
    source_id: int,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT DISTINCT

            s.id,
            s.name,
            s.symbol,
            s.isin,

            ss.symbol AS cik

        FROM security s

        JOIN source_symbols ss
            ON ss.security_id = s.id
           AND ss.source_id = ?

        LEFT JOIN positions p
            ON p.security_id = s.id

        LEFT JOIN watchlist w
            ON w.security_id = s.id

        WHERE s.active = 1

          AND LOWER(s.asset_type) = 'stock'

          AND (
                p.shares > 0
                OR
                w.security_id IS NOT NULL
          )

        ORDER BY s.name
        """,
        (
            source_id,
        ),
    ).fetchall()


# ============================================================
# HTTP
# ============================================================

def get_json(
    url: str,
) -> Optional[dict]:

    attempts = MAX_RETRIES + 1

    for attempt in range(attempts):

        request = urllib.request.Request(
            url,
            headers=SEC_HEADERS,
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:

                raw = response.read()

                return json.loads(
                    raw.decode("utf-8")
                )

        except urllib.error.HTTPError as exc:

            if exc.code == 404:
                return None

            if attempt < attempts - 1:
                continue

            print(
                f"      HTTP {exc.code}"
            )

            return None

        except Exception as exc:

            if attempt < attempts - 1:
                continue

            print(
                f"      HTTP error: {exc}"
            )

            return None

    return None


# ============================================================
# LOCAL SEC COMPANYFACTS CACHE
# ============================================================

def cache_path_for_cik(
    cik: str,
) -> Path:

    return SEC_CACHE_DIR / f"CIK{cik}.json"


def load_cache(
    path: Path,
) -> Optional[dict]:

    try:

        if not path.exists():
            return None

        age_seconds = (
            time.time() - path.stat().st_mtime
        )

        if age_seconds > CACHE_MAX_AGE_SECONDS:
            return None

        with path.open(
            "r",
            encoding="utf-8",
        ) as handle:

            return json.load(handle)

    except (OSError, ValueError):

        return None


def save_cache(
    path: Path,
    data: dict,
) -> None:

    try:

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with path.open(
            "w",
            encoding="utf-8",
        ) as handle:

            json.dump(data, handle)

    except OSError:

        pass


def fetch_companyfacts(
    cik: str,
    use_cache: bool,
) -> tuple[Optional[dict], bool]:
    """Returns (data, from_cache). Only performs the SEC network
    request (and the rate-limit pause) when the cache is disabled,
    missing, or older than CACHE_MAX_AGE_SECONDS."""

    cache_file = cache_path_for_cik(cik)

    if use_cache:

        cached = load_cache(cache_file)

        if cached is not None:
            return cached, True

    url = (
        "https://data.sec.gov/api/xbrl/"
        f"companyfacts/CIK{cik}.json"
    )

    data = get_json(url)

    time.sleep(
        REQUEST_DELAY_SECONDS
    )

    if data is not None:
        save_cache(cache_file, data)

    return data, False


# ============================================================
# HELPERS
# ============================================================

def utc_now() -> str:

    return datetime.now(
        timezone.utc
    ).isoformat()


@lru_cache(maxsize=4096)
def parse_date(
    value: Optional[str],
):

    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()

    except ValueError:
        return None


def duration_days(
    item: dict,
) -> Optional[int]:

    start = parse_date(
        item.get("start")
    )

    end = parse_date(
        item.get("end")
    )

    if start is None or end is None:
        return None

    return (
        end - start
    ).days + 1


def infer_quarter(
    item: dict,
) -> Optional[int]:

    fp = (
        item.get("fp")
        or ""
    ).upper()

    if fp == "Q1":
        return 1

    if fp == "Q2":
        return 2

    if fp == "Q3":
        return 3

    if fp == "Q4":
        return 4

    frame = (
        item.get("frame")
        or ""
    ).upper()

    for quarter in range(
        1,
        5,
    ):

        if f"Q{quarter}" in frame:
            return quarter

    return None


def period_type_for_fact(
    item: dict,
    duration_field: bool,
) -> Optional[str]:

    form = (
        item.get("form")
        or ""
    ).upper()

    fp = (
        item.get("fp")
        or ""
    ).upper()

    days = duration_days(
        item
    )

    # --------------------------------------------------------
    # Duration concepts
    # --------------------------------------------------------

    if duration_field:

        # Annual duration should be roughly one year.
        if (
            form in ANNUAL_FORMS
            and days is not None
            and 300 <= days <= 400
        ):
            return "annual"

        # Quarterly values must represent an actual quarter,
        # not six- or nine-month YTD values.
        if (
            form in QUARTER_FORMS
            and days is not None
            and 60 <= days <= 120
        ):
            return "quarterly"

        return None

    # --------------------------------------------------------
    # Instant concepts
    # --------------------------------------------------------

    if form in ANNUAL_FORMS:
        return "annual"

    if form in QUARTER_FORMS:

        # 6-K is broad. Require quarter evidence when possible.
        if form.startswith("6-K"):

            if (
                fp.startswith("Q")
                or infer_quarter(item) is not None
            ):
                return "quarterly"

            return None

        return "quarterly"

    return None


# ============================================================
# FACT EXTRACTION
# ============================================================

def units_for_concept(
    taxonomy_data: dict,
    concept: str,
) -> dict:

    record = taxonomy_data.get(
        concept
    )

    if not isinstance(
        record,
        dict,
    ):
        return {}

    units = record.get(
        "units"
    )

    if not isinstance(
        units,
        dict,
    ):
        return {}

    return units


def expected_unit_type(
    metric: str,
) -> str:

    if metric == "shares_outstanding":
        return "shares"

    if metric in {
        "eps_basic",
        "eps_diluted",
    }:
        return "per_share"

    return "money"


def unit_rank(
    unit: str,
    metric: str,
) -> int:

    unit_upper = unit.upper()

    kind = expected_unit_type(
        metric
    )

    if kind == "shares":

        if unit_upper == "SHARES":
            return 100

        return 0

    if kind == "per_share":

        if (
            "-PER-SHARES" in unit_upper
            or "/SHARES" in unit_upper
        ):
            return 100

        return 20

    # Monetary concepts.
    if unit_upper in {
        "USD",
        "EUR",
        "GBP",
        "CAD",
        "KRW",
        "JPY",
        "TWD",
        "CHF",
    }:
        return 100

    return 10


def prepare_concept_facts(
    taxonomy_data: dict,
    concept: str,
) -> list[dict]:
    """Parse a concept's raw units/facts exactly once: date parsing,
    duration/instant period classification and quarter inference are
    computed here a single time per concept, then reused for every
    metric that maps to this concept (e.g. IFRS
    ProfitLossFromOperatingActivities is used by both
    operating_income and ebit). Both the duration- and instant-field
    classification are precomputed so this stays correct regardless
    of which metric later consumes it, with no change to the
    classification logic itself."""

    prepared = []

    units = units_for_concept(
        taxonomy_data,
        concept,
    )

    for unit, facts in units.items():

        if not isinstance(
            facts,
            list,
        ):
            continue

        for item in facts:

            if not isinstance(
                item,
                dict,
            ):
                continue

            value = item.get(
                "val"
            )

            if value is None:
                continue

            end = item.get(
                "end"
            )

            if not end:
                continue

            prepared.append(
                {
                    "unit": unit,
                    "value": value,
                    "item": item,
                    "end": end,
                    "period_type_duration":
                        period_type_for_fact(
                            item,
                            True,
                        ),
                    "period_type_instant":
                        period_type_for_fact(
                            item,
                            False,
                        ),
                    "quarter":
                        infer_quarter(item),
                }
            )

    return prepared


def iter_concept_facts(
    taxonomy_data: dict,
    concepts: list[str],
    metric: str,
    concept_cache: dict,
):

    for concept_priority, concept in enumerate(
        concepts
    ):

        prepared = concept_cache.get(
            concept
        )

        if prepared is None:

            prepared = prepare_concept_facts(
                taxonomy_data,
                concept,
            )

            concept_cache[concept] = prepared

        for entry in prepared:

            rank = unit_rank(
                entry["unit"],
                metric,
            )

            if rank <= 0:
                continue

            yield {
                "concept": concept,
                "concept_priority":
                    concept_priority,

                "unit": entry["unit"],
                "unit_rank": rank,

                "item": entry["item"],
                "value": entry["value"],

                "_prepared": entry,
            }


def choose_period_fact(
    candidates: list[dict],
) -> Optional[dict]:

    if not candidates:
        return None

    def filing_lag_days(
        candidate: dict,
    ) -> int:

        item = candidate["item"]

        end = parse_date(
            item.get("end")
        )

        filed = parse_date(
            item.get("filed")
        )

        if end is None or filed is None:
            return 999999

        lag = (
            filed - end
        ).days

        # A filing before its reported period end is not a
        # sensible primary candidate.
        if lag < 0:
            return 999999

        return lag

    # Prefer in this exact order:
    #
    # 1. best unit
    # 2. earliest concept from our explicit preference list
    # 3. filing closest to the reported period end
    #
    # The third rule is important because CompanyFacts also
    # contains prior-period comparison values repeated in
    # later filings. Choosing the latest filing would assign
    # e.g. a FY2025 quarter the FY metadata of a FY2026 filing.
    candidates.sort(
        key=lambda c: (
            -c["unit_rank"],
            c["concept_priority"],
            filing_lag_days(c),
            c["item"].get("filed") or "",
        )
    )

    return candidates[0]


def extract_metric_periods(
    taxonomy_data: dict,
    concepts: list[str],
    metric: str,
    concept_cache: dict,
) -> dict[tuple, dict]:

    duration_field = (
        metric in DURATION_FIELDS
    )

    groups = {}

    for fact in iter_concept_facts(
        taxonomy_data,
        concepts,
        metric,
        concept_cache,
    ):

        prepared = fact["_prepared"]

        end = prepared["end"]

        ptype = (
            prepared["period_type_duration"]
            if duration_field
            else prepared["period_type_instant"]
        )

        if ptype is None:
            continue

        key = (
            end,
            ptype,
        )

        groups.setdefault(
            key,
            [],
        ).append(
            fact
        )

    selected = {}

    for key, candidates in groups.items():

        winner = choose_period_fact(
            candidates
        )

        if winner is None:
            continue

        selected[key] = winner

    return selected


# ============================================================
# PERIOD RECORD ASSEMBLY
# ============================================================

def ensure_record(
    records: dict,
    key: tuple,
) -> dict:

    if key not in records:

        records[key] = {
            "period_end": key[0],
            "period_type": key[1],

            "fiscal_year": None,
            "fiscal_quarter": None,

            "filing_date": None,
            "currency": None,

            "revenue": None,
            "gross_profit": None,
            "operating_income": None,
            "ebit": None,
            "ebitda": None,
            "net_income": None,

            "eps_basic": None,
            "eps_diluted": None,

            "operating_cash_flow": None,
            "capex": None,
            "free_cash_flow": None,

            "cash": None,
            "total_debt": None,

            "total_assets": None,
            "total_liabilities": None,
            "total_equity": None,

            "shares_outstanding": None,

            "_debt_current": None,
            "_debt_noncurrent": None,
            "_short_term_borrowings": None,
            "_cost_of_revenue": None,
            "_depreciation_amortization": None,
            "_latest_filed": None,
        }

    return records[key]


def merge_metric(
    records: dict,
    metric: str,
    periods: dict,
) -> None:

    for key, fact in periods.items():

        record = ensure_record(
            records,
            key,
        )

        item = fact["item"]

        value = fact["value"]

        # Capex is stored as positive expenditure.
        if (
            metric == "capex"
            and value is not None
        ):

            try:
                value = abs(
                    float(value)
                )

            except (
                TypeError,
                ValueError,
            ):
                pass

        # Internal helper mappings for debt concepts.
        if metric == "total_debt_direct":
            target_metric = "total_debt"
        elif metric == "debt_current":
            target_metric = "_debt_current"
        elif metric == "debt_noncurrent":
            target_metric = "_debt_noncurrent"
        elif metric == "short_term_borrowings":
            target_metric = "_short_term_borrowings"
        elif metric == "cost_of_revenue":
            target_metric = "_cost_of_revenue"
        elif metric == "depreciation_amortization":
            target_metric = "_depreciation_amortization"
        else:
            target_metric = metric

        record[target_metric] = value

        # Fiscal metadata must come from duration statement
        # facts where possible. Instant balance-sheet facts can
        # refer to comparison dates and must not overwrite the
        # actual reporting-period metadata.
        is_duration_metric = (
            metric in DURATION_FIELDS
        )

        fy = item.get(
            "fy"
        )

        if (
            fy is not None
            and (
                is_duration_metric
                or record["fiscal_year"] is None
            )
        ):
            try:
                record["fiscal_year"] = int(
                    fy
                )
            except (
                TypeError,
                ValueError,
            ):
                pass

        if (
            key[1] == "quarterly"
            and is_duration_metric
        ):

            quarter = fact["_prepared"]["quarter"]

            if quarter is not None:
                record[
                    "fiscal_quarter"
                ] = quarter

        filed = item.get(
            "filed"
        )

        if (
            filed
            and (
                record["_latest_filed"] is None
                or
                filed > record["_latest_filed"]
            )
        ):

            record[
                "_latest_filed"
            ] = filed

            record[
                "filing_date"
            ] = filed

        unit = fact.get(
            "unit"
        )

        if metric not in {
            "eps_basic",
            "eps_diluted",
            "shares_outstanding",
        }:

            if (
                unit
                and unit.upper() in {
                    "USD",
                    "EUR",
                    "GBP",
                    "CAD",
                    "KRW",
                    "JPY",
                    "TWD",
                    "CHF",
                }
            ):

                # Prefer revenue currency where available,
                # otherwise first monetary currency.
                if (
                    metric == "revenue"
                    or record["currency"] is None
                ):

                    record[
                        "currency"
                    ] = unit.upper()


# ============================================================
# SHARES / DEI
# ============================================================

def extract_shares(
    facts: dict,
) -> dict:

    dei = facts.get(
        "dei"
    )

    if not isinstance(
        dei,
        dict,
    ):
        return {}

    return extract_metric_periods(
        dei,
        DEI_SHARES,
        "shares_outstanding",
        {},
    )


# ============================================================
# BUILD FUNDAMENTALS
# ============================================================

def build_records(
    companyfacts: dict,
) -> tuple[
    list[dict],
    Optional[str],
]:

    facts = (
        companyfacts.get("facts")
        or {}
    )

    if (
        "us-gaap" in facts
        and len(
            facts.get(
                "us-gaap",
                {},
            )
        ) >= 20
    ):

        taxonomy_name = "us-gaap"
        taxonomy_data = facts[
            "us-gaap"
        ]
        mapping = US_GAAP

    elif (
        "ifrs-full" in facts
        and len(
            facts.get(
                "ifrs-full",
                {},
            )
        ) >= 20
    ):

        taxonomy_name = "ifrs-full"
        taxonomy_data = facts[
            "ifrs-full"
        ]
        mapping = IFRS

    else:

        return [], None

    records = {}

    concept_cache: dict = {}

    for metric, concepts in mapping.items():

        periods = extract_metric_periods(
            taxonomy_data,
            concepts,
            metric,
            concept_cache,
        )

        merge_metric(
            records,
            metric,
            periods,
        )

    shares_periods = extract_shares(
        facts
    )

    merge_metric(
        records,
        "shares_outstanding",
        shares_periods,
    )

    # --------------------------------------------------------
    # Derived values
    # --------------------------------------------------------

    for record in records.values():

        # Debt:
        #
        # 1. Prefer an explicit long-term debt concept that
        #    already includes current maturities.
        #
        # 2. If unavailable, reconstruct long-term debt from
        #    explicit current + noncurrent portions.
        #
        # 3. Add ShortTermBorrowings separately because these
        #    are generally not included in long-term debt.
        direct_debt = record.get(
            "total_debt"
        )

        debt_current = record.get(
            "_debt_current"
        )

        debt_noncurrent = record.get(
            "_debt_noncurrent"
        )

        short_term = record.get(
            "_short_term_borrowings"
        )

        long_term_total = None

        if direct_debt is not None:

            long_term_total = float(
                direct_debt
            )

        elif (
            debt_current is not None
            or debt_noncurrent is not None
        ):

            long_term_total = (
                float(
                    debt_current or 0
                )
                +
                float(
                    debt_noncurrent or 0
                )
            )

        if (
            long_term_total is not None
            or short_term is not None
        ):

            record[
                "total_debt"
            ] = (
                float(
                    long_term_total or 0
                )
                +
                float(
                    short_term or 0
                )
            )

        # FCF = operating cash flow - capex.
        ocf = record.get(
            "operating_cash_flow"
        )

        capex = record.get(
            "capex"
        )

        if (
            ocf is not None
            and capex is not None
        ):

            record[
                "free_cash_flow"
            ] = (
                float(ocf)
                -
                float(capex)
            )

        # Gross profit fallback: revenue - cost_of_revenue.
        #
        # Pure arithmetic identity between two duration facts of the
        # same period, used only when the filer has no standardized
        # GrossProfit concept at all (e.g. a nature-of-expense income
        # statement that still separately tags a cost-of-revenue
        # line). Never backed into from a margin percentage.
        if record.get("gross_profit") is None:

            revenue = record.get("revenue")
            cost_of_revenue = record.get("_cost_of_revenue")

            if (
                revenue is not None
                and cost_of_revenue is not None
            ):

                record["gross_profit"] = (
                    float(revenue) - float(cost_of_revenue)
                )

        # EBITDA fallback: operating_income + depreciation_amortization.
        #
        # Pure arithmetic identity (same formula already used by hand
        # throughout this project, e.g. HENSOLDT/AIXTRON/RENK: EBIT +
        # D&A from the cash flow statement), used only when the filer
        # has no standardized EBITDA concept. Never used to approximate
        # operating_income/ebit themselves -- those remain untouched
        # and NULL unless an explicit standardized concept exists.
        if record.get("ebitda") is None:

            operating_income = record.get("operating_income")
            d_and_a = record.get("_depreciation_amortization")

            if (
                operating_income is not None
                and d_and_a is not None
            ):

                record["ebitda"] = (
                    float(operating_income) + float(d_and_a)
                )

        # Total liabilities fallback: total_assets - total_equity.
        #
        # This is the accounting equation itself (Assets = Liabilities
        # + Equity), not an estimate -- used only when the filer has
        # no standardized Liabilities concept for this period but both
        # Assets and (Stockholders)Equity are present. Where a filer's
        # own Liabilities concept IS present but does not reconcile
        # with Assets - Equity (e.g. due to temporary/redeemable
        # equity, a legitimate third balance-sheet bucket some filers
        # use), that existing value is left untouched here -- see the
        # Phase 4 coverage-scan review notes for RTX/Chevron/Palantir/
        # BridgeBio.
        if record.get("total_liabilities") is None:

            total_assets = record.get("total_assets")
            total_equity = record.get("total_equity")

            if (
                total_assets is not None
                and total_equity is not None
            ):

                record["total_liabilities"] = (
                    float(total_assets) - float(total_equity)
                )

    # --------------------------------------------------------
    # Quality filter
    # --------------------------------------------------------

    usable = []

    important = {
        "revenue",
        "operating_income",
        "net_income",
        "operating_cash_flow",
        "total_assets",
        "total_equity",
    }

    for record in records.values():

        populated = sum(
            1
            for field in important
            if record.get(field) is not None
        )

        # Do not create almost-empty periods.
        if populated < 2:
            continue

        # A reporting period must contain at least one real
        # duration-statement metric. Otherwise an instant
        # balance-sheet comparison date from a later filing can
        # accidentally become a fake quarterly/annual period.
        has_duration_metric = any(
            record.get(field) is not None
            for field in DURATION_FIELDS
        )

        if not has_duration_metric:
            continue

        usable.append(
            record
        )

    # --------------------------------------------------------
    # Limit history
    # --------------------------------------------------------

    annual = sorted(
        (
            r
            for r in usable
            if r["period_type"] == "annual"
        ),
        key=lambda r: r["period_end"],
        reverse=True,
    )[
        :MAX_ANNUAL_PERIODS
    ]

    quarterly = sorted(
        (
            r
            for r in usable
            if r["period_type"] == "quarterly"
        ),
        key=lambda r: r["period_end"],
        reverse=True,
    )[
        :MAX_QUARTERLY_PERIODS
    ]

    result = sorted(
        annual + quarterly,
        key=lambda r: (
            r["period_end"],
            r["period_type"],
        ),
    )

    return (
        result,
        taxonomy_name,
    )


# ============================================================
# STORE
# ============================================================

def store_records(
    conn: sqlite3.Connection,
    security_id: int,
    source_id: int,
    records: list[dict],
) -> int:

    fetched_at = utc_now()

    conn.execute(
        """
        DELETE FROM fundamentals
        WHERE security_id = ?
          AND source_id = ?
        """,
        (
            security_id,
            source_id,
        ),
    )

    conn.executemany(
        """
            INSERT INTO fundamentals (

                security_id,

                period_end,
                period_type,

                fiscal_year,
                fiscal_quarter,

                filing_date,
                currency,

                revenue,
                gross_profit,
                operating_income,
                ebit,
                ebitda,
                net_income,

                eps_basic,
                eps_diluted,

                operating_cash_flow,
                capex,
                free_cash_flow,

                cash,
                total_debt,

                total_assets,
                total_liabilities,
                total_equity,

                shares_outstanding,

                source_id,
                fetched_at
            )

            VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?,
                ?, ?
            )

            ON CONFLICT (
                security_id,
                period_end,
                period_type,
                source_id
            )

            DO UPDATE SET

                fiscal_year =
                    excluded.fiscal_year,

                fiscal_quarter =
                    excluded.fiscal_quarter,

                filing_date =
                    excluded.filing_date,

                currency =
                    excluded.currency,

                revenue =
                    excluded.revenue,

                gross_profit =
                    excluded.gross_profit,

                operating_income =
                    excluded.operating_income,

                ebit =
                    excluded.ebit,

                ebitda =
                    excluded.ebitda,

                net_income =
                    excluded.net_income,

                eps_basic =
                    excluded.eps_basic,

                eps_diluted =
                    excluded.eps_diluted,

                operating_cash_flow =
                    excluded.operating_cash_flow,

                capex =
                    excluded.capex,

                free_cash_flow =
                    excluded.free_cash_flow,

                cash =
                    excluded.cash,

                total_debt =
                    excluded.total_debt,

                total_assets =
                    excluded.total_assets,

                total_liabilities =
                    excluded.total_liabilities,

                total_equity =
                    excluded.total_equity,

                shares_outstanding =
                    excluded.shares_outstanding,

                fetched_at =
                    excluded.fetched_at
            """,
        [
            (
                security_id,

                row["period_end"],
                row["period_type"],

                row["fiscal_year"],
                row["fiscal_quarter"],

                row["filing_date"],
                row["currency"],

                row["revenue"],
                row["gross_profit"],
                row["operating_income"],
                row["ebit"],
                row["ebitda"],
                row["net_income"],

                row["eps_basic"],
                row["eps_diluted"],

                row["operating_cash_flow"],
                row["capex"],
                row["free_cash_flow"],

                row["cash"],
                row["total_debt"],

                row["total_assets"],
                row["total_liabilities"],
                row["total_equity"],

                row["shares_outstanding"],

                source_id,
                fetched_at,
            )
            for row in records
        ],
    )

    return len(records)


# ============================================================
# SINGLE SECURITY
# ============================================================

def import_security(
    conn: sqlite3.Connection,
    security: sqlite3.Row,
    source_id: int,
    use_cache: bool,
    timing: dict,
) -> tuple[
    bool,
    int,
    Optional[str],
]:

    cik = (
        security["cik"]
        or ""
    ).strip()

    if not cik:
        return False, 0, None

    print(
        f"    -> CIK: {cik}"
    )

    t_http0 = time.perf_counter()

    data, from_cache = fetch_companyfacts(
        cik,
        use_cache,
    )

    t_http = time.perf_counter() - t_http0

    timing["http"] += t_http

    print(
        f"    -> source: "
        f"{'cache' if from_cache else 'SEC'} "
        f"({t_http:.2f}s)"
    )

    if not data:

        print(
            "    -> CompanyFacts unavailable"
        )

        return False, 0, None

    t_parse0 = time.perf_counter()

    records, taxonomy = build_records(
        data
    )

    timing["parse"] += (
        time.perf_counter() - t_parse0
    )

    if taxonomy is None:

        concept_groups = (
            data.get("facts")
            or {}
        )

        details = ", ".join(
            f"{key}={len(value)}"
            for key, value
            in concept_groups.items()
            if isinstance(
                value,
                dict,
            )
        )

        print(
            "    -> no usable standard taxonomy"
        )

        if details:
            print(
                f"    -> facts: {details}"
            )

        return False, 0, None

    print(
        f"    -> taxonomy: {taxonomy}"
    )

    print(
        f"    -> parsed periods: {len(records)}"
    )

    if not records:

        print(
            "    -> no usable periods"
        )

        return False, 0, taxonomy

    annual_count = sum(
        1
        for r in records
        if r["period_type"] == "annual"
    )

    quarterly_count = sum(
        1
        for r in records
        if r["period_type"] == "quarterly"
    )

    t_db0 = time.perf_counter()

    conn.execute(
        "BEGIN IMMEDIATE"
    )

    try:

        written = store_records(
            conn,
            security["id"],
            source_id,
            records,
        )

        conn.execute(
            "COMMIT"
        )

    except Exception:

        conn.execute(
            "ROLLBACK"
        )

        raise

    timing["db"] += (
        time.perf_counter() - t_db0
    )

    print(
        f"    -> annual: {annual_count}"
    )

    print(
        f"    -> quarterly: {quarterly_count}"
    )

    print(
        f"    -> rows written: {written}"
    )

    return (
        True,
        written,
        taxonomy,
    )


# ============================================================
# REPORT
# ============================================================

def report(
    conn: sqlite3.Connection,
    source_id: int,
) -> None:

    print()
    print(
        "==============================================="
    )
    print(
        " SEC Fundamentals Status"
    )
    print(
        "==============================================="
    )

    rows = conn.execute(
        """
        SELECT

            s.id,
            s.name,

            COUNT(f.id) AS total_rows,

            SUM(
                CASE
                    WHEN f.period_type = 'annual'
                    THEN 1
                    ELSE 0
                END
            ) AS annual_rows,

            SUM(
                CASE
                    WHEN f.period_type = 'quarterly'
                    THEN 1
                    ELSE 0
                END
            ) AS quarterly_rows,

            MAX(f.period_end) AS latest_period

        FROM fundamentals f

        JOIN security s
            ON s.id = f.security_id

        WHERE f.source_id = ?

        GROUP BY
            s.id,
            s.name

        ORDER BY
            s.name
        """,
        (
            source_id,
        ),
    ).fetchall()

    total = 0

    for row in rows:

        total += row[
            "total_rows"
        ]

        print(
            f"{row['id']:>3} | "
            f"{row['name'][:32]:<32} | "
            f"rows={row['total_rows']:>2} | "
            f"A={row['annual_rows']:>2} | "
            f"Q={row['quarterly_rows']:>2} | "
            f"latest={row['latest_period']}"
        )

    print()

    print(
        f"Fundamental securities : {len(rows)}"
    )

    print(
        f"Fundamental rows       : {total}"
    )

    print()
    print(
        "==============================================="
    )


# ============================================================
# MAIN
# ============================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=(
            "SEC XBRL fundamentals bulk backfill."
        )
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Force a live SEC download for every security, "
            "ignoring the local CompanyFacts cache."
        ),
    )

    return parser.parse_args()


def main() -> None:

    args = parse_args()

    use_cache = not args.no_cache

    t_total0 = time.perf_counter()

    timing = {
        "http": 0.0,
        "parse": 0.0,
        "db": 0.0,
    }

    conn = connect()

    try:

        validate_schema(
            conn
        )

        source_id = get_sec_source_id(
            conn
        )

        securities = get_relevant_sec_securities(
            conn,
            source_id,
        )

        print()
        print(
            "==============================================="
        )
        print(
            " Trading SEC Fundamentals Backfill v1"
        )
        print(
            "==============================================="
        )

        print(
            f"Mapped securities : {len(securities)}"
        )

        print(
            f"Annual history    : {MAX_ANNUAL_PERIODS}"
        )

        print(
            f"Quarter history   : {MAX_QUARTERLY_PERIODS}"
        )

        print(
            f"Local cache       : "
            f"{'enabled' if use_cache else 'disabled (--no-cache)'}"
        )

        print()

        resolved = 0
        skipped = 0
        failed = 0
        total_rows = 0

        skipped_items = []
        failed_items = []

        for index, security in enumerate(
            securities,
            start=1,
        ):

            print(
                f"[{index}/{len(securities)}] "
                f"{security['name']}"
            )

            try:

                ok, written, taxonomy = (
                    import_security(
                        conn,
                        security,
                        source_id,
                        use_cache,
                        timing,
                    )
                )

                if ok:

                    resolved += 1
                    total_rows += written

                else:

                    skipped += 1

                    skipped_items.append(
                        {
                            "id":
                                security["id"],

                            "name":
                                security["name"],
                        }
                    )

            except Exception as exc:

                failed += 1

                failed_items.append(
                    {
                        "id":
                            security["id"],

                        "name":
                            security["name"],

                        "error":
                            str(exc),
                    }
                )

                print(
                    f"    -> ERROR: {exc}"
                )

            print()

        print(
            "==============================================="
        )
        print(
            " Backfill Summary"
        )
        print(
            "==============================================="
        )

        print(
            f"Imported securities : {resolved}"
        )

        print(
            f"Skipped securities  : {skipped}"
        )

        print(
            f"Failed securities   : {failed}"
        )

        print(
            f"Rows written        : {total_rows}"
        )

        if skipped_items:

            print()
            print(
                "Skipped:"
            )

            for item in skipped_items:

                print(
                    f"  {item['id']:>3} | "
                    f"{item['name']}"
                )

        if failed_items:

            print()
            print(
                "Failed:"
            )

            for item in failed_items:

                print(
                    f"  {item['id']:>3} | "
                    f"{item['name']} | "
                    f"{item['error']}"
                )

        report(
            conn,
            source_id,
        )

        total_elapsed = (
            time.perf_counter() - t_total0
        )

        print()
        print(
            f"Total: {total_elapsed:.2f}s | "
            f"HTTP: {timing['http']:.2f}s | "
            f"Parse: {timing['parse']:.2f}s | "
            f"DB: {timing['db']:.2f}s"
        )

    finally:

        conn.close()


if __name__ == "__main__":
    main()
