"""Read-only Swing-candidate promotion evaluation and explicit approval."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import sqlite3
from typing import Optional

from analysis_contracts import AvailabilityStatus, StrategyType, to_primitive
from analysis_engine import build_analysis_snapshot


PROMOTE = "PROMOTE"
KEEP_WATCHING = "KEEP_WATCHING"
REJECT = "REJECT"
DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
PROMOTION_FEATURE_KEY = "candidate_promotion_schema_version"
PROMOTION_FEATURE_VERSION = "1"


class PromotionApprovalError(ValueError):
    """Approval was unsafe, stale, or not eligible for a strategy write."""


@dataclass(frozen=True)
class PromotionDecision:
    security_id: int
    symbol: Optional[str]
    name: str
    watchlist_status: Optional[str]
    watchlist_priority: Optional[int]
    watchlist_entry_reason: Optional[str]
    recommendation: str
    reasons: tuple[str, ...]
    plan_token: str
    current_price: Optional[float]
    current_price_currency: Optional[str]
    current_price_eur: Optional[float]
    sma50: Optional[float]
    sma200: Optional[float]
    perf_1m_pct: Optional[float]
    perf_3m_pct: Optional[float]
    perf_6m_pct: Optional[float]
    rsi14: Optional[float]
    drawdown_52w_pct: Optional[float]
    volatility_60d_annualized_pct: Optional[float]
    momentum_score: Optional[float]
    technical_quality: str
    fundamental_quality: str
    valuation_quality: str
    event_risk_quality: str

    def primitive(self) -> dict:
        return to_primitive(self)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _active_assignment(conn: sqlite3.Connection, security_id: int, as_of: str):
    return conn.execute(
        """SELECT id, strategy_type, effective_from, effective_to FROM strategy_assignment
           WHERE security_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
           ORDER BY effective_from DESC, id DESC""",
        (security_id, as_of, as_of),
    ).fetchone()


def _open_campaign(conn: sqlite3.Connection, security_id: int) -> bool:
    return _table_exists(conn, "swing_campaign") and conn.execute(
        "SELECT 1 FROM swing_campaign WHERE security_id=? AND status='open'", (security_id,)
    ).fetchone() is not None


def _token(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def evaluate_swing_promotion(
    conn: sqlite3.Connection,
    security_id: int,
    *,
    as_of: Optional[str] = None,
) -> PromotionDecision:
    """Evaluate exactly one security without writing a promotion record.

    Fundamentals, research, events, and valuation remain diagnostics.  No
    authoritative threshold for them exists, so they cannot fabricate a
    promotion rejection.  Current cash and portfolio allocation are likewise
    intentionally absent: those belong to entry sizing, not promotion.
    """
    evaluation_date = as_of or date.today().isoformat()
    security = conn.execute("SELECT id, symbol, name FROM security WHERE id=?", (security_id,)).fetchone()
    if security is None:
        raise ValueError(f"security_id {security_id} not found")
    watch = conn.execute(
        "SELECT status, priority, entry_reason, updated_at FROM watchlist WHERE security_id=?", (security_id,)
    ).fetchone()
    snapshot = build_analysis_snapshot(security_id, as_of=evaluation_date, connection=conn)
    position = snapshot.position
    assignment = _active_assignment(conn, security_id, evaluation_date)
    technical = snapshot.technical

    recommendation = PROMOTE
    reasons: list[str] = ["PROMOTION_READY"]
    if watch is None or watch["status"] != "WATCH":
        recommendation, reasons = REJECT, ["PROMOTION_NOT_ON_WATCHLIST"]
    elif position.has_position is True or (position.shares is not None and float(position.shares) > 0):
        recommendation, reasons = REJECT, ["PROMOTION_EXISTING_POSITION"]
    elif _open_campaign(conn, security_id):
        recommendation, reasons = REJECT, ["PROMOTION_OPEN_CAMPAIGN"]
    elif assignment is not None and assignment["strategy_type"] == "swing":
        recommendation, reasons = KEEP_WATCHING, ["PROMOTION_ALREADY_SWING"]
    elif assignment is not None:
        recommendation, reasons = REJECT, ["PROMOTION_STRATEGY_CONFLICT"]
    elif technical.quality.status is AvailabilityStatus.STALE:
        recommendation, reasons = DATA_INSUFFICIENT, ["PROMOTION_TECHNICAL_DATA_STALE"]
    elif technical.quality.status is not AvailabilityStatus.AVAILABLE:
        recommendation, reasons = DATA_INSUFFICIENT, ["PROMOTION_REQUIRED_DATA_INCOMPLETE"]
    elif technical.current_price is None:
        recommendation, reasons = DATA_INSUFFICIENT, ["PROMOTION_PRICE_UNAVAILABLE"]
    elif technical.sma50 is None:
        recommendation, reasons = DATA_INSUFFICIENT, ["PROMOTION_SMA50_UNAVAILABLE"]
    elif technical.sma200 is None:
        recommendation, reasons = DATA_INSUFFICIENT, ["PROMOTION_SMA200_UNAVAILABLE"]
    elif position.current_price_cost_currency is None or (position.valuation_currency or "").upper() != "EUR":
        recommendation, reasons = DATA_INSUFFICIENT, ["PROMOTION_MARKET_DATA_MISSING"]
    elif float(technical.current_price) <= float(technical.sma50):
        recommendation, reasons = KEEP_WATCHING, ["PROMOTION_PRICE_BELOW_OR_EQUAL_SMA50"]
    elif float(technical.current_price) <= float(technical.sma200):
        recommendation, reasons = KEEP_WATCHING, ["PROMOTION_PRICE_BELOW_OR_EQUAL_SMA200"]
    elif float(technical.sma50) <= float(technical.sma200):
        recommendation, reasons = KEEP_WATCHING, ["PROMOTION_SMA50_BELOW_OR_EQUAL_SMA200"]

    token_payload = {
        "security_id": security_id,
        "as_of": evaluation_date,
        "watch": dict(watch) if watch else None,
        "assignment": dict(assignment) if assignment else None,
        "has_position": position.has_position,
        "shares": position.shares,
        "open_campaign": _open_campaign(conn, security_id),
        "technical": {"quality": technical.quality.status.value, "as_of": technical.quality.as_of, "price": technical.current_price, "sma50": technical.sma50, "sma200": technical.sma200},
        "valuation": {"eur_price": position.current_price_cost_currency, "currency": position.valuation_currency, "fx": position.fx_quality.status.value},
        "recommendation": recommendation,
        "reasons": reasons,
    }
    return PromotionDecision(
        security_id=security_id, symbol=security["symbol"], name=security["name"],
        watchlist_status=watch["status"] if watch else None,
        watchlist_priority=watch["priority"] if watch else None,
        watchlist_entry_reason=watch["entry_reason"] if watch else None,
        recommendation=recommendation, reasons=tuple(reasons), plan_token=_token(token_payload),
        current_price=technical.current_price,
        current_price_currency=position.current_price_currency,
        current_price_eur=position.current_price_cost_currency,
        sma50=technical.sma50, sma200=technical.sma200,
        perf_1m_pct=technical.perf_1m_pct, perf_3m_pct=technical.perf_3m_pct,
        perf_6m_pct=technical.perf_6m_pct, rsi14=technical.rsi14,
        drawdown_52w_pct=technical.drawdown_52w_pct,
        volatility_60d_annualized_pct=technical.volatility_60d_annualized_pct,
        momentum_score=technical.momentum_score,
        technical_quality=technical.quality.status.value,
        fundamental_quality=snapshot.fundamental.quality.status.value,
        valuation_quality=snapshot.valuation.quality.status.value,
        event_risk_quality=snapshot.event_risk.quality.status.value,
    )


def evaluate_swing_candidates(conn: sqlite3.Connection, *, as_of: Optional[str] = None) -> list[PromotionDecision]:
    """Evaluate zero-position active generic-watchlist rows, read-only."""
    rows = conn.execute(
        """SELECT w.security_id FROM watchlist w
           LEFT JOIN positions p ON p.security_id=w.security_id AND p.shares>0
           WHERE w.status='WATCH' AND p.security_id IS NULL ORDER BY w.priority DESC, w.security_id"""
    ).fetchall()
    return [evaluate_swing_promotion(conn, int(row["security_id"]), as_of=as_of) for row in rows]


def _require_approval_schema(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "candidate_promotion"):
        raise PromotionApprovalError("candidate_promotion migration has not been applied")
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (PROMOTION_FEATURE_KEY,)).fetchone()
    if row is None or row["value"] != PROMOTION_FEATURE_VERSION:
        raise PromotionApprovalError("candidate_promotion_schema_version must equal 1")


def approve_swing_promotion(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    plan_token: str,
    effective_from: str,
    approved_by: str,
) -> dict:
    """Explicitly turn a still-current PROMOTE plan into one swing assignment."""
    try:
        effective = date.fromisoformat(effective_from).isoformat()
    except ValueError as exc:
        raise PromotionApprovalError("effective_from must be YYYY-MM-DD") from exc
    _require_approval_schema(conn)
    current = evaluate_swing_promotion(conn, security_id, as_of=effective)
    if current.recommendation == KEEP_WATCHING and current.reasons == ("PROMOTION_ALREADY_SWING",):
        assignment = _active_assignment(conn, security_id, effective)
        return {"written": False, "idempotent": True, "assignment_id": assignment["id"], "plan_token": current.plan_token}
    if current.plan_token != plan_token:
        raise PromotionApprovalError("PROMOTION_PLAN_STALE")
    if current.recommendation != PROMOTE:
        raise PromotionApprovalError("PROMOTION_NOT_APPROVABLE: " + ", ".join(current.reasons))
    details = json.dumps(current.primitive(), sort_keys=True, separators=(",", ":"))
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """INSERT INTO strategy_assignment(security_id, strategy_type, effective_from, source, rationale)
               VALUES (?, 'swing', ?, 'candidate_promotion', ?)""",
            (security_id, effective, "approved Swing candidate promotion"),
        )
        conn.execute(
            """INSERT INTO candidate_promotion(security_id, evaluated_at, status, source, rationale, details_json, approved_at, approved_by)
               VALUES (?, ?, 'promoted', 'candidate_promotion', ?, ?, CURRENT_TIMESTAMP, ?)""",
            (security_id, effective, "approved Swing candidate promotion", details, approved_by),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"written": True, "idempotent": False, "assignment_id": cursor.lastrowid, "plan_token": current.plan_token}
