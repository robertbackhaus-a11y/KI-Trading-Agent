# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Read-only effective single-name exposure (the ``get_effective_exposure`` MCP tool, the "Einzelwertrisiko" section of the "Sektoren" page).

``direct position + the share of the same company in every held ETF = effective exposure of that company``, in EUR and as a share of the invested value.

* Direct positions: the EUR market value of the portfolio context (``analysis_engine.build_portfolio_context``), the same figure as everywhere.
* ETFs: the ETF's EUR value x the weight of every holding in the provider's holdings (``etf_holdings``; a synthetic ETF uses its index composition, weights as published).
  Non-equity rows (cash, futures, FX) and the part of a physical fund that the published weights do not explain are "other" and never spread over companies; the excess of
  a published index list over 100 % is the "rounding drift" (see ``etf_holdings``).  An ETF without usable holdings stays one unresolved block, visible.
* **Matching** of a holding to a security of the portfolio, in this order, exact only: (1) the ISIN; (2) an explicit, reviewed alias of the holdings file (an ADR and the
  ordinary share of one issuer); (3) the provider ticker equal to the security's symbol *and* the exchange equal after ``etf_holdings.norm`` -- if more than one
  security qualifies the holding stays ambiguous.  There is no fuzzy matching.  Share classes of one issuer that the holdings file lists as an issuer group (Alphabet A and C) are one company.  A holding without a portfolio counterpart is a company of its own, identified by its ISIN,
  else by ticker, exchange and name; the same company in several ETFs is aggregated once, and so is a company held directly and in ETFs.
* Every EUR of the invested value falls into exactly one bucket (``buckets``): ``direct_matched`` (companies with a direct position, including their ETF share), ``etf_only``
  (companies that are *identified* by an ISIN, an alias or a ticker on an exchange but are not held directly -- being absent from the Security Master is no defect),
  ``ambiguous_unmatched`` (holdings whose match is ambiguous or that carry neither ISIN nor ticker, plus ETFs without usable holdings), ``other`` (cash, derivatives,
  the part a physical fund's weights do not explain) and ``rounding_drift`` (negative, synthetic index lists).  The buckets add up to the invested value (``checks``).

Inside the process (``with_companies``) the ``contributors`` also carry the provider's own ``gics_sector`` and ``location`` of the holding; the answer of the MCP tool leaves
them out (``INTERNAL_CONTRIBUTOR_FIELDS``: the 40,000-character limit of the server, the company table does not show them).

Only SELECTs and file reads; no write, no network, no order, and no risk guard is read or changed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

import etf_holdings as eh

OPERATION = "get_effective_exposure"
MATCH_ISIN, MATCH_ALIAS, MATCH_TICKER, STANDALONE, AMBIGUOUS, NAME_ONLY = "isin", "alias", "ticker_exchange", "standalone", "ambiguous", "name_only"
DIRECT_MATCHED, ETF_ONLY, AMBIGUOUS_UNMATCHED = "direct_matched", "etf_only", "ambiguous_unmatched"  # the three classes of a company / a holding
INTERNAL_CONTRIBUTOR_FIELDS = ("gics_sector", "location")  # used by the sector overview, not part of the public answer
DEFAULT_TOP = 25
MAX_TOP = 40  # the MCP server refuses results above 40,000 characters; 25 companies are about 19,000, 200 would be about 120,000
_EPS = 1e-9


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class Matcher:
    """Exact matching of provider holdings to the securities of the database."""

    def __init__(self, securities: list[dict[str, Any]], aliases: tuple[eh.Alias, ...]) -> None:
        self.by_isin: dict[str, int] = {}
        self.by_listing: dict[tuple[str, str], list[int]] = {}
        self.info = {s["id"]: s for s in securities}
        for s in securities:
            if s.get("isin"):
                self.by_isin.setdefault(str(s["isin"]).upper(), s["id"])
            if s.get("symbol"):
                self.by_listing.setdefault((str(s["symbol"]).upper(), eh.norm(s.get("exchange"))), []).append(s["id"])
        self.alias = {(a.ticker, a.exchange): (self.by_isin.get(a.isin), a) for a in aliases}

    def match(self, holding: eh.Holding) -> tuple[Optional[int], str]:
        if holding.isin and holding.isin in self.by_isin:
            return self.by_isin[holding.isin], MATCH_ISIN
        ticker, exchange = holding.ticker.upper(), eh.norm(holding.exchange)
        if (ticker, exchange) in self.alias and self.alias[(ticker, exchange)][0] is not None:
            return self.alias[(ticker, exchange)][0], MATCH_ALIAS
        candidates = self.by_listing.get((ticker, exchange), []) if ticker and ticker != "-" and exchange else []
        if len(candidates) == 1:
            return candidates[0], MATCH_TICKER
        return (None, AMBIGUOUS) if len(candidates) > 1 else (None, STANDALONE)


def company_key(holding: eh.Holding, security_id: Optional[int], groups: Optional[Mapping[str, str]] = None, security_isin: str = "") -> str:
    isin = (security_isin or holding.isin or "").upper()
    if groups and isin in groups:
        return "grp:" + eh.norm(groups[isin])
    if security_id is not None:
        return f"sec:{security_id}"
    if holding.isin:
        return f"isin:{holding.isin}"
    return f"tk:{holding.ticker.upper()}|{eh.norm(holding.exchange)}|{eh.norm(holding.name)}"


def public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A company row as the MCP answer shows it: without the contributor fields that only in-process users need."""
    out = dict(row)
    out["contributors"] = [{k: v for k, v in c.items() if k not in INTERNAL_CONTRIBUTOR_FIELDS} for c in row["contributors"]]
    return out


def guard_inputs(etfs: list[dict[str, Any]], companies: list[dict[str, Any]]) -> dict[str, Any]:
    """What the single-name guard may use: for every security of a company (all share classes of an issuer group, held or not) the ETF value of the company and the direct value of
    its OTHER share classes, and whether the ETF information is complete.  The company is the unit: two directly held classes of one issuer, or one class plus ETFs, are one exposure.

    ``usable`` only if EVERY held ETF has holdings that are present and not stale (``APPLIED``); an ETF with stale, missing or invalid holdings makes the effective exposure incomplete, and the
    guard must then say so instead of treating the direct exposure as complete (``status`` ``stale`` / ``partial`` / ``unavailable``; no ETF held: ``not_applicable``).  The ETF map is only
    given when it is usable; the direct values of the other classes do not depend on any holdings file and are always given.  The rounding drift is not part of any company.
    """
    siblings: dict[str, float] = {}
    for c in companies:
        own = {p["security_id"]: p["value_eur"] for p in c["direct_positions"]}
        for sid in c["security_ids"]:
            other = c["direct_value_eur"] - own.get(sid, 0.0)
            if other > _EPS:
                siblings[str(int(sid))] = other
    if not etfs:
        return {"usable": True, "status": "not_applicable", "details": ["no ETF held"], "etf_value_by_security_id": {}, "other_class_direct_by_security_id": siblings}
    problems = [e for e in etfs if e["status"] != eh.APPLIED]
    details = [f"ETF_HOLDINGS_{e['status']}:{e['symbol']}" + (f" (as of {e['as_of']})" if e.get("as_of") else "") for e in problems]
    if not problems:
        status = "available"
    elif all(e["status"] == eh.STALE for e in problems):
        status = "stale"
    elif len(problems) < len(etfs):
        status = "partial"
    else:
        status = "unavailable"
    values: dict[str, float] = {}
    if not problems:
        for c in companies:
            if c["etf_value_eur"] > 0:
                for sid in c["security_ids"]:  # one company, one ETF value, whichever class is looked at
                    values[str(int(sid))] = c["etf_value_eur"]
    return {"usable": not problems, "status": status, "details": details, "etf_value_by_security_id": values, "other_class_direct_by_security_id": siblings}


def build_effective_exposure(
    conn: sqlite3.Connection,
    *,
    as_of: Optional[str] = None,
    universe_dir: Optional[Path] = None,
    top: int = DEFAULT_TOP,
    context: Optional[Any] = None,
    with_companies: bool = False,
) -> dict[str, Any]:
    """The effective exposure of every company (direct + ETF holdings) of the open positions.  Read-only.  At most ``MAX_TOP`` companies are listed.

    ``with_companies`` adds ``companies`` (every company row, not capped) for in-process users such as the sector overview; the MCP tool never asks for it (size limit)."""
    import analysis_engine

    top = max(1, min(int(top), MAX_TOP))

    if context is None:  # the orchestrator hands in the portfolio context it already computed: one valuation, one set of figures
        context = analysis_engine.build_portfolio_context(as_of=as_of, connection=conn)
    evaluation = str(context.evaluation_as_of)[:10]
    warnings: list[str] = []
    try:
        layers = eh.read_layers(universe_dir)
    except eh.HoldingsError as exc:
        layers = eh.Layers({}, {})
        warnings.append(f"HOLDINGS_UNAVAILABLE: {exc}; ETFs stay single unresolved blocks")
    warnings.extend(layers.warnings)
    securities = [{"id": int(r[0]), "symbol": r[1], "name": r[2], "isin": r[3], "exchange": r[4], "asset_type": r[5]}
                  for r in conn.execute("SELECT id, symbol, name, isin, exchange, asset_type FROM security")]
    matcher = Matcher(securities, layers.aliases)
    for alias in layers.aliases:
        if matcher.alias[(alias.ticker, alias.exchange)][0] is None:
            warnings.append(f"ALIAS_UNUSED: no security with ISIN {alias.isin} for the alias {alias.ticker}")

    held = []
    unvalued = []
    for e in context.exposures:
        if e.quantity is None or float(e.quantity) <= 0:
            continue
        sec = matcher.info.get(int(e.security_id), {})
        value = _num(e.market_value_eur)
        item = {"security_id": int(e.security_id), "symbol": e.symbol, "name": sec.get("name"), "isin": sec.get("isin"), "is_etf": str(sec.get("asset_type") or "").lower() == "etf",
                "value": value}
        (held if value is not None and value > 0 else unvalued).append(item)
    total = sum(p["value"] for p in held)

    companies: dict[str, dict[str, Any]] = {}

    def company(key: str, name: str, security_id: Optional[int], ticker: str, isin: str) -> dict[str, Any]:
        if key.startswith("grp:"):
            name = layers.issuer_groups.get((isin or "").upper(), name) if isin else name
        if key not in companies:
            companies[key] = {"key": key, "name": name, "security_id": security_id, "ticker": ticker, "isin": isin or None, "matched": security_id is not None,
                              "direct_value_eur": 0.0, "etf_value_eur": 0.0, "contributors": [], "match_bases": [],
                              "direct_positions": [], "security_ids": []}
        return companies[key]

    for p in held:
        if not p["is_etf"]:
            c = company(company_key(eh.Holding(p["name"] or "", p["symbol"], p["isin"] or "", 0.0, eh.EQUITY), p["security_id"], layers.issuer_groups, p["isin"] or ""), p["name"] or p["symbol"],
                        p["security_id"], p["symbol"], p["isin"] or "")
            c["direct_value_eur"] += p["value"]
            c["direct_positions"].append({"security_id": p["security_id"], "symbol": p["symbol"], "name": p["name"], "isin": p["isin"], "value_eur": p["value"]})  # every directly held class stays visible
            c["security_ids"].append(p["security_id"])
    etfs: list[dict[str, Any]] = []
    other_eur = 0.0
    drift_eur = 0.0
    unresolved_eur = 0.0
    for p in held:
        if not p["is_etf"]:
            continue
        status, data, detail = eh.status_of(layers, p["isin"], evaluation)
        report = {"security_id": p["security_id"], "symbol": p["symbol"], "name": p["name"], "isin": p["isin"], "value_eur": p["value"], "status": status, "detail": detail,
                  "as_of": data.as_of if data else None, "provider": data.provider if data else None, "source": data.source if data else None, "kind": data.kind if data else None,
                  "holdings_count": 0, "equity_count": 0, "equity_weight_pct": None, "other_weight_pct": None, "isin_weight_pct": None, "weights_total_pct": None,
                  "weights_drift_pct": None, "rounding_drift_eur": 0.0,
                  **{f"{k}_{f}": (0 if f == "count" else None) for k in (DIRECT_MATCHED, ETF_ONLY, AMBIGUOUS_UNMATCHED) for f in ("count", "weight_pct")}}
        etfs.append(report)
        if data is None:
            unresolved_eur += p["value"]
            warnings.append(f"HOLDINGS_{status}: {p['symbol']} stays one unresolved block ({detail})")
            continue
        if status == eh.STALE:
            warnings.append(f"HOLDINGS_STALE: holdings of {p['symbol']} are from {data.as_of}")
        equity_w = isin_w = other_w = 0.0
        classes = {DIRECT_MATCHED: [0, 0.0], ETF_ONLY: [0, 0.0], AMBIGUOUS_UNMATCHED: [0, 0.0]}
        by_key: dict[str, list[Any]] = {}
        for h in data.holdings:
            if not h.is_equity:
                other_w += h.weight
                continue
            security_id, basis = matcher.match(h)
            w = h.weight
            equity_w += w
            report["equity_count"] += 1
            if h.isin:
                isin_w += w
            key = company_key(h, security_id, layers.issuer_groups, matcher.info[security_id]["isin"] or "" if security_id is not None else "")
            if basis == AMBIGUOUS:
                klass = AMBIGUOUS_UNMATCHED
            elif security_id is None and not h.isin and h.ticker in ("", "-"):
                klass, basis = AMBIGUOUS_UNMATCHED, NAME_ONLY  # no identifier at all: only a name, which is never matched
            elif key in companies and companies[key]["direct_value_eur"] > 0:
                klass = DIRECT_MATCHED
            else:
                klass = ETF_ONLY
            classes[klass][0] += 1
            classes[klass][1] += w
            if key in by_key:  # the same listing twice in one file: one company, the weights add up
                by_key[key][1] += w
                continue
            by_key[key] = [h, w, security_id, basis]
        for key, (h, w, security_id, basis) in by_key.items():
            value = p["value"] * w / 100.0
            name = matcher.info[security_id]["name"] if security_id is not None else h.name
            c = company(key, name or h.name, security_id, matcher.info[security_id]["symbol"] if security_id is not None else h.ticker, (matcher.info[security_id]["isin"] or "") if security_id is not None else h.isin)
            if key.startswith("grp:"):
                c["name"] = layers.issuer_groups.get((matcher.info[security_id]["isin"] or h.isin or "").upper() if security_id is not None else h.isin.upper(), c["name"])
                c["matched"] = c["matched"] or security_id is not None
                c["security_id"] = c["security_id"] if c["security_id"] is not None else security_id
            c["etf_value_eur"] += value
            if security_id is not None and security_id not in c["security_ids"]:
                c["security_ids"].append(security_id)
            c["contributors"].append({"etf": p["symbol"], "etf_security_id": p["security_id"], "value_eur": value, "weight_in_etf_pct": w, "match": basis,
                                      "holding_name": h.name, "etf_as_of": data.as_of, "gics_sector": h.sector, "location": h.location})
            if basis not in c["match_bases"]:
                c["match_bases"].append(basis)
            if basis in (AMBIGUOUS, NAME_ONLY):
                c["identity_unclear"] = basis
        residual = (100.0 - data.total_weight) if data.kind == eh.PHYSICAL else 0.0  # the part of a physical fund the published weights do not explain
        drift = (data.total_weight - 100.0) if data.kind == eh.SYNTHETIC_INDEX else 0.0  # the excess of a published index list over 100 %
        other_eur += p["value"] * (other_w + residual) / 100.0
        drift_eur -= p["value"] * drift / 100.0
        report.update(holdings_count=len(data.holdings), equity_weight_pct=equity_w, other_weight_pct=other_w + residual, isin_weight_pct=isin_w, weights_total_pct=data.total_weight,
                      weights_drift_pct=drift if data.kind == eh.SYNTHETIC_INDEX else None, rounding_drift_eur=-p["value"] * drift / 100.0, note=data.note)
        for klass, (count, weight) in classes.items():
            report[f"{klass}_count"], report[f"{klass}_weight_pct"] = count, weight

    for s in securities:  # every security of a reviewed issuer group belongs to that company, held or not (a candidate in the group is limited by what the group already is)
        group = layers.issuer_groups.get(str(s.get("isin") or "").upper())
        company_row = companies.get("grp:" + eh.norm(group)) if group else None
        if company_row is not None and s["id"] not in company_row["security_ids"]:
            company_row["security_ids"].append(s["id"])
    rows = []
    for c in companies.values():
        c["security_ids"] = sorted(set(c["security_ids"]))
        for position in c["direct_positions"]:
            position["weight_pct"] = position["value_eur"] / total * 100.0 if total > _EPS else 0.0
        c["direct_positions"].sort(key=lambda x: -x["value_eur"])
        effective = c["direct_value_eur"] + c["etf_value_eur"]
        c["effective_value_eur"] = effective
        c["category"] = DIRECT_MATCHED if c["direct_value_eur"] > 0 else AMBIGUOUS_UNMATCHED if c.get("identity_unclear") else ETF_ONLY
        for field_name in ("direct", "etf", "effective"):
            c[f"{field_name}_weight_pct"] = c[f"{field_name}_value_eur"] / total * 100.0 if total > _EPS else 0.0
        c["contributors"].sort(key=lambda x: -x["value_eur"])
        c["etf_count"] = len({x["etf"] for x in c["contributors"]})
        rows.append(c)
    rows.sort(key=lambda r: (-r["effective_value_eur"], r["name"]))
    sum_companies = sum(r["effective_value_eur"] for r in rows)
    etf_total = sum(e["value_eur"] for e in etfs)
    resolved = sum(e["value_eur"] for e in etfs if e["status"] in (eh.APPLIED, eh.STALE))
    context_total = _num(context.total_market_value)
    if unvalued:
        warnings.append(f"UNVALUED_POSITIONS: {len(unvalued)} open position(s) without a EUR valuation are not part of the values")
    def bucket(category: str) -> dict[str, Any]:
        members = [r for r in rows if r["category"] == category]
        value = sum(r["effective_value_eur"] for r in members)
        return {"value_eur": value, "pct_of_total": value / total * 100.0 if total > _EPS else 0.0, "company_count": len(members),
                "direct_value_eur": sum(r["direct_value_eur"] for r in members), "etf_value_eur": sum(r["etf_value_eur"] for r in members)}

    unclear = bucket(AMBIGUOUS_UNMATCHED)
    unclear_value = unclear["value_eur"] + unresolved_eur
    unclear.update(value_eur=unclear_value, pct_of_total=unclear_value / total * 100.0 if total > _EPS else 0.0, unresolved_etf_value_eur=unresolved_eur,
                   ambiguous_value_eur=sum(r["effective_value_eur"] for r in rows if r["category"] == AMBIGUOUS_UNMATCHED and r.get("identity_unclear") == AMBIGUOUS),
                   name_only_value_eur=sum(r["effective_value_eur"] for r in rows if r["category"] == AMBIGUOUS_UNMATCHED and r.get("identity_unclear") == NAME_ONLY))
    buckets = {DIRECT_MATCHED: bucket(DIRECT_MATCHED), ETF_ONLY: bucket(ETF_ONLY), AMBIGUOUS_UNMATCHED: unclear,
               "other": {"value_eur": other_eur, "pct_of_total": other_eur / total * 100.0 if total > _EPS else 0.0},
               "rounding_drift": {"value_eur": drift_eur, "pct_of_total": drift_eur / total * 100.0 if total > _EPS else 0.0}}
    guard = guard_inputs(etfs, rows)
    reconciled = abs(sum(b["value_eur"] for b in buckets.values()) - total) <= 0.01 and abs(sum_companies + other_eur + drift_eur + unresolved_eur - total) <= 0.01
    return {
        "ok": True, "operation": OPERATION, "as_of": evaluation, "base_currency": "EUR", "total_market_value_eur": total, "limit_note": "no limit is applied here; the risk guard is unchanged",
        "company_count": len(rows), "direct_company_count": sum(1 for r in rows if r["direct_value_eur"] > 0), "etf_company_count": sum(1 for r in rows if r["etf_value_eur"] > 0),
        "overlap_company_count": sum(1 for r in rows if r["direct_value_eur"] > 0 and r["etf_value_eur"] > 0),
        "top": [public_row(r) for r in rows[: max(0, int(top))]], "top_count": min(len(rows), max(0, int(top))), "top_share_pct": sum(r["effective_value_eur"] for r in rows[: max(0, int(top))]) / total * 100.0 if total > _EPS else 0.0,
        "etfs": etfs,
        "coverage": {"etf_value_eur": etf_total, "resolved_etf_value_eur": resolved, "resolved_pct": resolved / etf_total * 100.0 if etf_total > _EPS else None,
                     "unresolved_etf_value_eur": unresolved_eur, "other_value_eur": other_eur, "other_pct_of_total": other_eur / total * 100.0 if total > _EPS else 0.0,
                     "rounding_drift_eur": drift_eur, "equity_holdings": sum(e["equity_count"] for e in etfs),
                     **{f"{k}_holdings": sum(e[f"{k}_count"] for e in etfs) for k in (DIRECT_MATCHED, ETF_ONLY, AMBIGUOUS_UNMATCHED)}},
        "buckets": buckets, "guard": guard,
        "unvalued": unvalued,
        "checks": {"companies_eur": sum_companies, "other_eur": other_eur, "rounding_drift_eur": drift_eur, "unresolved_eur": unresolved_eur, "context_total_eur": context_total, "reconciles": reconciled and (context_total is None or abs(context_total - total) <= 0.01)},
        "warnings": warnings,
        **({"companies": rows} if with_companies else {}),
    }
