"""Volatility estimators: EWMA, GARCH(1,1), Yang-Zhang.

All functions take/return daily volatility unless `annualize=True`. Annualizing
uses sqrt(252). These feed signal risk-adjustment, covariance, and vol-targeting.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize

TRADING_DAYS = 252


def log_returns(prices: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    return np.log(prices / prices.shift(1)).dropna()


def ewma_vol(returns: pd.Series, lam: float = 0.94, annualize: bool = False) -> float:
    """RiskMetrics EWMA: sigma^2_t = lam*sigma^2_{t-1} + (1-lam)*r^2_{t-1}."""
    r = returns.dropna().to_numpy()
    if r.size == 0:
        return 0.0
    var = float(np.var(r))  # seed with sample variance
    for x in r:
        var = lam * var + (1.0 - lam) * x * x
    sigma = float(np.sqrt(var))
    return sigma * np.sqrt(TRADING_DAYS) if annualize else sigma


def garch11_vol(returns: pd.Series, annualize: bool = False) -> float:
    """Fit GARCH(1,1) by MLE (normal innovations), return 1-step-ahead sigma.

    sigma^2_t = omega + alpha*eps^2_{t-1} + beta*sigma^2_{t-1}
    Falls back to EWMA if the optimizer fails to converge.
    """
    r = returns.dropna().to_numpy()
    if r.size < 50:
        return ewma_vol(returns, annualize=annualize)
    r = r - r.mean()
    uncond_var = float(np.var(r))

    def neg_loglik(params: np.ndarray) -> float:
        omega, alpha, beta = params
        if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 1:
            return 1e10
        sigma2 = np.empty_like(r)
        sigma2[0] = uncond_var
        for t in range(1, r.size):
            sigma2[t] = omega + alpha * r[t - 1] ** 2 + beta * sigma2[t - 1]
        ll = -0.5 * np.sum(np.log(2 * np.pi) + np.log(sigma2) + r**2 / sigma2)
        return -ll

    x0 = np.array([uncond_var * 0.05, 0.05, 0.90])
    bounds = [(1e-12, None), (0.0, 1.0), (0.0, 1.0)]
    try:
        res = minimize(neg_loglik, x0, bounds=bounds, method="L-BFGS-B")
        omega, alpha, beta = res.x
        if not res.success or alpha + beta >= 1:
            return ewma_vol(returns, annualize=annualize)
        # one-step-ahead forecast
        sigma2 = uncond_var
        for t in range(1, r.size):
            sigma2 = omega + alpha * r[t - 1] ** 2 + beta * sigma2
        sigma2_next = omega + alpha * r[-1] ** 2 + beta * sigma2
        sigma = float(np.sqrt(sigma2_next))
    except Exception:
        return ewma_vol(returns, annualize=annualize)
    return sigma * np.sqrt(TRADING_DAYS) if annualize else sigma


def yang_zhang_vol(
    ohlcv: pd.DataFrame, window: int = 30, annualize: bool = False
) -> float:
    """Yang-Zhang OHLC volatility — low-variance, gap-aware estimator."""
    df = ohlcv.tail(window + 1)
    if len(df) < 5:
        return 0.0
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    prev_c = c.shift(1)

    overnight = np.log(o / prev_c).dropna()
    open_close = np.log(c / o)
    rs = (np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o))

    n = len(overnight)
    if n < 2:
        return 0.0
    sigma_o2 = float(np.var(overnight, ddof=1))
    sigma_c2 = float(np.var(open_close.iloc[1:], ddof=1))
    sigma_rs2 = float(rs.iloc[1:].mean())

    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var = sigma_o2 + k * sigma_c2 + (1 - k) * sigma_rs2
    sigma = float(np.sqrt(max(var, 0.0)))
    return sigma * np.sqrt(TRADING_DAYS) if annualize else sigma
