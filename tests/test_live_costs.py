"""Single cost surface (E19 deferred item): live marking must charge the same
spread + square-root impact + commission stack the paper broker and backtests
use, with pessimistic fallbacks when a name lacks stored ADV/vol inputs."""
import pandas as pd
import pytest

from core.broker.costs import CostModel, CostParams
from core.config import CONFIG
from runtime.live import FALLBACK_ADV, FALLBACK_DVOL, LiveRuntime


@pytest.fixture
def rt(tmp_path, monkeypatch):
    r = LiveRuntime(state_path=str(tmp_path / "state.json"))
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    return r


def _eq_model():
    return CostModel(CostParams(**CONFIG.costs.equities))


def test_zero_turnover_costs_nothing(rt):
    w = pd.Series({"AAPL": 0.03, "MSFT": -0.02})
    assert rt._turnover_cost(w, w, 3_000_000.0) == 0.0
    empty = pd.Series(dtype=float)
    assert rt._turnover_cost(empty, empty, 3_000_000.0) == 0.0


def test_impact_charged_with_stored_inputs(rt):
    # inputs stamped at rebalance: 20d ADV and 30d daily vol per name
    rt.state["cost_inputs"] = {"AAPL": [200e6, 0.015]}
    pw = pd.Series(dtype=float)
    new_w = pd.Series({"AAPL": 0.05})
    cost = rt._turnover_cost(pw, new_w, 3_000_000.0)
    bd = _eq_model().estimate(0.05 * 3_000_000.0, adv=200e6, daily_vol=0.015)
    assert cost == pytest.approx(0.05 * (bd.slippage + bd.commission), rel=1e-9)
    # and it strictly exceeds the old spread+commission-only charge — impact
    # is no longer free on the live surface
    spread_comm = 0.05 * (CONFIG.costs.equities["half_spread_bps"]
                          + CONFIG.costs.equities["commission_bps"]) * 1e-4
    assert cost > spread_comm


def test_missing_inputs_fall_back_pessimistically(rt):
    # a name with no stamped inputs still pays impact (ADV floor + typical vol)
    pw = pd.Series(dtype=float)
    new_w = pd.Series({"ZZZZ": 0.05})
    cost = rt._turnover_cost(pw, new_w, 3_000_000.0)
    bd = _eq_model().estimate(0.05 * 3_000_000.0, adv=FALLBACK_ADV,
                              daily_vol=FALLBACK_DVOL)
    assert cost == pytest.approx(0.05 * (bd.slippage + bd.commission), rel=1e-9)
    assert bd.impact > 0                      # the fallback still charges impact


def test_crypto_symbols_use_crypto_cost_params(rt):
    rt.state["cost_inputs"] = {"BTC/USDT": [500e6, 0.03]}
    pw = pd.Series(dtype=float)
    new_w = pd.Series({"BTC/USDT": 0.05})
    cost = rt._turnover_cost(pw, new_w, 3_000_000.0)
    cm = CostModel(CostParams(**CONFIG.costs.crypto))
    bd = cm.estimate(0.05 * 3_000_000.0, adv=500e6, daily_vol=0.03)
    assert cost == pytest.approx(0.05 * (bd.slippage + bd.commission), rel=1e-9)


def test_partial_rebalance_charges_only_the_delta(rt):
    rt.state["cost_inputs"] = {"AAPL": [200e6, 0.015]}
    pw = pd.Series({"AAPL": 0.03})
    new_w = pd.Series({"AAPL": 0.05})
    cost = rt._turnover_cost(pw, new_w, 3_000_000.0)
    bd = _eq_model().estimate(0.02 * 3_000_000.0, adv=200e6, daily_vol=0.015)
    assert cost == pytest.approx(0.02 * (bd.slippage + bd.commission), rel=1e-9)
