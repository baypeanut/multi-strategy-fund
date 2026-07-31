"""Tests for System 3 (LLM discretionary + wrapper) and System 4 (combined)."""
import numpy as np
import pandas as pd

from systems.s3_llm.engine import S3Engine
from systems.s3_llm.pm import HeuristicPM
from systems.s3_llm.regime import compute_regime
from systems.s3_llm.wrapper import RiskWrapper
from systems.s4_combined.combine import (
    combine_target_weights,
    inverse_vol_system_weights,
    min_variance_system_weights,
)
from systems.s4_combined.engine import S4Engine

rng = np.random.default_rng(11)
UNIV = {"AAPL", "MSFT", "NVDA", "JPM"}


# --- regime --------------------------------------------------------------
def test_regime_trend():
    idx = pd.date_range("2024-01-01", periods=300, freq="D")
    up = pd.Series(np.linspace(100, 200, 300), index=idx)
    panel = pd.DataFrame({"A": up, "B": up * 1.1})
    vix = pd.Series(np.full(300, 15.0), index=idx)
    reg = compute_regime(panel, up, vix)
    assert reg.trend == "risk_on"
    assert 0.0 <= reg.risk_scale <= 1.0

    down = pd.Series(np.linspace(200, 100, 300), index=idx)
    reg2 = compute_regime(pd.DataFrame({"A": down}), down, vix)
    assert reg2.trend == "risk_off"
    assert reg2.risk_scale < 1.0


# --- wrapper (the safety core) -------------------------------------------
def make_wrapper(**kw):
    params = dict(universe=UNIV, max_position=0.05, max_gross=1.0,
                  adv_cap=0.10, max_turnover=1.0, nav=3_000_000)
    params.update(kw)
    return RiskWrapper(**params)


def test_wrapper_citation_check_drops_hallucination():
    w = make_wrapper()
    res = w.validate({"AAPL": 0.03, "FAKE": 0.04})
    assert "FAKE" not in res.weights.index
    assert any("hallucinated" in v for v in res.violations)


def test_wrapper_per_name_cap():
    w = make_wrapper()
    res = w.validate({"AAPL": 0.20})
    assert abs(res.weights["AAPL"] - 0.05) < 1e-12


def test_wrapper_gross_scaling():
    w = make_wrapper()
    res = w.validate({"AAPL": 0.05, "MSFT": 0.05, "NVDA": 0.05, "JPM": 0.05})
    assert abs(res.weights.abs().sum() - 1.0) < 1e-9 or res.weights.abs().sum() <= 1.0 + 1e-9


def test_wrapper_liquidity_clip():
    w = make_wrapper()
    # ADV 1M, cap 10%, nav 3M -> max weight 0.0333
    res = w.validate({"AAPL": 0.05}, adv={"AAPL": 1_000_000})
    assert abs(res.weights["AAPL"] - 0.10 * 1_000_000 / 3_000_000) < 1e-9


def test_wrapper_non_finite_dropped():
    w = make_wrapper()
    res = w.validate({"AAPL": float("nan"), "MSFT": float("inf"), "NVDA": 0.02})
    assert list(res.weights.index) == ["NVDA"]


def test_wrapper_turnover_throttle():
    w = make_wrapper(max_turnover=0.04)
    res = w.validate({"AAPL": 0.05, "MSFT": -0.05}, current_weights={})
    # requested turnover 0.10, throttled to 0.04
    assert res.weights.abs().sum() <= 0.04 + 1e-9
    assert any("throttle" in v for v in res.violations)


# --- heuristic PM + S3 engine --------------------------------------------
def briefing_fixture(risk_scale=1.0):
    return {
        "regime": {"risk_scale": risk_scale},
        "names": [
            {"symbol": "AAPL", "s1_quant_z": 1.5, "s2_news_z": 1.0},
            {"symbol": "MSFT", "s1_quant_z": -1.0, "s2_news_z": -0.5},
            {"symbol": "NVDA", "s1_quant_z": 0.2, "s2_news_z": 0.0},
            {"symbol": "JPM", "s1_quant_z": -0.7, "s2_news_z": -0.5},
        ],
    }


def test_heuristic_pm_within_caps_and_regime():
    pm = HeuristicPM(max_position=0.05)
    full = pm.propose(briefing_fixture(1.0))
    half = pm.propose(briefing_fixture(0.3))
    assert all(abs(v) <= 0.05 + 1e-12 for v in full.values())
    # regime scaling is the runtime's job (target-vol level), NOT the PM's
    # the PM must produce the same book regardless of risk_scale
    assert full == half
    assert full["AAPL"] > 0 and full["MSFT"] < 0


def test_s3_engine_drops_hallucinations_end_to_end():
    class BadPM:
        def propose(self, briefing):
            return {"AAPL": 0.03, "GHOST": 0.04}
    eng = S3Engine(wrapper=make_wrapper(), pm=BadPM())
    res = eng.generate(briefing_fixture())
    assert "GHOST" not in res.weights.index
    assert "AAPL" in res.weights.index


# --- S4 combine ----------------------------------------------------------
def test_inverse_vol_system_weights():
    rets = {
        "s1": pd.Series(rng.normal(0, 0.01, 300)),
        "s2": pd.Series(rng.normal(0, 0.02, 300)),
    }
    a = inverse_vol_system_weights(rets)
    assert abs(sum(a.values()) - 1.0) < 1e-9
    assert a["s1"] > a["s2"]  # lower vol -> higher weight


def test_min_variance_sums_to_one():
    rets = {f"s{i}": pd.Series(rng.normal(0, 0.01 * (i + 1), 300)) for i in range(3)}
    a = min_variance_system_weights(rets)
    assert abs(sum(a.values()) - 1.0) < 1e-9
    assert all(v >= -1e-9 for v in a.values())


def test_combine_target_weights_nets_and_caps():
    sw = {
        "s1": pd.Series({"AAPL": 0.05, "MSFT": -0.05}),
        "s2": pd.Series({"AAPL": -0.02, "NVDA": 0.04}),
    }
    alphas = {"s1": 0.5, "s2": 0.5}
    c = combine_target_weights(sw, alphas, max_gross=1.0)
    # AAPL nets: 0.5*0.05 + 0.5*(-0.02) = 0.015
    assert abs(c["AAPL"] - 0.015) < 1e-9
    assert c.abs().sum() <= 1.0 + 1e-9


def test_s4_engine_end_to_end():
    sw = {"s1": pd.Series({"AAPL": 0.04, "MSFT": -0.04}),
          "s2": pd.Series({"AAPL": -0.01, "NVDA": 0.03})}
    sr = {"s1": pd.Series(rng.normal(0, 0.01, 200)),
          "s2": pd.Series(rng.normal(0, 0.015, 200))}
    res = S4Engine(method="risk_parity").generate(sw, sr)
    assert abs(sum(res.system_alphas.values()) - 1.0) < 1e-9
    assert res.weights.abs().sum() <= 1.0 + 1e-9
