"""Ensemble combination across S1/S2/S3.

Two layers:
- combine_target_weights: point-in-time blend of each system's target weights,
  weighted by inverse realized vol (risk parity across systems), then netted by
  symbol. Used live.
- min_variance_system_weights: given each system's return history, solve the
  minimum-variance blend with Ledoit-Wolf shrinkage. Used for the ensemble's
  static allocation in validation.

Deliberately simple: no performance-chasing. Slow Bayesian updates only later.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from systems.s1_quant.covariance import ledoit_wolf_cov


def inverse_vol_system_weights(returns: dict[str, pd.Series]) -> dict[str, float]:
    """Risk-parity weights across systems from realized vol."""
    vols = {}
    for name, r in returns.items():
        sd = r.dropna().std()
        if sd and sd > 0:
            vols[name] = 1.0 / sd
    total = sum(vols.values())
    if total == 0:
        return {k: 1.0 / len(returns) for k in returns}
    return {k: v / total for k, v in vols.items()}


def min_variance_system_weights(returns: dict[str, pd.Series]) -> dict[str, float]:
    """Minimum-variance blend across systems (long-only, sums to 1)."""
    df = pd.DataFrame(returns).dropna()
    if df.shape[1] < 2 or df.shape[0] < 30:
        return inverse_vol_system_weights(returns)
    cov = ledoit_wolf_cov(df).to_numpy()
    try:
        inv = np.linalg.pinv(cov)
        ones = np.ones(cov.shape[0])
        w = inv @ ones / (ones @ inv @ ones)
        w = np.clip(w, 0, None)             # no shorting a system
        if w.sum() == 0:
            raise ValueError
        w = w / w.sum()
    except Exception:
        return inverse_vol_system_weights(returns)
    return dict(zip(df.columns, w))


def combine_target_weights(
    system_weights: dict[str, pd.Series],
    system_alphas: dict[str, float],
    max_gross: float = 1.0,
) -> pd.Series:
    """Blend per-system target weights by alpha, net by symbol, cap gross."""
    combined = pd.Series(dtype=float)
    for name, w in system_weights.items():
        a = system_alphas.get(name, 0.0)
        combined = combined.add(a * w, fill_value=0.0)
    combined = combined[combined.abs() > 1e-9]
    gross = combined.abs().sum()
    if gross > max_gross and gross > 0:
        combined = combined * (max_gross / gross)
    return combined
