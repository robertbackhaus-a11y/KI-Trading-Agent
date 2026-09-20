from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path


DB_PATH = Path(r"C:\KI-Stack\data\trading\trading.db")

SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "okami.de robert@okami.de",
)

SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/json",
}

REQUEST_DELAY = 0.20


# ============================================================
# NAME NORMALIZATION
# ============================================================

LEGAL_SUFFIXES = {
    "INC",
    "INCORPORATED",
    "CORP",
    "CORPORATION",
    "COMPANY",
    "CO",
    "PLC",
    "NV",
    "N V",
    "SE",
    "AG",
    "SA",
    "LTD",
    "LIMITED",
    "HOLDING",
    "HOLDINGS",
    "GROUP",
}


def normalize_name(value: str | None) -> str:

    if not value:
        return ""

    value = (
        value.upper()
        .replace("&", " AND ")
        .replace(".", " ")
        .replace(",", " ")
        .replace("-", " ")
        .replace("/", " ")
        .replace("'", "")
        .replace('"', "")
    )

    words = value.split()

    return " ".join(words)


def core_words(value: str | None) -> set[str]:

    words = normalize_name(value).split()

    return {
        w
        for w in words
        if w not in LEGAL_SUFFIXES
        and len(w) > 1
    }


# ============================================================
# HTTP
# ============================================================

def get_json(url: str):

    req = urllib.request.Request(
        url,
        headers=SEC_HEADERS,
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=20,
        ) as response:

            raw = response.read()

            return json.loads(
                raw.decode("utf-8")
            )

    except urllib.error.HTTPError as exc:

        if exc.code == 404:
            return None

        raise


# ============================================================
# DATABASE
# ============================================================

def connect():

    conn = sqlite3.connect(
        str(DB_PATH)
    )

    conn.row_factory = sqlite3.Row

    return conn


def get_relevant_stocks(conn):

    return conn.execute(
        """
        SELECT DISTINCT
            s.id,
            s.name,
            s.symbol,
            s.isin,
            s.country,
            s.exchange

        FROM security s

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
        """
    ).fetchall()


# ============================================================
# MATCHING
# ============================================================

def evaluate_candidate(
    security,
    candidate,
):

    security_name = normalize_name(
        security["name"]
    )

    candidate_name = normalize_name(
        candidate.get("title")
    )

    security_words = core_words(
        security["name"]
    )

    candidate_words = core_words(
        candidate.get("title")
    )

    security_symbol = (
        security["symbol"]
        or ""
    ).upper().rstrip(".")

    candidate_ticker = (
        candidate.get("ticker")
        or ""
    ).upper().rstrip(".")

    ticker_match = (
        bool(security_symbol)
        and security_symbol == candidate_ticker
    )

    exact_name = (
        bool(security_name)
        and security_name == candidate_name
    )

    contained_name = (
        bool(security_name)
        and bool(candidate_name)
        and (
            security_name in candidate_name
            or candidate_name in security_name
        )
    )

    intersection = (
        security_words
        & candidate_words
    )

    union = (
        security_words
        | candidate_words
    )

    similarity = (
        len(intersection) / len(union)
        if union
        else 0.0
    )

    score = 0

    if exact_name:
        score += 120

    elif contained_name:
        score += 90

    if similarity >= 0.80:
        score += 80

    elif similarity >= 0.60:
        score += 55

    elif similarity >= 0.40:
        score += 25

    if ticker_match:
        score += 30

    # Ticker alone is NEVER sufficient.
    name_evidence = (
        exact_name
        or contained_name
        or similarity >= 0.60
    )

    return {
        "score": score,
        "name_evidence": name_evidence,
        "ticker_match": ticker_match,
        "similarity": similarity,
        "candidate": candidate,
    }


def find_match(
    security,
    ticker_data,
):

    evaluated = []

    for candidate in ticker_data.values():

        result = evaluate_candidate(
            security,
            candidate,
        )

        if result["score"] > 0:
            evaluated.append(result)

    evaluated.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    if not evaluated:
        return "NOT_FOUND", None, []

    best = evaluated[0]

    # Strong automatic match requires NAME evidence.
    if (
        best["name_evidence"]
        and best["score"] >= 100
    ):

        if len(evaluated) > 1:

            second = evaluated[1]

            if (
                second["name_evidence"]
                and second["score"]
                >= best["score"] - 10
            ):

                return (
                    "AMBIGUOUS",
                    None,
                    evaluated[:5],
                )

        return (
            "MATCH",
            best,
            evaluated[:5],
        )

    return (
        "AMBIGUOUS",
        None,
        evaluated[:5],
    )


# ============================================================
# COMPANYFACTS
# ============================================================

def inspect_companyfacts(cik: str):

    facts = get_json(
        "https://data.sec.gov/"
        f"api/xbrl/companyfacts/CIK{cik}.json"
    )

    time.sleep(
        REQUEST_DELAY
    )

    if not facts:

        return {
            "available": False,
            "entity": None,
            "taxonomy": None,
            "concepts": 0,
        }

    groups = (
        facts.get("facts")
        or {}
    )

    if "us-gaap" in groups:

        taxonomy = "us-gaap"

    elif "ifrs-full" in groups:

        taxonomy = "ifrs-full"

    else:

        taxonomy = None

    concepts = (
        len(groups.get(taxonomy, {}))
        if taxonomy
        else 0
    )

    return {
        "available": True,
        "entity": facts.get("entityName"),
        "taxonomy": taxonomy,
        "concepts": concepts,
    }


# ============================================================
# MAIN
# ============================================================

def main():

    conn = connect()

    try:

        securities = get_relevant_stocks(
            conn
        )

        print()
        print(
            "==============================================="
        )
        print(
            " Trading SEC Identifier Discovery v1"
        )
        print(
            "==============================================="
        )

        print(
            f"Relevant stocks : {len(securities)}"
        )

        print(
            f"SEC User-Agent  : {SEC_USER_AGENT}"
        )

        print()

        print(
            "Loading SEC ticker index..."
        )

        ticker_data = get_json(
            "https://www.sec.gov/files/"
            "company_tickers.json"
        )

        print(
            f"SEC entities    : {len(ticker_data)}"
        )

        print()

        matches = []
        ambiguous = []
        missing = []

        for index, security in enumerate(
            securities,
            start=1,
        ):

            print(
                f"[{index}/{len(securities)}] "
                f"{security['name']}"
            )

            status, best, candidates = (
                find_match(
                    security,
                    ticker_data,
                )
            )

            if status == "NOT_FOUND":

                print(
                    "    -> NOT FOUND"
                )

                missing.append(
                    security
                )

                print()
                continue

            if status == "AMBIGUOUS":

                print(
                    "    -> AMBIGUOUS / MANUAL REVIEW"
                )

                for candidate in candidates:

                    c = candidate[
                        "candidate"
                    ]

                    print(
                        "       "
                        f"{c.get('ticker', '-'):<8} | "
                        f"{str(c.get('cik_str', '-')):<10} | "
                        f"{c.get('title', '-'):<45} | "
                        f"score={candidate['score']:<3} | "
                        f"name={candidate['similarity']:.2f}"
                    )

                ambiguous.append(
                    security
                )

                print()
                continue

            candidate = best[
                "candidate"
            ]

            cik = str(
                candidate["cik_str"]
            ).zfill(10)

            print(
                f"    -> MATCH"
            )

            print(
                f"    -> CIK       : {cik}"
            )

            print(
                f"    -> SEC ticker: "
                f"{candidate.get('ticker')}"
            )

            print(
                f"    -> SEC title : "
                f"{candidate.get('title')}"
            )

            print(
                f"    -> score     : "
                f"{best['score']}"
            )

            companyfacts = (
                inspect_companyfacts(
                    cik
                )
            )

            print(
                f"    -> CompanyFacts: "
                f"{'YES' if companyfacts['available'] else 'NO'}"
            )

            if companyfacts[
                "available"
            ]:

                print(
                    f"    -> Entity    : "
                    f"{companyfacts['entity'] or '-'}"
                )

                print(
                    f"    -> Taxonomy  : "
                    f"{companyfacts['taxonomy'] or 'OTHER'}"
                )

                print(
                    f"    -> Concepts  : "
                    f"{companyfacts['concepts']}"
                )

            matches.append(
                {
                    "security_id":
                        security["id"],

                    "name":
                        security["name"],

                    "cik":
                        cik,

                    "ticker":
                        candidate.get(
                            "ticker"
                        ),

                    "sec_title":
                        candidate.get(
                            "title"
                        ),

                    "companyfacts":
                        companyfacts[
                            "available"
                        ],

                    "taxonomy":
                        companyfacts[
                            "taxonomy"
                        ],

                    "concepts":
                        companyfacts[
                            "concepts"
                        ],
                }
            )

            print()

        print(
            "==============================================="
        )
        print(
            " Discovery Summary"
        )
        print(
            "==============================================="
        )

        print(
            f"Strong matches : {len(matches)}"
        )

        print(
            f"Ambiguous      : {len(ambiguous)}"
        )

        print(
            f"Not found      : {len(missing)}"
        )

        print()

        print(
            "STRONG MATCHES"
        )

        print(
            "--------------"
        )

        for item in matches:

            print(
                f"{item['security_id']:>3} | "
                f"{item['name'][:32]:<32} | "
                f"{item['cik']} | "
                f"{(item['ticker'] or '-'):<8} | "
                f"{(item['taxonomy'] or 'OTHER'):<10} | "
                f"{item['concepts']:>4}"
            )

        if ambiguous:

            print()
            print(
                "MANUAL REVIEW"
            )

            print(
                "-------------"
            )

            for security in ambiguous:

                print(
                    f"{security['id']:>3} | "
                    f"{security['name']}"
                )

        if missing:

            print()
            print(
                "NO SEC MATCH"
            )

            print(
                "------------"
            )

            for security in missing:

                print(
                    f"{security['id']:>3} | "
                    f"{security['name']}"
                )

    finally:

        conn.close()


if __name__ == "__main__":
    main()
