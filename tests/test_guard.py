"""W2 tests: data-coverage guard must prevent phantom turnover (two-tier era)."""
import json

import numpy as np
import pandas as pd
import pytest

from runtime.live import (Coverage, LightData, LiveRuntime,
                          MIN_CRYPTO_COVERAGE, MIN_EQUITY_COVERAGE)


def make_df(n=300, price=100.0):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": price, "high": price * 1.01, "low": price * 0.99,
        "close": np.linspace(price, price * 1.1, n), "volume": 1e6,
    }, index=idx)


# --- Coverage logic --------------------------------------------------------
def test_coverage_thresholds():
    ok = Coverage(equity=0.95, crypto=0.80, spy_ok=True, vix_ok=True)
    assert ok.ok
    assert not Coverage(equity=0.85, crypto=1.0, spy_ok=True, vix_ok=True).ok
    assert not Coverage(equity=1.0, crypto=0.5, spy_ok=True, vix_ok=True).ok
    assert not Coverage(equity=1.0, crypto=1.0, spy_ok=False, vix_ok=True).ok
    assert not Coverage(equity=1.0, crypto=1.0, spy_ok=True, vix_ok=False).ok
    assert MIN_EQUITY_COVERAGE == 0.90 and MIN_CRYPTO_COVERAGE == 0.75  # pre-registered


def test_light_data_ok_semantics():
    assert LightData("2026-07-09", {"A": 1.0}, eq_cov=0.95, cx_cov=0.8).ok
    assert not LightData("", {}, eq_cov=1.0, cx_cov=1.0).ok          # no bar date
    assert not LightData("2026-07-09", {}, eq_cov=0.5, cx_cov=1.0).ok


# --- no-trade ticks ----------------------------------------------------------
@pytest.fixture
def runtime(tmp_path, monkeypatch):
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    rt.cache_path = tmp_path / "cache" / "hist.pkl"
    rt.state["systems"]["s1"]["weights"] = {"AAPL": 0.03, "MSFT": -0.02}
    rt.state["systems"]["s4"]["weights"] = {"AAPL": 0.02}
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    return rt


def degraded_light(self):
    # grouped call failed for most names -> coverage under the floor
    return LightData(bar_date="2026-07-09", prices={"AAPL": 110.0},
                     eq_cov=0.02, cx_cov=0.0)


def test_degraded_light_causes_no_trade(runtime, monkeypatch):
    monkeypatch.setattr(LiveRuntime, "_fetch_light", degraded_light)

    def boom(self, *a, **k):
        raise AssertionError("must not run on degraded tick")
    monkeypatch.setattr(LiveRuntime, "_run_systems", boom)
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy", boom)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", boom)

    out = runtime.tick()
    assert out["no_trade"] is True and out["rebalanced"] is False
    # weights preserved exactly -> zero turnover
    assert runtime.state["systems"]["s1"]["weights"] == {"AAPL": 0.03, "MSFT": -0.02}
    assert runtime.state["data_incidents"][-1]["kind"] == "NO-TRADE tick"


def test_no_trade_still_marks_equity(runtime, monkeypatch):
    monkeypatch.setattr(LiveRuntime, "_fetch_light", degraded_light)
    monkeypatch.setattr(LiveRuntime, "_run_systems",
                        lambda self, *a: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(LiveRuntime, "_ingest_news",
                        lambda self, *a: (_ for _ in ()).throw(AssertionError))
    eq_before = runtime.state["systems"]["s1"]["equity"]
    runtime.tick()
    # AAPL 100 -> 110; s1 holds +3% AAPL -> PnL must be realized
    assert runtime.state["systems"]["s1"]["equity"] != eq_before


def test_missing_symbol_price_reference_survives(runtime, monkeypatch):
    monkeypatch.setattr(LiveRuntime, "_fetch_light", degraded_light)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda self, *a: None)
    runtime.tick()
    # MSFT missing from this tick's prices, but its reference must persist
    assert runtime.state["prev_prices"].get("MSFT") == 200.0


def test_heavy_degradation_blocks_rebalance(runtime, monkeypatch):
    light = LightData(bar_date="2026-07-09",
                      prices={"AAPL": 105.0, "MSFT": 201.0},
                      eq_cov=1.0, cx_cov=1.0, spy_close=500.0)
    monkeypatch.setattr(LiveRuntime, "_fetch_light", lambda self: light)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda self, s, n: None)
    monkeypatch.setattr(LiveRuntime, "_news_raw",
                        lambda self, n: pd.Series(dtype=float))
    # fingerprint differs -> heavy path triggers, but heavy fetch is degraded
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: ({}, {}, pd.Series(dtype=float), False))
    monkeypatch.setattr(LiveRuntime, "_run_systems",
                        lambda self, *a: (_ for _ in ()).throw(AssertionError))
    out = runtime.tick()
    assert out["no_trade"] is True and out["rebalanced"] is False
    assert runtime.state["systems"]["s1"]["weights"] == {"AAPL": 0.03, "MSFT": -0.02}


def test_schema_v2_migration(tmp_path):
    old = {
        "nav0": 3e6,
        "systems": {s: {"equity": 3e6, "weights": {}} for s in ["s1", "s2", "s3", "s4"]},
        "prev_prices": {}, "equity_history": {s: [] for s in ["s1", "s2", "s3", "s4"]},
        "ticks": 5, "last_tick": None, "last_actions": [], "regime": {},
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(old))
    rt = LiveRuntime(state_path=str(p))
    assert rt.state["schema"] == 2
    assert rt.state["data_incidents"] == []
    assert rt.state["ticks"] == 5  # history preserved
