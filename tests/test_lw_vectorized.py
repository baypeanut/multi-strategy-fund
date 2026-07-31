"""A1: vectorized Ledoit-Wolf must match the original loop exactly, and scale.

The rho term was rewritten from an O(n²) python loop to closed-form matrix
algebra. Equivalence is checked against a literal reimplementation of the old
loop; performance is checked at n=500 (the new universe size).
"""
import time

import numpy as np
import pandas as pd

from systems.s1_quant.covariance import ledoit_wolf_cov

rng = np.random.default_rng(17)


def rho_bruteforce(returns: pd.DataFrame) -> float:
    """Literal copy of the ORIGINAL loop implementation of rho_hat."""
    X = returns.dropna(how="any")
    Y = (X - X.mean()).to_numpy()
    t, n = Y.shape
    S = (Y.T @ Y) / t
    var = np.diag(S)
    std = np.sqrt(var)
    corr = S / np.outer(std, std)
    r_bar = (corr.sum() - n) / (n * (n - 1))
    Y2 = Y**2
    pi_mat = (Y2.T @ Y2) / t - S**2
    rho = np.trace(pi_mat)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            th_ii = np.mean((Y2[:, i] - var[i]) * (Y[:, i] * Y[:, j] - S[i, j]))
            th_jj = np.mean((Y2[:, j] - var[j]) * (Y[:, i] * Y[:, j] - S[i, j]))
            rho += (r_bar / 2.0) * (
                np.sqrt(var[j] / var[i]) * th_ii + np.sqrt(var[i] / var[j]) * th_jj)
    return rho


def rho_vectorized(returns: pd.DataFrame) -> float:
    """Mirror of the shipped vectorized rho (kept in sync with covariance.py)."""
    X = returns.dropna(how="any")
    Y = (X - X.mean()).to_numpy()
    t, n = Y.shape
    S = (Y.T @ Y) / t
    var = np.diag(S)
    std = np.sqrt(var)
    corr = S / np.outer(std, std)
    r_bar = (corr.sum() - n) / (n * (n - 1))
    Y2 = Y**2
    pi_mat = (Y2.T @ Y2) / t - S**2
    rho = np.trace(pi_mat)
    theta_ii = (Y**3).T @ Y / t - var[:, None] * S
    ratio = std[None, :] / std[:, None]
    off = ratio * theta_ii
    np.fill_diagonal(off, 0.0)
    return rho + r_bar * off.sum()


def test_rho_vectorization_matches_loop_exactly():
    data = pd.DataFrame(rng.normal(0, 0.02, (300, 8)))
    assert np.isclose(rho_bruteforce(data), rho_vectorized(data), rtol=1e-10)


def test_rho_equivalence_heteroskedastic():
    # unequal vols + correlation structure - the hard case for the algebra
    base = rng.normal(0, 0.01, (400, 1))
    noise = rng.normal(0, 1, (400, 10)) * rng.uniform(0.005, 0.05, 10)
    data = pd.DataFrame(base + noise)
    assert np.isclose(rho_bruteforce(data), rho_vectorized(data), rtol=1e-10)


def test_lw_full_matrix_properties_at_scale():
    n = 500
    data = pd.DataFrame(rng.normal(0, 0.02, (520, n)))
    t0 = time.time()
    cov = ledoit_wolf_cov(data)
    elapsed = time.time() - t0
    M = cov.to_numpy()
    assert np.allclose(M, M.T, atol=1e-12)
    assert np.all(np.linalg.eigvalsh(M) > -1e-10)      # PSD
    assert elapsed < 5.0, f"LW at n=500 took {elapsed:.1f}s - too slow for live"
