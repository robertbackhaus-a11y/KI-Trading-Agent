# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The exposure basis of the single-name guard (the per-security weight limit of the ADD and BUY sizing): direct position + the share of the same company in the held ETFs.

The company is the unit: other directly held share classes of the same issuer (a reviewed issuer group such as Alphabet A and C) count with the direct position, whatever the state of
the ETF holdings.

The weight limit (``SizingPolicyConfig.max_security_weight``, 20 % of the invested value) used to look at the direct position only.  A company that is also inside an ETF the portfolio
holds carries more risk than its direct position shows (a 6.2 % direct TSMC position plus 1.1 % through an ETF is 7.3 % of the portfolio).  The effective figures come from
``effective_exposure`` (one calculation, run once per orchestrator run and handed in through ``PortfolioContext.etf_exposure_by_security``); this module only decides **which basis a
sizing function may use** and says so, so that nothing is hidden:

* ``effective``: every ETF the portfolio holds has usable (present and not stale) holdings; the limit applies to direct + ETF share;
* ``direct``: the portfolio holds no ETF, direct and effective exposure are the same thing;
* ``direct_only`` (NOT complete): the ETF holdings were not evaluated, are missing, stale or invalid.  The limit still applies to the direct position (as before), but the result says
  that the effective exposure is **not assessed** and why (``notes``), so a direct figure is never presented as complete.  There is no silent fallback.

The rounding drift of a published index list never enters here: the companies' ETF values are the published weights times the ETF value, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from analysis_contracts import AvailabilityStatus

EFFECTIVE, DIRECT, DIRECT_ONLY = "effective", "direct", "direct_only"
NOT_EVALUATED = "EFFECTIVE_EXPOSURE_NOT_EVALUATED"


@dataclass(frozen=True)
class SingleName:
    """The exposure of one security as the guard sees it (EUR)."""

    direct_value: float
    etf_value: float  # 0 unless the basis is ``effective``
    effective_value: float  # direct + etf (equal to direct for the bases ``direct`` and ``direct_only``)
    basis: str
    notes: tuple[str, ...] = ()
    other_class_value: float = 0.0  # direct value of the other share classes of the same issuer (already inside ``effective_value``)

    @property
    def complete(self) -> bool:
        """False when the effective exposure is not assessed (basis ``direct_only``)."""
        return self.basis != DIRECT_ONLY


def resolve(portfolio: Optional[Any], security_id: Optional[int], direct_value: float) -> SingleName:
    """The basis for ``security_id``.  ``portfolio`` is a ``PortfolioContext`` (``None`` or without ETF information: not evaluated)."""
    quality = getattr(portfolio, "etf_exposure_quality", None) if portfolio is not None else None
    # the other share classes of the issuer are direct positions: they count whatever the state of the ETF holdings
    other = float(dict(getattr(portfolio, "other_class_direct_by_security", ()) or ()).get(int(security_id), 0.0)) if portfolio is not None and security_id is not None else 0.0
    base = direct_value + other
    if quality is None:
        return SingleName(direct_value, 0.0, base, DIRECT_ONLY, (NOT_EVALUATED,), other)
    if quality.status is AvailabilityStatus.NOT_APPLICABLE:
        return SingleName(direct_value, 0.0, base, DIRECT, (), other)
    if quality.status is not AvailabilityStatus.AVAILABLE:
        notes = (f"EFFECTIVE_EXPOSURE_{quality.status.name}", *quality.details)
        return SingleName(direct_value, 0.0, base, DIRECT_ONLY, tuple(notes), other)
    by_security = dict(getattr(portfolio, "etf_exposure_by_security", ()) or ())
    etf_value = float(by_security.get(int(security_id), 0.0)) if security_id is not None else 0.0
    return SingleName(direct_value, etf_value, base + etf_value, EFFECTIVE, (), other)
