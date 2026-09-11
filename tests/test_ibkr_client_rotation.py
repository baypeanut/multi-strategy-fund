"""Stable order ownership plus bounded, non-overlapping broker jobs.

The old rotation hid working orders from cancellation. Reads retain a separate
client band; the writer retains ownership and expires overdue work. Offline.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from runtime.live import LiveRuntime


class CapturingBroker:
    """Records the cfg each construction receives; never touches a socket."""
    captured: list = []

    def __init__(self, cfg):
        CapturingBroker.captured.append(dict(cfg))

    def sync(self, targets, prices, *, execute, max_position, max_gross):
        return {"mode": "dry_run", "fills_today": [], "orders_placed": 0,
                "failed": []}

    def refresh(self, targets, prices, *, max_position):
        return {"mode": "refresh", "fills_today": []}


@pytest.fixture
def rt(tmp_path, monkeypatch):
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    monkeypatch.setattr("core.broker.ibkr_broker.IBKRBroker", CapturingBroker)
    CapturingBroker.captured = []
    return LiveRuntime(state_path=str(tmp_path / "state.json"))


def _now():
    return datetime.now(timezone.utc)


def _mirror_ids(rt, ticks):
    ids = []
    for t in ticks:
        rt.state["ticks"] = t
        CapturingBroker.captured = []
        rt._mirror_to_ibkr(pd.Series({"AAPL": 0.05}), {}, _now())
        ids.append(CapturingBroker.captured[0]["client_id"])
    return ids


def _refresh_ids(rt, ticks):
    ids = []
    for t in ticks:
        rt.state["ticks"] = t
        CapturingBroker.captured = []
        rt._refresh_ibkr({}, _now())
        ids.append(CapturingBroker.captured[0]["client_id"])
    return ids


def test_mirror_client_id_preserves_order_ownership_across_ticks(rt):
    ids = _mirror_ids(rt, [0, 1, 2])
    assert len(set(ids)) == 1


def test_mirror_never_uses_fixed_base_id(rt):
    # the old fixed id (base 7) is retired from live use entirely — a stale
    # session parked on it by ANY abandoned thread can never collide again
    ids = _mirror_ids(rt, range(8))
    assert 7 not in ids


def test_mirror_and_refresh_bands_disjoint(rt):
    # Stable writer base+11 is outside the read-only base+1..+8 band.
    m = set(_mirror_ids(rt, range(8)))
    r = set(_refresh_ids(rt, range(8)))
    assert m.isdisjoint(r)
    assert len(m) == 1 and len(r) == 8


def test_refresh_rotation_regression(rt):
    # E27's refresh rotation must keep working exactly as before
    ids = _refresh_ids(rt, [0, 1, 2])
    assert len(set(ids)) == 3
