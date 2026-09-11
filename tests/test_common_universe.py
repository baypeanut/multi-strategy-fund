"""Common-universe S3-vs-S1 diagnostic (E19 backlog): the equity-only paired
readout must exclude crypto, compound correctly, stay DIAGNOSTIC-only (no
verdict field, ever), and share the clock window with the headline test. The
pre-registered rule itself lives in _paired_s3_vs_s1 and is untouched."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from runtime.live import MAX_HISTORY, LiveRuntime

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    return LiveRuntime(state_path=str(tmp_path / "state.json"))


def test_crypto_excluded_from_common_index(rt):
    # S1 holds equity + crypto; only the equity leg may enter the common index
    pw = pd.Series({"AAPL": 0.05, "BTC/USDT": 0.05})
    prev = {"AAPL": 100.0, "BTC/USDT": 100.0}
    cur = {"AAPL": 110.0, "BTC/USDT": 150.0}
    rt._mark_common("s1", pw, prev, cur, NOW)
    assert rt.state["common_idx"]["s1"] == pytest.approx(1.0 + 0.05 * 0.10)


def test_only_s1_and_s3_accrue(rt):
    pw = pd.Series({"AAPL": 0.05})
    rt._mark_common("s2", pw, {"AAPL": 100.0}, {"AAPL": 110.0}, NOW)
    rt._mark_common("s4", pw, {"AAPL": 100.0}, {"AAPL": 110.0}, NOW)
    assert "common_idx" not in rt.state


def test_index_compounds_and_history_appends(rt):
    pw = pd.Series({"AAPL": 0.10})
    rt._mark_common("s3", pw, {"AAPL": 100.0}, {"AAPL": 110.0}, NOW)
    rt._mark_common("s3", pw, {"AAPL": 110.0}, {"AAPL": 121.0}, NOW)
    assert rt.state["common_idx"]["s3"] == pytest.approx(1.01 * 1.01)
    assert len(rt.state["common_idx_history"]["s3"]) == 2


def test_missing_price_contributes_nothing(rt):
    # a symbol with no mark this tick is skipped, exactly like the main loop
    pw = pd.Series({"AAPL": 0.05, "ZZZZ": 0.05})
    rt._mark_common("s1", pw, {"AAPL": 100.0, "ZZZZ": 50.0},
                    {"AAPL": 110.0}, NOW)
    assert rt.state["common_idx"]["s1"] == pytest.approx(1.0 + 0.05 * 0.10)


def test_history_capped(rt):
    rt.state["common_idx_history"] = {"s1": [["t", 1.0]] * MAX_HISTORY}
    rt._mark_common("s1", pd.Series(dtype=float), {}, {}, NOW)
    assert len(rt.state["common_idx_history"]["s1"]) == MAX_HISTORY


def _seed_daily(rt, key, vals, start="2026-07-01"):
    # business days: the readouts count TRADING days (E48e), so a calendar-day
    # fixture would silently test a basis the code no longer uses
    dates = pd.bdate_range(start, periods=len(vals))
    rt.state.setdefault("common_idx_history", {})[key] = [
        [d.isoformat(), float(v)] for d, v in zip(dates, vals)]


def test_paired_common_is_diagnostic_only(rt):
    rng = np.random.default_rng(3)
    base = list(1.0 + np.cumsum(rng.normal(0, 0.001, 30)))
    _seed_daily(rt, "s1", base)
    _seed_daily(rt, "s3", [v * (1 + 0.0002 * i) for i, v in enumerate(base)])
    out = rt._paired_common()
    assert out["diagnostic_only"] is True
    # the armor check: this readout can NEVER carry a verdict — the
    # pre-registered decision rule stays the full-book test
    assert "verdict_allowed" not in out
    assert out["n"] == 29        # 30 business-day closes -> 29 diffs
    assert out["mean_daily_bps"] is not None


def test_paired_common_respects_clock_start(rt):
    _seed_daily(rt, "s1", [1.0 + 0.001 * i for i in range(30)])
    _seed_daily(rt, "s3", [1.0 + 0.0012 * i for i in range(30)])
    rt.state["clock_start"] = "2026-07-16"
    out = rt._paired_common()
    # 30 business days from 07-01 run to 08-11; 07-16 onward is 19 closes
    # -> 18 paired daily diffs, all of them trading days
    assert out["n"] == 18
