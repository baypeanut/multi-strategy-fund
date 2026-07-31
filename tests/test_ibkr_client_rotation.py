"""IBKR clientId rotation (E27 completion): an abandoned hung connection must
never block the NEXT attempt on a 'clientId already in use' collision - on the
refresh path (already rotating since E27) AND the mirror path (was fixed at
the base id; observed live 2026-07-23: one mirror timeout wedged every
subsequent hourly sync until the Gateway's daily restart). All offline - the
broker class is replaced by a capturing fake, no network."""
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


def test_mirror_client_id_rotates_across_ticks(rt):
    # a hung, abandoned mirror thread's clientId can't block the next sync
    ids = _mirror_ids(rt, [0, 1, 2])
    assert len(set(ids)) == 3


def test_mirror_never_uses_fixed_base_id(rt):
    # the old fixed id (base 7) is retired from live use entirely - a stale
    # session parked on it by ANY abandoned thread can never collide again
    ids = _mirror_ids(rt, range(8))
    assert 7 not in ids


def test_mirror_and_refresh_bands_disjoint(rt):
    # mirror uses base+11..+18, refresh base+1..+8 - over a full 8-tick
    # rotation cycle the two paths can never collide with each other
    m = set(_mirror_ids(rt, range(8)))
    r = set(_refresh_ids(rt, range(8)))
    assert m.isdisjoint(r)
    assert len(m) == 8 and len(r) == 8


def test_refresh_rotation_regression(rt):
    # E27's refresh rotation must keep working exactly as before
    ids = _refresh_ids(rt, [0, 1, 2])
    assert len(set(ids)) == 3
