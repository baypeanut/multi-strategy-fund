"""Tests for backtest metrics and engine (synthetic, deterministic)."""
import numpy as np
import pandas as pd

from backtest.metrics import (
    annualized_vol,
    cscv_pbo,
    deflated_sharpe_ratio,
    max_drawdown,
    probabilistic_sharpe_ratio,
    sharpe_ratio,
    t_statistic,
)

rng = np.random.default_rng(7)


def test_sharpe_of_constant_positive():
    r = pd.Series([0.001] * 252)
    # zero vol -> guarded to 0
    assert sharpe_ratio(r) == 0.0


def test_sharpe_sign_and_scale():
    r = pd.Series(rng.normal(0.0008, 0.01, 2000))
    sr = sharpe_ratio(r)
    assert sr > 0
    # annualized sharpe roughly mean/std*sqrt(252)
    expected = r.mean() / r.std(ddof=1) * np.sqrt(252)
    assert abs(sr - expected) < 1e-9


def test_max_drawdown_monotonic():
    eq = pd.Series(np.cumprod(1 + np.full(100, 0.001)))
    assert max_drawdown(eq) == 0.0
    eq2 = pd.Series([1.0, 1.2, 0.9, 1.1])
    assert abs(max_drawdown(eq2) - (0.9 / 1.2 - 1)) < 1e-12


def test_psr_and_dsr_bounds():
    r = pd.Series(rng.normal(0.0005, 0.01, 1000))
    psr = probabilistic_sharpe_ratio(r)
    dsr = deflated_sharpe_ratio(r, n_trials=50)
    assert 0.0 <= psr <= 1.0
    assert 0.0 <= dsr <= 1.0
    # more trials -> harder bar -> DSR <= PSR
    assert dsr <= psr + 1e-9


def test_dsr_penalizes_trials():
    r = pd.Series(rng.normal(0.0006, 0.01, 1500))
    few = deflated_sharpe_ratio(r, n_trials=1)
    many = deflated_sharpe_ratio(r, n_trials=1000)
    assert many <= few + 1e-9


def test_t_statistic():
    r = pd.Series(rng.normal(0.001, 0.01, 500))
    t = t_statistic(r)
    assert abs(t - sharpe_ratio(r, annualize=False) * np.sqrt(500)) < 1e-9


def test_cscv_pbo_random_is_around_half():
    # pure noise configs -> PBO near 0.5
    M = pd.DataFrame(rng.normal(0, 0.01, (240, 8)))
    pbo = cscv_pbo(M, s=8)
    assert 0.2 < pbo < 0.8


def test_cscv_pbo_dominant_config_low():
    # one config with persistent edge -> low PBO
    base = rng.normal(0, 0.01, (240, 7))
    winner = rng.normal(0.003, 0.01, (240, 1))
    M = pd.DataFrame(np.hstack([winner, base]))
    pbo = cscv_pbo(M, s=8)
    assert pbo < 0.3
