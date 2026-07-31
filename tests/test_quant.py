"""Deterministic tests for System 1 quant modules (synthetic data, no network)."""
import numpy as np
import pandas as pd

from systems.s1_quant import signals as sig
from systems.s1_quant.covariance import ledoit_wolf_cov, portfolio_vol
from systems.s1_quant.engine import QuantEngine
from systems.s1_quant.portfolio import (
    construct_signal_portfolio,
    erc_weights,
    inverse_vol_weights,
    kelly_fraction,
    risk_contributions,
)
from systems.s1_quant.volatility import ewma_vol, garch11_vol, log_returns, yang_zhang_vol

rng = np.random.default_rng(42)


def synth_ohlcv(n=300, mu=0.0005, sigma=0.02, start=100.0):
    rets = rng.normal(mu, sigma, n)
    close = start * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, sigma / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, sigma / 2, n)))
    open_ = np.concatenate([[start], close[:-1]]) * (1 + rng.normal(0, sigma / 4, n))
    vol = rng.uniform(1e6, 5e6, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


# --- volatility ----------------------------------------------------------
def test_ewma_zero_on_constant():
    s = pd.Series(np.ones(100))
    assert ewma_vol(log_returns(s)) == 0.0


def test_yang_zhang_positive_and_reasonable():
    df = synth_ohlcv()
    yz = yang_zhang_vol(df, window=30, annualize=True)
    assert 0.1 < yz < 1.0  # synthetic ~2% daily -> ~30% annual ballpark


def test_garch_runs_positive():
    df = synth_ohlcv()
    g = garch11_vol(log_returns(df["close"]))
    assert g > 0


# --- signals -------------------------------------------------------------
def test_zscore_properties():
    z = sig.cross_sectional_zscore(pd.Series([1.0, 2, 3, 4, 5]))
    assert abs(z.mean()) < 1e-9
    assert abs(z.std(ddof=0) - 1.0) < 1e-9


def test_ou_half_life_mean_reverting():
    # AR(1) mean-reverting series should have finite, positive half-life
    n = 500
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = 0.8 * x[t - 1] + rng.normal(0, 1)
    H = sig.ou_half_life(pd.Series(x))
    assert 0 < H < 50


# --- portfolio -----------------------------------------------------------
def test_inverse_vol_weights():
    w = inverse_vol_weights(pd.Series({"A": 0.1, "B": 0.2, "C": 0.4}))
    assert abs(w.sum() - 1.0) < 1e-9
    assert w["A"] > w["B"] > w["C"]  # lower vol -> higher weight


def test_erc_equalizes_risk_contributions():
    cov = pd.DataFrame(np.diag([0.04, 0.01, 0.09]), index=["A", "B", "C"], columns=["A", "B", "C"])
    w = erc_weights(cov)
    rc = risk_contributions(w, cov)
    assert rc.std() / rc.mean() < 0.02  # contributions ~equal


def test_kelly_fraction():
    assert abs(kelly_fraction(0.10, 0.20, fraction=0.25) - 0.25 * 0.10 / 0.04) < 1e-12


def test_ledoit_wolf_symmetric_psd():
    data = pd.DataFrame(rng.normal(0, 0.02, (250, 10)))
    cov = ledoit_wolf_cov(data)
    M = cov.to_numpy()
    assert np.allclose(M, M.T, atol=1e-12)
    assert np.all(np.linalg.eigvalsh(M) > -1e-10)  # PSD


def test_construct_respects_limits():
    syms = [f"S{i}" for i in range(10)]
    scores = pd.Series(rng.normal(0, 1, 10), index=syms)
    vols = pd.Series(rng.uniform(0.2, 0.5, 10), index=syms)
    data = pd.DataFrame(rng.normal(0, 0.02, (250, 10)), columns=syms)
    cov = ledoit_wolf_cov(data)
    w = construct_signal_portfolio(scores, cov, vols, target_vol=0.10,
                                   max_position=0.05, max_gross=1.0)
    assert w.abs().max() <= 0.05 + 1e-9
    assert w.abs().sum() <= 1.0 + 1e-9


# --- engine --------------------------------------------------------------
def test_engine_end_to_end():
    histories = {f"S{i}": synth_ohlcv(start=50 + i) for i in range(12)}
    eng = QuantEngine()
    res = eng.generate(histories)
    assert res.gross <= 1.0 + 1e-9
    assert res.weights.abs().max() <= 0.05 + 1e-9
    assert len(res.weights) == 12
