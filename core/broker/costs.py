"""Transaction cost model (intentionally pessimistic).

Implements the spec's cost stack:

    TC = half_spread + market_impact + commission  (+ borrow if short, per day)

Market impact uses the square-root law (Almgren-Chriss family):

    impact_bps = Y * sigma_daily_bps * sqrt(Q / ADV)

where Y is a calibration coefficient (~0.1), sigma_daily is the asset's daily
volatility, Q is the trade's dollar value, and ADV its 20-day average dollar
volume. Costs are returned in fractional terms (1 bp = 1e-4).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

BPS = 1e-4


@dataclass
class CostParams:
    commission_bps: float
    half_spread_bps: float
    impact_coef: float          # Y in the square-root law
    borrow_annual_bps: float = 0.0


@dataclass
class CostBreakdown:
    half_spread: float          # fractional
    impact: float               # fractional
    commission: float           # fractional
    @property
    def slippage(self) -> float:
        """Price slippage applied to the fill (spread + impact)."""
        return self.half_spread + self.impact

    @property
    def total(self) -> float:
        return self.half_spread + self.impact + self.commission


class CostModel:
    def __init__(self, params: CostParams) -> None:
        self.p = params

    def estimate(
        self,
        trade_value: float,
        adv: float,
        daily_vol: float,
    ) -> CostBreakdown:
        """Return fractional cost components for a single trade.

        trade_value : dollar notional of the order (abs)
        adv         : 20-day average dollar volume of the name
        daily_vol   : daily return volatility (e.g. 0.02 = 2%)
        """
        half_spread = self.p.half_spread_bps * BPS

        participation = (trade_value / adv) if adv > 0 else 0.0
        # square-root impact law; daily_vol already in return units
        impact = self.p.impact_coef * daily_vol * math.sqrt(max(participation, 0.0))

        commission = self.p.commission_bps * BPS
        return CostBreakdown(half_spread=half_spread, impact=impact, commission=commission)

    def borrow_cost(self, short_value: float, days: float = 1.0) -> float:
        """Dollar borrow cost for holding a short position `days` days."""
        daily_rate = (self.p.borrow_annual_bps * BPS) / 252.0
        return abs(short_value) * daily_rate * days
