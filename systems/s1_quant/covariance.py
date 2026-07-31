"""Covariance estimation with Ledoit-Wolf shrinkage.

Sample covariance is noisy when N (names) is large relative to T (observations).
We shrink toward a constant-correlation target:

    Sigma_hat = delta * F + (1 - delta) * S

where S is the sample covariance, F the constant-correlation target, and delta
the optimal shrinkage intensity (Ledoit & Wolf 2004, "Honey, I Shrunk the Sample
Covariance Matrix", constant-correlation case).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sample_cov(returns: pd.DataFrame) -> pd.DataFrame:
    return returns.cov()


def ledoit_wolf_cov(returns: pd.DataFrame) -> pd.DataFrame:
    """Ledoit-Wolf constant-correlation shrinkage covariance (daily)."""
    X = returns.dropna(how="any")
    cols = X.columns
    Y = (X - X.mean()).to_numpy()             # centered, t x n
    t, n = Y.shape
    if t < 2 or n < 2:
        return returns.cov()

    S = (Y.T @ Y) / t                         # sample cov (MLE)
    var = np.diag(S)
    std = np.sqrt(var)
    outer_std = np.outer(std, std)
    corr = S / outer_std
    r_bar = (corr.sum() - n) / (n * (n - 1))  # mean off-diagonal correlation

    F = r_bar * outer_std                     # constant-correlation target
    np.fill_diagonal(F, var)

    # pi: sum of asymptotic variances of sample-cov entries
    Y2 = Y**2
    pi_mat = (Y2.T @ Y2) / t - S**2
    pi_hat = pi_mat.sum()

    # rho: covariance between target and sample-cov estimation error.
    # Vectorized (500 names would need 250k python-loop iterations otherwise).
    # For centered Y with S = YᵀY/t:
    #   theta_ii[i,j] = E[(Yi²−vari)(YiYj−Sij)] = E[Yi³Yj] − vari·Sij
    # and the pair sum Σ_{i≠j} √(varj/vari)·theta_ii + √(vari/varj)·theta_jj
    # collapses (by i↔j relabeling) to 2·Σ_{i≠j} (stdj/stdi)·theta_ii[i,j],
    # matching the loop's (r_bar/2)·(...) with both terms.
    rho_hat = np.trace(pi_mat)                # diagonal part
    theta_ii = (Y**3).T @ Y / t - var[:, None] * S
    ratio = std[None, :] / std[:, None]       # ratio[i,j] = std_j / std_i
    off = ratio * theta_ii
    np.fill_diagonal(off, 0.0)
    rho_hat += r_bar * off.sum()

    gamma_hat = float(np.sum((F - S) ** 2))   # misspecification of target
    if gamma_hat <= 0:
        delta = 0.0
    else:
        kappa = (pi_hat - rho_hat) / gamma_hat
        delta = max(0.0, min(1.0, kappa / t))

    Sigma = delta * F + (1.0 - delta) * S
    return pd.DataFrame(Sigma, index=cols, columns=cols)


def portfolio_vol(weights: pd.Series, cov: pd.DataFrame, annualize: bool = True) -> float:
    w = weights.reindex(cov.index).fillna(0.0).to_numpy()
    daily = float(np.sqrt(max(w @ cov.to_numpy() @ w, 0.0)))
    return daily * np.sqrt(252) if annualize else daily
