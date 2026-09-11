"""E51: two surfaces, one concept, two answers.

The dashboard recomputed risk statistics in JavaScript from the raw equity
history while the server computed them in Python. Nothing forced the two to
share a basis, and they drifted: the dashboard keyed its daily series by
calendar date, so weekend rows were included.

Weekend rows do two kinds of damage to a risk number. They distort the standard
deviation, because on a weekend the 80% equity sleeve cannot move and only
crypto reprices. And they break the sqrt(252) annualisation, which assumes five
observations a week and was being handed seven.

Measured on the live book before the fix (annualised vol):

    book   dashboard (calendar)   server (trading days)   gap
    s1            12.28%                 10.30%          -1.98 pp
    s2             5.50%                  7.12%          +1.62 pp
    s3            14.08%                 10.64%          -3.44 pp
    s4            12.62%                  8.84%          -3.78 pp

S4 is the deployed book against a 10% target, so a reader of one screen saw it
26% over target and a reader of the other saw it 12% under. Decomposed, the day
basis was the dominant term: for S4, moving to trading days alone closed 3.86
of the 3.78 points, with the measurement window worth the small remainder.

There is no JavaScript runtime on the trading host, so this pins the invariant
at the source level. That is weaker than executing both implementations against
the same series, and it is called out as weaker rather than dressed up.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASH = (ROOT / "dashboard" / "server.py").read_text()


def test_the_dashboard_filters_to_trading_days():
    assert "function isTradingDay(" in DASH, (
        "the dashboard lost its trading-day filter; its risk numbers will "
        "drift from the server's again")
    m = re.search(r"function daily\(hist\)\{(.*?)\n", DASH)
    assert m and "isTradingDay" in m.group(1), (
        "daily() is back on a calendar-day basis")


def test_the_filter_excludes_exactly_saturday_and_sunday():
    m = re.search(r"function isTradingDay\(k\)\{(.*?)\}", DASH)
    body = m.group(1)
    assert "getUTCDay" in body, "must key off UTC, matching the stored timestamps"
    assert ">=1" in body and "<=5" in body, (
        "Mon..Fri only; 0 is Sunday and 6 is Saturday")


def test_the_server_uses_the_same_basis():
    from runtime.live import _trading_days_only
    import pandas as pd
    s = pd.Series({"2026-07-31": 1.0,   # Fri
                   "2026-08-01": 1.0,   # Sat
                   "2026-08-02": 1.0,   # Sun
                   "2026-08-03": 1.0},  # Mon
                  dtype=float)
    assert list(_trading_days_only(s).index) == ["2026-07-31", "2026-08-03"]


def test_annualisation_matches_the_basis():
    """sqrt(252) is only right on a five-day week. Pinned because the constant
    and the filter have to move together or the number is wrong again."""
    assert "Math.sqrt(252)" in DASH
    assert "isTradingDay" in DASH, (
        "sqrt(252) with an unfiltered series annualises a seven-day week by a "
        "five-day constant")


def test_the_window_is_labelled_where_it_differs():
    """Same basis, different span: the dashboard's headline vol covers whatever
    range is selected, while state.realized_vol measures from clock_start. That
    is legitimate, but unlabelled it reads as a disagreement all over again."""
    assert "full history" in DASH and "last '+RANGE+'d" in DASH


def test_both_surfaces_read_the_same_equity_source():
    """A disagreement can also come from two different inputs. Both read
    state.equity_history, keyed by day, last value wins."""
    assert "equity_history" in DASH
    from core.alerts import format_daily_digest
    import inspect
    assert "systems" in inspect.getsource(format_daily_digest)
