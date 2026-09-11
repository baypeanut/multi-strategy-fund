"""Portfolio construction: inverse-vol, ERC risk-parity, vol-targeting, Kelly.

The quant book is built as a market-neutral-ish long/short tilt: combined signal
z-scores are risk-adjusted, scaled to a target volatility via the covariance
matrix, capped per name, and clipped to the gross limit.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .covariance import portfolio_vol


def inverse_vol_weights(vols: pd.Series) -> pd.Series:
    inv = 1.0 / vols.replace(0, np.nan)
    inv = inv.dropna()
    return inv / inv.sum()


def erc_weights(cov: pd.DataFrame, iters: int = 500, tol: float = 1e-8) -> pd.Series:
    """Equal Risk Contribution (risk parity) via cyclical coordinate descent."""
    Sigma = cov.to_numpy()
    n = Sigma.shape[0]
    w = np.ones(n) / n
    for _ in range(iters):
        w_prev = w.copy()
        Sw = Sigma @ w
        for i in range(n):
            # target: each asset contributes 1/n of total risk
            num = w[i] * Sw[i]
            # update toward equal risk contribution
            w[i] = w[i] * (1.0 / n) / (num / (w @ Sw))
        w = np.clip(w, 0, None)
        w /= w.sum()
        if np.max(np.abs(w - w_prev)) < tol:
            break
    return pd.Series(w, index=cov.index)


def risk_contributions(weights: pd.Series, cov: pd.DataFrame) -> pd.Series:
    w = weights.reindex(cov.index).fillna(0.0).to_numpy()
    Sigma = cov.to_numpy()
    port_var = w @ Sigma @ w
    if port_var <= 0:
        return pd.Series(0.0, index=cov.index)
    rc = w * (Sigma @ w) / np.sqrt(port_var)
    return pd.Series(rc, index=cov.index)


def scale_to_target_vol(
    weights: pd.Series,
    cov: pd.DataFrame,
    target_vol: float,
    max_position: float = 0.05,
    max_gross: float = 1.00,
) -> pd.Series:
    """Rescale an arbitrary weight vector to an ex-ante annualized vol target.

    Applies the SAME post-processing S1 gets (per-name cap, then gross clip),
    so books passed through this share one risk normalization and their equity
    curves are comparable (W3: fair horse race).
    """
    common = weights.index.intersection(cov.index)
    w = weights.reindex(common).fillna(0.0)
    if w.abs().sum() == 0:
        return w
    pv = portfolio_vol(w, cov.reindex(index=common, columns=common), annualize=True)
    if pv > 0:
        w = w * (target_vol / pv)
    w = w.clip(lower=-max_position, upper=max_position)
    gross = w.abs().sum()
    if gross > max_gross:
        w = w * (max_gross / gross)
    return w


def kelly_fraction(mu: float, sigma: float, fraction: float = 0.25) -> float:
    """Continuous Kelly f* = mu/sigma^2, scaled by a safety fraction."""
    if sigma <= 0:
        return 0.0
    return fraction * mu / (sigma**2)


def construct_signal_portfolio(
    signal_scores: pd.Series,
    cov: pd.DataFrame,
    vols: pd.Series,
    target_vol: float = 0.10,
    max_position: float = 0.05,
    max_gross: float = 1.00,
    market_neutral: bool = True,
) -> pd.Series:
    """Turn combined signal z-scores into target weights.

    1. risk-adjust:  raw_i = z_i / sigma_i
    2. (optional) demean for market-neutrality
    3. scale to target annualized vol using the covariance matrix
    4. cap per-name and clip gross
    """
    common = signal_scores.index.intersection(cov.index).intersection(vols.index)
    z = signal_scores.reindex(common).fillna(0.0)
    sig = vols.reindex(common).replace(0, np.nan)

    raw = (z / sig).fillna(0.0)
    if market_neutral and len(raw) > 1:
        raw = raw - raw.mean()
    if raw.abs().sum() == 0:
        return pd.Series(0.0, index=common)

    # normalize to unit gross first
    w = raw / raw.abs().sum()

    # vol-target scaling
    pv = portfolio_vol(w, cov.reindex(index=common, columns=common), annualize=True)
    if pv > 0:
        w = w * (target_vol / pv)

    # per-name cap then gross clip
    #
    # E64. The clip DESTROYS the market-neutrality step 2 established, and
    # nothing used to restore it. Measured on the live book 2026-08-18: the S1
    # equity sleeve left here at net -0.0555 on gross 0.2038 - 27% of gross net
    # SHORT - despite market_neutral=True. Loosening max_position to 0.25 took
    # the net to exactly 0.0, which is the proof that the clip and not the
    # signals was the cause.
    #
    # Worse than it looks, because the caller then scales the book UP to reach
    # the vol target, magnifying the imbalance instead of correcting it: -0.044
    # became -0.255 on the live merged book. S1, the CONTROL arm of the
    # registered experiment, was running a 23% net short in a market that rose
    # 3.74% over the same window, at a beta of -0.14, with nobody having chosen
    # that exposure.
    #
    # Fix: alternate demean and clip until both hold. One pass is not enough -
    # demeaning after a clip can push a name back over the cap - and it
    # converges because each demean shifts a bounded vector by its own mean.
    # Capped at 20 iterations to guarantee termination; the residual net is
    # asserted small in tests rather than assumed.
    for _ in range(20):
        w = w.clip(lower=-max_position, upper=max_position)
        if not (market_neutral and len(w) > 1):
            break
        drift = w.mean()
        if abs(drift) * len(w) < 1e-9:
            break
        w = w - drift
    if market_neutral and len(w) > 1:
        w = w.clip(lower=-max_position, upper=max_position)

    gross = w.abs().sum()
    if gross > max_gross:
        w = w * (max_gross / gross)
    return w
