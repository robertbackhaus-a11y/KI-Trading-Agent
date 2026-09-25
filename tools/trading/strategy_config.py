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
class RiskTargetConfig:
    """Configured only; Phase 2 intentionally assigns no mathematical meaning."""

    risk_target_min: float = 0.60
    risk_target_max: float = 0.70


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
class StrategyConfig:
    """Single entry point for all deterministic strategy parameters."""

    swing: SwingStrategyConfig = field(default_factory=SwingStrategyConfig)
    portfolio: PortfolioTargetConfig = field(default_factory=PortfolioTargetConfig)
    capital_state: CapitalStateConfig = field(default_factory=CapitalStateConfig)
    sizing: SizingPolicyConfig = field(default_factory=SizingPolicyConfig)
    risk: RiskTargetConfig = field(default_factory=RiskTargetConfig)
    data_quality: DataQualityConfig = field(default_factory=DataQualityConfig)
    fx: FXConfig = field(default_factory=FXConfig)
