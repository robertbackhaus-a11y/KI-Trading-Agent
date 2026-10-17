# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Explicit, typed configuration for deterministic trading rules.

This module deliberately contains parameters only.  It does not load files,
open a database, or infer rules that have not been defined yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SwingStrategyConfig:
    """Known swing-position parameters; undefined rules stay ``None``."""

    horizon_months_min: int = 3
    horizon_months_max: int = 6
    tp1_gain_pct: float = 0.20
    tp1_sell_fraction: float = 0.25
    tp2_gain_pct: float = 0.25
    tp2_sell_fraction: float = 0.50
    remainder_management: str = "momentum_guided"
    # Maximum loss from the immutable campaign reference price. A value of
    # 0.15 means the hard-stop price is 15% below that reference.
    hard_stop_loss_pct: float = 0.15

    # These intentionally have no default trading rule yet.
    entry_rule: Optional[str] = None
    add_rule: Optional[str] = None
    stop_rule: Optional[str] = None
    position_sizing_rule: Optional[str] = None


@dataclass(frozen=True)
class PortfolioTargetConfig:
    """Portfolio allocation ranges; informational until sizing is defined."""

    long_term_min_pct: float = 0.60
    long_term_max_pct: float = 0.70
    swing_min_pct: float = 0.30
    swing_max_pct: float = 0.40

    # Compatibility aliases for the original ETF-focused naming.  The live
    # long-term allocation currently consists of ETF holdings, but the policy
    # is strategy-based rather than inferred from an asset type.
    @property
    def etf_target_min(self) -> float:
        return self.long_term_min_pct

    @property
    def etf_target_max(self) -> float:
        return self.long_term_max_pct

    @property
    def swing_target_min(self) -> float:
        return self.swing_min_pct

    @property
    def swing_target_max(self) -> float:
        return self.swing_max_pct


@dataclass(frozen=True)
class CapitalStateConfig:
    """Capital-state freshness is opt-in; ``None`` means no age threshold."""

    freshness_max_age_days: Optional[int] = None


@dataclass(frozen=True)
class SizingPolicyConfig:
    """Central deterministic policy for position additions in EUR portfolios."""

    max_security_weight: Optional[float] = 0.20
    max_initial_swing_weight: Optional[float] = 0.10
    max_add_pct_of_original: Optional[float] = 0.25
    minimum_cash_reserve: Optional[float] = 10_000.0
    max_add_count: Optional[int] = 1
    whole_share_policy: Optional[str] = "floor"


@dataclass(frozen=True)
class DataQualityConfig:
    """Deterministic data-freshness limits; not a trading rule."""

    market_data_max_age_days: int = 5


@dataclass(frozen=True)
class FXConfig:
    """Currency-normalization parameters, separate from trading rules."""

    portfolio_base_currency: str = "EUR"
    fx_max_age_days: int = 5
    preferred_fx_source: str = "ECB"


@dataclass(frozen=True)
class TaxConfig:
    """German capital-gains tax parameters for the planner's net-proceeds simulation.

    The combined rate applies to the taxable realized gain, never to gross
    proceeds.  The surcharge is a share of the capital gains tax.  Church tax is
    not modelled (the reduced-rate formula for church tax payers is missing), so
    only ``0.0`` is accepted.  Simulation parameters, not tax advice.
    """

    capital_gains_tax_rate: float = 0.25
    solidarity_surcharge_rate: float = 0.055
    church_tax_rate: float = 0.0

    def __post_init__(self) -> None:
        for name in ("capital_gains_tax_rate", "solidarity_surcharge_rate", "church_tax_rate"):
            value = getattr(self, name)
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")
        if self.church_tax_rate != 0.0:
            raise ValueError("church tax is not modelled; church_tax_rate must be 0.0")

    @property
    def combined_rate(self) -> float:
        """0.25 * (1 + 0.055) = 26.375 % of the taxable realized gain."""
        return self.capital_gains_tax_rate * (1.0 + self.solidarity_surcharge_rate)


@dataclass(frozen=True)
class DiscoveryConfig:
    """Pre-filter parameters of the read-only watchlist candidate discovery.

    Proposal, not a trading rule: they only keep illiquid or penny-priced stocks
    out of a *suggestion* list.  A median daily traded value of 5 million EUR keeps
    a typical position of the portfolio (up to about 60,000 EUR) below roughly 1 %
    of a day's turnover; 5 EUR is the usual penny-stock boundary.
    """

    min_price_eur: float = 5.0
    min_median_daily_value_eur: float = 5_000_000.0
    liquidity_window_days: int = 60


@dataclass(frozen=True)
class MarketIntelligenceConfig:
    """Window and freshness of the read-only market intelligence report (proposal, not a trading rule).

    News are time critical: the window keeps one trading week of items and a report older than
    24 hours (one trading day) is flagged ``REPORT_STALE`` by the reader.  The existing
    ``market_data_max_age_days`` (5 days) is a market-data rule and too coarse for news.
    """

    lookback_days: int = 7
    report_max_age_hours: int = 24


@dataclass(frozen=True)
class RiskGuardConfig:
    """Sector and correlation guards of the Portfolio Action Planner (proposal, derived from existing limits and measured data).

    ``max_sector_weight`` (30 % of the invested value) is three full initial Swing positions
    (3 x ``SizingPolicyConfig.max_initial_swing_weight`` of 10 %) and 1.5 x the per-security maximum of 20 %.  The same limit
    applies to a *correlation cluster* (the candidate plus every holding whose daily returns correlate at
    ``correlation_threshold`` or more), so one limit covers both guards.  ``correlation_threshold`` 0.70 is the usual boundary
    of a strong correlation; on the measured universe only about 1 % of all pairs reach it.  Correlations use the last
    ``correlation_window_days`` common daily returns and are only valid with at least ``correlation_min_common_days`` of them
    (about half a year; the standard error of r = 0.7 is then about 0.05).
    """

    max_sector_weight: float = 0.30
    correlation_threshold: float = 0.70
    correlation_window_days: int = 250
    correlation_min_common_days: int = 120

    def __post_init__(self) -> None:
        if not 0.0 < float(self.max_sector_weight) <= 1.0:
            raise ValueError("max_sector_weight must be within (0, 1]")
        if not 0.0 < float(self.correlation_threshold) <= 1.0:
            raise ValueError("correlation_threshold must be within (0, 1]")
        if int(self.correlation_min_common_days) < 20 or int(self.correlation_window_days) < int(self.correlation_min_common_days):
            raise ValueError("correlation_min_common_days must be >= 20 and <= correlation_window_days")


@dataclass(frozen=True)
class StrategyConfig:
    """Single entry point for all deterministic strategy parameters."""

    swing: SwingStrategyConfig = field(default_factory=SwingStrategyConfig)
    portfolio: PortfolioTargetConfig = field(default_factory=PortfolioTargetConfig)
    capital_state: CapitalStateConfig = field(default_factory=CapitalStateConfig)
    sizing: SizingPolicyConfig = field(default_factory=SizingPolicyConfig)
    data_quality: DataQualityConfig = field(default_factory=DataQualityConfig)
    fx: FXConfig = field(default_factory=FXConfig)
    tax: TaxConfig = field(default_factory=TaxConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    market_intelligence: MarketIntelligenceConfig = field(default_factory=MarketIntelligenceConfig)
    risk: RiskGuardConfig = field(default_factory=RiskGuardConfig)
