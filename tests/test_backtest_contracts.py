"""Input ordering must be causal before a strategy sees a history slice."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.engine import run_backtest
from core.broker.costs import CostModel, CostParams


def history():
    return pd.DataFrame({"close": np.arange(100.0, 108.0), "volume": 1e6},
                        index=pd.date_range("2026-01-01", periods=8))


def costs():
    return CostModel(CostParams(commission_bps=0, half_spread_bps=0, impact_coef=0))


class RecordingEngine:
    def __init__(self):
        self.seen = []

    def generate(self, histories):
        self.seen.append(histories)
        return SimpleNamespace(weights=pd.Series({"A": .1}))


@pytest.mark.parametrize("kind", ["unsorted", "duplicate", "missing_timestamp"])
def test_ambiguous_history_rejected_before_strategy_call(kind):
    frame = history()
    if kind == "unsorted":
        frame = frame.iloc[[0, 2, 1, 3, 4, 5, 6, 7]]
        # The pre-change engine sorted only the valuation panel. At Jan 2,
        # this original history's label slice also contains Jan 3: look-ahead.
        assert frame.loc[:pd.Timestamp("2026-01-02")].index.max() > pd.Timestamp("2026-01-02")
    elif kind == "duplicate":
        frame.index = pd.DatetimeIndex([frame.index[0], *frame.index[1:7], frame.index[6]])
    else:
        frame.index = pd.DatetimeIndex([*frame.index[:7], pd.NaT])
    engine = RecordingEngine()
    with pytest.raises(ValueError, match="unique, increasing, non-missing"):
        run_backtest({"A": frame}, engine, costs(), warmup=1)
    assert not engine.seen


@pytest.mark.parametrize("field,value", [
    ("warmup", True), ("warmup", 1.5), ("warmup", 0),
    ("rebalance_days", False), ("rebalance_days", 2.5), ("rebalance_days", 0),
    ("nav0", True), ("nav0", float("nan")), ("nav0", float("inf")), ("nav0", -1),
])
def test_invalid_parameters_are_rejected_before_strategy_call(field, value):
    engine = RecordingEngine()
    kwargs = {"warmup": 1, "rebalance_days": 2, "nav0": 10000, field: value}
    with pytest.raises(ValueError):
        run_backtest({"A": history()}, engine, costs(), **kwargs)
    assert not engine.seen


def test_valid_slices_never_include_future_decision_rows():
    engine = RecordingEngine()
    frame = history()
    result = run_backtest({"A": frame}, engine, costs(), warmup=1, rebalance_days=2)
    expected_dates = frame.index[1::2]
    assert len(engine.seen) == len(expected_dates)
    for histories, cutoff in zip(engine.seen, expected_dates):
        pd.testing.assert_frame_equal(histories["A"], frame.loc[:cutoff])
        assert histories["A"].index.max() == cutoff
    assert result.total_cost == 0 and result.equity.iloc[-1] > result.nav0


def test_non_datetime_chronological_indices_remain_supported():
    frame = history().reset_index(drop=True)
    result = run_backtest({"A": frame}, RecordingEngine(), costs(), warmup=1)
    assert result.equity.index.equals(frame.index[1:])
