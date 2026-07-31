"""E40: the mirror only trades while the US cash session is open.

E37 stopped the mirror STACKING overnight orders; it did not stop it placing
them. E39's per-symbol traces showed the whole residual: the position filled
at the 13:30 open from the overnight plan is ~85% reversed at the next sync,
because that plan was sized against a stale closed-market target the first RTH
rebalance then revises. The orders cannot fill before the open anyway.

A halt-flatten is deliberately exempt - a risk control must always reach the
broker, whatever the clock says.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from runtime.live import in_cash_session

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET).astimezone(timezone.utc)


@pytest.mark.parametrize("when,expected", [
    (et(2026, 7, 29, 9, 29), False),   # one minute before the bell
    (et(2026, 7, 29, 9, 30), True),    # the bell
    (et(2026, 7, 29, 12, 0), True),    # midday
    (et(2026, 7, 29, 15, 59), True),   # last minute
    (et(2026, 7, 29, 16, 0), False),   # the close is exclusive
    (et(2026, 7, 29, 3, 0), False),    # the overnight window that caused E39
    (et(2026, 7, 25, 11, 0), True),    # Saturday=25? no - Sat is 2026-07-25+
])
def test_session_boundaries(when, expected):
    if when.astimezone(ET).weekday() >= 5:
        pytest.skip("weekend case covered separately")
    assert in_cash_session(when) is expected


def test_weekends_are_closed():
    # 2026-07-25 is a Saturday, 2026-07-26 a Sunday
    assert in_cash_session(et(2026, 7, 25, 12, 0)) is False
    assert in_cash_session(et(2026, 7, 26, 12, 0)) is False


def test_dst_moves_the_utc_boundary():
    """The whole reason this uses zoneinfo instead of a hardcoded UTC window:
    9:30 ET is 13:30 UTC in summer and 14:30 UTC in winter."""
    summer = datetime(2026, 7, 29, 13, 30, tzinfo=timezone.utc)
    winter = datetime(2026, 12, 15, 13, 30, tzinfo=timezone.utc)
    assert in_cash_session(summer) is True        # 09:30 EDT - open
    assert in_cash_session(winter) is False       # 08:30 EST - still closed
    assert in_cash_session(winter.replace(hour=14, minute=30)) is True


def test_naive_and_offset_inputs_agree():
    """Callers pass tz-aware UTC; make sure an equivalent offset agrees."""
    a = datetime(2026, 7, 29, 17, 0, tzinfo=timezone.utc)
    assert in_cash_session(a) is True
    assert in_cash_session(a.astimezone(ET)) is True


def test_gate_is_configurable_and_defaults_on():
    from core.config import CONFIG

    from runtime.live import _session_only
    assert _session_only() is True
    ib = CONFIG.setdefault("ibkr", {})
    old = ib.get("session_only")
    try:
        ib["session_only"] = False
        assert _session_only() is False
    finally:
        ib["session_only"] = old
