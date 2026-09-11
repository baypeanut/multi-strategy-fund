"""Mark quarantine: an absurd light tick must never mark a book, price a
broker order, or poison the S3 forward ledger.

The incident this pins: 2026-07-31, AXTI printed at ~66x its previous mark
(the split-adjustment race between today's adjusted grouped bar and
yesterday's prev_prices) and every book realized the phantom. The same print
on a 5%-cap short is a phantom -20% day -> daily_loss_kill -> halt latch ->
IBKR flatten, off a data artifact.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import runtime.live
from core.config import CONFIG
from runtime.live import MARK_JUMP_MAX, LightData, LiveRuntime


def _runtime(tmp_path, monkeypatch, prices, bar_date="2026-07-31"):
    """A LiveRuntime whose only live input is a mutable light snapshot.

    Returns (rt, box); mutate box['prices'] / box['bar_date'] between ticks.
    """
    monkeypatch.setattr(runtime.live, "send_telegram", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(LiveRuntime, "_should_rebalance", lambda *a, **k: (False, ""))
    # the heavy tier never runs offline: a bootstrap tick (empty s4 book)
    # would otherwise reach for the network — degrade it to a NO-TRADE hold
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: ({}, {}, pd.Series(dtype=float), False))
    box = {"bar_date": bar_date, "prices": dict(prices)}
    monkeypatch.setattr(
        LiveRuntime, "_fetch_light",
        lambda self: LightData(bar_date=box["bar_date"], prices=dict(box["prices"]),
                               eq_cov=1.0, cx_cov=1.0, spy_close=500.0))
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    return rt, box


def _quarantine_incidents(rt):
    return [i for i in rt.state["data_incidents"] if i["kind"] == "MARK-QUARANTINE"]


def test_long_phantom_is_quarantined(tmp_path, monkeypatch):
    rt, _ = _runtime(tmp_path, monkeypatch, {"AAPL": 6600.0, "MSFT": 202.0})
    rt.state["systems"]["s1"]["weights"] = {"AAPL": 0.03, "MSFT": 0.02}
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}
    eq0 = rt.state["systems"]["s1"]["equity"]

    rt.tick()

    # ONLY the MSFT leg is realized (2% of the book on a +1% move); the 66x
    # AAPL print contributes exactly nothing
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(
        eq0 * (1 + 0.02 * 0.01), abs=1.0)
    inc = _quarantine_incidents(rt)
    assert len(inc) == 1
    assert "AAPL" in inc[0]["detail"]
    # basis adopted: the RAW print becomes the new marking reference
    assert rt.state["prev_prices"]["AAPL"] == 6600.0


def test_round_trip_nets_zero(tmp_path, monkeypatch):
    rt, box = _runtime(tmp_path, monkeypatch, {"AAPL": 6600.0, "MSFT": 202.0})
    rt.state["systems"]["s1"]["weights"] = {"AAPL": 0.03, "MSFT": 0.02}
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}
    eq0 = rt.state["systems"]["s1"]["equity"]

    rt.tick()
    after_first = rt.state["systems"]["s1"]["equity"]

    box["prices"] = {"AAPL": 100.0, "MSFT": 202.0}   # the bad print reverses
    rt.tick()

    # the reversal inverts the ratio (~0.015x) and is quarantined again, so
    # the round trip nets to exactly zero for AAPL
    assert rt.state["prev_prices"]["AAPL"] == 100.0
    assert len(_quarantine_incidents(rt)) == 2
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(after_first, abs=1e-6)
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(
        eq0 * (1 + 0.02 * 0.01), abs=1.0)


def test_phantom_halt_is_prevented(tmp_path, monkeypatch):
    """The money test: a 5x print on a 5%-cap SHORT must not latch a halt."""
    rt, box = _runtime(tmp_path, monkeypatch, {"AAPL": 500.0})
    rt.state["systems"]["s4"]["weights"] = {"AAPL": -0.05}
    rt.state["prev_prices"] = {"AAPL": 100.0}
    eq0 = rt.state["systems"]["s4"]["equity"]
    # two prior daily marks so the governor's daily-loss / drawdown rules are
    # armed (the governor reads the curve BEFORE this tick's marking, so the
    # damage of tick 1 would reach it on tick 2)
    now = datetime.now(timezone.utc)
    for days_ago in (2, 1):
        rt.state["equity_history"]["s4"].append(
            [(now - timedelta(days=days_ago)).isoformat(), eq0])

    rt.tick()                          # the phantom print lands here
    box["prices"] = {"AAPL": 500.0}    # steady from here (ratio 1.0)
    rt.tick()                          # pre-change, the governor halts here

    assert "halt_latched" not in rt.state
    assert _quarantine_incidents(rt)                  # fired instead
    assert rt.state["systems"]["s4"]["equity"] > 0.99 * eq0   # no phantom -20%


def test_boundaries(tmp_path, monkeypatch):
    rt, _ = _runtime(tmp_path, monkeypatch,
                     {"AAPL": 390.0, "MSFT": 400.0, "NVDA": 450.0})
    rt.state["systems"]["s1"]["weights"] = {"AAPL": 0.01, "MSFT": 0.01, "NVDA": 0.01}
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0}
    eq0 = rt.state["systems"]["s1"]["equity"]

    # STRICT inequality on both sides: exactly MARK_JUMP_MAX still marks
    assert MARK_JUMP_MAX == 4.0
    assert set(rt._quarantined_marks(
        {"AAPL": 390.0, "MSFT": 400.0, "NVDA": 450.0})) == {"NVDA"}
    assert rt._quarantined_marks({"AAPL": 25.0}) == {}          # exactly 1/4x
    assert set(rt._quarantined_marks({"AAPL": 24.9})) == {"AAPL"}

    rt.tick()

    # 3.9x (+290%) and exactly 4.0x (+300%) both mark; 4.5x does not
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(
        eq0 * (1 + 0.01 * 2.9 + 0.01 * 3.0), abs=1.0)
    inc = _quarantine_incidents(rt)
    assert len(inc) == 1
    assert "NVDA" in inc[0]["detail"]
    assert "AAPL" not in inc[0]["detail"] and "MSFT" not in inc[0]["detail"]


def test_broker_paths_never_see_the_garbage(tmp_path, monkeypatch):
    rt, box = _runtime(tmp_path, monkeypatch, {"AAPL": 6600.0, "MSFT": 202.0})
    monkeypatch.setitem(CONFIG["ibkr"], "enabled", True)
    seen: dict[str, dict] = {}
    monkeypatch.setattr(
        LiveRuntime, "_refresh_ibkr",
        lambda self, prices, now: seen.__setitem__("refresh", dict(prices)))
    monkeypatch.setattr(
        LiveRuntime, "_mirror_to_ibkr",
        lambda self, w_s4, prices, now: seen.__setitem__("mirror", dict(prices)))
    rt.state["systems"]["s1"]["weights"] = {"AAPL": 0.03, "MSFT": 0.02}
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}

    rt.tick()
    assert "AAPL" not in seen["refresh"]
    assert seen["refresh"]["MSFT"] == 202.0

    # a latched halt still reaches the broker every tick — and still never
    # with the garbage price. Written to disk so _sync_halt_clear_from_disk
    # does not read the absent on-disk latch as a human clear.
    rt.state["halt_latched"] = {"ts": "T0", "reason": "test"}
    rt._save()
    box["prices"] = {"AAPL": 100.0, "MSFT": 202.0}
    rt.tick()
    assert "AAPL" not in seen["mirror"]
    assert seen["mirror"]["MSFT"] == 202.0


def test_s3_forward_guarded(tmp_path, monkeypatch):
    rt, _ = _runtime(tmp_path, monkeypatch, {})
    rt.state["s3_forward_pending"] = {"ts": "T0", "bar_date": "2026-07-30",
                                      "prices": {"AAA": 100.0, "BBB": 50.0}}

    rt._backfill_s3_forward("2026-07-31", {"AAA": 6600.0, "BBB": 50.5})

    rows = [json.loads(ln) for ln in rt.s3_ledger_path.read_text().splitlines() if ln]
    fwd = [r for r in rows if r["kind"] == "forward"][-1]
    assert fwd["returns"]["BBB"] == pytest.approx(0.01, abs=1e-9)
    assert "AAA" not in fwd["returns"]        # no four-digit "forward return"
    assert "s3_forward_pending" not in rt.state


def test_flood_safety_one_incident(tmp_path, monkeypatch):
    syms = [f"S{i}" for i in range(8)]
    rt, _ = _runtime(tmp_path, monkeypatch, {s: 1000.0 for s in syms})
    rt.state["systems"]["s1"]["weights"] = {s: 0.001 for s in syms}
    rt.state["prev_prices"] = {s: 100.0 for s in syms}

    rt.tick()

    inc = _quarantine_incidents(rt)
    assert len(inc) == 1                      # one incident for the whole tick
    assert "8" in inc[0]["detail"]
    # at most five names are spelled out, so a wholesale provider glitch can
    # neither flush the 200-row incident ring nor write a novel into it
    assert inc[0]["detail"].count("->") == 5


def test_healthy_marks_unchanged(tmp_path, monkeypatch):
    rt, _ = _runtime(tmp_path, monkeypatch, {"AAPL": 101.0, "MSFT": 196.0})
    rt.state["systems"]["s1"]["weights"] = {"AAPL": 0.03, "MSFT": 0.02}
    rt.state["prev_prices"] = {"AAPL": 100.0, "MSFT": 200.0}
    eq0 = rt.state["systems"]["s1"]["equity"]

    rt.tick()

    assert _quarantine_incidents(rt) == []
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(
        eq0 * (1 + 0.03 * 0.01 + 0.02 * -0.02), abs=1.0)


def test_quarantine_fault_never_stops_the_tick(tmp_path, monkeypatch):
    """A fault in the new check degrades to the old behaviour, loudly."""
    rt, _ = _runtime(tmp_path, monkeypatch, {"AAPL": 101.0})

    def boom(self, prices):
        raise ValueError("bad prices")

    monkeypatch.setattr(LiveRuntime, "_quarantined_marks", boom)
    rt.state["systems"]["s1"]["weights"] = {"AAPL": 0.03}
    rt.state["prev_prices"] = {"AAPL": 100.0}
    eq0 = rt.state["systems"]["s1"]["equity"]

    out = rt.tick()

    assert out["tick"] == 1                                  # the fund kept marking
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(
        eq0 * (1 + 0.03 * 0.01), abs=1.0)
    assert any(i["kind"] == "MARK-QUARANTINE-FAIL"
               for i in rt.state["data_incidents"])
