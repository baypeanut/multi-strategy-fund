"""S5 event sleeve: position logic must match the confirmed calendar-time spec,
and the feed must derive reaction signs / prune expired filings correctly."""
import numpy as np
import pandas as pd
import pytest

from systems.s5_event.engine import S5EventEngine
from systems.s5_event import feed as s5feed


def _diag_cov(names, var=0.0004):
    return pd.DataFrame(np.eye(len(names)) * var, index=names, columns=names)


def test_active_window_matches_entry_exit_lag():
    eng = S5EventEngine(entry_lag=2, exit_lag=10, min_units=1)
    names = ["AAA"]
    cov = _diag_cov(names)
    filing = "2026-07-01"
    # before entry_lag -> flat
    r = eng.generate([("AAA", filing, 1.0)], today="2026-07-02", cov=cov)
    assert r.n_units == 0 and r.weights.empty
    # inside window -> active
    r = eng.generate([("AAA", filing, 1.0)], today="2026-07-05", cov=cov)
    assert r.n_units == 1 and r.weights["AAA"] > 0
    # exactly at exit_lag boundary -> still active
    r = eng.generate([("AAA", filing, 1.0)], today="2026-07-11", cov=cov)
    assert r.n_units == 1
    # past exit_lag -> flat
    r = eng.generate([("AAA", filing, 1.0)], today="2026-07-12", cov=cov)
    assert r.n_units == 0 and r.weights.empty


def test_breadth_guard_flattens_below_min_units():
    eng = S5EventEngine(entry_lag=2, exit_lag=10, min_units=4)
    names = ["AAA", "BBB", "CCC"]
    cov = _diag_cov(names)
    evs = [("AAA", "2026-07-01", 1.0), ("BBB", "2026-07-01", -1.0),
           ("CCC", "2026-07-01", 1.0)]  # only 3 units < 4
    r = eng.generate(evs, today="2026-07-05", cov=cov)
    assert r.n_units == 3 and r.weights.empty
    # add a 4th -> book turns on
    evs.append(("AAA", "2026-07-02", 1.0))
    r = eng.generate(evs, today="2026-07-05", cov=cov)
    assert r.n_units == 4 and not r.weights.empty


def test_sign_and_overlap_stacking():
    # no per-name cap so the raw +2 vs -1 stacking is visible post vol-norm
    eng = S5EventEngine(entry_lag=2, exit_lag=10, min_units=1,
                        max_position=1.0, max_gross=10.0)
    names = ["AAA", "BBB"]
    cov = _diag_cov(names)
    # AAA has two positive-reaction events (stack to +2), BBB one negative
    evs = [("AAA", "2026-07-01", 1.0), ("AAA", "2026-07-02", 1.0),
           ("BBB", "2026-07-01", -1.0)]
    r = eng.generate(evs, today="2026-07-05", cov=cov)
    # AAA long, BBB short, AAA twice the raw weight of BBB
    assert r.weights["AAA"] > 0 and r.weights["BBB"] < 0
    assert abs(r.weights["AAA"]) == pytest.approx(2 * abs(r.weights["BBB"]), rel=1e-6)
    assert r.n_units == 3


def test_opposing_events_net_out():
    eng = S5EventEngine(entry_lag=2, exit_lag=10, min_units=1)
    names = ["AAA", "BBB"]
    cov = _diag_cov(names)
    # AAA gets +1 and -1 -> nets to flat; BBB carries the book
    evs = [("AAA", "2026-07-01", 1.0), ("AAA", "2026-07-02", -1.0),
           ("BBB", "2026-07-01", 1.0), ("CCC", "2026-07-01", 1.0)]
    cov = _diag_cov(["AAA", "BBB", "CCC"])
    r = eng.generate(evs, today="2026-07-05", cov=cov)
    assert "AAA" not in r.weights            # netted out, dropped
    assert r.n_units == 4                    # units still counted for breadth


def test_vol_normalization_respects_caps():
    eng = S5EventEngine(entry_lag=2, exit_lag=10, min_units=1,
                        target_vol=0.10, max_position=0.05, max_gross=1.0)
    names = [f"N{i}" for i in range(6)]
    cov = _diag_cov(names, var=0.04)   # high vol -> scaling pulls weights down
    evs = [(n, "2026-07-01", 1.0 if i % 2 else -1.0) for i, n in enumerate(names)]
    r = eng.generate(evs, today="2026-07-05", cov=cov)
    assert r.weights.abs().max() <= 0.05 + 1e-9
    assert r.weights.abs().sum() <= 1.0 + 1e-9


# ------------------------------------------------------------------- feed ----
def _price_panel_with_reaction(ticker, filing_date, up=True):
    """Build a price panel where `ticker` jumps up/down the day after filing.
    SPY carries real variation and there is >250 bdays of history so the
    market-model estimation window is well-posed."""
    idx = pd.bdate_range("2025-01-01", "2026-07-15")
    rng = np.random.default_rng(0)
    spy_ret = rng.normal(0.0003, 0.008, len(idx))
    spy = pd.Series(100.0 * (1 + spy_ret).cumprod(), index=idx)
    fd = pd.Timestamp(filing_date)
    pos = int(idx.searchsorted(fd))
    # asset tracks SPY (beta 1) with its own noise, then a clean day-after jump
    a_ret = spy_ret + rng.normal(0.0, 0.004, len(idx))
    move = 0.08 if up else -0.08
    if pos + 1 < len(idx):
        a_ret[pos + 1] += move                     # the reaction, day after filing
    px = pd.Series(100.0 * (1 + a_ret).cumprod(), index=idx)
    panel = {ticker: pd.DataFrame({"close": px}),
             "SPY": pd.DataFrame({"close": spy})}
    return panel


def test_ar01_sign_reads_reaction_direction():
    panel = _price_panel_with_reaction("AAA", "2026-07-01", up=True)
    spy_ret = panel["SPY"]["close"].pct_change().dropna()
    assert s5feed.ar01_sign("AAA", "2026-07-01", panel, spy_ret) == 1.0
    panel = _price_panel_with_reaction("AAA", "2026-07-01", up=False)
    spy_ret = panel["SPY"]["close"].pct_change().dropna()
    assert s5feed.ar01_sign("AAA", "2026-07-01", panel, spy_ret) == -1.0


def test_ar01_sign_none_when_no_day_after():
    # filing on the very last bar -> no day-after return -> not computable yet
    panel = _price_panel_with_reaction("AAA", "2026-07-15", up=True)
    spy_ret = panel["SPY"]["close"].pct_change().dropna()
    assert s5feed.ar01_sign("AAA", "2026-07-15", panel, spy_ret) is None


def test_refresh_prunes_expired_and_polls_once_per_day():
    calls = {"n": 0}

    def fake_fetch(universe, start, end):
        calls["n"] += 1
        return pd.DataFrame([{"ticker": "AAA", "date": pd.Timestamp("2026-07-10"),
                              "acc": "acc1"}])

    today = pd.Timestamp("2026-07-12")
    cached = [["OLD", "2026-06-01", "accOld"]]   # far outside retain horizon
    rows, fetch_date = s5feed.refresh_raw_filings(
        cached, ["AAA"], today, exit_lag=10, last_fetch=None, fetch_fn=fake_fetch)
    assert calls["n"] == 1
    assert ["OLD", "2026-06-01", "accOld"] not in rows      # pruned
    assert any(r[0] == "AAA" for r in rows)                 # fetched
    assert fetch_date == "2026-07-12"

    # same day again -> no second EDGAR poll, dedup holds
    rows2, _ = s5feed.refresh_raw_filings(
        rows, ["AAA"], today, exit_lag=10, last_fetch=fetch_date, fetch_fn=fake_fetch)
    assert calls["n"] == 1
    assert len([r for r in rows2 if r[0] == "AAA"]) == 1


# --------------------------------------------------- runtime integration ----
def test_runtime_untouched_when_s5_disabled(tmp_path, monkeypatch):
    """The four-book race must be byte-for-byte unchanged when S5 is off:
    no s5 key appears in state, active_systems is exactly the four books."""
    from runtime.live import LiveRuntime, SYSTEMS
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "s5_enabled", lambda: False)
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    assert rt._active_systems() == SYSTEMS
    rt._ensure_s5_state()
    assert "s5" not in rt.state["systems"]


def test_runtime_marks_s5_when_enabled(tmp_path, monkeypatch):
    """When enabled, S5 gets its own equity slot and marks to market like any
    other book, without disturbing the four-book race."""
    from runtime.live import LiveRuntime, LightData, SYSTEMS
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "s5_enabled", lambda: True)
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    monkeypatch.setattr(LiveRuntime, "_fetch_light",
        lambda self: LightData(bar_date="2026-07-15",
                               prices={"AAPL": 101.0, "MSFT": 201.0},
                               eq_cov=1.0, cx_cov=1.0, spy_close=500.0))
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_should_rebalance", lambda *a, **k: (False, ""))

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    assert rt._active_systems() == SYSTEMS + ["s5"]
    for s in rt._active_systems():
        rt.state["systems"].setdefault(s, {"equity": rt.nav0, "weights": {}})
        rt.state["systems"][s]["weights"] = {"AAPL": 0.03}
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}

    rt.tick()
    # s5 marked and recorded, and AAPL +1% with weight 0.03 lifted its equity
    assert "s5" in rt.state["systems"]
    assert rt.state["systems"]["s5"]["equity"] > rt.nav0
    assert len(rt.state["equity_history"]["s5"]) == 1
