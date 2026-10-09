"""Invalid risk inputs halt with finite targets, rather than passing comparisons."""
import numpy as np
import pandas as pd
import pytest

from core.risk.governor import RiskGovernor


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "0.1", True])
def test_invalid_proposal_halts_to_finite_cash(value):
    decision = RiskGovernor().assess(pd.Series({"AAPL": value}))
    assert decision.halted and decision.risk_scale == 0
    assert np.isfinite(decision.weights).all() and decision.weights.eq(0).all()
    assert "invalid proposed weights" in decision.actions[0]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0, -1.0])
def test_invalid_equity_cannot_hide_behind_dropna(value):
    decision = RiskGovernor().assess(pd.Series({"AAPL": .03}), equity_curve=pd.Series([100.0, value]))
    assert decision.halted and decision.weights.eq(0).all()
    assert "equity history" in decision.actions[0]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "bad"])
def test_invalid_exposed_covariance_halts(value):
    cov = pd.DataFrame([[value]], index=["AAPL"], columns=["AAPL"])
    decision = RiskGovernor().assess(pd.Series({"AAPL": .03}), cov_daily=cov)
    assert decision.halted and decision.weights.eq(0).all()
    assert "covariance" in decision.actions[0]


def test_cash_book_does_not_require_a_covariance_estimate():
    cov = pd.DataFrame([[float("nan")]], index=["AAPL"], columns=["AAPL"])
    decision = RiskGovernor().assess(pd.Series({"AAPL": 0.0}), cov_daily=cov)
    assert not decision.halted and decision.weights.eq(0).all()


def test_finite_inputs_preserve_existing_var_scaling():
    covariance = pd.DataFrame([[.02 ** 2]], index=["AAPL"], columns=["AAPL"])
    decision = RiskGovernor().assess(pd.Series({"AAPL": 1.0}), cov_daily=covariance)
    assert not decision.halted
    assert decision.weights["AAPL"] == pytest.approx(.02 / (1.645 * .02))


def test_zero_previous_drawdown_scale_remains_valid_after_a_halt():
    decision = RiskGovernor().assess(pd.Series({"AAPL": 0.0}), applied_drawdown_scale=0.0)
    assert not decision.halted and decision.weights.eq(0).all()


def test_duplicate_asset_targets_cannot_bypass_the_boundary():
    decision = RiskGovernor().assess(pd.Series([.03, .04], index=["AAPL", "AAPL"]))
    assert decision.halted and decision.weights.eq(0).all()


@pytest.mark.parametrize("index", [
    pd.DatetimeIndex([pd.NaT, pd.NaT]),
    pd.DatetimeIndex(["2026-01-02", "2026-01-01"]),
    pd.DatetimeIndex(["2026-01-01", "2026-01-01"]),
])
def test_ambiguous_equity_timestamps_halt_before_daily_resampling(index):
    decision = RiskGovernor().assess(pd.Series({"AAPL": .03}),
                                    equity_curve=pd.Series([100., 101.], index=index))
    assert decision.halted and decision.weights.eq(0).all()
    assert "equity timestamps" in decision.actions[0]


def test_complex_covariance_is_rejected_without_discarding_imaginary_values():
    covariance = pd.DataFrame([[.02 ** 2 + .1j]], index=["AAPL"], columns=["AAPL"])
    decision = RiskGovernor().assess(pd.Series({"AAPL": .03}), cov_daily=covariance)
    assert decision.halted and decision.weights.eq(0).all()
    assert "covariance" in decision.actions[0]
