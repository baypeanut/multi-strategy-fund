"""Economic invariants, independent of live credentials and model calls."""
from datetime import datetime, timezone

import pandas as pd
import pytest

import runtime.live as live
from core.ledger.live_book import mark_book, settle_book
from core.risk.final_limits import briefing_focus, final_limits
from core.risk.governor import RiskGovernor


def zero_cost(*args):
    return 0.0


def test_hold_preserves_quantity_and_cash_on_round_trip():
    mark = mark_book(1000, pd.Series({"A": .5, "B": -.2}),
                     {"A": 100, "B": 100}, {"A": 110, "B": 120})
    result = settle_book(mark, mark.weights, zero_cost, borrow_usd=2)
    assert result["equity"] == pytest.approx(1008)
    assert result["holdings_usd"] == pytest.approx({"A": 550, "B": -240})
    assert result["cash_usd"] == pytest.approx(698)
    assert result["turnover"] == 0
    # Restore solely from persisted NAV/weights, just as after process restart.
    back = mark_book(result["equity"], pd.Series(result["weights"]),
                     {"A": 110, "B": 120}, {"A": 100, "B": 100})
    assert back.equity == pytest.approx(998)
    assert back.holdings.to_dict() == pytest.approx({"A": 500, "B": -200})


def test_rebalance_cost_uses_drifted_dollars_and_post_cost_nav():
    mark = mark_book(1000, pd.Series({"A": .5}), {"A": 100}, {"A": 110})
    def one_percent(old, target, nav):
        return float((target - old).abs().sum()) * .01
    result = settle_book(mark, pd.Series({"A": .5}), one_percent)
    # nav = 1050 - .01 * (550 - .5 * nav)
    expected_nav = (1050 - 5.5) / .995
    assert result["equity"] == pytest.approx(expected_nav)
    assert result["holdings_usd"]["A"] == pytest.approx(.5 * expected_nav)
    assert result["transaction_cost_usd"] == pytest.approx(1050 - expected_nav)
    assert result["cash_usd"] + sum(result["holdings_usd"].values()) == pytest.approx(expected_nav)


def test_missing_mark_does_not_create_a_free_exit_or_entry():
    mark = mark_book(1000, pd.Series({"A": .5}), {"A": 100}, {})
    result = settle_book(mark, pd.Series({"B": .3}), zero_cost, frozen={"A", "B"})
    assert result["holdings_usd"] == {"A": 500}
    assert result["turnover"] == 0
    recovery = mark_book(result["equity"], pd.Series(result["weights"]),
                         {"A": 100}, {"A": 120})
    assert recovery.equity == 1100


def test_unpriced_holding_cannot_fund_an_increase_elsewhere():
    mark = mark_book(1000, pd.Series({"A": .4, "B": .1}),
                     {"A": 100, "B": 100}, {"B": 100})
    result = settle_book(mark, pd.Series({"B": .8, "C": .1}), zero_cost, frozen={"A"})
    assert result["holdings_usd"] == {"A": 400, "B": 100}


def test_borrow_cash_debit_cannot_push_held_gross_over_limit():
    mark = mark_book(1000, pd.Series({"A": .5, "B": -.5}), {}, {})
    result = settle_book(mark, mark.weights, zero_cost, borrow_usd=10,
                         hold_limits=(.5, 1.))
    assert result["equity"] == 990
    assert sum(map(abs, result["weights"].values())) <= 1.
    assert result["turnover"] == pytest.approx(.01)


def limits(target, current=None, adv=None, turnover=None):
    return final_limits(pd.Series(target, dtype=float),
                        pd.Series(current or {}, dtype=float), 1000,
                        adv or {}, .05, 1., .1, turnover)


def test_final_limits_spot_adv_and_missing_volume():
    result = limits({"A": .05, "B": .05, "C": -.02, "BTC/USD": -.03},
                    {"C": -.03}, {"A": 20, "BTC/USD": 1000})
    assert result.to_dict() == pytest.approx({"A": .002, "C": -.02})


def test_turnover_interpolation_cannot_restore_illegal_holdings():
    result = limits({"A": .05}, {"B": .2, "BTC/USD": -.2},
                    {"A": 1000, "B": 1000}, turnover=.01)
    assert result.to_dict() == {"B": .05}  # mandatory risk exits exceed budget


def test_turnover_budget_applies_after_scaling():
    result = limits({"A": -.05, "B": .05}, {"A": .05, "B": -.05},
                    {"A": 1000, "B": 1000}, turnover=.04)
    assert (result - pd.Series({"A": .05, "B": -.05})).abs().sum() == pytest.approx(.04)


def test_focus_stable_and_all_existing_holdings_visible():
    held = {f"H{i}": .001 for i in range(70)}
    quant = pd.Series({"Z": 4., "A": 4.})
    available = set(held) | set(quant.index)
    a = briefing_focus(held, quant, quant, available)
    b = briefing_focus(dict(reversed(list(held.items()))), quant.iloc[::-1], quant, available)
    assert a == b
    assert set(a) == set(held)
    assert briefing_focus({}, quant, pd.Series(dtype=float), available) == ["A", "Z"]


def test_governor_applies_drawdown_cut_once_and_never_rerisk_on_hold():
    g = RiskGovernor(daily_loss_kill=-.5)
    curve = pd.Series([100, 89], index=pd.date_range("2026-01-01", periods=2))
    first = g.assess(pd.Series({"A": .1}), curve)
    second = g.assess(first.weights, curve, applied_drawdown_scale=first.risk_scale)
    assert first.weights["A"] == .05
    assert second.weights["A"] == .05
    recovery = g.assess(second.weights, pd.Series([100, 95]), applied_drawdown_scale=.5)
    assert recovery.weights["A"] == .05


def test_tick_hold_round_trip_across_restart(tmp_path, monkeypatch):
    path = str(tmp_path / "state.json")
    monkeypatch.setattr(live, "maybe_send_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(live.LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(live.LiveRuntime, "_should_rebalance", lambda *a: (False, ""))
    price = [110.]
    monkeypatch.setattr(live.LiveRuntime, "_fetch_light", lambda self: live.LightData(
        "2026-09-15", {"A": price[0]}, 1., 1.))
    rt = live.LiveRuntime(path)
    for book in ("s1", "s4"):
        rt.state["systems"][book] = {"equity": 1000., "weights": {"A": .05}}
    rt.state["prev_prices"] = {"A": 100.}
    rt.state["clock_start"] = "2026-08-18"
    rt.state["ticks"] = 100
    rt.tick()
    assert rt.state["systems"]["s1"]["holdings_usd"]["A"] == pytest.approx(55)
    rt = live.LiveRuntime(path)
    price[0] = 100.
    rt.tick()
    assert rt.state["systems"]["s1"]["equity"] == pytest.approx(1000)
    assert rt.state["cost_paid_usd"]["s1"] == 0
    assert rt.state["turnover_l1_cum"]["s1"] == 0
    assert len(rt.state["measurement_breaks"]) == 1
    assert rt.state["clock_start"] == "2026-08-18"
    assert rt.state["s3_vs_s1"]["measurement_comparable"] is False
    assert rt.state["s3_vs_s1"]["verdict_allowed"] is False


def test_final_live_limits_apply_on_held_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "maybe_send_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(live.LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(live.LiveRuntime, "_should_rebalance", lambda *a: (False, ""))
    monkeypatch.setattr(live.LiveRuntime, "_fetch_light", lambda self: live.LightData(
        "2026-09-15", {"AVAX/USD": 10., "BTC/USD": 10., "A": 100.}, 1., 1.))
    rt = live.LiveRuntime(str(tmp_path / "state.json"))
    rt.state["systems"]["s1"] = {"equity": 1000., "weights": {"AVAX/USD": .04, "BTC/USD": -.03}}
    rt.state["systems"]["s4"]["weights"] = {"A": .01}
    rt.state["cost_inputs"] = {"AVAX/USD": [20., .02], "BTC/USD": [1e6, .02], "A": [1e9, .02]}
    rt.state["prev_prices"] = {"AVAX/USD": 10., "BTC/USD": 10., "A": 100.}
    rt.tick()
    book = rt.state["systems"]["s1"]
    assert "BTC/USD" not in book["weights"]
    assert book["holdings_usd"]["AVAX/USD"] <= 2.
    assert book["last_costs"]["transaction_cost_usd"] > 0
    assert book["cash_usd"] + sum(book["holdings_usd"].values()) == pytest.approx(book["equity"])


def test_measurement_boundary_blocks_even_significant_old_readout(tmp_path):
    import numpy as np
    rt = live.LiveRuntime(str(tmp_path / "state.json"))
    days = pd.bdate_range("2026-01-01", periods=90)
    rng = np.random.default_rng(3)
    b = rng.normal(0., .001, len(days))
    a = b + .001 + rng.normal(0., .00001, len(days))
    for book, returns in (("s1", b), ("s3", a)):
        rt.state["equity_history"][book] = list(zip(days.astype(str), 1000 * np.cumprod(1 + returns)))
    assert rt._paired_s3_vs_s1()["verdict_allowed"] is True
    rt.state["measurement_breaks"] = [{"version": "self_financing_v2"}]
    result = rt._paired_s3_vs_s1()
    assert result["statistical_gate_passed"] is True
    assert result["verdict_allowed"] is False


def test_governor_cut_survives_runtime_restart(tmp_path, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(live, "maybe_send_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(live.LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(live.LiveRuntime, "_should_rebalance", lambda *a: (False, ""))
    monkeypatch.setattr(live.LiveRuntime, "_fetch_light", lambda self: live.LightData(
        "2026-09-15", {"A": 100.}, 1., 1.))
    path = str(tmp_path / "state.json")
    rt = live.LiveRuntime(path)
    rt.state["systems"]["s4"] = {"equity": 890., "weights": {"A": .04}}
    rt.state["prev_prices"] = {"A": 100.}
    now = datetime.now(timezone.utc)
    rt.state["equity_history"]["s4"] = [
        [(now - timedelta(days=2)).isoformat(), 1000.],
        [(now - timedelta(days=1)).isoformat(), 890.]]
    rt.tick()
    first = rt.state["systems"]["s4"]
    assert first["weights"]["A"] == pytest.approx(.02)
    rt = live.LiveRuntime(path)
    rt.tick()
    second = rt.state["systems"]["s4"]
    assert second["weights"]["A"] == pytest.approx(.02)
    assert second["last_costs"]["turnover"] == 0
    assert second["equity"] == first["equity"]


def test_failed_settlement_cannot_partially_mark_books(tmp_path, monkeypatch):
    import copy
    monkeypatch.setattr(live, "maybe_send_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(live.LiveRuntime, "_ingest_news", lambda *a, **k: None)
    monkeypatch.setattr(live.LiveRuntime, "_should_rebalance", lambda *a: (False, ""))
    monkeypatch.setattr(live.LiveRuntime, "_fetch_light", lambda self: live.LightData(
        "2026-09-15", {"A": 110.}, 1., 1.))
    rt = live.LiveRuntime(str(tmp_path / "state.json"))
    for book in live.SYSTEMS:
        rt.state["systems"][book] = {"equity": 1000., "weights": {"A": .02}}
    rt.state["prev_prices"] = {"A": 100.}
    before = copy.deepcopy(rt.state)
    real_settle = live.settle_book
    calls = []
    def fail_second(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("simulated settlement failure")
        return real_settle(*a, **kw)
    monkeypatch.setattr(live, "settle_book", fail_second)
    with pytest.raises(ValueError, match="simulated settlement"):
        rt.tick()
    assert rt.state["systems"] == before["systems"]
    assert rt.state["equity_history"] == before["equity_history"]
    assert rt.state["prev_prices"] == before["prev_prices"]
    assert "last_borrow_date" not in rt.state
