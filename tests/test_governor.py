"""Tests for the RiskGovernor circuit breakers."""
import numpy as np
import pandas as pd

from core.risk.governor import RiskGovernor, resample_daily

gov = RiskGovernor(max_gross=1.0, dd_gate_1=-0.10, dd_gate_2=-0.15,
                   daily_loss_kill=-0.03, var_limit_95=0.02)


def test_gate2_halts_to_cash():
    w = pd.Series({"A": 0.05, "B": -0.05})
    eq = pd.Series([100.0, 84.0])  # -16% drawdown
    d = gov.assess(w, equity_curve=eq)
    assert d.halted and d.weights.abs().sum() == 0.0


def test_daily_kill_halts():
    w = pd.Series({"A": 0.05})
    eq = pd.Series([100.0, 100.0, 96.0])  # -4% on the day, dd -4% (> gate2)
    d = gov.assess(w, equity_curve=eq)
    assert d.halted


def test_gate1_halves_sizing():
    w = pd.Series({"A": 0.04, "B": -0.04})
    eq = pd.Series([100.0, 95.0, 92.0, 91.0, 89.5])  # dd -10.5%, daily -1.6%
    d = gov.assess(w, equity_curve=eq)
    assert not d.halted and abs(d.risk_scale - 0.5) < 1e-9
    assert abs(d.weights.abs().sum() - 0.04) < 1e-9  # halved from 0.08


def test_var_limit_scales_down():
    w = pd.Series({"A": 1.0})
    cov = pd.DataFrame([[0.02**2]], index=["A"], columns=["A"])  # daily vol 2%
    d = gov.assess(w, equity_curve=None, cov_daily=cov)
    # var95 = 1.645*0.02 = 0.0329 > 0.02 -> scale ~0.608
    assert d.weights["A"] < 1.0
    assert abs(d.weights["A"] - 0.02 / (1.645 * 0.02)) < 1e-6


def test_gross_cap():
    w = pd.Series({"A": 0.6, "B": -0.6})
    d = gov.assess(w)
    assert abs(d.weights.abs().sum() - 1.0) < 1e-9


def test_net_cap_scales_directional_book():
    g = RiskGovernor(max_gross=2.0, max_net=0.5)
    w = pd.Series({"A": 0.6, "B": 0.4})          # net +1.0 long-only book
    d = g.assess(w)
    assert abs(d.weights.sum() - 0.5) < 1e-9      # scaled to the net cap
    assert any("net" in a for a in d.actions)


def test_net_cap_ignores_neutral_book():
    g = RiskGovernor(max_gross=2.0, max_net=0.5)
    w = pd.Series({"A": 0.6, "B": -0.6})
    d = g.assess(w)
    assert (d.weights == w).all()


def test_no_breach_passes_through():
    w = pd.Series({"A": 0.03, "B": -0.03})
    eq = pd.Series([100.0, 101.0, 102.0])
    d = gov.assess(w, equity_curve=eq)
    assert not d.halted and d.risk_scale == 1.0
    assert abs(d.weights.abs().sum() - 0.06) < 1e-9


# --- W1 regression: daily-resample semantics on hourly marks --------------
def hourly_curve(day_values):
    """Build an hourly-marked curve: day_values = list of (day_start, day_end).

    Each day gets 24 hourly marks linearly interpolating start->end.
    """
    vals, stamps = [], []
    t0 = pd.Timestamp("2026-06-01 00:00:00")
    for d, (a, b) in enumerate(day_values):
        for h in range(24):
            vals.append(a + (b - a) * h / 23)
            stamps.append(t0 + pd.Timedelta(days=d, hours=h))
    return pd.Series(vals, index=pd.DatetimeIndex(stamps))


def test_slow_bleed_over_24_ticks_triggers_daily_kill():
    # -4% spread across a full day of hourly marks: every tick-over-tick move
    # is tiny (~0.17%), the OLD tick-based rule never fired. The daily rule must.
    eq = hourly_curve([(100.0, 100.0), (100.0, 96.0)])
    d = gov.assess(pd.Series({"A": 0.05}), equity_curve=eq)
    assert d.halted
    assert any("day loss" in a for a in d.actions)


def test_single_hour_crash_still_triggers():
    # flat, then -3.5% in the final hour: vs yesterday's close it's -3.5%
    eq = hourly_curve([(100.0, 100.0), (100.0, 100.0)])
    eq.iloc[-1] = 96.5
    d = gov.assess(pd.Series({"A": 0.05}), equity_curve=eq)
    assert d.halted


def test_weekend_flat_marks_do_not_trigger():
    # gentle -1%/day drift with hourly marks incl. flat weekend: no kill
    eq = hourly_curve([(100.0, 100.0), (100.0, 100.0), (100.0, 99.0)])
    d = gov.assess(pd.Series({"A": 0.05}), equity_curve=eq)
    assert not d.halted


def test_resample_daily_collapses_hourly():
    eq = hourly_curve([(100.0, 101.0), (101.0, 102.0), (102.0, 103.0)])
    daily = resample_daily(eq)
    assert len(daily) == 3
    assert abs(daily.iloc[-1] - 103.0) < 1e-9  # day close = last mark


def test_non_datetime_series_backward_compatible():
    # plain-int-indexed series (backtests/tests) behave as before
    eq = pd.Series([100.0, 100.0, 96.0])
    d = gov.assess(pd.Series({"A": 0.05}), equity_curve=eq)
    assert d.halted
