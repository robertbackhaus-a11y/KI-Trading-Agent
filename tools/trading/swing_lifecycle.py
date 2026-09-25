"""Read-only helpers for the explicit Phase-3B.1 Swing campaign lifecycle.

The helpers in this module never infer a campaign from transactions and never
change persistence.  They only derive an auditable view from campaigns and
explicitly recorded lifecycle events.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from math import floor
from typing import Optional

from analysis_contracts import AvailabilityStatus, DataQuality


FEATURE_VERSION_KEY = "swing_campaign_schema_version"
FEATURE_VERSION = "1"

CAMPAIGN_STATUSES = {"open", "closed"}
EVENT_TYPES = {
    "baseline",
    "add",
    "tp1_signal",
    "tp1_execution",
    "tp2_signal",
    "tp2_execution",
    "manual_reduction",
    "stop_execution",
    "close",
}
QUANTITY_INCREASING_EVENT_TYPES = {"baseline", "add"}
QUANTITY_REDUCING_EVENT_TYPES = {
    "tp1_execution",
    "tp2_execution",
    "manual_reduction",
    "stop_execution",
    "close",
}
EXECUTION_EVENT_TYPES = {
    "tp1_execution",
    "tp2_execution",
    "manual_reduction",
    "stop_execution",
}


@dataclass(frozen=True)
class LifecycleContext:
    """The derived state for one open Swing campaign, if one is recorded."""

    campaign_id: Optional[int]
    security_id: int
    status: Optional[str]
    opened_at: Optional[str]
    original_quantity: Optional[float]
    reference_avg_cost: Optional[float]
    reference_currency: Optional[str]
    tp1_status: Optional[str]
    tp2_status: Optional[str]
    total_add_quantity: float = 0.0
    add_event_count: int = 0
    tp1_executed_quantity: float = 0.0
    tp2_executed_quantity: float = 0.0
    manual_reduction_quantity: float = 0.0
    post_tp2_add_detected: bool = False
    stop_execution_quantity: float = 0.0
    linked_executed_reduction_quantity: float = 0.0
    event_derived_quantity: Optional[float] = None
    reconciliation_delta: Optional[float] = None
    quality: DataQuality = DataQuality(AvailabilityStatus.UNAVAILABLE)
    reconciliation_quality: DataQuality = DataQuality(AvailabilityStatus.UNAVAILABLE)


def lifecycle_schema_available(conn: sqlite3.Connection) -> bool:
    """Return whether both Phase-3B.1 tables and its marker are available."""

    tables = {
        row[0]
        for row in conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name IN ('swing_campaign', 'swing_campaign_event')
            """
        )
    }
    if tables != {"swing_campaign", "swing_campaign_event"}:
        return False
    metadata_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
    ).fetchone()
    if metadata_table is None:
        return False
    marker = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (FEATURE_VERSION_KEY,)
    ).fetchone()
    return marker is not None and marker[0] == FEATURE_VERSION


def _unavailable_context(security_id: int, detail: str) -> LifecycleContext:
    quality = DataQuality(AvailabilityStatus.UNAVAILABLE, (detail,))
    return LifecycleContext(
        campaign_id=None,
        security_id=security_id,
        status=None,
        opened_at=None,
        original_quantity=None,
        reference_avg_cost=None,
        reference_currency=None,
        tp1_status=None,
        tp2_status=None,
        quality=quality,
        reconciliation_quality=quality,
    )


def derive_open_lifecycle(
    conn: sqlite3.Connection,
    security_id: int,
    *,
    current_quantity: Optional[float],
    evaluation_as_of: Optional[str] = None,
) -> LifecycleContext:
    """Derive the currently open campaign without interpreting transactions.

    ``not_recorded`` is deliberately a lifecycle-recording state, not evidence
    that a market target was never crossed.
    """

    if not lifecycle_schema_available(conn):
        return _unavailable_context(
            security_id, "swing campaign lifecycle migration has not been applied"
        )
    query = """
        SELECT id, security_id, status, opened_at, original_quantity,
               reference_avg_cost, reference_currency
        FROM swing_campaign
        WHERE security_id = ?
    """
    params: list[object] = [security_id]
    if evaluation_as_of is not None:
        # Lifecycle identity resolves against evaluation time, never against
        # the latest market-data timestamp. A future campaign cannot leak
        # into an as-of snapshot.
        query += """
          AND substr(opened_at, 1, 10) <= ?
          AND (closed_at IS NULL OR substr(closed_at, 1, 10) >= ?)
        """
        params.extend((evaluation_as_of[:10], evaluation_as_of[:10]))
    else:
        # The management CLI lists only campaigns that are open now. Snapshot
        # callers always pass an evaluation date and therefore use the full
        # temporal interval above, including campaigns closed in the future.
        query += " AND status = 'open'"
    query += " ORDER BY opened_at DESC, id DESC"
    campaigns = conn.execute(query, params).fetchall()
    if not campaigns:
        return _unavailable_context(security_id, "no open Swing campaign is recorded")
    if len(campaigns) != 1:
        return _unavailable_context(
            security_id, "multiple open Swing campaigns are recorded"
        )

    campaign = campaigns[0]
    event_rows = conn.execute(
        """
        SELECT event_type, quantity, transaction_id
        FROM swing_campaign_event
        WHERE campaign_id = ?
        ORDER BY event_at, id
        """,
        (campaign["id"],),
    ).fetchall()
    quantities = {event_type: 0.0 for event_type in EVENT_TYPES}
    event_types = set()
    linked_reductions = 0.0
    original_quantity = float(campaign["original_quantity"])
    tp2_cumulative_target = floor(original_quantity * 0.75)
    tp2_target_completed = tp2_cumulative_target == 0
    post_tp2_add_detected = False
    for event in event_rows:
        event_type = event["event_type"]
        event_types.add(event_type)
        quantity = float(event["quantity"] or 0.0)
        if event_type == "add" and tp2_target_completed:
            post_tp2_add_detected = True
        quantities[event_type] = quantities.get(event_type, 0.0) + quantity
        if event_type in {"tp1_execution", "tp2_execution"}:
            executed_tp_quantity = (
                quantities["tp1_execution"] + quantities["tp2_execution"]
            )
            if executed_tp_quantity >= tp2_cumulative_target:
                tp2_target_completed = True
        if event_type in EXECUTION_EVENT_TYPES and event["transaction_id"] is not None:
            linked_reductions += quantity

    tp1_status = (
        "executed"
        if "tp1_execution" in event_types
        else "signaled" if "tp1_signal" in event_types else "not_recorded"
    )
    tp2_status = (
        "executed"
        if "tp2_execution" in event_types
        else "signaled" if "tp2_signal" in event_types else "not_recorded"
    )
    additions = quantities["add"]
    add_event_count = sum(1 for event in event_rows if event["event_type"] == "add")
    reductions = sum(quantities[event_type] for event_type in QUANTITY_REDUCING_EVENT_TYPES)
    event_derived_quantity = original_quantity + additions - reductions
    if current_quantity is None:
        reconciliation_delta = None
        reconciliation_quality = DataQuality(
            AvailabilityStatus.NOT_APPLICABLE,
            ("no current position quantity is available for reconciliation",),
        )
    else:
        reconciliation_delta = float(current_quantity) - event_derived_quantity
        if abs(reconciliation_delta) < 1e-9:
            reconciliation_quality = DataQuality(AvailabilityStatus.AVAILABLE)
        else:
            reconciliation_quality = DataQuality(
                AvailabilityStatus.PARTIAL,
                (
                    "current position quantity differs from explicit campaign events "
                    f"by {reconciliation_delta:g}",
                ),
            )
    return LifecycleContext(
        campaign_id=int(campaign["id"]),
        security_id=int(campaign["security_id"]),
        status=campaign["status"],
        opened_at=campaign["opened_at"],
        original_quantity=original_quantity,
        reference_avg_cost=(
            float(campaign["reference_avg_cost"])
            if campaign["reference_avg_cost"] is not None
            else None
        ),
        reference_currency=campaign["reference_currency"],
        tp1_status=tp1_status,
        tp2_status=tp2_status,
        total_add_quantity=additions,
        add_event_count=add_event_count,
        tp1_executed_quantity=quantities["tp1_execution"],
        tp2_executed_quantity=quantities["tp2_execution"],
        manual_reduction_quantity=quantities["manual_reduction"],
        post_tp2_add_detected=post_tp2_add_detected,
        stop_execution_quantity=quantities["stop_execution"],
        linked_executed_reduction_quantity=linked_reductions,
        event_derived_quantity=event_derived_quantity,
        reconciliation_delta=reconciliation_delta,
        quality=DataQuality(AvailabilityStatus.AVAILABLE),
        reconciliation_quality=reconciliation_quality,
    )


def lifecycle_to_primitive(context: LifecycleContext) -> dict:
    """Return a stable JSON-friendly representation for the management CLI."""

    return {
        "campaign_id": context.campaign_id,
        "security_id": context.security_id,
        "campaign_status": context.status,
        "opened_at": context.opened_at,
        "original_quantity": context.original_quantity,
        "reference_avg_cost": context.reference_avg_cost,
        "reference_currency": context.reference_currency,
        "tp1_status": context.tp1_status,
        "tp2_status": context.tp2_status,
        "total_add_quantity": context.total_add_quantity,
        "add_event_count": context.add_event_count,
        "tp1_executed_quantity": context.tp1_executed_quantity,
        "tp2_executed_quantity": context.tp2_executed_quantity,
        "manual_reduction_quantity": context.manual_reduction_quantity,
        "post_tp2_add_detected": context.post_tp2_add_detected,
        "stop_execution_quantity": context.stop_execution_quantity,
        "linked_executed_reduction_quantity": context.linked_executed_reduction_quantity,
        "event_derived_quantity": context.event_derived_quantity,
        "reconciliation_delta": context.reconciliation_delta,
        "quality": context.quality.status.value,
        "quality_details": list(context.quality.details),
        "reconciliation_quality": context.reconciliation_quality.status.value,
        "reconciliation_details": list(context.reconciliation_quality.details),
    }
