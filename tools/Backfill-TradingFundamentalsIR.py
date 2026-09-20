from __future__ import annotations

import io
import re
import html
import json
import sqlite3
import http.cookiejar
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

from pypdf import PdfReader

from datetime import datetime, timedelta, timezone
from pathlib import Path


# ============================================================
# CONFIG
# ============================================================

DB_PATH = Path(
    r"C:\KI-Stack\data\trading\trading.db"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}


ASML_URL = (
    "https://ourbrand.asml.com/asset/"
    "157479ce-ba4f-4114-be8b-07b015f553bc/"
    "Financial-statements-US-GAAP-Q2-2026-excel.xlsx"
)

ING_URL = (
    "https://ing.com/binaries/content/assets/"
    "documents/results/2q2026/"
    "2q2026-ing-historical-trend-data.xlsx"
)


NS_MAIN = {
    "m":
        "http://schemas.openxmlformats.org/"
        "spreadsheetml/2006/main",

    "r":
        "http://schemas.openxmlformats.org/"
        "officeDocument/2006/relationships",
}

NS_REL = {
    "r":
        "http://schemas.openxmlformats.org/"
        "package/2006/relationships",
}


# ============================================================
# GENERIC HELPERS
# ============================================================

def utc_now():

    return datetime.now(
        timezone.utc
    ).isoformat()


def connect():

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


def get_source_id(
    conn,
    name,
):

    row = conn.execute(
        """
        SELECT id
        FROM data_sources
        WHERE name = ?
        LIMIT 1
        """,
        (
            name,
        ),
    ).fetchone()

    if row is None:

        raise RuntimeError(
            f"Missing data source: {name}"
        )

    return row["id"]


def download(
    url,
):

    request = urllib.request.Request(
        url,
        headers=HEADERS,
    )

    with urllib.request.urlopen(
        request,
        timeout=60,
    ) as response:

        return response.read()


def number(
    value,
):

    if value is None:
        return None

    try:

        return float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):

        return None


def excel_date(
    value,
):

    value = number(
        value
    )

    if value is None:
        return None

    serial = int(
        value
    )

    # Valid modern Excel date range.
    if not (
        30000
        <= serial
        <= 60000
    ):

        return None

    base = datetime(
        1899,
        12,
        30,
    )

    return (
        base
        +
        timedelta(
            days=serial
        )
    ).date().isoformat()


def multiply_million(
    value,
):

    value = number(
        value
    )

    if value is None:
        return None

    return (
        value
        * 1_000_000
    )


# ============================================================
# XLSX READER - STANDARD LIBRARY ONLY
# ============================================================

def col_number(
    cell_ref,
):

    letters = "".join(
        c
        for c in cell_ref
        if c.isalpha()
    )

    result = 0

    for c in letters:

        result = (
            result * 26
            +
            ord(
                c.upper()
            )
            -
            ord("A")
            +
            1
        )

    return result


def load_shared_strings(
    zf,
):

    try:

        raw = zf.read(
            "xl/sharedStrings.xml"
        )

    except KeyError:

        return []

    root = ET.fromstring(
        raw
    )

    result = []

    for si in root.findall(
        "m:si",
        NS_MAIN,
    ):

        texts = []

        for t in si.iter(
            "{http://schemas.openxmlformats.org/"
            "spreadsheetml/2006/main}t"
        ):

            if t.text:

                texts.append(
                    t.text
                )

        result.append(
            "".join(
                texts
            )
        )

    return result


def workbook_sheets(
    zf,
):

    wb = ET.fromstring(
        zf.read(
            "xl/workbook.xml"
        )
    )

    rels = ET.fromstring(
        zf.read(
            "xl/_rels/workbook.xml.rels"
        )
    )

    rel_map = {}

    for rel in rels.findall(
        "r:Relationship",
        NS_REL,
    ):

        rel_map[
            rel.attrib["Id"]
        ] = rel.attrib[
            "Target"
        ]

    result = {}

    for sheet in wb.findall(
        "m:sheets/m:sheet",
        NS_MAIN,
    ):

        name = sheet.attrib[
            "name"
        ]

        rid = sheet.attrib[
            "{http://schemas.openxmlformats.org/"
            "officeDocument/2006/relationships}id"
        ]

        target = rel_map[
            rid
        ]

        if target.startswith("/"):

            path = target.lstrip("/")

        else:

            path = (
                "xl/"
                +
                target
            )

        result[
            name
        ] = path

    return result


def read_sheet(
    zf,
    path,
    shared,
):

    root = ET.fromstring(
        zf.read(
            path
        )
    )

    result = []

    for row in root.findall(
        ".//m:sheetData/m:row",
        NS_MAIN,
    ):

        excel_row_number = int(
            row.attrib.get(
                "r",
                "0",
            )
        )

        values = {}

        for cell in row.findall(
            "m:c",
            NS_MAIN,
        ):

            ref = cell.attrib.get(
                "r",
                "",
            )

            col = col_number(
                ref
            )

            ctype = cell.attrib.get(
                "t"
            )

            vnode = cell.find(
                "m:v",
                NS_MAIN,
            )

            inline = cell.find(
                "m:is",
                NS_MAIN,
            )

            value = None

            if (
                ctype == "s"
                and vnode is not None
                and vnode.text is not None
            ):

                idx = int(
                    vnode.text
                )

                if idx < len(
                    shared
                ):

                    value = shared[
                        idx
                    ]

            elif (
                ctype == "inlineStr"
                and inline is not None
            ):

                texts = []

                for t in inline.iter(
                    "{http://schemas.openxmlformats.org/"
                    "spreadsheetml/2006/main}t"
                ):

                    if t.text:

                        texts.append(
                            t.text
                        )

                value = "".join(
                    texts
                )

            elif (
                vnode is not None
                and vnode.text is not None
            ):

                value = vnode.text

            values[
                col
            ] = value

        result.append(
            {
                "row_number":
                    excel_row_number,

                "values":
                    values,
            }
        )

    return result


def load_xlsx(
    data,
):

    zf = zipfile.ZipFile(
        io.BytesIO(
            data
        )
    )

    shared = load_shared_strings(
        zf
    )

    paths = workbook_sheets(
        zf
    )

    result = {}

    for name, path in paths.items():

        result[
            name
        ] = read_sheet(
            zf,
            path,
            shared,
        )

    zf.close()

    return result


# ============================================================
# XLSX SEARCH HELPERS
# ============================================================

def row_values(
    row,
):

    return row[
        "values"
    ]


def normalized(
    value,
):

    if value is None:
        return ""

    return (
        str(value)
        .strip()
        .lower()
        .replace("’", "'")
    )


def find_label_row(
    rows,
    label,
):

    target = normalized(
        label
    )

    for row in rows:

        values = row_values(
            row
        )

        first = values.get(
            1
        )

        if normalized(
            first
        ) == target:

            return values

    return {}


def find_first_matching_label(
    rows,
    labels,
):

    for label in labels:

        row = find_label_row(
            rows,
            label,
        )

        if row:

            return row

    return {}


def find_excel_date_row(
    rows,
    min_dates=3,
):

    best = None

    best_count = 0

    for row in rows:

        values = row_values(
            row
        )

        count = 0

        for value in values.values():

            if excel_date(
                value
            ):

                count += 1

        if count > best_count:

            best_count = count
            best = values

    if (
        best is None
        or best_count < min_dates
    ):

        return {}

    return best


def find_quarter_header_row(
    rows,
):

    regex = re.compile(
        r"^[1-4]Q\d{4}$",
        re.IGNORECASE,
    )

    best = None
    best_count = 0

    for row in rows:

        values = row_values(
            row
        )

        count = 0

        for value in values.values():

            if (
                value is not None
                and regex.match(
                    str(value).strip()
                )
            ):

                count += 1

        if count > best_count:

            best_count = count
            best = values

    if (
        best is None
        or best_count < 2
    ):

        return {}

    return best


# ============================================================
# DATABASE UPSERT
# ============================================================

def upsert_fundamental(
    conn,
    source_id,
    security_id,
    row,
):

    conn.execute(
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
        (
            security_id,

            row["period_end"],
            row.get(
                "period_type",
                "quarterly",
            ),

            row["fiscal_year"],
            row["fiscal_quarter"],

            row.get(
                "filing_date"
            ),

            row["currency"],

            row.get(
                "revenue"
            ),

            row.get(
                "gross_profit"
            ),

            row.get(
                "operating_income"
            ),

            row.get(
                "ebit"
            ),

            row.get(
                "ebitda"
            ),

            row.get(
                "net_income"
            ),

            row.get(
                "eps_basic"
            ),

            row.get(
                "eps_diluted"
            ),

            row.get(
                "operating_cash_flow"
            ),

            row.get(
                "capex"
            ),

            row.get(
                "free_cash_flow"
            ),

            row.get(
                "cash"
            ),

            row.get(
                "total_debt"
            ),

            row.get(
                "total_assets"
            ),

            row.get(
                "total_liabilities"
            ),

            row.get(
                "total_equity"
            ),

            row.get(
                "shares_outstanding"
            ),

            source_id,
            utc_now(),
        ),
    )


# ============================================================
# ASML
# ============================================================

def parse_asml():

    data = download(
        ASML_URL
    )

    sheets = load_xlsx(
        data
    )

    ops = sheets[
        "Q Statements of Operations"
    ]

    bs = sheets[
        "Q Consolidated Balance Sheets"
    ]

    cf = sheets[
        "Q Statements of Cash Flows"
    ]

    date_row = find_excel_date_row(
        ops
    )

    if not date_row:

        raise RuntimeError(
            "ASML date row not found"
        )

    # Build actual data columns from date serials.
    date_columns = {}

    for col, value in date_row.items():

        date = excel_date(
            value
        )

        if date:

            date_columns[
                col
            ] = date

    if not date_columns:

        raise RuntimeError(
            "ASML has no usable date columns"
        )

    revenue = find_label_row(
        ops,
        "Total net sales",
    )

    gross_profit = find_label_row(
        ops,
        "Gross profit",
    )

    operating_income = find_label_row(
        ops,
        "Income from operations",
    )

    net_income = find_first_matching_label(
        cf,
        [
            "Net income",
        ],
    )

    operating_cash_flow = find_first_matching_label(
        cf,
        [
            "Net cash provided by (used in) operating activities",
        ],
    )

    capex = find_first_matching_label(
        cf,
        [
            "Purchase of property, plant and equipment",
        ],
    )

    cash = find_first_matching_label(
        bs,
        [
            "Cash and cash equivalents",
        ],
    )

    total_assets = find_first_matching_label(
        bs,
        [
            "Total assets",
        ],
    )

    total_liabilities = find_first_matching_label(
        bs,
        [
            "Total liabilities",
        ],
    )

    total_equity = find_first_matching_label(
        bs,
        [
            "Total shareholders' equity",
            "Total shareholders’ equity",
            "Total equity",
        ],
    )

    current_debt = find_first_matching_label(
        bs,
        [
            "Current portion of long-term debt",
            "Current portion of long term debt",
            "Current portion of long-term debt and finance lease obligations",
        ],
    )

    long_term_debt = find_first_matching_label(
        bs,
        [
            "Long-term debt",
            "Long term debt",
            "Long-term debt and finance lease obligations",
        ],
    )

    result = []

    for col, period_end in sorted(
        date_columns.items(),
        key=lambda x: x[1],
    ):

        revenue_value = number(
            revenue.get(
                col
            )
        )

        # Ignore a date column if there is no quarterly P&L data.
        if revenue_value is None:
            continue

        dt = datetime.strptime(
            period_end,
            "%Y-%m-%d",
        )

        quarter = (
            (
                dt.month
                - 1
            )
            // 3
            + 1
        )

        gp = number(
            gross_profit.get(
                col
            )
        )

        op = number(
            operating_income.get(
                col
            )
        )

        ni = number(
            net_income.get(
                col
            )
        )

        ocf = number(
            operating_cash_flow.get(
                col
            )
        )

        capex_value = number(
            capex.get(
                col
            )
        )

        if capex_value is not None:

            capex_value = abs(
                capex_value
            )

        debt_current = number(
            current_debt.get(
                col
            )
        )

        debt_long = number(
            long_term_debt.get(
                col
            )
        )

        total_debt = None

        if (
            debt_current is not None
            or
            debt_long is not None
        ):

            total_debt = (
                float(
                    debt_current or 0
                )
                +
                float(
                    debt_long or 0
                )
            )

        result.append(
            {
                "period_end":
                    period_end,

                "fiscal_year":
                    dt.year,

                "fiscal_quarter":
                    quarter,

                "filing_date":
                    None,

                "currency":
                    "EUR",

                "revenue":
                    multiply_million(
                        revenue_value
                    ),

                "gross_profit":
                    multiply_million(
                        gp
                    ),

                "operating_income":
                    multiply_million(
                        op
                    ),

                # For ASML's quarterly US-GAAP statement,
                # income from operations is used as EBIT.
                "ebit":
                    multiply_million(
                        op
                    ),

                "ebitda":
                    None,

                "net_income":
                    multiply_million(
                        ni
                    ),

                "eps_basic":
                    None,

                "eps_diluted":
                    None,

                "operating_cash_flow":
                    multiply_million(
                        ocf
                    ),

                "capex":
                    multiply_million(
                        capex_value
                    ),

                "free_cash_flow":
                    multiply_million(
                        (
                            ocf
                            -
                            capex_value
                        )
                        if (
                            ocf is not None
                            and capex_value is not None
                        )
                        else None
                    ),

                "cash":
                    multiply_million(
                        cash.get(
                            col
                        )
                    ),

                "total_debt":
                    multiply_million(
                        total_debt
                    ),

                "total_assets":
                    multiply_million(
                        total_assets.get(
                            col
                        )
                    ),

                "total_liabilities":
                    multiply_million(
                        total_liabilities.get(
                            col
                        )
                    ),

                "total_equity":
                    multiply_million(
                        total_equity.get(
                            col
                        )
                    ),

                "shares_outstanding":
                    None,
            }
        )

    return result


# ============================================================
# ING
# ============================================================

def quarter_end(
    year,
    quarter,
):

    if quarter == 1:
        return f"{year}-03-31"

    if quarter == 2:
        return f"{year}-06-30"

    if quarter == 3:
        return f"{year}-09-30"

    return f"{year}-12-31"


def parse_ing():

    data = download(
        ING_URL
    )

    sheets = load_xlsx(
        data
    )

    pnl = sheets[
        "1.3 P&L QO"
    ]

    assets = sheets[
        "2.2 Group Bal Assets QO"
    ]

    liabilities = sheets[
        "2.4 Group Bal Liabilities QO"
    ]

    header = find_quarter_header_row(
        pnl
    )

    if not header:

        raise RuntimeError(
            "ING quarterly header not found"
        )

    quarter_columns = {}

    for col, value in header.items():

        if value is None:
            continue

        match = re.fullmatch(
            r"([1-4])Q(\d{4})",
            str(
                value
            ).strip(),
        )

        if not match:
            continue

        quarter = int(
            match.group(1)
        )

        year = int(
            match.group(2)
        )

        quarter_columns[
            col
        ] = (
            year,
            quarter,
        )

    if not quarter_columns:

        raise RuntimeError(
            "ING quarter columns not found"
        )

    total_income = find_label_row(
        pnl,
        "Total income",
    )

    gross_result = find_label_row(
        pnl,
        "Gross result",
    )

    result_before_tax = find_label_row(
        pnl,
        "Result before tax",
    )

    taxation = find_label_row(
        pnl,
        "Taxation",
    )

    non_controlling_interests = find_label_row(
        pnl,
        "Non-controlling interests",
    )

    net_result = find_first_matching_label(
        pnl,
        [
            "Net result",
            "Net result attributable to shareholders of ING Group",
            "Net result attributable to shareholders",
        ],
    )

    cash = find_first_matching_label(
        assets,
        [
            "Cash and balances with central banks",
        ],
    )

    total_assets = find_first_matching_label(
        assets,
        [
            "Total assets",
        ],
    )

    total_liabilities = find_first_matching_label(
        liabilities,
        [
            "Total liabilities",
        ],
    )

    total_equity = find_first_matching_label(
        liabilities,
        [
            "Total equity",
        ],
    )

    debt_securities = find_first_matching_label(
        liabilities,
        [
            "Debt securities in issue",
        ],
    )

    subordinated_loans = find_first_matching_label(
        liabilities,
        [
            "Subordinated loans",
        ],
    )

    result = []

    for col, (
        year,
        quarter,
    ) in sorted(
        quarter_columns.items(),
        key=lambda x: (
            x[1][0],
            x[1][1],
        ),
    ):

        income = number(
            total_income.get(
                col
            )
        )

        # Future/blank quarter.
        if income is None:
            continue

        debt_issued = number(
            debt_securities.get(
                col
            )
        )

        sub_debt = number(
            subordinated_loans.get(
                col
            )
        )

        total_debt = None

        if (
            debt_issued is not None
            or
            sub_debt is not None
        ):

            total_debt = (
                float(
                    debt_issued or 0
                )
                +
                float(
                    sub_debt or 0
                )
            )

        gross = number(
            gross_result.get(
                col
            )
        )

        before_tax = number(
            result_before_tax.get(
                col
            )
        )

        net = number(
            net_result.get(
                col
            )
        )

        # ING Historical Trend Data does not necessarily expose
        # a dedicated quarterly "Net result attributable" row.
        # Reconstruct attributable net income when needed:
        #
        # Result before tax
        # - Taxation
        # - Non-controlling interests
        #
        # ING reports Taxation and NCI as positive deductions.
        if net is None:

            tax = number(
                taxation.get(
                    col
                )
            )

            nci = number(
                non_controlling_interests.get(
                    col
                )
            )

            before_tax_value = number(
                result_before_tax.get(
                    col
                )
            )

            if (
                before_tax_value is not None
                and tax is not None
            ):

                net = (
                    before_tax_value
                    -
                    tax
                    -
                    float(
                        nci or 0
                    )
                )

        result.append(
            {
                "period_end":
                    quarter_end(
                        year,
                        quarter,
                    ),

                "fiscal_year":
                    year,

                "fiscal_quarter":
                    quarter,

                "filing_date":
                    None,

                "currency":
                    "EUR",

                # Bank-specific interpretation:
                # revenue = Total income.
                "revenue":
                    multiply_million(
                        income
                    ),

                "gross_profit":
                    None,

                # Gross result after operating expenses.
                "operating_income":
                    multiply_million(
                        gross
                    ),

                # Result before tax is NOT identical to EBIT
                # for a bank; keep EBIT NULL.
                "ebit":
                    None,

                "ebitda":
                    None,

                "net_income":
                    multiply_million(
                        net
                    ),

                "eps_basic":
                    None,

                "eps_diluted":
                    None,

                "operating_cash_flow":
                    None,

                "capex":
                    None,

                "free_cash_flow":
                    None,

                "cash":
                    multiply_million(
                        cash.get(
                            col
                        )
                    ),

                "total_debt":
                    multiply_million(
                        total_debt
                    ),

                "total_assets":
                    multiply_million(
                        total_assets.get(
                            col
                        )
                    ),

                "total_liabilities":
                    multiply_million(
                        total_liabilities.get(
                            col
                        )
                    ),

                "total_equity":
                    multiply_million(
                        total_equity.get(
                            col
                        )
                    ),

                "shares_outstanding":
                    None,
            }
        )

    return result



# ============================================================
# TSMC
# ============================================================

TSMC_QUARTERS = [
    (2025, 1),
    (2025, 2),
    (2025, 3),
    (2025, 4),
    (2026, 1),
    (2026, 2),
]


TSMC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)


def tsmc_make_opener():

    cookies = http.cookiejar.CookieJar()

    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(
            cookies
        )
    )


def tsmc_get_page(
    opener,
    url,
):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                TSMC_UA,

            "Accept":
                "text/html,"
                "application/xhtml+xml,"
                "*/*;q=0.8",

            "Accept-Language":
                "en-US,en;q=0.9",
        },
    )

    with opener.open(
        request,
        timeout=60,
    ) as response:

        return response.read().decode(
            "utf-8",
            errors="replace",
        )


def tsmc_find_pdf(
    page_url,
    page_html,
):

    links = re.findall(
        r"""href=["']([^"']+\.pdf(?:\?[^"']*)?)["']""",
        page_html,
        flags=re.I,
    )

    candidates = []

    for href in links:

        href = html.unescape(
            href
        )

        full = urllib.parse.urljoin(
            page_url,
            href,
        )

        candidates.append(
            full
        )

    preferred = [
        url
        for url in candidates
        if (
            re.search(
                r"/FS(?:\.pdf|\?)",
                url,
                re.I,
            )
            or
            "financial" in url.lower()
        )
    ]

    if preferred:
        return preferred[0]

    raise RuntimeError(
        f"TSMC Financial Statements PDF not found: {page_url}"
    )


def tsmc_download_pdf(
    opener,
    page_url,
    pdf_url,
):

    request = urllib.request.Request(
        pdf_url,
        headers={
            "User-Agent":
                TSMC_UA,

            "Referer":
                page_url,

            "Accept":
                "application/pdf,"
                "*/*;q=0.8",

            "Accept-Language":
                "en-US,en;q=0.9",
        },
    )

    with opener.open(
        request,
        timeout=60,
    ) as response:

        data = response.read()

    if not data.startswith(
        b"%PDF"
    ):

        raise RuntimeError(
            f"TSMC response is not PDF: {pdf_url}"
        )

    return data


def tsmc_pdf_lines(
    data,
):

    reader = PdfReader(
        io.BytesIO(
            data
        )
    )

    text = "\n".join(
        page.extract_text() or ""
        for page in reader.pages
    )

    return [
        re.sub(
            r"\s+",
            " ",
            line,
        ).strip()

        for line in text.splitlines()

        if line.strip()
    ]


def tsmc_numbers(
    line,
):

    # Numbers may look like:
    #
    # 1,270,381
    # 100.0
    # (496,002)
    #
    # Parentheses indicate a negative value.
    tokens = re.findall(
        r"\(?-?\d[\d,]*(?:\.\d+)?\)?",
        line,
    )

    result = []

    for token in tokens:

        negative = (
            token.startswith("(")
            and token.endswith(")")
        )

        cleaned = (
            token
            .strip("()")
            .replace(",", "")
        )

        value = float(
            cleaned
        )

        if negative:
            value = -value

        result.append(
            value
        )

    return result


def tsmc_find_line(
    lines,
    label,
    minimum_numbers=2,
):

    target = label.lower()

    for line in lines:

        if not line.lower().startswith(
            target
        ):
            continue

        values = tsmc_numbers(
            line
        )

        if len(values) >= minimum_numbers:
            return line

    raise RuntimeError(
        f"TSMC line not found: {label}"
    )


def tsmc_balance_value(
    lines,
    label,
):

    line = tsmc_find_line(
        lines,
        label,
        minimum_numbers=2,
    )

    values = tsmc_numbers(
        line
    )

    # Layout:
    #
    # USD equivalent | NTD current | % | NTD previous ...
    #
    # We store the NTD value.
    return values[1]


def tsmc_quarter_income_value(
    lines,
    label,
):

    line = tsmc_find_line(
        lines,
        label,
        minimum_numbers=2,
    )

    values = tsmc_numbers(
        line
    )

    # First occurrence in every report is the
    # three-month statement:
    #
    # USD equivalent | NTD current quarter | %
    return values[1]


def tsmc_cashflow_value(
    lines,
    label,
    quarter,
):

    line = tsmc_find_line(
        lines,
        label,
        minimum_numbers=2,
    )

    values = tsmc_numbers(
        line
    )

    # Q1:
    # USD | current-quarter NTD | comparison...
    #
    # Q2-Q4:
    # USD | YTD NTD | current-quarter NTD |
    # prior-quarter/comparison...
    if quarter == 1:

        if len(values) < 2:
            raise RuntimeError(
                f"TSMC Q1 cash-flow layout invalid: {line}"
            )

        return values[1]

    if len(values) < 3:
        raise RuntimeError(
            f"TSMC cash-flow layout invalid: {line}"
        )

    return values[2]


def tsmc_capex_value(
    lines,
    quarter,
):

    # "Property, Plant and Equipment" appears in:
    #
    # 1. Balance sheet
    # 2. Cash-flow CAPEX purchase line
    # 3. Cash-flow disposal/proceeds line
    #
    # The balance-sheet row can itself contain a negative
    # change value, so "any negative number" is not sufficient.
    #
    # The CAPEX cash-flow row consistently contains several
    # negative cash-flow figures.

    candidates = []

    for line in lines:

        if not line.lower().startswith(
            "property, plant and equipment"
        ):
            continue

        values = tsmc_numbers(
            line
        )

        negative_count = sum(
            1
            for value in values
            if value < 0
        )

        if negative_count >= 3:
            candidates.append(
                (line, values)
            )

    if len(candidates) != 1:

        print()
        print(
            "TSMC CAPEX candidates:"
        )

        for line, values in candidates:
            print(
                "  ",
                line,
            )

        raise RuntimeError(
            "TSMC CAPEX line ambiguous: "
            f"{len(candidates)} candidates"
        )

    line, values = candidates[0]

    # Q1:
    # USD | current-quarter NTD | comparison...
    if quarter == 1:

        if len(values) < 2:
            raise RuntimeError(
                f"TSMC Q1 CAPEX layout invalid: {line}"
            )

        return abs(
            values[1]
        )

    # Q2-Q4:
    # USD | YTD NTD | current-quarter NTD |
    # prior-quarter/comparison...
    if len(values) < 3:

        raise RuntimeError(
            f"TSMC CAPEX layout invalid: {line}"
        )

    return abs(
        values[2]
    )



def parse_tsmc_quarter(
    year,
    quarter,
):

    page_url = (
        "https://investor.tsmc.com/english/"
        f"quarterly-results/{year}/q{quarter}"
    )

    opener = tsmc_make_opener()

    page_html = tsmc_get_page(
        opener,
        page_url,
    )

    pdf_url = tsmc_find_pdf(
        page_url,
        page_html,
    )

    pdf = tsmc_download_pdf(
        opener,
        page_url,
        pdf_url,
    )

    lines = tsmc_pdf_lines(
        pdf
    )

    revenue = tsmc_quarter_income_value(
        lines,
        "Net Revenue",
    )

    gross_profit = tsmc_quarter_income_value(
        lines,
        "Gross Profit",
    )

    operating_income = tsmc_quarter_income_value(
        lines,
        "Income from Operations",
    )

    net_income = tsmc_quarter_income_value(
        lines,
        "Shareholders of the Parent",
    )

    cash = tsmc_balance_value(
        lines,
        "Cash and Cash Equivalents",
    )

    total_assets = tsmc_balance_value(
        lines,
        "Total Assets",
    )

    total_liabilities = tsmc_balance_value(
        lines,
        "Total Liabilities",
    )

    # Total equity including non-controlling interests:
    # Assets - Liabilities.
    total_equity = (
        total_assets
        -
        total_liabilities
    )

    operating_cash_flow = tsmc_cashflow_value(
        lines,
        "Net Cash Generated by Operating Activities",
        quarter,
    )

    capex = tsmc_capex_value(
        lines,
        quarter,
    )

    free_cash_flow = (
        operating_cash_flow
        -
        capex
    )

    period_end = quarter_end(
        year,
        quarter,
    )

    return {
        "period_end":
            period_end,

        "fiscal_year":
            year,

        "fiscal_quarter":
            quarter,

        "filing_date":
            None,

        "currency":
            "TWD",

        # TSMC reports amounts in NT$ millions.
        "revenue":
            revenue
            * 1_000_000,

        "gross_profit":
            gross_profit
            * 1_000_000,

        "operating_income":
            operating_income
            * 1_000_000,

        "ebit":
            operating_income
            * 1_000_000,

        "ebitda":
            None,

        # Net income attributable to shareholders
        # of the parent.
        "net_income":
            net_income
            * 1_000_000,

        "eps_basic":
            None,

        "eps_diluted":
            None,

        "operating_cash_flow":
            operating_cash_flow
            * 1_000_000,

        "capex":
            capex
            * 1_000_000,

        "free_cash_flow":
            free_cash_flow
            * 1_000_000,

        "cash":
            cash
            * 1_000_000,

        # Do not infer total debt from incomplete
        # debt-line coverage.
        "total_debt":
            None,

        "total_assets":
            total_assets
            * 1_000_000,

        "total_liabilities":
            total_liabilities
            * 1_000_000,

        "total_equity":
            total_equity
            * 1_000_000,

        "shares_outstanding":
            None,

        "_pdf_url":
            pdf_url,
    }


def parse_tsmc():

    result = []

    for year, quarter in TSMC_QUARTERS:

        print(
            f"    TSMC {year} Q{quarter}..."
        )

        row = parse_tsmc_quarter(
            year,
            quarter,
        )

        result.append(
            row
        )

    return result


# ============================================================
# SK HYNIX (DART / OpenDART XBRL viewer, no API key required)
# ============================================================

# rcpNo (DART filing id) -> xbrlExtSeq (internal viewer id) resolved via
# https://opendart.fss.or.kr/xbrl/viewer/main.do?lang=en&rcpNo=<rcpNo>
#
# Q1/Q3 2025, Q1 2026 : quarterly report (분기보고서)
# Q2 2025, Q2 2026    : semiannual report (반기보고서)
# FY2025 (Q4)         : annual report (사업보고서) -- no discrete Q4 column
SK_HYNIX_XBRL_EXT_SEQ = {
    (2025, 1): "20250516000639",
    (2025, 2): "20250814001572",
    (2025, 3): "20251114001304",
    (2025, 4): "20260317000044",
    (2026, 1): "20260515001618",
    (2026, 2): "20260814001897",
}

SK_HYNIX_QUARTERS = [
    (2025, 1),
    (2025, 2),
    (2025, 3),
    (2025, 4),
    (2026, 1),
    (2026, 2),
]

# IFRS consolidated-statements role ids (Statement of financial position,
# comprehensive income and cash flows) found on the filings' main.do
# role tree.
SK_HYNIX_ROLE_INCOME = "D431410"
SK_HYNIX_ROLE_BALANCE = "D210000"
SK_HYNIX_ROLE_CASHFLOW = "D520000"

SK_HYNIX_INCOME_LABELS = {
    "revenue":
        "Revenue",

    "gross_profit":
        "Gross profit",

    # DART auto-generates the "(loss)" suffix only when a loss is
    # present in some column of that filing -- 2025 filings show
    # "Operating income(loss)", 2026 filings show plain "Operating
    # income". dart_row() matches by prefix so both resolve.
    "operating_income":
        "Operating income",

    # net_income is resolved separately via dart_row_any() below --
    # SK hynix's own press releases report "Net Income" as TOTAL
    # profit for the period (label "Profit (loss)" / "Profit",
    # before the owners/non-controlling-interest split), not the
    # "attributable to owners of parent" line. Verified against SK
    # hynix's official Q2 2026 release: 93,922.6bn KRW matches the
    # "Profit" line (93,922,593,000,000), not the 93,820.2bn
    # "attributable to owners of parent" line.
    "eps_basic":
        "Basic earnings (loss) per share",

    "eps_diluted":
        "Diluted earnings (loss) per share",
}

# Exact-match candidates only (no prefix fallback): "Profit" alone
# would otherwise risk matching "Profit before tax" if iterated
# first, since dict-key lookup for dart_row_any() requires an exact
# label match, never a substring/prefix match.
SK_HYNIX_NET_INCOME_LABELS = [
    "Profit (loss)",
    "Profit",
]

SK_HYNIX_BALANCE_LABELS = {
    "cash":
        "Cash and cash equivalents",

    "total_assets":
        "Total assets",

    "total_liabilities":
        "Total liabilities",

    "total_equity":
        "Total equity",
}

# Both are clean, complete "borrowings" totals (not the broader
# "other financial liabilities" lines) -- summed only when at least
# one is present, per the same current+long-term pattern used for ASML.
SK_HYNIX_DEBT_LABELS = [
    "Current borrowings and current portion of non-current borrowings",
    "Non-current portion of non-current borrowings",
]

SK_HYNIX_CASHFLOW_LABELS = {
    "operating_cash_flow":
        "Cash flows from (used in) operating activities",

    "capex":
        "Purchase of property, plant and equipment, "
        "classified as investing activities",
}


def dart_fetch_view(
    xbrl_ext_seq,
    role_id,
):

    url = (
        "https://opendart.fss.or.kr/xbrl/viewer/view.do"
        f"?xbrlExtSeq={xbrl_ext_seq}"
        f"&roleId={role_id}"
        "&lang=en"
    )

    return download(
        url
    ).decode(
        "utf-8",
        errors="replace",
    )


def dart_parse_fact_table(
    html_text,
):

    start = html_text.find(
        'class="fact-table"'
    )

    if start == -1:

        raise RuntimeError(
            "DART fact-table not found"
        )

    table_html = html_text[start:]

    end = table_html.find(
        "</table>"
    )

    if end != -1:

        table_html = table_html[
            : end + len("</table>")
        ]

    headers = re.findall(
        r'<th class="period">([^<]*)</th>',
        table_html,
    )

    rows = {}

    for tr in re.findall(
        r"<tr>(.*?)</tr>",
        table_html,
        re.S,
    ):

        label_match = re.search(
            r'concept-label"[^>]*>([^<]*)</span>',
            tr,
        )

        if not label_match:
            continue

        label = label_match.group(1).strip()

        values = re.findall(
            r'fact-value"[^>]*>([^<]*)</span>',
            tr,
        )

        if not values:
            continue

        if label not in rows:

            rows[label] = values

    return headers, rows


def dart_row(
    rows,
    label_prefix,
):

    if label_prefix in rows:
        return rows[label_prefix]

    for label, values in rows.items():

        if label.startswith(
            label_prefix
        ):
            return values

    return None


def dart_row_any(
    rows,
    exact_candidates,
):

    for candidate in exact_candidates:

        if candidate in rows:
            return rows[candidate]

    return None


def dart_pick_column(
    headers,
    values,
    target_period,
):

    if values is None:
        return None

    for header, value in zip(
        headers,
        values,
    ):

        if header.strip() == target_period:
            return value

    return None


def dart_number(
    raw,
):

    if raw is None:
        return None

    raw = raw.strip()

    if raw == "":
        return None

    try:

        return float(
            raw.replace(
                ",",
                "",
            )
        )

    except ValueError:

        return None


def dart_extract_income(
    html_text,
    period_text,
    include_eps,
):

    headers, rows = dart_parse_fact_table(
        html_text
    )

    result = {}

    for field, label in SK_HYNIX_INCOME_LABELS.items():

        if not include_eps and field in (
            "eps_basic",
            "eps_diluted",
        ):
            result[field] = None
            continue

        values = dart_row(
            rows,
            label,
        )

        result[field] = dart_number(
            dart_pick_column(
                headers,
                values,
                period_text,
            )
        )

    net_income_values = dart_row_any(
        rows,
        SK_HYNIX_NET_INCOME_LABELS,
    )

    result["net_income"] = dart_number(
        dart_pick_column(
            headers,
            net_income_values,
            period_text,
        )
    )

    return result


def dart_extract_balance(
    html_text,
    instant_date,
):

    headers, rows = dart_parse_fact_table(
        html_text
    )

    result = {}

    for field, label in SK_HYNIX_BALANCE_LABELS.items():

        values = dart_row(
            rows,
            label,
        )

        result[field] = dart_number(
            dart_pick_column(
                headers,
                values,
                instant_date,
            )
        )

    debt_total = 0.0
    debt_found = False

    for label in SK_HYNIX_DEBT_LABELS:

        values = dart_row(
            rows,
            label,
        )

        value = dart_number(
            dart_pick_column(
                headers,
                values,
                instant_date,
            )
        )

        if value is not None:

            debt_total += value
            debt_found = True

    result["total_debt"] = (
        debt_total
        if debt_found
        else None
    )

    return result


def dart_extract_cashflow(
    html_text,
    period_text,
):

    headers, rows = dart_parse_fact_table(
        html_text
    )

    result = {}

    for field, label in SK_HYNIX_CASHFLOW_LABELS.items():

        values = dart_row(
            rows,
            label,
        )

        value = dart_number(
            dart_pick_column(
                headers,
                values,
                period_text,
            )
        )

        # DART reports this line as a negative (cash outflow);
        # stored as a positive magnitude to match the convention
        # already used for ASML/TSMC, where
        # free_cash_flow = operating_cash_flow - capex.
        if field == "capex" and value is not None:

            value = abs(
                value
            )

        result[field] = value

    return result


def parse_sk_hynix():

    # Cache every (year, quarter)'s three statement pages once --
    # several quarters need each other's cash-flow cumulative totals.
    income_html = {}
    balance_html = {}
    cashflow_html = {}

    for year, quarter in SK_HYNIX_QUARTERS:

        xbrl_ext_seq = SK_HYNIX_XBRL_EXT_SEQ[
            (year, quarter)
        ]

        print(
            f"    SK hynix {year} Q{quarter}..."
        )

        income_html[(year, quarter)] = dart_fetch_view(
            xbrl_ext_seq,
            SK_HYNIX_ROLE_INCOME,
        )

        balance_html[(year, quarter)] = dart_fetch_view(
            xbrl_ext_seq,
            SK_HYNIX_ROLE_BALANCE,
        )

        cashflow_html[(year, quarter)] = dart_fetch_view(
            xbrl_ext_seq,
            SK_HYNIX_ROLE_CASHFLOW,
        )

    def quarter_start(
        year,
        quarter,
    ):

        month = {
            1: "01-01",
            2: "04-01",
            3: "07-01",
            4: "10-01",
        }[quarter]

        return f"{year}-{month}"

    def std_period_text(
        year,
        quarter,
    ):

        return (
            f"{quarter_start(year, quarter)} ~ "
            f"{quarter_end(year, quarter)}"
        )

    # Standalone-quarter cash flow (only Q1 filings expose a column
    # whose date range equals the quarter itself; every other filing
    # only ever exposes a year-to-date cumulative column -- confirmed
    # by inspecting Q1/H1/9M/FY cash-flow pages directly). For every
    # other quarter, the standalone value is derived as an EXACT
    # subtraction of two officially filed cumulative totals:
    #   Q2 = H1_cumulative  - Q1_actual
    #   Q3 = 9M_cumulative  - H1_cumulative
    #   Q4 = FY_cumulative  - 9M_cumulative
    # This is not an estimate -- both operands are complete, verified
    # primary-source totals, and the identity is exact for flow items.
    cashflow_cumulative = {}

    for year, quarter in SK_HYNIX_QUARTERS:

        cashflow_cumulative[(year, quarter)] = dart_extract_cashflow(
            cashflow_html[(year, quarter)],
            f"{year}-01-01 ~ {quarter_end(year, quarter)}",
        )

    cashflow_standalone = {}

    prior_cumulative = None

    for year, quarter in SK_HYNIX_QUARTERS:

        current_cumulative = cashflow_cumulative[(year, quarter)]

        if quarter == 1:

            cashflow_standalone[(year, quarter)] = dict(
                current_cumulative
            )

        else:

            derived = {}

            for field in SK_HYNIX_CASHFLOW_LABELS:

                current_value = current_cumulative.get(
                    field
                )

                prior_value = (
                    prior_cumulative.get(
                        field
                    )
                    if prior_cumulative
                    else None
                )

                derived[field] = (
                    current_value
                    - prior_value
                    if current_value is not None
                    and prior_value is not None
                    else None
                )

            cashflow_standalone[(year, quarter)] = derived

        prior_cumulative = current_cumulative

    result = []

    for year, quarter in SK_HYNIX_QUARTERS:

        if quarter == 4:

            # No discrete Q4 income-statement column exists (annual
            # reports only ever expose the full year). Derived the
            # same way as the cash-flow quarters above: FY - 9M,
            # an exact subtraction of two complete, filed cumulative
            # totals. EPS is never derived (not additive across a
            # changing weighted-average share count) -- left NULL.
            fy_cumulative = dart_extract_income(
                income_html[(year, quarter)],
                f"{year}-01-01 ~ {year}-12-31",
                include_eps=False,
            )

            ninem_cumulative = dart_extract_income(
                income_html[(year, 3)],
                f"{year}-01-01 ~ {year}-09-30",
                include_eps=False,
            )

            income = {}

            for field, fy_value in fy_cumulative.items():

                ninem_value = ninem_cumulative.get(
                    field
                )

                income[field] = (
                    fy_value
                    - ninem_value
                    if fy_value is not None
                    and ninem_value is not None
                    else None
                )

            income["eps_basic"] = None
            income["eps_diluted"] = None

        else:

            income = dart_extract_income(
                income_html[(year, quarter)],
                std_period_text(
                    year,
                    quarter,
                ),
                include_eps=True,
            )

        balance = dart_extract_balance(
            balance_html[(year, quarter)],
            quarter_end(
                year,
                quarter,
            ),
        )

        cashflow = cashflow_standalone[(year, quarter)]

        operating_cash_flow = cashflow.get(
            "operating_cash_flow"
        )

        capex = cashflow.get(
            "capex"
        )

        free_cash_flow = (
            operating_cash_flow
            - capex
            if operating_cash_flow is not None
            and capex is not None
            else None
        )

        result.append(
            {
                "period_end":
                    quarter_end(
                        year,
                        quarter,
                    ),

                "fiscal_year":
                    year,

                "fiscal_quarter":
                    quarter,

                "filing_date":
                    None,

                "currency":
                    "KRW",

                "revenue":
                    income.get(
                        "revenue"
                    ),

                "gross_profit":
                    income.get(
                        "gross_profit"
                    ),

                "operating_income":
                    income.get(
                        "operating_income"
                    ),

                # Not disclosed as a distinct line item --
                # never mapped from operating income.
                "ebit":
                    None,

                "ebitda":
                    None,

                "net_income":
                    income.get(
                        "net_income"
                    ),

                "eps_basic":
                    income.get(
                        "eps_basic"
                    ),

                "eps_diluted":
                    income.get(
                        "eps_diluted"
                    ),

                "operating_cash_flow":
                    operating_cash_flow,

                "capex":
                    capex,

                "free_cash_flow":
                    free_cash_flow,

                "cash":
                    balance.get(
                        "cash"
                    ),

                "total_debt":
                    balance.get(
                        "total_debt"
                    ),

                "total_assets":
                    balance.get(
                        "total_assets"
                    ),

                "total_liabilities":
                    balance.get(
                        "total_liabilities"
                    ),

                "total_equity":
                    balance.get(
                        "total_equity"
                    ),

                "shares_outstanding":
                    None,
            }
        )

    return result


# ============================================================
# BAE SYSTEMS
# ============================================================

# baesystems.com serves every /investors/dam/ asset (PDF and ESEF-zip
# downloads) behind Incapsula bot-detection that returns HTTP 403 to
# stdlib urllib requests -- confirmed even when replicating the exact
# browser User-Agent and session cookies, so this is almost certainly
# a TLS/JA3 fingerprint check a plain Python HTTPS client cannot pass.
# There is no quarterly reporting to fall back on either: like most UK
# main-market issuers, BAE Systems stopped publishing quarterly
# financial statements industry-wide after the FCA dropped the
# requirement in 2014 -- only Half-Year and Full-Year results carry
# full statements; "Trading Statement" releases are narrative-only.
#
# The figures below were fetched and extracted once via an interactive
# browser session (which passes Incapsula's check) from BAE's own
# primary sources -- the FY2025 Annual Report's official UK ESEF/
# iXBRL package, and the H1 2025 / H1 2026 Half-Yearly Report PDFs --
# and cross-validated (FY2025 balance sheet figures independently
# confirmed identical between the ESEF package and the comparative
# column of the H1 2026 PDF; net_income verified against disclosed
# EPS x weighted-average shares for every period). Full source URLs,
# extraction notes and the exact XBRL concepts used are recorded in
# data/bae_systems_snapshot.json. This snapshot must be refreshed by
# hand (same browser-based extraction) whenever BAE publishes a new
# Half-Year or Full-Year result -- the plain script genuinely cannot
# fetch this source live.
BAE_SNAPSHOT_PATH = (
    Path(__file__).parent
    / "data"
    / "bae_systems_snapshot.json"
)


def parse_bae_systems():

    if not BAE_SNAPSHOT_PATH.exists():

        raise FileNotFoundError(
            "BAE Systems snapshot not found: "
            f"{BAE_SNAPSHOT_PATH} -- see the BAE SYSTEMS section "
            "comment above for why this cannot be fetched live."
        )

    with open(
        BAE_SNAPSHOT_PATH,
        "r",
        encoding="utf-8",
    ) as f:

        snapshot = json.load(f)

    result = []

    for period in snapshot["periods"]:

        result.append(
            {
                "period_end":
                    period["period_end"],

                "period_type":
                    period["period_type"],

                "fiscal_year":
                    period["fiscal_year"],

                "fiscal_quarter":
                    period["fiscal_quarter"],

                "filing_date":
                    None,

                "currency":
                    period["currency"],

                "revenue":
                    period["revenue"],

                "gross_profit":
                    period["gross_profit"],

                "operating_income":
                    period["operating_income"],

                "ebit":
                    period["ebit"],

                "ebitda":
                    period["ebitda"],

                "net_income":
                    period["net_income"],

                "eps_basic":
                    period["eps_basic"],

                "eps_diluted":
                    period["eps_diluted"],

                "operating_cash_flow":
                    period["operating_cash_flow"],

                "capex":
                    period["capex"],

                "free_cash_flow":
                    period["free_cash_flow"],

                "cash":
                    period["cash"],

                "total_debt":
                    period["total_debt"],

                "total_assets":
                    period["total_assets"],

                "total_liabilities":
                    period["total_liabilities"],

                "total_equity":
                    period["total_equity"],

                "shares_outstanding":
                    period["shares_outstanding"],
            }
        )

    return result


# ============================================================
# HENSOLDT AG
# ============================================================

# HENSOLDT's own IR site serves PDFs directly with no WAF/bot-detection
# in the way (unlike baesystems.com) -- confirmed reachable via plain
# stdlib urllib, so this parser fetches live on every run like ASML/
# ING/TSMC/SK hynix, not from a local snapshot like BAE.
#
# HENSOLDT only publishes full IFRS statements for Q1 ("3M"), H1 and
# FY -- there is no standalone Q2/Q3/Q4 statement (9M is a further
# cumulative statement, not a discrete Q3). Per the source's own
# presentation, H1 exposes only the cumulative six-month figures, with
# no separate three-month breakout column anywhere -- so H1 is stored
# as-is with period_type "semiannual" and fiscal_quarter=None; no
# Q2 = H1 - Q1 derivation is attempted (Q1 and H1 come from different
# report types/review depth, and the source itself never juxtaposes a
# discrete Q2 column, unlike SK hynix's DART filings).
HENSOLDT_URLS = {
    "FY2025": (
        "https://investors.hensoldt.net/media/document/"
        "a5ee9bbe-ee55-4b0f-960e-542b2dc673eb/assets/"
        "DE000HAG0005-JA-2025-EQ-E-00.pdf"
    ),
    "Q1_2026": (
        "https://investors.hensoldt.net/media/document/"
        "85eba46d-cf4e-4c62-a0cd-b14fd8deb816/assets/"
        "DE000HAG0005-Q1-2026-EQ-E-00.pdf"
    ),
    "H1_2026": (
        "https://investors.hensoldt.net/media/document/"
        "a145ab30-148b-4131-8ed0-592b5eab2de7/assets/"
        "DE000HAG0005-Q2-2026-EQ-E-00.pdf"
    ),
}


def hensoldt_pdf_lines(
    data,
):

    reader = PdfReader(
        io.BytesIO(
            data
        )
    )

    if reader.is_encrypted:

        # Official HENSOLDT report PDFs are AES-encrypted with no
        # user password required for reading -- an empty password
        # unlocks them.
        reader.decrypt(
            ""
        )

    text = "\n".join(
        page.extract_text() or ""
        for page in reader.pages
    )

    return [
        re.sub(
            r"\s+",
            " ",
            line,
        ).strip()

        for line in text.splitlines()

        if line.strip()
    ]


def hensoldt_section(
    lines,
    heading,
    window=90,
):

    # Headings can wrap across two PDF text lines (e.g. "CONSOLIDATED
    # STATEMENT OF" / "FINANCIAL POSITION"), so match against joins of
    # 1-3 consecutive lines. Matching is EXACT (not startswith/
    # substring): a startswith check also fires on table-of-contents
    # rows (same heading text followed by dot leaders and a page
    # number) and on auditor's-report prose that happens to embed the
    # phrase inside a longer sentence -- both are real false positives
    # seen in practice, not hypothetical. Exact-equality across a
    # short line join excludes both.
    target = heading.lower()

    matches = []

    for i in range(len(lines)):

        for span in (1, 2, 3):

            joined = " ".join(
                lines[i : i + span]
            ).lower()

            if joined == target:

                matches.append(i)
                break

    if not matches:

        raise RuntimeError(
            f"HENSOLDT section not found: {heading}"
        )

    start = matches[-1]

    return lines[start : start + window]


def hensoldt_line_values(
    section,
    label,
    minimum_numbers=1,
):

    target = label.lower()

    for i, line in enumerate(section):

        if line.lower().startswith(
            target
        ):

            values = tsmc_numbers(
                line
            )

            if len(values) >= minimum_numbers:
                return values

        # The label text itself can wrap across this line and the
        # next (e.g. "...property, plant and" / "equipment -136 -82")
        # -- retry against a 2-line join before moving on.
        if i + 1 >= len(section):
            continue

        joined_text = (
            line
            + " "
            + section[i + 1]
        )

        if not joined_text.lower().startswith(
            target
        ):
            continue

        values = tsmc_numbers(
            joined_text
        )

        if len(values) >= minimum_numbers:
            return values

    raise RuntimeError(
        f"HENSOLDT line not found: {label}"
    )


def hensoldt_value(
    section,
    label,
    minimum_numbers=1,
):

    values = hensoldt_line_values(
        section,
        label,
        minimum_numbers,
    )

    # The annual report and the semi-annual report inline a note
    # reference number (sometimes two, slash-separated, e.g. "18/19")
    # before the actual current/prior-year figures on statement rows
    # -- e.g. "Revenue 10 2,455 2,240" (10 = note number). The
    # quarterly release has no such reference column. Regardless of
    # how many reference numbers precede them, the current and prior
    # period values are always the LAST two numbers on the line.
    return values[-2]


HENSOLDT_HEADINGS = {
    "FY2025": {
        "income": "CONSOLIDATED INCOME STATEMENT",
        "balance": "CONSOLIDATED STATEMENT OF FINANCIAL POSITION",
        "cashflow": "CONSOLIDATED STATEMENT OF CASH FLOWS",
        "segment": "9.2 Segment information",
    },
    "Q1_2026": {
        "income": "1 Consolidated Income Statement",
        "balance": "3 Consolidated Statement of Financial Position",
        "cashflow": "4 Consolidated Statement of Cash Flows",
        "segment": "6 Segment information",
    },
    "H1_2026": {
        "income": "1 Consolidated Income Statement",
        "balance": "3 Consolidated Statement of Financial Position",
        "cashflow": "4 Consolidated Statement of Cash Flows",
        "segment": "6 Segment information",
    },
}


def parse_hensoldt_period(
    lines,
    headings,
    period_end,
    period_type,
    fiscal_year,
    fiscal_quarter,
):

    income = hensoldt_section(
        lines,
        headings["income"],
        window=40,
    )

    balance = hensoldt_section(
        lines,
        headings["balance"],
        window=90,
    )

    cashflow = hensoldt_section(
        lines,
        headings["cashflow"],
        window=70,
    )

    segment = hensoldt_section(
        lines,
        headings["segment"],
        window=120,
    )

    revenue = hensoldt_value(
        income,
        "Revenue",
        minimum_numbers=2,
    )

    gross_profit = hensoldt_value(
        income,
        "Gross profit",
        minimum_numbers=2,
    )

    # HENSOLDT discloses only ONE operating-level profit measure --
    # "Earnings before financial result and income taxes (EBIT)" --
    # with no separately labelled "operating profit/income" line, so
    # operating_income and ebit are the same reported figure here
    # (unlike BAE, which discloses two genuinely distinct concepts).
    ebit = hensoldt_value(
        income,
        "Earnings before financial result and income taxes",
        minimum_numbers=2,
    )

    # Statutory (non-adjusted) group EBITDA from the segment note's
    # reconciliation table -- cross-checked: EBITDA - D&A == EBIT
    # exactly for every period. "Adjusted EBITDA" is a distinct,
    # separately labelled line and is never used here.
    ebitda_values = hensoldt_line_values(
        segment,
        "EBITDA",
        minimum_numbers=2,
    )

    ebitda = ebitda_values[-1]

    net_income = hensoldt_value(
        income,
        "thereof attributable to the owners of HENSOLDT AG",
        minimum_numbers=2,
    )

    eps_values = hensoldt_line_values(
        income,
        "Basic and diluted earnings per share",
        minimum_numbers=2,
    )

    # Same note-reference-prefix caveat as hensoldt_value() -- the
    # current period's figure is the second-to-last number.
    eps_basic = eps_values[-2]
    eps_diluted = eps_values[-2]

    cash = hensoldt_value(
        balance,
        "Cash and cash equivalents",
        minimum_numbers=2,
    )

    total_assets = hensoldt_value(
        balance,
        "Total assets",
        minimum_numbers=2,
    )

    non_current_liabilities = hensoldt_value(
        balance,
        "Non-current liabilities",
        minimum_numbers=2,
    )

    current_liabilities = hensoldt_value(
        balance,
        "Current liabilities",
        minimum_numbers=2,
    )

    total_liabilities = (
        non_current_liabilities
        + current_liabilities
    )

    total_equity = hensoldt_value(
        balance,
        "Equity, total",
        minimum_numbers=2,
    )

    # "Financing liabilities" only -- lease liabilities and other
    # financial liabilities are distinct IFRS line items, deliberately
    # excluded to keep this a clean, complete borrowings total.
    non_current_debt = hensoldt_value(
        balance,
        "Non-current financing liabilities",
        minimum_numbers=2,
    )

    current_debt = hensoldt_value(
        balance,
        "Current financing liabilities",
        minimum_numbers=2,
    )

    total_debt = (
        non_current_debt
        + current_debt
    )

    operating_cash_flow = hensoldt_value(
        cashflow,
        "Cash flows from operating activities",
        minimum_numbers=2,
    )

    capex = abs(
        hensoldt_value(
            cashflow,
            "Acquisition / addition of intangible assets and "
            "property, plant and equipment",
            minimum_numbers=2,
        )
    )

    free_cash_flow = (
        operating_cash_flow
        - capex
    )

    return {
        "period_end":
            period_end,

        "period_type":
            period_type,

        "fiscal_year":
            fiscal_year,

        "fiscal_quarter":
            fiscal_quarter,

        "filing_date":
            None,

        "currency":
            "EUR",

        "revenue":
            revenue * 1_000_000,

        "gross_profit":
            gross_profit * 1_000_000,

        "operating_income":
            ebit * 1_000_000,

        "ebit":
            ebit * 1_000_000,

        "ebitda":
            ebitda * 1_000_000,

        "net_income":
            net_income * 1_000_000,

        "eps_basic":
            eps_basic,

        "eps_diluted":
            eps_diluted,

        "operating_cash_flow":
            operating_cash_flow * 1_000_000,

        "capex":
            capex * 1_000_000,

        "free_cash_flow":
            free_cash_flow * 1_000_000,

        "cash":
            cash * 1_000_000,

        "total_debt":
            total_debt * 1_000_000,

        "total_assets":
            total_assets * 1_000_000,

        "total_liabilities":
            total_liabilities * 1_000_000,

        "total_equity":
            total_equity * 1_000_000,

        # Only explicitly disclosed for FY2025 ("Number of shares:
        # 115,500,000" in the shares-at-a-glance section, cross-
        # checked against the disclosed market cap / closing price --
        # matches exactly). No equivalent clean disclosure found in
        # the shorter Q1/H1 releases.
        "shares_outstanding":
            None,
    }


def parse_hensoldt():

    result = []

    print(
        "    HENSOLDT FY2025..."
    )

    fy_lines = hensoldt_pdf_lines(
        download(
            HENSOLDT_URLS["FY2025"]
        )
    )

    fy_row = parse_hensoldt_period(
        fy_lines,
        HENSOLDT_HEADINGS["FY2025"],
        period_end="2025-12-31",
        period_type="annual",
        fiscal_year=2025,
        fiscal_quarter=None,
    )

    fy_row["shares_outstanding"] = 115_500_000.0

    result.append(
        fy_row
    )

    print(
        "    HENSOLDT 3M 2026..."
    )

    q1_lines = hensoldt_pdf_lines(
        download(
            HENSOLDT_URLS["Q1_2026"]
        )
    )

    result.append(
        parse_hensoldt_period(
            q1_lines,
            HENSOLDT_HEADINGS["Q1_2026"],
            period_end="2026-03-31",
            period_type="quarterly",
            fiscal_year=2026,
            fiscal_quarter=1,
        )
    )

    print(
        "    HENSOLDT H1 2026..."
    )

    h1_lines = hensoldt_pdf_lines(
        download(
            HENSOLDT_URLS["H1_2026"]
        )
    )

    result.append(
        parse_hensoldt_period(
            h1_lines,
            HENSOLDT_HEADINGS["H1_2026"],
            period_end="2026-06-30",
            period_type="semiannual",
            fiscal_year=2026,
            fiscal_quarter=None,
        )
    )

    return result


# ============================================================
# STORE
# ============================================================

def write_company(
    conn,
    ir_source_id,
    security_id,
    name,
    rows,
):

    if not rows:

        print(
            f"{name}: no rows"
        )

        return 0

    conn.execute(
        "BEGIN IMMEDIATE"
    )

    try:

        conn.execute(
            """
            DELETE FROM fundamentals
            WHERE security_id = ?
              AND source_id = ?
            """,
            (
                security_id,
                ir_source_id,
            ),
        )

        count = 0

        for row in rows:

            upsert_fundamental(
                conn,
                ir_source_id,
                security_id,
                row,
            )

            count += 1

        conn.execute(
            "COMMIT"
        )

    except Exception:

        conn.execute(
            "ROLLBACK"
        )

        raise

    print(
        f"{name}: {count} quarterly rows"
    )

    return count


# ============================================================
# REPORT
# ============================================================

def report(
    conn,
    ir_source_id,
):

    print()
    print(
        "==============================================="
    )
    print(
        " Company IR Fundamentals Status"
    )
    print(
        "==============================================="
    )

    rows = conn.execute(
        """
        SELECT

            s.id,
            s.name,

            COUNT(f.id) AS rows,

            MIN(f.period_end) AS first_period,
            MAX(f.period_end) AS latest_period,

            SUM(
                CASE
                    WHEN f.revenue IS NOT NULL
                    THEN 1
                    ELSE 0
                END
            ) AS rev,

            SUM(
                CASE
                    WHEN f.total_assets IS NOT NULL
                    THEN 1
                    ELSE 0
                END
            ) AS assets,

            SUM(
                CASE
                    WHEN f.total_debt IS NOT NULL
                    THEN 1
                    ELSE 0
                END
            ) AS debt

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
            ir_source_id,
        ),
    ).fetchall()

    total = 0

    for row in rows:

        total += row[
            "rows"
        ]

        print(
            f"{row['id']:>3} | "
            f"{row['name'][:30]:<30} | "
            f"rows={row['rows']:>2} | "
            f"rev={row['rev']:>2} | "
            f"assets={row['assets']:>2} | "
            f"debt={row['debt']:>2} | "
            f"{row['first_period']} "
            f"-> {row['latest_period']}"
        )

    print()
    print(
        f"IR securities : {len(rows)}"
    )

    print(
        f"IR rows       : {total}"
    )

    print()
    print(
        "==============================================="
    )


# ============================================================
# MAIN
# ============================================================

def main():

    conn = connect()

    try:

        ir_source_id = get_source_id(
            conn,
            "Company IR",
        )

        print()
        print(
            "==============================================="
        )
        print(
            " Trading Company IR Fundamentals v2"
        )
        print(
            "==============================================="
        )
        print()

        print(
            "Downloading / parsing ASML..."
        )

        asml_rows = parse_asml()

        print(
            f"    parsed rows: {len(asml_rows)}"
        )

        print()

        print(
            "Downloading / parsing ING..."
        )

        ing_rows = parse_ing()

        print(
            f"    parsed rows: {len(ing_rows)}"
        )

        print()

        print(
            "Downloading / parsing TSMC..."
        )

        tsmc_rows = parse_tsmc()

        print(
            f"    parsed rows: {len(tsmc_rows)}"
        )

        print()

        print(
            "Downloading / parsing SK hynix..."
        )

        sk_hynix_rows = parse_sk_hynix()

        print(
            f"    parsed rows: {len(sk_hynix_rows)}"
        )

        print()

        print(
            "Loading BAE Systems (local snapshot -- see code comment)..."
        )

        bae_rows = parse_bae_systems()

        print(
            f"    parsed rows: {len(bae_rows)}"
        )

        print()

        print(
            "Downloading / parsing HENSOLDT..."
        )

        hensoldt_rows = parse_hensoldt()

        print(
            f"    parsed rows: {len(hensoldt_rows)}"
        )

        print()

        total = 0

        total += write_company(
            conn,
            ir_source_id,
            4,
            "ASML Holding",
            asml_rows,
        )

        total += write_company(
            conn,
            ir_source_id,
            17,
            "ING Groep N.V.",
            ing_rows,
        )

        total += write_company(
            conn,
            ir_source_id,
            2,
            "TSMC (ADR)",
            tsmc_rows,
        )

        total += write_company(
            conn,
            ir_source_id,
            13,
            "SK hynix Inc.",
            sk_hynix_rows,
        )

        total += write_company(
            conn,
            ir_source_id,
            50,
            "BAE Systems",
            bae_rows,
        )

        total += write_company(
            conn,
            ir_source_id,
            56,
            "HENSOLDT AG",
            hensoldt_rows,
        )

        print()
        print(
            f"Rows written: {total}"
        )

        report(
            conn,
            ir_source_id,
        )

    finally:

        conn.close()


if __name__ == "__main__":

    main()

