"""Halt latch + human clear must actually lift while the process is alive."""
import json
from pathlib import Path

import pandas as pd
import pytest

from runtime.live import LightData, LiveRuntime, SYSTEMS


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    rt.cache_path = tmp_path / "cache" / "hist.pkl"
    for s in SYSTEMS:
        rt.state["systems"][s]["weights"] = {"AAPL": 0.03, "MSFT": -0.02}
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    return rt


def _ok_light(self):
    return LightData(bar_date="2026-07-15",
                     prices={"AAPL": 101.0, "MSFT": 201.0},
                     eq_cov=1.0, cx_cov=1.0, spy_close=500.0)


def test_latched_flattens_all_books(runtime, monkeypatch):
    monkeypatch.setattr(LiveRuntime, "_fetch_light", _ok_light)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda *a, **k: (False, ""))
    runtime.state["halt_latched"] = {"ts": "2026-07-15T00:00:00+00:00",
                                     "reason": "test"}
    # seed s4 weights so governor has something; latch must zero every book
    runtime.state["systems"]["s4"]["weights"] = {"AAPL": 0.04}

    out = runtime.tick()
    for s in SYSTEMS:
        assert runtime.state["systems"][s]["weights"] == {}
    assert any("halt latched" in a for a in out["actions"])


def test_clear_on_disk_is_honored_next_tick(runtime, monkeypatch):
    """Regression: clear_halt wrote disk, runtime memory still had latch,
    next _save re-latched forever. Sync-from-disk must lift it."""
    monkeypatch.setattr(LiveRuntime, "_fetch_light", _ok_light)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda *a, **k: (False, ""))

    runtime.state["halt_latched"] = {"ts": "2026-07-15T00:00:00+00:00",
                                     "reason": "DD gate2"}
    runtime._save()

    # human clear: remove latch from disk only (what clear_halt.py does)
    on_disk = json.loads(Path(runtime.state_path).read_text())
    assert "halt_latched" in on_disk
    on_disk.pop("halt_latched")
    tmp = runtime.state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(on_disk, indent=2))
    tmp.replace(runtime.state_path)

    # memory still latched until sync
    assert "halt_latched" in runtime.state
    runtime.tick()
    assert "halt_latched" not in runtime.state
    # weights may be empty if previous tick flattened; latch itself is gone
    assert not any("halt latched" in a for a in runtime.state["last_actions"])


def test_borrow_uses_252_day_basis(runtime, monkeypatch):
    monkeypatch.setattr(LiveRuntime, "_fetch_light", _ok_light)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda *a, **k: (False, ""))

    # 100% short book, first borrow charge of the day
    runtime.state["systems"]["s1"]["weights"] = {"AAPL": -1.0}
    runtime.state["systems"]["s1"]["equity"] = 1_000_000.0
    runtime.state.pop("last_borrow_date", None)
    eq0 = runtime.state["systems"]["s1"]["equity"]

    runtime.tick()
    # price move AAPL 100->101: short loses 1%; plus borrow 50bps/252
    # equity' = eq0 * (1 - 0.01) * (1 - 50e-4/252)
    expected = eq0 * 0.99 * (1.0 - 50e-4 / 252.0)
    assert abs(runtime.state["systems"]["s1"]["equity"] - expected) < 0.02


def test_stale_mark_alerts_on_non_s4_book(runtime, monkeypatch):
    monkeypatch.setattr(LiveRuntime, "_fetch_light",
                        lambda self: LightData(
                            bar_date="2026-07-15",
                            prices={"MSFT": 201.0},  # AAPL missing
                            eq_cov=1.0, cx_cov=1.0, spy_close=500.0))
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda *a, **k: (False, ""))

    runtime.state["systems"]["s1"]["weights"] = {"AAPL": 0.05}  # 5% unmarked
    runtime.state["systems"]["s4"]["weights"] = {}
    runtime.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}

    runtime.tick()
    kinds = [i["kind"] for i in runtime.state["data_incidents"]]
    assert "STALE-MARK" in kinds


def test_s4_attribution_reflects_real_pnl(runtime, monkeypatch):
    """Dashboard attribution panel must come from the real p0/p1 marking
    loop, not a fabricated series — regression guard for that contract."""
    monkeypatch.setattr(LiveRuntime, "_fetch_light", _ok_light)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda *a, **k: (False, ""))

    runtime.state["systems"]["s4"]["weights"] = {"AAPL": 0.03, "MSFT": -0.02}
    eq0 = runtime.state["systems"]["s4"]["equity"]

    runtime.tick()
    attr = {row[0]: row for row in runtime.state["s4_attribution"]}
    # AAPL 100->101 (+1%), weight 0.03 -> positive $ contribution
    assert attr["AAPL"][1] == pytest.approx(0.03)
    assert attr["AAPL"][2] == pytest.approx(0.01, abs=1e-6)
    assert attr["AAPL"][3] == pytest.approx(0.03 * 0.01 * eq0, abs=0.5)
    # MSFT 200->201 (+0.5%), weight -0.02 -> negative $ contribution
    assert attr["MSFT"][3] < 0


def test_s4_attribution_accumulates_within_a_bar_resets_on_new_bar(runtime, monkeypatch):
    """Regression for the dashboard bug: equities re-mark once/day but crypto
    re-marks every tick, so a single-tick snapshot showed equities at $0 on
    every tick except the bar-rollover one. Attribution must accumulate
    across ticks that share a bar_date, and reset when the bar rolls."""
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda *a, **k: (False, ""))
    runtime.state["systems"]["s4"]["weights"] = {"AAPL": 0.03}

    # tick 1: same bar, AAPL 100->101
    monkeypatch.setattr(LiveRuntime, "_fetch_light",
        lambda self: LightData(bar_date="2026-07-15", prices={"AAPL": 101.0},
                               eq_cov=1.0, cx_cov=1.0, spy_close=500.0))
    runtime.tick()
    first = {row[0]: row for row in runtime.state["s4_attribution"]}["AAPL"][3]
    assert first > 0

    # tick 2: SAME bar, AAPL unchanged (101->101, as it would be intraday) —
    # accumulated $ must NOT collapse back to ~0
    monkeypatch.setattr(LiveRuntime, "_fetch_light",
        lambda self: LightData(bar_date="2026-07-15", prices={"AAPL": 101.0},
                               eq_cov=1.0, cx_cov=1.0, spy_close=500.0))
    runtime.tick()
    second = {row[0]: row for row in runtime.state["s4_attribution"]}["AAPL"][3]
    assert second == pytest.approx(first, abs=0.5)

    # tick 3: NEW bar, AAPL 101->99 — attribution resets, no leftover from
    # the prior day's accumulation
    monkeypatch.setattr(LiveRuntime, "_fetch_light",
        lambda self: LightData(bar_date="2026-07-16", prices={"AAPL": 99.0},
                               eq_cov=1.0, cx_cov=1.0, spy_close=500.0))
    runtime.tick()
    third = {row[0]: row for row in runtime.state["s4_attribution"]}["AAPL"][3]
    assert third < 0
