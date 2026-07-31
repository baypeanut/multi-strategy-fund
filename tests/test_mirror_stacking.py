"""E37: the mirror must not stack duplicate orders across syncs.

`plan_equity_mirror` sizes deltas against FILLED positions, so a still-live
order from an earlier sync is invisible to it. Orders are tif=DAY and the
mirror syncs ~every 2h, so a closed-market night queued ~9-15 copies of every
order; at the 13:30 open they all filled and the next sync unwound the
overshoot. Measured 2026-07-27: $20.25M gross traded on a $979k NAV for $613k
of net repositioning - 97% round-trip.

The fix is cancel-then-plan. These tests pin that the cancel happens BEFORE
the position snapshot the plan is built from, that it is scoped to this
account, that it is skipped on dry runs, and that it fails soft.
"""
from types import SimpleNamespace

import pytest

from core.broker.ibkr_broker import IBKRBroker


class FakeOrder:
    def __init__(self, oid, account=None):
        self.orderId, self.account = oid, account


class FakeTrade:
    def __init__(self, oid, symbol="AAPL", account=None, active=True):
        self.order = FakeOrder(oid, account)
        self.contract = SimpleNamespace(symbol=symbol)
        self._active = active

    def isActive(self):
        return self._active


class FakeIB:
    """Records the call order so we can prove cancel precedes the snapshot."""

    def __init__(self, trades, calls):
        self._trades, self.calls = trades, calls
        self.cancelled = []

    def openTrades(self):
        return self._trades

    def cancelOrder(self, order):
        self.calls.append(f"cancel:{order.orderId}")
        self.cancelled.append(order.orderId)

    def sleep(self, s):
        pass


@pytest.fixture
def broker():
    b = IBKRBroker.__new__(IBKRBroker)
    b.account = "DU1234567"
    b._errors = []
    return b


def test_cancels_this_accounts_working_orders(broker):
    calls = []
    broker._ib = FakeIB([FakeTrade(1), FakeTrade(2, account="DU1234567")], calls)
    assert broker.cancel_open_orders() == 2
    assert broker._ib.cancelled == [1, 2]


def test_leaves_other_accounts_and_dead_orders_alone(broker):
    calls = []
    broker._ib = FakeIB([
        FakeTrade(1, account="DU999999"),      # someone else's account
        FakeTrade(2, active=False),            # already filled/cancelled
        FakeTrade(3),                          # ours, working
    ], calls)
    assert broker.cancel_open_orders() == 1
    assert broker._ib.cancelled == [3]


def test_cancel_failure_is_soft(broker):
    class Boom(FakeIB):
        def cancelOrder(self, order):
            raise RuntimeError("gateway busy")

    broker._ib = Boom([FakeTrade(1)], [])
    assert broker.cancel_open_orders() == 0        # no exception escapes
    assert any("cancel" in e for e in broker._errors)


def test_openTrades_failure_is_soft(broker):
    class Boom(FakeIB):
        def openTrades(self):
            raise RuntimeError("not connected")

    broker._ib = Boom([], [])
    assert broker.cancel_open_orders() == 0
    assert any("cancel_open_orders" in e for e in broker._errors)


def test_sync_cancels_before_snapshotting_positions(broker, monkeypatch):
    """The ordering IS the fix: planning against a pre-cancel snapshot would
    still size deltas against positions that stale orders are about to move."""
    calls = []
    broker._ib = FakeIB([FakeTrade(1)], calls)
    broker.top_n, broker.min_trade_usd, broker.max_orders = 100, 200.0, 150
    monkeypatch.setattr(broker, "connect", lambda: calls.append("connect"))
    monkeypatch.setattr(broker, "disconnect", lambda: None)
    monkeypatch.setattr(broker, "snapshot",
                        lambda: (calls.append("snapshot"), (1_000_000.0, {}))[1])
    monkeypatch.setattr(broker, "recent_fills", lambda: [])

    out = broker.sync({}, {}, execute=True)

    assert calls.index("cancel:1") < calls.index("snapshot")
    assert out["plan"]["cancelled_stale"] == 1


def test_dry_run_never_cancels(broker, monkeypatch):
    calls = []
    broker._ib = FakeIB([FakeTrade(1)], calls)
    broker.top_n, broker.min_trade_usd, broker.max_orders = 100, 200.0, 150
    monkeypatch.setattr(broker, "connect", lambda: None)
    monkeypatch.setattr(broker, "disconnect", lambda: None)
    monkeypatch.setattr(broker, "snapshot", lambda: (1_000_000.0, {}))
    monkeypatch.setattr(broker, "recent_fills", lambda: [])

    out = broker.sync({}, {}, execute=False)

    assert broker._ib.cancelled == []
    assert out["plan"]["cancelled_stale"] == 0
