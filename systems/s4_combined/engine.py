"""S4Engine - the combined book.

Takes each sub-system's target weights and recent return history, computes
risk-parity (or min-variance) system allocations, and produces the netted
ensemble book.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .combine import (
    combine_target_weights,
    inverse_vol_system_weights,
    min_variance_system_weights,
)


@dataclass
class S4Result:
    weights: pd.Series
    system_alphas: dict[str, float]


@dataclass
class S4Engine:
    method: str = "risk_parity"          # or "min_variance"
    max_gross: float = 1.0

    def generate(
        self,
        system_weights: dict[str, pd.Series],
        system_returns: dict[str, pd.Series] | None = None,
    ) -> S4Result:
        if system_returns and self.method == "min_variance":
            alphas = min_variance_system_weights(system_returns)
        elif system_returns:
            alphas = inverse_vol_system_weights(system_returns)
        else:
            n = len(system_weights)
            alphas = {k: 1.0 / n for k in system_weights}
        combined = combine_target_weights(system_weights, alphas, max_gross=self.max_gross)
        return S4Result(weights=combined, system_alphas=alphas)
