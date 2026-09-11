"""Tests for the RiskGovernor circuit breakers."""
import numpy as np
import pytest
import pandas as pd

from core.risk.governor import RiskGovernor, resample_daily

gov = RiskGovernor(max_gross=1.0, dd_gate_1=-0.10, dd_gate_2=-0.15,
                   daily_loss_kill=-0.03, var_limit_95=0.02)


def test_gate2_halts_to_cash():
    w = pd.Series({"A": 0.05, "B": -0.05})
    eq = pd.Series([100.0, 84.0])  # -16% drawdown
    d = gov.assess(w, equity_curve=eq)
    assert d.halted and d.weights.abs().sum() == 0.0


def test_daily_kill_halts():
    w = pd.Series({"A": 0.05})
    eq = pd.Series([100.0, 100.0, 96.0])  # -4% on the day, dd -4% (> gate2)
    d = gov.assess(w, equity_curve=eq)
    assert d.halted


def test_gate1_halves_sizing():
    w = pd.Series({"A": 0.04, "B": -0.04})
    eq = pd.Series([100.0, 95.0, 92.0, 91.0, 89.5])  # dd -10.5%, daily -1.6%
    d = gov.assess(w, equity_curve=eq)
    assert not d.halted and abs(d.risk_scale - 0.5) < 1e-9
    assert abs(d.weights.abs().sum() - 0.04) < 1e-9  # halved from 0.08


def test_var_limit_scales_down():
    w = pd.Series({"A": 1.0})
    cov = pd.DataFrame([[0.02**2]], index=["A"], columns=["A"])  # daily vol 2%
    d = gov.assess(w, equity_curve=None, cov_daily=cov)
    # var95 = 1.645*0.02 = 0.0329 > 0.02 -> scale ~0.608
    assert d.weights["A"] < 1.0
    assert abs(d.weights["A"] - 0.02 / (1.645 * 0.02)) < 1e-6


def test_gross_cap():
    w = pd.Series({"A": 0.6, "B": -0.6})
    d = gov.assess(w)
    assert abs(d.weights.abs().sum() - 1.0) < 1e-9


def test_net_cap_scales_directional_book():
    g = RiskGovernor(max_gross=2.0, max_net=0.5)
    w = pd.Series({"A": 0.6, "B": 0.4})          # net +1.0 long-only book
    d = g.assess(w)
    assert abs(d.weights.sum() - 0.5) < 1e-9      # scaled to the net cap
    assert any("net" in a for a in d.actions)


def test_net_cap_ignores_neutral_book():
    g = RiskGovernor(max_gross=2.0, max_net=0.5)
    w = pd.Series({"A": 0.6, "B": -0.6})
    d = g.assess(w)
    assert (d.weights == w).all()


def test_no_breach_passes_through():
    w = pd.Series({"A": 0.03, "B": -0.03})
    eq = pd.Series([100.0, 101.0, 102.0])
    d = gov.assess(w, equity_curve=eq)
    assert not d.halted and d.risk_scale == 1.0
    assert abs(d.weights.abs().sum() - 0.06) < 1e-9


# --- W1 regression: daily-resample semantics on hourly marks --------------
def hourly_curve(day_values):
    """Build an hourly-marked curve: day_values = list of (day_start, day_end).

    Each day gets 24 hourly marks linearly interpolating start->end.
    """
    vals, stamps = [], []
    t0 = pd.Timestamp("2026-06-01 00:00:00")
    for d, (a, b) in enumerate(day_values):
        for h in range(24):
            vals.append(a + (b - a) * h / 23)
            stamps.append(t0 + pd.Timedelta(days=d, hours=h))
    return pd.Series(vals, index=pd.DatetimeIndex(stamps))


def test_slow_bleed_over_24_ticks_triggers_daily_kill():
    # -4% spread across a full day of hourly marks: every tick-over-tick move
    # is tiny (~0.17%), the OLD tick-based rule never fired. The daily rule must.
    eq = hourly_curve([(100.0, 100.0), (100.0, 96.0)])
    d = gov.assess(pd.Series({"A": 0.05}), equity_curve=eq)
    assert d.halted
    assert any("day loss" in a for a in d.actions)


def test_single_hour_crash_still_triggers():
    # flat, then -3.5% in the final hour: vs yesterday's close it's -3.5%
    eq = hourly_curve([(100.0, 100.0), (100.0, 100.0)])
    eq.iloc[-1] = 96.5
    d = gov.assess(pd.Series({"A": 0.05}), equity_curve=eq)
    assert d.halted


def test_weekend_flat_marks_do_not_trigger():
    # gentle -1%/day drift with hourly marks incl. flat weekend: no kill
    eq = hourly_curve([(100.0, 100.0), (100.0, 100.0), (100.0, 99.0)])
    d = gov.assess(pd.Series({"A": 0.05}), equity_curve=eq)
    assert not d.halted


def test_resample_daily_collapses_hourly():
    eq = hourly_curve([(100.0, 101.0), (101.0, 102.0), (102.0, 103.0)])
    daily = resample_daily(eq)
    assert len(daily) == 3
    assert abs(daily.iloc[-1] - 103.0) < 1e-9  # day close = last mark


def test_non_datetime_series_backward_compatible():
    # plain-int-indexed series (backtests/tests) behave as before
    eq = pd.Series([100.0, 100.0, 96.0])
    d = gov.assess(pd.Series({"A": 0.05}), equity_curve=eq)
    assert d.halted


# --- the wiring, not the unit ------------------------------------------------
def test_the_runtime_governor_carries_the_configured_limits(tmp_path):
    """E63, the other half of the wiring gap.

    Every test above builds its own RiskGovernor from hardcoded literals and
    never reads CONFIG, so nothing proved the CONFIGURED limits reach the
    governor the fund actually runs. The value pins in
    test_preregistered_constants.py prove config says the right thing; this
    proves the runtime is listening.

    Both halves are needed and they fail for different reasons: change
    config.yaml and the value pin goes red; break runtime/live.py's wiring so a
    limit falls back to the dataclass default and only THIS goes red."""
    from core.config import CONFIG
    from runtime.live import LiveRuntime

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    g, r = rt.governor, CONFIG["risk"]

    assert g.max_gross == r["max_gross"]
    assert g.max_net == r["max_net"]
    assert g.dd_gate_1 == r["dd_gate_1"]
    assert g.dd_gate_2 == r["dd_gate_2"]
    assert g.daily_loss_kill == r["daily_loss_kill"]
    assert g.var_limit_95 == r["var_limit_95"]
    assert g.max_sector == r["max_sector"]
    assert g.sectors, "the sector cap is inert without a symbol -> sector map"


def test_the_governor_follows_config_rather_than_a_matching_literal(monkeypatch,
                                                                   tmp_path):
    """The equality check above is necessary and not sufficient.

    Measured: replacing `dd_gate_2=CONFIG.risk.dd_gate_2` in runtime/live.py
    with the literal `-0.15` keeps that test green, because the literal happens
    to EQUAL the configured value. The link would be severed and the assertion
    could not tell - it would only surface later, when someone changed
    config.yaml and the fund quietly ignored them.

    Same lesson as the marginal p-value fixture: a test can only detect a
    change it is positioned to distinguish. So move config to a value nothing
    else in the repo uses, rebuild the runtime, and require the governor to
    follow."""
    from core.config import CONFIG
    from runtime.live import LiveRuntime

    sentinel = {"dd_gate_1": -0.111, "dd_gate_2": -0.222,
                "daily_loss_kill": -0.333, "var_limit_95": 0.444,
                "max_net": 5.55, "max_sector": 0.666}
    for k, v in sentinel.items():
        monkeypatch.setitem(CONFIG["risk"], k, v)

    g = LiveRuntime(state_path=str(tmp_path / "state.json")).governor

    for k, v in sentinel.items():
        assert getattr(g, k) == v, (
            f"config set risk.{k}={v} and the runtime's governor carries "
            f"{getattr(g, k)}. The config-to-governor link is severed, so "
            f"editing a risk limit changes nothing.")



def test_the_runtime_actually_applies_the_governors_weights(tmp_path, monkeypatch):
    """E63. Every test above this line proves the governor DECIDES correctly.
    None of them proved the runtime USES the decision.

    Measured 2026-08-10 by mutation: replacing `weights["s4"] =
    decision.weights` in runtime/live.py with `pass` left all 557 tests green.
    The loud half of the governor survives that edit - `decision.halted` still
    latches the halt, `decision.actions` still reach Telegram and the dashboard
    - so the book reports its risk controls as applied while trading
    ungoverned. Gross clipping, sector clipping, VaR de-risking and the -10%
    gate's 0.5x sizing all stop silently.

    This is the failure shape the settle loop was built to survive: a diff that
    reads plausibly, a green suite, an auto-commit. Pinned behaviourally rather
    than by asserting on source text, because a source-string pin breaks on
    reformatting and passes on a comment."""
    import pandas as pd
    import runtime.live as live_mod
    from core.risk.governor import GovernorDecision
    from runtime.live import LightData, LiveRuntime

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    rt.cache_path = tmp_path / "cache" / "hist.pkl"
    rt.state["systems"]["s4"]["weights"] = {"AAPL": 0.40}
    rt.state["prev_prices"] = {"AAPL": 100.0}
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)

    # degraded fetch -> NO-TRADE tick, no network. The governor still runs:
    # its gates have to be able to halt a HELD book, not only a trading one.
    monkeypatch.setattr(
        LiveRuntime, "_fetch_light",
        lambda self: LightData(bar_date="2026-08-10", prices={"AAPL": 100.0},
                               eq_cov=0.01, cx_cov=0.0))

    # a decision nothing else in the tick could have produced
    sentinel = pd.Series({"AAPL": 0.011})
    monkeypatch.setattr(
        rt.governor, "assess",
        lambda w, **kw: GovernorDecision(weights=sentinel, halted=False,
                                         risk_scale=1.0, actions=["clipped"]))

    rt.tick()

    got = rt.state["systems"]["s4"]["weights"]
    assert got.get("AAPL") == pytest.approx(0.011), (
        f"the governor returned 0.011 for AAPL and the book carries {got}. "
        f"The runtime is not applying the governor's decision, so every limit "
        f"in SPEC section 4 is advisory.")
