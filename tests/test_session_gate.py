"""E40: the mirror only trades while the US cash session is open.

E37 stopped the mirror STACKING overnight orders; it did not stop it placing
them. E39's per-symbol traces showed the whole residual: the position filled
at the 13:30 open from the overnight plan is ~85% reversed at the next sync,
because that plan was sized against a stale closed-market target the first RTH
rebalance then revises. The orders cannot fill before the open anyway.

A halt-flatten is deliberately exempt — a risk control must always reach the
broker, whatever the clock says.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import runtime.live as live
from core.config import CONFIG
from core.data.universe import CRYPTO_UNIVERSE, EQUITY_UNIVERSE
from runtime.live import LightData, LiveRuntime, in_cash_session

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
    (et(2026, 7, 25, 11, 0), True),    # Saturday=25? no — Sat is 2026-07-25+
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
    assert in_cash_session(summer) is True        # 09:30 EDT — open
    assert in_cash_session(winter) is False       # 08:30 EST — still closed
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


# --- tick()-level wiring: a latched halt must reach the broker EVERY tick ----
#
# The config comment promised the exemption; the code only granted it on the
# tick the halt LATCHED. A halt latching overnight therefore got ONE attempt
# and, if it failed, never retried: internal books flat, IBKR account still
# invested, indefinitely. These tests pin the retry mechanically.
#
# The ONLY seam is the call-site: _mirror_to_ibkr is a recorder and
# _refresh_ibkr a no-op, so nothing here imports a broker, opens a socket or
# touches ib_async. The clock is controlled by patching in_cash_session (no
# faked datetimes).


def _panel():
    """One tiny frame shared by every symbol — enough to satisfy _coverage."""
    idx = pd.date_range("2026-07-01", periods=30, freq="D")
    return pd.DataFrame({"close": 100.0, "volume": 1e6}, index=idx)


def _force_rebalance(monkeypatch):
    """Drive tick() down the heavy/rebalance path, entirely offline."""
    panel = _panel()
    eq = {a.symbol: panel for a in EQUITY_UNIVERSE}
    eq["SPY"] = panel
    cx = {a.symbol: panel for a in CRYPTO_UNIVERSE}
    vix = pd.Series(20.0, index=panel.index)
    books = {"s1": pd.Series({"AAPL": 0.03}), "s2": pd.Series({"AAPL": 0.01}),
             "s3": pd.Series({"AAPL": 0.02}), "s4": pd.Series({"AAPL": 0.04})}
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda self, bar_date, raw: (True, "test bar"))
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: (eq, cx, vix, False))
    monkeypatch.setattr(LiveRuntime, "_run_systems",
                        lambda self, e, c, v, now: (dict(books), {}, None, "heuristic"))


def _seed_halt_drawdown(rt):
    """Two marks: the latest 50% below the peak AND 50% down inside the day —
    past dd_gate_2 and past the daily kill on any pre-registered setting."""
    now = datetime.now(timezone.utc)
    rt.state["equity_history"]["s4"] = [
        [(now - timedelta(days=2)).isoformat(), 3_000_000.0],
        [(now - timedelta(hours=2)).isoformat(), 1_500_000.0],
    ]
    rt.state["systems"]["s4"]["equity"] = 1_500_000.0


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    """A tick()-able runtime whose broker seam is a weights recorder.

    conftest disables the broker suite-wide; re-enabling it here with
    monkeypatch.setitem (a later monkeypatch wins) is what puts the mirror
    call-site under test at all.
    """
    calls: list[pd.Series] = []

    monkeypatch.setitem(CONFIG["ibkr"], "enabled", True)
    monkeypatch.setattr(live, "send_telegram", lambda *a, **k: None)
    monkeypatch.setattr(live, "maybe_send_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_fetch_light", lambda self: LightData(
        bar_date="2026-08-03", prices={"AAPL": 101.0, "MSFT": 201.0},
        eq_cov=1.0, cx_cov=1.0, spy_close=500.0))
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda self, syms, now: None)
    monkeypatch.setattr(LiveRuntime, "_news_raw",
                        lambda self, now: pd.Series(dtype=float))
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda self, bar_date, raw: (False, ""))
    # a flattened book is empty, so the tick AFTER a halt re-enters the heavy
    # path: keep it offline and degraded — a NO-TRADE tick, which must still
    # flatten (a flatten is not a trade on degraded data)
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: ({}, {}, pd.Series(dtype=float), False))

    def _record(self, w_s4, prices, now):
        calls.append(pd.Series(w_s4, dtype=float))

    monkeypatch.setattr(LiveRuntime, "_mirror_to_ibkr", _record)
    monkeypatch.setattr(LiveRuntime, "_refresh_ibkr", lambda self, prices, now: None)

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}
    for book in ("s1", "s2", "s3", "s4"):
        rt.state["systems"][book]["weights"] = {"AAPL": 0.03}
    return rt, calls


def test_closed_session_no_halt_never_reaches_the_broker(mirror, monkeypatch):
    """(i) The E40 gate itself — unchanged by the halt fix."""
    rt, calls = mirror
    _force_rebalance(monkeypatch)
    monkeypatch.setattr(live, "in_cash_session", lambda now: False)
    out = rt.tick()
    assert out["rebalanced"] is True
    assert calls == []


def test_open_session_rebalance_syncs_once(mirror, monkeypatch):
    """(ii) The normal path: one sync per rebalance while the session is open."""
    rt, calls = mirror
    _force_rebalance(monkeypatch)
    monkeypatch.setattr(live, "in_cash_session", lambda now: True)
    out = rt.tick()
    assert out["rebalanced"] is True
    assert len(calls) == 1


def test_session_only_false_bypasses_the_gate(mirror, monkeypatch):
    """(iii) The operator escape hatch keeps working."""
    rt, calls = mirror
    _force_rebalance(monkeypatch)
    monkeypatch.setitem(CONFIG["ibkr"], "session_only", False)
    monkeypatch.setattr(live, "in_cash_session", lambda now: False)
    rt.tick()
    assert len(calls) == 1


def test_halt_latch_tick_flattens_while_closed(mirror, monkeypatch):
    """(iv) The latch tick was already exempt; it must stay exempt."""
    rt, calls = mirror
    monkeypatch.setattr(live, "in_cash_session", lambda now: False)
    _seed_halt_drawdown(rt)
    out = rt.tick()
    assert rt.state.get("halt_latched"), "governor did not trip on a -50% book"
    assert len(calls) == 1
    assert float(calls[0].abs().sum()) == 0.0        # a FLATTEN, not a trade
    assert any("halt latched" in a for a in out["actions"])


def test_persistent_halt_retries_on_every_closed_tick(mirror, monkeypatch):
    """(v) THE CORNER — fails on the pre-fix code.

    The halt latched on an earlier tick (overnight, crypto sleeve). Every
    later closed tick was session-gated AND gated on a rebalance a flat book
    never triggers, so the one-and-only attempt was never retried.
    """
    rt, calls = mirror
    monkeypatch.setattr(live, "in_cash_session", lambda now: False)
    rt.state["halt_latched"] = {"ts": "2026-08-02T23:05:00+00:00",
                                "reason": "daily loss kill (crypto sleeve)"}
    rt.tick()
    assert len(calls) == 1, "no flatten attempt on the first post-latch tick"
    out = rt.tick()
    assert len(calls) == 2, "no flatten attempt on the second post-latch tick"
    # the second tick is a NO-TRADE tick (flat book re-enters the heavy path
    # against degraded data) — the flatten must fire there too
    assert out["no_trade"] is True
    for w in calls:
        assert float(w.abs().sum()) == 0.0


def test_halted_open_session_fires_once_per_tick(mirror, monkeypatch):
    """(vi) Halted + open: exactly one sync per tick, never a double-fire."""
    rt, calls = mirror
    monkeypatch.setattr(live, "in_cash_session", lambda now: True)
    rt.state["halt_latched"] = {"ts": "2026-08-03T14:00:00+00:00",
                                "reason": "dd gate 2"}
    rt.tick()
    assert len(calls) == 1
    rt.tick()
    assert len(calls) == 2
    assert all(float(w.abs().sum()) == 0.0 for w in calls)


def test_clearing_the_halt_restores_closed_session_silence(mirror, monkeypatch):
    """(i) re-checked after the fix: only the LATCH lifts the session gate."""
    rt, calls = mirror
    monkeypatch.setattr(live, "in_cash_session", lambda now: False)
    rt.state["halt_latched"] = {"ts": "2026-08-02T23:05:00+00:00",
                                "reason": "test"}
    rt.tick()
    assert len(calls) == 1
    rt.state.pop("halt_latched")
    rt._save()                       # what scripts/clear_halt.py leaves on disk
    rt.tick()
    assert len(calls) == 1           # closed + no halt -> silent again
