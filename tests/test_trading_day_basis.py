"""E48e: the pre-registered rule counts TRADING days, and this counted calendar days.

Measured on the live book, 2026-08-01: the window since clock_start held 21
rows, 6 of them Saturdays and Sundays. On a weekend the 80% equity sleeve
cannot move, so the only thing separating S3 from S1 is the crypto sleeve
repricing. Those are not days on which the two books can meaningfully disagree.

Two consequences, both bad:

  - the gate. "No superiority claim before 60 trading days" would have opened
    after roughly 43 actual trading days, because weekends were padding n.
  - the statistic. Newey-West lag selection is n**(1/3) and the variance is
    estimated off the difference series, so injecting ~29% low-information
    rows changes the standard error, not just the count.

Filtering happens BEFORE differencing on purpose. Monday's return then spans
Friday's close and carries the whole weekend move exactly once, which is what a
daily equity return series is. Filtering after would drop the weekend move
entirely and understate variance.

Exchange holidays are a known residual: roughly nine a year, on which both
books hold the same flat equity sleeve. Called out rather than silently
absorbed.
"""
import pandas as pd
import pytest

from runtime.live import _trading_days_only


def _series(dates):
    return pd.Series({d: 100.0 + i for i, d in enumerate(dates)}, dtype=float)


def test_weekends_are_dropped():
    s = _series(["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13"])
    kept = list(_trading_days_only(s).index)
    assert kept == ["2026-07-10", "2026-07-13"], "Sat and Sun must not count"


def test_weekdays_all_survive():
    week = ["2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17"]
    assert list(_trading_days_only(_series(week)).index) == week


def test_an_empty_series_is_handled():
    assert _trading_days_only(pd.Series(dtype=float)).empty


def test_the_weekend_move_is_kept_once_not_lost():
    """The reason filtering precedes differencing. Friday 100 -> Monday 110 is
    one +10% observation, not zero and not two."""
    s = pd.Series({"2026-07-17": 100.0,   # Fri
                   "2026-07-18": 104.0,   # Sat, crypto only
                   "2026-07-19": 107.0,   # Sun, crypto only
                   "2026-07-20": 110.0},  # Mon
                  dtype=float)
    r = _trading_days_only(s).pct_change().dropna()
    assert len(r) == 1
    assert r.iloc[0] == pytest.approx(0.10), "the weekend move must survive"


def test_the_live_window_shrinks_to_trading_days():
    """The exact shape of the observed defect: 21 calendar rows, 15 trading."""
    days = pd.bdate_range("2026-07-13", periods=15).strftime("%Y-%m-%d").tolist()
    weekend = ["2026-07-18", "2026-07-19", "2026-07-25", "2026-07-26",
               "2026-08-01", "2026-07-12"]
    s = _series(sorted(days + weekend))
    assert len(s) == 21
    assert len(_trading_days_only(s)) == 15


def test_both_readouts_use_the_same_day_basis():
    """The headline test and the common-universe diagnostic must not drift
    apart on what a day is, or the diagnostic stops diagnosing the decision."""
    import inspect

    from runtime.live import LiveRuntime
    for fn in (LiveRuntime._paired_s3_vs_s1, LiveRuntime._paired_common):
        assert "_trading_days_only" in inspect.getsource(fn), (
            f"{fn.__name__} is back on a calendar-day basis")
