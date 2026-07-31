"""Head-quant audit fixes: information-gated rebalancing, LLM budget, clock."""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from runtime.live import LiveRuntime


@pytest.fixture
def rt(tmp_path, monkeypatch):
    r = LiveRuntime(state_path=str(tmp_path / "state.json"))
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    return r


def hist_df(n=300, price=100.0, seed=1):
    idx = pd.date_range("2025-06-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    close = price * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1e6}, index=idx)


# --- fingerprint semantics ---------------------------------------------------
def test_fingerprint_stable_under_decay(rt):
    z1 = pd.Series({"AAA": 1.03, "BBB": -0.55})
    z2 = pd.Series({"AAA": 0.98, "BBB": -0.52})   # tiny decay drift (same quantum)
    assert rt._decision_fingerprint("2026-07-09", z1) == \
        rt._decision_fingerprint("2026-07-09", z2)


def test_fingerprint_changes_on_material_news(rt):
    z1 = pd.Series({"AAA": 0.10})
    z2 = pd.Series({"AAA": 0.90})                 # crosses several quanta
    assert rt._decision_fingerprint("2026-07-09", z1) != \
        rt._decision_fingerprint("2026-07-09", z2)


def test_fingerprint_changes_on_new_bar(rt):
    z = pd.Series({"AAA": 0.5})
    assert rt._decision_fingerprint("2026-07-09", z) != \
        rt._decision_fingerprint("2026-07-10", z)


# --- gate holds weights (zero churn, zero LLM) --------------------------------
def test_gate_holds_weights_without_engines(rt, monkeypatch):
    from runtime.live import LightData
    prev = {"s1": {"AAA": 0.03}, "s2": {}, "s3": {"BBB": -0.02}, "s4": {"AAA": 0.02}}
    for k, w in prev.items():
        rt.state["systems"][k]["weights"] = w
    rt.state["prev_prices"] = {"AAA": 100.0, "BBB": 200.0}

    light = LightData(bar_date="2026-07-09",
                      prices={"AAA": 101.0, "BBB": 198.0},
                      eq_cov=1.0, cx_cov=1.0, spy_close=500.0)
    monkeypatch.setattr(LiveRuntime, "_fetch_light", lambda self: light)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda self, s, n: None)
    raw = pd.Series({"AAA": 0.5})
    monkeypatch.setattr(LiveRuntime, "_news_raw", lambda self, n: raw)

    # stamp the reference so the gate closes
    rt._stamp_rebalance_ref("2026-07-09", raw)

    called = {"engine": 0, "heavy": 0}
    from systems.s1_quant.engine import QuantEngine
    monkeypatch.setattr(QuantEngine, "generate",
                        lambda self, *a, **k: called.__setitem__("engine", 1))
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: called.__setitem__("heavy", 1))

    out = rt.tick()
    assert out["rebalanced"] is False and out["no_trade"] is False
    assert called == {"engine": 0, "heavy": 0}            # nothing heavy ran
    assert rt.state["systems"]["s1"]["weights"] == prev["s1"]   # held exactly
    # equity marked to market despite holding: AAA +1% * 3% weight
    assert rt.state["systems"]["s1"]["equity"] != rt.nav0


# --- materiality gate (E15 fix: reference-vector, not quantum-crossing) --------
def test_gate_ignores_pure_decay_drift(rt):
    rt._stamp_rebalance_ref("2026-07-09", pd.Series({"AAA": 0.60, "BBB": -0.40}))
    # an hour of decay moves scores by ~2% - far under the 0.5 shock threshold
    should, _ = rt._should_rebalance("2026-07-09",
                                     pd.Series({"AAA": 0.585, "BBB": -0.39}))
    assert should is False


def test_gate_fires_on_news_shock(rt):
    rt._stamp_rebalance_ref("2026-07-09", pd.Series({"AAA": 0.10}))
    should, reason = rt._should_rebalance("2026-07-09", pd.Series({"AAA": 0.85}))
    assert should is True and "AAA" in reason


def test_gate_fires_on_fresh_name(rt):
    rt._stamp_rebalance_ref("2026-07-09", pd.Series(dtype=float))
    should, _ = rt._should_rebalance("2026-07-09", pd.Series({"NEW": 0.7}))
    assert should is True


def test_gate_fires_on_new_bar_only_once(rt):
    rt._stamp_rebalance_ref("2026-07-09", pd.Series({"AAA": 0.3}))
    should, reason = rt._should_rebalance("2026-07-10", pd.Series({"AAA": 0.3}))
    assert should is True and "new bar" in reason
    rt._stamp_rebalance_ref("2026-07-10", pd.Series({"AAA": 0.3}))
    should, _ = rt._should_rebalance("2026-07-10", pd.Series({"AAA": 0.31}))
    assert should is False


# --- LLM daily budget ----------------------------------------------------------
def test_llm_budget_caps_and_resets(rt):
    from core.config import CONFIG
    cap = CONFIG.get("llm", {}).get("max_pm_calls_per_day", 12)
    assert rt._llm_budget_ok("pm")
    for _ in range(cap):
        rt._llm_budget_spend("pm")
    assert not rt._llm_budget_ok("pm")            # cap hit
    assert rt._llm_budget_ok("scorer")            # independent counter
    rt.state["llm_budget"]["date"] = "2000-01-01" # new day -> reset
    assert rt._llm_budget_ok("pm")


# --- clock filter -----------------------------------------------------------------
def test_paired_test_respects_clock_start(rt):
    base = 3_000_000.0
    hist = []
    for i, d in enumerate(pd.date_range("2026-06-01", periods=30, freq="D")):
        hist.append([d.isoformat(), base * (1 + 0.001 * i)])
    rt.state["equity_history"]["s3"] = hist
    rt.state["equity_history"]["s1"] = [[t, v * 0.999] for t, v in hist]

    res_all = rt._paired_s3_vs_s1()
    rt.state["clock_start"] = "2026-06-20"
    res_clock = rt._paired_s3_vs_s1()
    assert res_clock["n"] < res_all["n"]          # pre-clock days excluded
    assert res_clock["n"] == 10                   # 11 closes -> 10 returns
