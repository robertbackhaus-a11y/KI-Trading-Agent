"""Corporate-action (stock split) support, additive to the existing schema.

Design principle: a split is represented as *data*, never applied by
rewriting historical transaction rows. Position reconstruction
(``parqet_import._rebuild_position``) consults this module to convert each
transaction's raw, as-executed share quantity into its as-of-equivalent
(split-adjusted) quantity. Cashflow (amount/fees/taxes/realized_gain) is
never touched here -- a split is non-cash by definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import sqlite3
from typing import Optional


CORPORATE_ACTION_SCHEMA_KEY = "corporate_action_schema_version"
CORPORATE_ACTION_SCHEMA_VERSION = "1"
TABLE_NAME = "corporate_action"

STOCK_SPLIT = "STOCK_SPLIT"
SUPPORTED_ACTION_TYPES = (STOCK_SPLIT,)


class CorporateActionError(ValueError):
    """Raised when a corporate action cannot be safely represented or applied."""


@dataclass(frozen=True)
class CorporateAction:
    id: Optional[int]
    security_id: int
    action_type: str
    effective_date: str
    ratio_numerator: float
    ratio_denominator: float
    source: Optional[str]
    source_reference: Optional[str]
    notes: Optional[str]

    @property
    def ratio(self) -> float:
        return float(self.ratio_numerator) / float(self.ratio_denominator)


def corporate_action_schema_available(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE_NAME,)
    ).fetchone()
    if table is None:
        return False
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (CORPORATE_ACTION_SCHEMA_KEY,)
    ).fetchone()
    return row is not None and row[0] == CORPORATE_ACTION_SCHEMA_VERSION


def _row_to_action(row: sqlite3.Row) -> CorporateAction:
    return CorporateAction(
        id=int(row["id"]) if row["id"] is not None else None,
        security_id=int(row["security_id"]),
        action_type=row["action_type"],
        effective_date=row["effective_date"],
        ratio_numerator=float(row["ratio_numerator"]),
        ratio_denominator=float(row["ratio_denominator"]),
        source=row["source"],
        source_reference=row["source_reference"],
        notes=row["notes"],
    )


def load_corporate_actions(conn: sqlite3.Connection, security_id: int) -> list[CorporateAction]:
    """Read-only. Returns an empty list if the feature schema is not present."""
    if not corporate_action_schema_available(conn):
        return []
    rows = conn.execute(
        """SELECT id, security_id, action_type, effective_date, ratio_numerator,
                  ratio_denominator, source, source_reference, notes
           FROM corporate_action
           WHERE security_id = ?
           ORDER BY effective_date, id""",
        (security_id,),
    ).fetchall()
    return [_row_to_action(row) for row in rows]


def cumulative_split_factor(
    actions: list[CorporateAction],
    transaction_date: str,
    as_of: Optional[str] = None,
) -> float:
    """Multiplier converting a raw historical share quantity into its
    as-of-equivalent (split-adjusted) quantity.

    A STOCK_SPLIT applies to one transaction only when both hold:

    - the transaction predates the split's effective date, and
    - the split has already happened relative to ``as_of`` (default: today).

    An evaluation ``as_of`` a date *before* a split's effective date therefore
    never applies that split -- historical evaluation keeps the pre-split
    basis. Multiple applicable splits combine multiplicatively; the order of
    application does not change the product (e.g. 2:1 then 3:1 == factor 6).
    Non-STOCK_SPLIT action types are ignored (none are supported yet).
    """
    evaluation_date = as_of or date.today().isoformat()
    factor = 1.0
    for action in actions:
        if action.action_type != STOCK_SPLIT:
            continue
        if transaction_date < action.effective_date <= evaluation_date:
            factor *= action.ratio
    return factor


def validate_new_split(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    effective_date: str,
    ratio_numerator: float,
    ratio_denominator: float,
) -> list[str]:
    """Read-only pre-write checks. An empty list means safe to add."""
    problems: list[str] = []

    security = conn.execute("SELECT id FROM security WHERE id = ?", (security_id,)).fetchone()
    if security is None:
        problems.append(f"security_id {security_id} not found")

    try:
        date.fromisoformat(str(effective_date))
    except ValueError:
        problems.append(f"effective_date must be an ISO date (YYYY-MM-DD): {effective_date!r}")

    if ratio_numerator is None or ratio_denominator is None:
        problems.append("ratio_numerator and ratio_denominator are required")
    elif float(ratio_numerator) <= 0 or float(ratio_denominator) <= 0:
        problems.append("ratio_numerator and ratio_denominator must be positive")
    elif float(ratio_numerator) == float(ratio_denominator):
        problems.append("ratio must not be 1:1 (that is not a split)")

    if corporate_action_schema_available(conn) and not problems:
        existing = conn.execute(
            """SELECT id FROM corporate_action
               WHERE security_id = ? AND action_type = ? AND effective_date = ?
                 AND ratio_numerator = ? AND ratio_denominator = ?""",
            (security_id, STOCK_SPLIT, effective_date, ratio_numerator, ratio_denominator),
        ).fetchone()
        if existing is not None:
            problems.append(f"an identical corporate action already exists (id={existing[0]})")

    return problems


def insert_stock_split(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    effective_date: str,
    ratio_numerator: float,
    ratio_denominator: float,
    source: str,
    source_reference: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """Write one STOCK_SPLIT row. Raises CorporateActionError if unsafe.

    The caller is expected to run inside its own transaction (matching the
    established ``BEGIN IMMEDIATE`` / commit-or-rollback pattern used
    elsewhere in this codebase); this function only issues the INSERT.
    """
    if not corporate_action_schema_available(conn):
        raise CorporateActionError(
            f"{TABLE_NAME} schema is missing; apply "
            "Migrate-TradingCorporateActions.py --write first"
        )
    problems = validate_new_split(
        conn,
        security_id=security_id,
        effective_date=effective_date,
        ratio_numerator=ratio_numerator,
        ratio_denominator=ratio_denominator,
    )
    if problems:
        raise CorporateActionError("; ".join(problems))

    normalized_source = str(source or "").strip()
    if not normalized_source:
        raise CorporateActionError("source must not be empty")

    cursor = conn.execute(
        """INSERT INTO corporate_action(
               security_id, action_type, effective_date,
               ratio_numerator, ratio_denominator, source, source_reference, notes
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            security_id, STOCK_SPLIT, effective_date,
            ratio_numerator, ratio_denominator, normalized_source, source_reference, notes,
        ),
    )
    return int(cursor.lastrowid)
