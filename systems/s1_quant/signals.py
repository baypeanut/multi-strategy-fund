"""Cross-sectional quant signals (no LLM).

Each signal returns a pandas Series indexed by symbol (latest cross-section),
then is converted to a cross-sectional z-score. Signals are combined by the
engine. Conventions: positive score = expected outperformance (go long).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr


def cross_sectional_zscore(s: pd.Series) -> pd.Series:
    s = s.dropna()
    if s.std(ddof=0) == 0 or len(s) < 2:
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / s.std(ddof=0)


# --- individual signals --------------------------------------------------
def momentum_12_1(close: pd.DataFrame, vols: pd.Series | None = None) -> pd.Series:
    """12-1 month momentum: P(t-21)/P(t-252) - 1, optionally risk-adjusted."""
    if len(close) < 252:
        lookback, skip = len(close) - 1, min(21, len(close) // 12)
    else:
        lookback, skip = 252, 21
    mom = close.iloc[-1 - skip] / close.iloc[-lookback] - 1.0
    if vols is not None:
        mom = mom / vols.replace(0, np.nan)
    return mom


def short_term_reversal(close: pd.DataFrame, n: int = 5) -> pd.Series:
    """Negative of standardized deviation from short SMA (buy losers)."""
    sma = close.rolling(n).mean().iloc[-1]
    sd = close.rolling(n).std(ddof=0).iloc[-1].replace(0, np.nan)
    z = (close.iloc[-1] - sma) / sd
    return -z


def low_volatility(vols: pd.Series) -> pd.Series:
    """Low-vol factor tilt: prefer lower-vol names."""
    return -cross_sectional_zscore(vols)


def ou_half_life(series: pd.Series) -> float:
    """Ornstein-Uhlenbeck mean-reversion half-life via AR(1) on differences.

    Regress dY_t on Y_{t-1}: slope b<0 implies mean reversion; H = -ln(2)/b.
    Returns np.inf if not mean-reverting.
    """
    y = series.dropna()
    if len(y) < 30:
        return float("inf")
    lag = y.shift(1).dropna()
    dy = (y - y.shift(1)).dropna()
    lag, dy = lag.align(dy, join="inner")
    b = np.polyfit(lag.to_numpy(), dy.to_numpy(), 1)[0]
    if b >= 0:
        return float("inf")
    return float(-np.log(2) / b)


def information_coefficient(signal: pd.Series, fwd_returns: pd.Series) -> float:
    """Spearman rank IC between a signal cross-section and forward returns."""
    s, f = signal.align(fwd_returns, join="inner")
    s, f = s.dropna(), f.dropna()
    s, f = s.align(f, join="inner")
    if len(s) < 3:
        return 0.0
    ic, _ = spearmanr(s.to_numpy(), f.to_numpy())
    return float(ic) if not np.isnan(ic) else 0.0
