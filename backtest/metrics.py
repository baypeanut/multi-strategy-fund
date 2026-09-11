"""Performance & overfit statistics — the honest scorecard.

Includes the two that matter most for avoiding self-deception:
- Deflated Sharpe Ratio (DSR): penalizes multiple testing + non-normality.
- Probability of Backtest Overfitting (PBO) via CSCV.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm

TRADING_DAYS = 252
EULER_GAMMA = 0.5772156649


def annualized_return(returns: pd.Series) -> float:
    r = returns.dropna()
    if r.empty:
        return 0.0
    return float((1 + r).prod() ** (TRADING_DAYS / len(r)) - 1)


def annualized_vol(returns: pd.Series) -> float:
    return float(returns.dropna().std(ddof=1) * np.sqrt(TRADING_DAYS))


def sharpe_ratio(returns: pd.Series, rf: float = 0.0, annualize: bool = True) -> float:
    r = returns.dropna() - rf / TRADING_DAYS
    sd = r.std(ddof=1)
    if sd < 1e-15 or len(r) < 2:
        return 0.0
    sr = r.mean() / sd
    return float(sr * np.sqrt(TRADING_DAYS)) if annualize else float(sr)


def t_statistic(returns: pd.Series) -> float:
    """t-stat of the mean return ~ SR_periodic * sqrt(N)."""
    sr = sharpe_ratio(returns, annualize=False)
    return float(sr * np.sqrt(len(returns.dropna())))


def max_drawdown(equity: pd.Series) -> float:
    roll_max = equity.cummax()
    dd = equity / roll_max - 1.0
    return float(dd.min())


def probabilistic_sharpe_ratio(returns: pd.Series, sr_benchmark: float = 0.0) -> float:
    """PSR: P(true periodic SR > benchmark), adjusting for skew & kurtosis."""
    r = returns.dropna().to_numpy()
    n = len(r)
    if n < 8:
        return 0.0
    sr = sharpe_ratio(returns, annualize=False)
    g3 = float(pd.Series(r).skew())
    g4 = float(pd.Series(r).kurt()) + 3.0  # pandas kurt is excess; want raw
    denom = np.sqrt(max(1 - g3 * sr + ((g4 - 1) / 4) * sr**2, 1e-12))
    z = (sr - sr_benchmark) * np.sqrt(n - 1) / denom
    return float(norm.cdf(z))


def deflated_sharpe_ratio(
    returns: pd.Series, n_trials: int, sr_trials_std: float | None = None
) -> float:
    """DSR: PSR against the expected-maximum Sharpe under `n_trials` trials.

    SR* = sr_std * [ (1-gamma)*Phi^-1(1 - 1/N) + gamma*Phi^-1(1 - 1/(N e)) ]
    """
    r = returns.dropna()
    n = len(r)
    if n < 8 or n_trials < 1:
        return 0.0
    # A single pre-specified trial has no selection penalty. The extreme-value
    # approximation below is undefined at N=1 (Phi^-1(0) = -inf), which used
    # to turn even a losing strategy into DSR=1.
    if n_trials == 1:
        return probabilistic_sharpe_ratio(r, sr_benchmark=0.0)
    sr = sharpe_ratio(returns, annualize=False)
    if sr_trials_std is None:
        g3 = float(r.skew())
        g4 = float(r.kurt()) + 3.0
        sr_trials_std = np.sqrt(max((1 - g3 * sr + ((g4 - 1) / 4) * sr**2) / (n - 1), 1e-12))
    N = max(n_trials, 1)
    e = np.e
    sr_star = sr_trials_std * (
        (1 - EULER_GAMMA) * norm.ppf(1 - 1.0 / N)
        + EULER_GAMMA * norm.ppf(1 - 1.0 / (N * e))
    )
    return probabilistic_sharpe_ratio(returns, sr_benchmark=sr_star)


def paired_test(returns_a: pd.Series, returns_b: pd.Series, lags: int | None = None) -> dict:
    """Diebold-Mariano-style paired comparison of two return streams.

    Tests H0: E[r_a - r_b] = 0 on the DAILY difference series with Newey-West
    (HAC) standard errors — the only honest way to compare two highly
    correlated books (two separate Sharpes are statistically meaningless).

    Returns dict: mean_daily_bps, t_stat, p_value, n, ci95_bps (mean +/-).
    """
    a, b = returns_a.align(returns_b, join="inner")
    d = (a - b).dropna()
    n = len(d)
    if n < 10:
        return {"mean_daily_bps": float("nan"), "t_stat": float("nan"),
                "p_value": float("nan"), "n": n, "ci95_bps": float("nan")}
    mean = float(d.mean())
    e = (d - mean).to_numpy()
    L = lags if lags is not None else max(1, int(round(n ** (1 / 3))))
    # Newey-West long-run variance with Bartlett weights
    gamma0 = float(np.mean(e * e))
    lrv = gamma0
    for lag in range(1, min(L, n - 1) + 1):
        w = 1.0 - lag / (L + 1.0)
        # Every autocovariance uses the same n denominator. Dividing by
        # n-lag can destroy positive semidefiniteness of the Bartlett kernel.
        cov = float(np.dot(e[lag:], e[:-lag]) / n)
        lrv += 2.0 * w * cov
    lrv = max(lrv, 1e-18)
    se = np.sqrt(lrv / n)
    t = mean / se
    p = 2.0 * (1.0 - norm.cdf(abs(t)))
    return {"mean_daily_bps": mean * 1e4, "t_stat": float(t), "p_value": float(p),
            "n": n, "ci95_bps": 1.96 * se * 1e4}


def cscv_pbo(perf_matrix: pd.DataFrame, s: int = 10) -> float:
    """Probability of Backtest Overfitting via Combinatorially Symmetric CV.

    perf_matrix: rows = time, columns = configs (e.g. daily returns per config).
    Splits time into s blocks; over all (s choose s/2) IS/OOS partitions, picks
    the IS-best config and records its OOS rank. PBO = P(best-IS lands below the
    OOS median).
    """
    from itertools import combinations

    M = perf_matrix.dropna()
    T, N = M.shape
    if N < 2 or T < s:
        return float("nan")
    s = s - (s % 2)
    blocks = np.array_split(np.arange(T), s)
    logits = []
    for is_idx in combinations(range(s), s // 2):
        is_rows = np.concatenate([blocks[b] for b in is_idx])
        oos_rows = np.concatenate([blocks[b] for b in range(s) if b not in is_idx])
        is_perf = M.iloc[is_rows].mean()
        oos_perf = M.iloc[oos_rows].mean()
        best = is_perf.idxmax()
        # OOS rank of the IS-best config (1=worst .. N=best)
        rank = oos_perf.rank().loc[best]
        w = rank / (N + 1)
        w = min(max(w, 1e-6), 1 - 1e-6)
        logits.append(np.log(w / (1 - w)))
    logits = np.array(logits)
    return float(np.mean(logits <= 0))  # fraction where best-IS is below OOS median
