"""W3 tests: common vol normalization + paired (DM/Newey-West) test."""
import numpy as np
import pandas as pd

from backtest.metrics import paired_test
from systems.s1_quant.covariance import ledoit_wolf_cov, portfolio_vol
from systems.s1_quant.portfolio import scale_to_target_vol

rng = np.random.default_rng(3)


# --- scale_to_target_vol ---------------------------------------------------
def make_cov(n=10, daily_vol=0.02):
    data = pd.DataFrame(rng.normal(0, daily_vol, (500, n)),
                        columns=[f"S{i}" for i in range(n)])
    return ledoit_wolf_cov(data)


def test_scaling_hits_target_when_caps_loose():
    cov = make_cov()
    w = pd.Series(rng.normal(0, 1, 10), index=cov.index)
    out = scale_to_target_vol(w, cov, target_vol=0.10, max_position=1.0, max_gross=10.0)
    assert abs(portfolio_vol(out, cov) - 0.10) < 1e-6


def test_scaling_respects_caps():
    cov = make_cov()
    w = pd.Series(rng.normal(0, 1, 10), index=cov.index)
    out = scale_to_target_vol(w, cov, target_vol=0.50, max_position=0.05, max_gross=1.0)
    assert out.abs().max() <= 0.05 + 1e-12
    assert out.abs().sum() <= 1.0 + 1e-12


def test_zero_weights_pass_through():
    cov = make_cov()
    w = pd.Series(0.0, index=cov.index)
    out = scale_to_target_vol(w, cov, target_vol=0.10)
    assert out.abs().sum() == 0.0


def test_regime_scales_target_not_shape():
    # halving target vol should halve every weight (caps loose), not reshape
    cov = make_cov()
    w = pd.Series(rng.normal(0, 1, 10), index=cov.index)
    full = scale_to_target_vol(w, cov, 0.10, max_position=1.0, max_gross=10.0)
    half = scale_to_target_vol(w, cov, 0.05, max_position=1.0, max_gross=10.0)
    ratio = (half / full).dropna()
    assert np.allclose(ratio, 0.5, atol=1e-9)


# --- paired test -------------------------------------------------------------
def test_paired_equal_series_not_significant():
    base = pd.Series(rng.normal(5e-4, 0.01, 500))
    noise_a = base + rng.normal(0, 5e-4, 500)
    noise_b = base + rng.normal(0, 5e-4, 500)
    res = paired_test(noise_a, noise_b)
    assert res["p_value"] > 0.05
    assert abs(res["mean_daily_bps"]) < 5


def test_paired_detects_real_edge_in_correlated_books():
    # two highly correlated books, A has +2bps/day true edge; each book adds
    # 10bps idiosyncratic noise so diff sigma = sqrt(2)*1e-3 ~ 1.41e-3.
    # t = 2e-4/(1.41e-3/sqrt(n)) = 0.142*sqrt(n) -> ~2.8 at n=400
    n = 400
    base = pd.Series(rng.normal(5e-4, 0.01, n))
    a = base + 2e-4 + rng.normal(0, 1e-3, n)
    b = base + rng.normal(0, 1e-3, n)
    res = paired_test(a, b)
    assert res["p_value"] < 0.05
    assert res["mean_daily_bps"] > 0


def test_paired_small_n_returns_nan():
    a = pd.Series([0.01] * 5)
    b = pd.Series([0.00] * 5)
    res = paired_test(a, b)
    assert np.isnan(res["p_value"]) and res["n"] == 5


def test_paired_autocorrelation_does_not_blow_up():
    # AR(1) differences: NW must widen SE vs naive, not crash
    n = 400
    e = np.zeros(n)
    for t in range(1, n):
        e[t] = 0.5 * e[t - 1] + rng.normal(0, 1e-3)
    a = pd.Series(rng.normal(5e-4, 0.01, n))
    res = paired_test(a + e, a)
    assert np.isfinite(res["t_stat"])
