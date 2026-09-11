"""E60b: per-book cumulative cost / turnover counters in the marking loop.

Offline and deterministic. The light fetch, the news ingest, the rebalance
gate and (on the rebalance tick) the heavy fetch + engines are all stubbed, so
every dollar asserted below is hand-computed against the SAME CostModel the
live loop charges — the counter is only trustworthy if it reconciles with the
`(1 - cost)` factor that actually moved equity.

Assertions are on the s1 book only: s4's weights pass through the governor.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

import runtime.live as live_mod
from core.broker.costs import CostModel, CostParams
from core.config import CONFIG
from runtime.live import Coverage, LightData, LiveRuntime


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    monkeypatch.setattr(live_mod, "maybe_send_daily_digest", lambda *a, **k: False)
    return LiveRuntime(state_path=str(tmp_path / "state.json"))


def _ok_light(monkeypatch, price: float = 100.0) -> None:
    """A healthy light tick: one equity mark, full coverage, no news I/O."""
    monkeypatch.setattr(LiveRuntime, "_fetch_light", lambda self: LightData(
        bar_date="2026-08-05", prices={"AAPL": price}, eq_cov=1.0, cx_cov=1.0,
        spy_close=500.0))
    monkeypatch.setattr(LiveRuntime, "_ingest_news", lambda self, *a, **k: None)


def _hold(monkeypatch) -> None:
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda self, b, r: (False, ""))


def _rebalance_to_s1(monkeypatch, w: dict) -> None:
    """Force one rebalance tick that hands s1 exactly `w`."""
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda self, b, r: (True, "t"))
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: ({}, {}, pd.Series([1.0]), False))
    monkeypatch.setattr(LiveRuntime, "_coverage", staticmethod(
        lambda eq, cx, vix: Coverage(equity=1.0, crypto=1.0, spy_ok=True,
                                     vix_ok=True)))
    books = {"s1": pd.Series(w, dtype=float), "s2": pd.Series(dtype=float),
             "s3": pd.Series(dtype=float), "s4": pd.Series(dtype=float)}
    monkeypatch.setattr(LiveRuntime, "_run_systems",
                        lambda self, e, c, v, now: (books, {}, None, "heuristic"))


def _seed_held_book(rt, weights: dict, equity: float | None = None) -> None:
    """Pre-set s1's held book. s4 gets a book too: tick() bootstraps a
    rebalance while s4 is flat (`should or not len(prev_w['s4'])`), and these
    cases are about a genuinely HELD tick."""
    rt.state["systems"]["s1"]["weights"] = dict(weights)
    if equity is not None:
        rt.state["systems"]["s1"]["equity"] = equity
    rt.state["systems"]["s4"]["weights"] = {"AAPL": 0.01}
    rt.state["prev_prices"] = {"AAPL": 100.0}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _borrow_daily() -> float:
    return CONFIG.costs.equities.get("borrow_annual_bps", 50) * 1e-4 / 252.0


def test_turnover_cost_accumulates_in_dollars(rt, monkeypatch):
    _ok_light(monkeypatch)
    _rebalance_to_s1(monkeypatch, {"AAPL": 0.05})
    rt.state["cost_inputs"] = {"AAPL": [200e6, 0.015]}
    rt.state["prev_prices"] = {"AAPL": 100.0}
    rt.state["last_borrow_date"] = _today()      # exclude borrow from this one
    eq0 = rt.state["systems"]["s1"]["equity"]
    assert not rt.state["systems"]["s1"]["weights"]

    out = rt.tick()
    assert out["rebalanced"]

    bd = CostModel(CostParams(**CONFIG.costs.equities)).estimate(
        0.05 * eq0, adv=200e6, daily_vol=0.015)
    frac = 0.05 * (bd.slippage + bd.commission)
    expected_usd = eq0 * frac            # realized == 0: the book started flat

    # counters are stored to the cent / 1e-6, hence the absolute slack
    assert rt.state["cost_paid_usd"]["s1"] == pytest.approx(
        expected_usd, rel=1e-6, abs=0.01)
    assert rt.state["turnover_l1_cum"]["s1"] == pytest.approx(0.05, abs=1e-9)
    # and the counter reconciles with what equity actually lost
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(
        eq0 * (1 - frac), rel=1e-12)


def test_held_tick_accrues_nothing(rt, monkeypatch):
    _ok_light(monkeypatch)
    _hold(monkeypatch)
    _seed_held_book(rt, {"AAPL": 0.05})
    rt.state["last_borrow_date"] = _today()
    eq0 = rt.state["systems"]["s1"]["equity"]

    out = rt.tick()
    assert not out["rebalanced"]

    # a held tick reuses prev weights, so turnover is exactly zero
    assert rt.state["cost_paid_usd"].get("s1", 0.0) == pytest.approx(0.0, abs=1e-9)
    assert rt.state["turnover_l1_cum"].get("s1", 0.0) == pytest.approx(0.0, abs=1e-9)
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(eq0, rel=1e-12)


def test_borrow_lands_in_the_counter(rt, monkeypatch):
    _ok_light(monkeypatch)
    _hold(monkeypatch)
    _seed_held_book(rt, {"AAPL": -1.0}, equity=1_000_000.0)
    rt.state.pop("last_borrow_date", None)       # so the daily borrow fires

    out = rt.tick()
    assert not out["rebalanced"]

    expected = 1_000_000.0 * _borrow_daily()
    assert rt.state["cost_paid_usd"]["s1"] == pytest.approx(
        expected, rel=1e-6, abs=0.01)
    assert rt.state["turnover_l1_cum"]["s1"] == pytest.approx(0.0, abs=1e-9)
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(
        1_000_000.0 * (1 - _borrow_daily()), rel=1e-12)


def test_corrupt_counter_state_never_stops_the_tick(rt, monkeypatch):
    _ok_light(monkeypatch)
    _hold(monkeypatch)
    _seed_held_book(rt, {"AAPL": 0.05})
    rt.state["last_borrow_date"] = _today()
    rt.state["cost_paid_usd"] = "garbage"        # wrong type entirely
    rt.state["turnover_l1_cum"] = 7

    out = rt.tick()

    assert out["tick"] == 1                      # the tick still marked
    assert isinstance(rt.state["cost_paid_usd"], dict)
    assert isinstance(rt.state["turnover_l1_cum"], dict)
    assert "s1" in rt.state["cost_paid_usd"]


def test_corrupt_per_book_value_is_treated_as_zero(rt, monkeypatch):
    _ok_light(monkeypatch)
    _hold(monkeypatch)
    _seed_held_book(rt, {"AAPL": -1.0}, equity=1_000_000.0)
    rt.state.pop("last_borrow_date", None)
    rt.state["cost_paid_usd"] = {"s1": "oops"}   # right type, unusable value
    rt.state["turnover_l1_cum"] = {"s1": None}

    out = rt.tick()

    assert out["tick"] == 1
    expected = 1_000_000.0 * _borrow_daily()
    assert rt.state["cost_paid_usd"]["s1"] == pytest.approx(
        expected, rel=1e-6, abs=0.01)
    assert rt.state["turnover_l1_cum"]["s1"] == pytest.approx(0.0, abs=1e-9)


def test_quant_view_carries_the_counters():
    import research.engineer as eng

    view = eng._quant_view({"cost_paid_usd": {"s1": 123.45},
                            "turnover_l1_cum": {"s1": 2.5}})
    payload = json.loads(view.partition("\n")[2])
    assert payload["cost_paid_usd_cum"]["s1"] == 123.45
    assert payload["turnover_l1_cum"]["s1"] == 2.5

    # absent keys render as JSON null, never a KeyError
    empty = json.loads(eng._quant_view({}).partition("\n")[2])
    assert empty["cost_paid_usd_cum"] is None
    assert empty["turnover_l1_cum"] is None
