"""E17b: vectorized calendar_time_daily must match the old loop, and scale."""
import time

import numpy as np
import pandas as pd

import research.primitives as P


def _synth(n_days=80, n_names=6, n_events=40, seed=3):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)
    names = [f"S{i}" for i in range(n_names)]
    px = {}
    for nm in names + ["SPY"]:
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n_days)))
        px[nm] = pd.DataFrame({"close": close, "volume": 1e6}, index=dates)
    rows = [{"ticker": rng.choice(names),
             "date": dates[rng.integers(0, n_days - 12)],
             "ar01": float(rng.normal(0, 0.03))} for _ in range(n_events)]
    tab = pd.DataFrame(rows)
    tab.attrs["prices"] = px
    return tab, px, dates


def _brute_force(tab, px, dates, entry_lag, exit_lag, cost_per_active):
    """Literal reimplementation of the ORIGINAL O(days x events) loop."""
    rets = {s: px[s]["close"].pct_change() for s in px if s != "SPY"}
    daily = []
    for d in dates:
        al, ash = [], []
        for _, ev in tab.iterrows():
            delta = (d - ev["date"]).days
            if entry_lag <= delta <= exit_lag and ev["ticker"] in rets:
                (al if ev["ar01"] > 0 else ash).append(ev["ticker"])
        n_pos = len(al) + len(ash)
        if n_pos < 4:
            daily.append(0.0)
            continue
        w = 1.0 / n_pos
        r = sum(w * float(rets[s].get(d, 0) or 0) for s in al) \
            - sum(w * float(rets[s].get(d, 0) or 0) for s in ash)
        daily.append(r - cost_per_active)
    return pd.Series(daily, index=dates)


def test_calendar_time_matches_loop_exactly():
    tab, px, dates = _synth()
    entry_lag, exit_lag = 2, 10
    cm = P._cost_model()
    turn = 2.0 / max(exit_lag - entry_lag, 1)
    leg = cm.estimate(50_000, adv=5e8, daily_vol=0.02)
    cost_per_active = turn * (leg.slippage + leg.commission)

    ref = _brute_force(tab, px, dates, entry_lag, exit_lag, cost_per_active)
    vec, _ = P.calendar_time_daily(tab, px, entry_lag, exit_lag, cm)  # the real path
    assert np.allclose(ref.values, vec.values, atol=1e-12)


def test_calendar_time_is_fast_at_scale():
    # 500 days x 17k events: old loop ~8.5M scalar ops (2h timeout); the
    # vectorized primitive must return in seconds.
    tab, px, dates = _synth(n_days=500, n_names=200, n_events=17000, seed=9)
    t0 = time.time()
    ser, units = P.calendar_time_daily(tab, px, 2, 10, P._cost_model())
    assert time.time() - t0 < 8.0
    assert len(ser) == len(dates) and units.max() >= 4
