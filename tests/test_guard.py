"""W2 tests: data-coverage guard must prevent phantom turnover (two-tier era)."""
import json
import pickle

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
    # quantities preserved exactly -> zero turnover
    book = runtime.state["systems"]["s1"]
    assert book["last_costs"]["turnover"] == 0
    for sym, old_w in {"AAPL": .03, "MSFT": -.02}.items():
        old_price = 100. if sym == "AAPL" else 200.
        latest_price = runtime.state["prev_prices"][sym]
        assert book["holdings_usd"][sym] / latest_price == pytest.approx(
            runtime.nav0 * old_w / old_price)
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
    book = runtime.state["systems"]["s1"]
    assert book["last_costs"]["turnover"] == 0
    for sym, old_w in {"AAPL": .03, "MSFT": -.02}.items():
        old_price = 100. if sym == "AAPL" else 200.
        latest_price = runtime.state["prev_prices"][sym]
        assert book["holdings_usd"][sym] / latest_price == pytest.approx(
            runtime.nav0 * old_w / old_price)


# --- cache-served NO-TRADE telemetry (2026-07-31 15:52/16:53/17:55/18:57) ----
def test_cache_fallback_records_fresh_coverage_and_cache_bar(runtime, monkeypatch):
    """The fresh fetch's FAILED coverage must survive the cache fallback."""
    import runtime.live as live_mod

    # cache holds a healthy-looking SPY panel whose last bar is 2026-07-30
    idx = pd.date_range(end="2026-07-30", periods=60, freq="D")
    cached_spy = pd.DataFrame({
        "open": 500.0, "high": 505.0, "low": 495.0,
        "close": np.linspace(500.0, 550.0, len(idx)), "volume": 1e6,
    }, index=idx)
    cached_vix = pd.Series([18.0, 19.0],
                           index=pd.date_range("2026-07-29", periods=2, freq="D"))
    runtime.cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(runtime.cache_path, "wb") as fh:
        pickle.dump(({"SPY": cached_spy}, {}, cached_vix), fh)

    # every fresh leg fails: no equities, no crypto, no VIX (fully offline)
    class StubEquity:
        def history(self, *a, **k):
            return {}

    class StubCrypto:
        def __init__(self, exchange=None):
            pass

        def history(self, *a, **k):
            return {}

    class StubMacro:
        def vix(self):
            return pd.Series(dtype=float)

    monkeypatch.setattr(live_mod, "make_equity_provider", lambda: StubEquity())
    monkeypatch.setattr(live_mod, "CryptoDataProvider", StubCrypto)
    monkeypatch.setattr(live_mod, "MacroDataProvider", StubMacro)

    eq, cx, vix, from_cache = runtime._fetch_heavy()
    assert from_cache is True
    assert "SPY" in eq                       # the cached panel was served
    assert "eq=0%" in runtime._heavy_fresh_desc
    assert "vix=MISSING" in runtime._heavy_fresh_desc
    assert runtime._heavy_cache_bar == "2026-07-30"


def test_cache_served_incident_names_the_fresh_failure(runtime, monkeypatch):
    """The NO-TRADE incident must name the failing fresh leg + the cache bar."""
    light = LightData(bar_date="2026-07-31",
                      prices={"AAPL": 105.0, "MSFT": 201.0},
                      eq_cov=1.0, cx_cov=1.0, spy_close=500.0)
    monkeypatch.setattr(LiveRuntime, "_fetch_light", lambda self: light)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda self, s, n: None)
    monkeypatch.setattr(LiveRuntime, "_news_raw",
                        lambda self, n: pd.Series(dtype=float))
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda self, b, r: (True, "test"))

    def fake_heavy(self):
        # fresh fetch lost the equity provider; the cache is served instead
        self._heavy_fresh_desc = "eq=3% cx=100% spy=ok vix=MISSING"
        self._heavy_cache_bar = "2026-07-30"
        return {}, {}, pd.Series(dtype=float), True
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy", fake_heavy)
    monkeypatch.setattr(LiveRuntime, "_run_systems",
                        lambda self, *a: (_ for _ in ()).throw(AssertionError))

    out = runtime.tick()
    assert out["no_trade"] is True and out["rebalanced"] is False
    # the guard holds quantities exactly -> zero turnover
    book = runtime.state["systems"]["s1"]
    assert book["last_costs"]["turnover"] == 0
    for sym, old_w in {"AAPL": .03, "MSFT": -.02}.items():
        old_price = 100. if sym == "AAPL" else 200.
        latest_price = runtime.state["prev_prices"][sym]
        assert book["holdings_usd"][sym] / latest_price == pytest.approx(
            runtime.nav0 * old_w / old_price)
    inc = runtime.state["data_incidents"][-1]
    assert inc["kind"] == "NO-TRADE tick"     # dashboard keys on this
    assert "eq=3%" in inc["detail"]
    assert "served cache" in inc["detail"]
    assert "cache bar 2026-07-30" in inc["detail"]


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


# --- E63: a name that stopped printing must leave the panel -------------------
def test_a_delisted_name_is_dropped_from_the_signal_panel():
    """Reproduces the live case measured on the box 2026-08-10.

    EA last printed 2026-08-04 and NUVL 2026-07-14, and both still carried
    weight because a 252-day panel computes a fine momentum score from stale
    prices, so every rebalance re-selected them. EA was 2.40% of S1 and
    $84.7k gross across s1+s4, marked forever at its final close of 209.70,
    unmarkable and unexitable.

    The drag was ASYMMETRIC across the registered pair - 2.55% of S1, the
    control, against 0% of S3 - which injected a measured -0.90 bps/day into
    the daily S3-S1 difference, 3% of the observed mean.
    """
    from runtime.live import MAX_HISTORY_STALENESS_DAYS, LiveRuntime

    def frame(last_day, n=300):
        idx = pd.date_range(end=last_day, periods=n, freq="D")
        return pd.DataFrame({
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": np.linspace(100.0, 110.0, n), "volume": 1e6,
        }, index=idx)

    eq = {
        "AAPL": frame("2026-08-10"),   # current
        "MSFT": frame("2026-08-07"),   # Friday, panel is Monday: alive
        "EA":   frame("2026-08-04"),   # 6 days: delisted
        "NUVL": frame("2026-07-14"),   # 27 days: long gone
        "SPY":  frame("2026-08-10"),
        "DEAD": pd.DataFrame(),        # empty frames are left alone
    }
    out = LiveRuntime._drop_stale_names(eq)

    assert "AAPL" in out and "SPY" in out
    assert "MSFT" in out, (
        f"a Friday close on a Monday panel is 3 days old and must survive; "
        f"the threshold is {MAX_HISTORY_STALENESS_DAYS} days")
    assert "EA" not in out, "EA stopped printing 6 days ago and must leave"
    assert "NUVL" not in out, "NUVL stopped printing 27 days ago and must leave"
    assert "DEAD" in out, "an empty frame is a coverage question, not a staleness one"


def test_the_staleness_filter_runs_before_the_cache_is_written(runtime, monkeypatch):
    """A stale name must not be pickled into the panel a later cache-served
    tick rebalances from. Gating after the cache write would leave the artifact
    on disk - the same shape as the provider-basis guard having to sit before
    _fetch_heavy."""
    import runtime.live as live_mod

    captured = {}
    def spy_coverage(eq, cx, vix):
        captured["eq"] = eq
        return Coverage(equity=1.0, crypto=1.0, spy_ok=True, vix_ok=True)

    # _coverage is a staticmethod; patch it as one or `self` lands in `eq`
    monkeypatch.setattr(live_mod.LiveRuntime, "_coverage",
                        staticmethod(spy_coverage))
    idx_new = pd.date_range(end="2026-08-10", periods=300, freq="D")
    idx_old = pd.date_range(end="2026-07-14", periods=300, freq="D")
    mk = lambda i: pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0,
                                 "close": 1.0, "volume": 1e6}, index=i)
    monkeypatch.setattr(live_mod, "make_equity_provider",
                        lambda: type("P", (), {
                            "history": lambda self, syms, period=None: {
                                "AAPL": mk(idx_new), "NUVL": mk(idx_old)}})())
    monkeypatch.setattr(live_mod, "CryptoDataProvider",
                        lambda **k: type("C", (), {"history": lambda self, *a, **kw: {}})())
    monkeypatch.setattr(live_mod, "MacroDataProvider",
                        lambda: type("M", (), {"vix": lambda self: pd.Series([1.0])})())
    runtime._legacy_eq = None
    runtime._fetch_heavy()

    assert "NUVL" not in captured["eq"], (
        "the stale name reached _coverage, so it also reached the cache write")
    assert "AAPL" in captured["eq"]
