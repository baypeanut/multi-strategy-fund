"""Expired paper broker jobs cannot dispatch late orders or overlap jobs."""
import sys
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from core.broker.ibkr_broker import IBKRBroker, MirrorAborted, PlannedOrder
from runtime.live import LiveRuntime


def test_timed_out_job_is_revoked_and_blocks_a_second_job(tmp_path):
    rt = LiveRuntime(str(tmp_path / 'state.json'))
    release = threading.Event()
    started = threading.Event()
    second = []

    def slow():
        started.set()
        assert release.wait(2)

    try:
        assert rt._ibkr_bounded(slow, 0.05) == {'timeout': True}
        assert started.is_set()
        assert rt._ibkr_cancel_event.is_set()
        assert rt._ibkr_bounded(lambda: second.append(True), 0.05)['busy']
        assert not second
    finally:
        release.set()
        rt._ibkr_worker.join(2)
    assert not rt._ibkr_worker.is_alive()
    assert rt._ibkr_bounded(lambda: second.append(True), 1) == {'ok': True}
    assert second == [True]
    assert not rt._ibkr_cancel_event.is_set()


@pytest.fixture
def broker(monkeypatch):
    class Order:
        def __init__(self, *args):
            pass

    monkeypatch.setitem(sys.modules, 'ib_async', SimpleNamespace(
        Stock=lambda *args: SimpleNamespace(symbol=args[0]),
        LimitOrder=Order, MarketOrder=Order))
    b = IBKRBroker({'account': 'DU123', 'client_id': 18})
    b.cancel_event = threading.Event()
    return b


def test_expiry_during_contract_lookup_prevents_dispatch(broker):
    placed = []

    def qualify(contract):
        broker.cancel_event.set()
        return [contract]

    broker._ib = SimpleNamespace(
        qualifyContracts=qualify, placeOrder=lambda *a: placed.append(a))
    with pytest.raises(MirrorAborted, match='expired'):
        broker.place([PlannedOrder('A', 'BUY', 1, 100, 100)])
    assert placed == []


def test_expiry_between_orders_stops_remaining_batch(broker):
    placed = []

    def place(contract, order):
        placed.append(order)
        broker.cancel_event.set()

    broker._ib = SimpleNamespace(
        qualifyContracts=lambda contract: [contract], placeOrder=place)
    with pytest.raises(MirrorAborted, match='expired'):
        broker.place([PlannedOrder(s, 'BUY', 1, 100, 100) for s in ['A', 'B']])
    assert len(placed) == 1
    assert placed[0].account == 'DU123'


def test_expired_job_cannot_cancel_working_orders(broker):
    broker.cancel_event.set()
    broker._ib = SimpleNamespace()
    with pytest.raises(MirrorAborted, match='expired'):
        broker.cancel_open_orders()


def test_expired_mirror_report_cannot_overwrite_runtime_state(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    rt = LiveRuntime(str(tmp_path / 'state.json'))
    rt._ibkr_cancel_event = threading.Event()
    merged = []
    monkeypatch.setattr(rt, '_merge_ibkr_state', lambda report: merged.append(report))

    class ExpiredBroker:
        def __init__(self, cfg):
            pass

        def sync(self, *args, **kwargs):
            self.cancel_event.set()
            return {'orders_placed': 1}

    monkeypatch.setattr('core.broker.ibkr_broker.IBKRBroker', ExpiredBroker)
    rt._mirror_to_ibkr(pd.Series({'A': 0.05}), {'A': 100},
                      datetime.now(timezone.utc))
    assert not merged
