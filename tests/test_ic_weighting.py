"""IC-weighted signal combining - research-only grammar primitive (E35).

Offline and deterministic: no network, seeded synthetic panels, the harness
ledger redirected to tmp_path so no test ever touches the real registry or
RESULTS.jsonl. Pins the grammar admissibility (both directions), spec-hash
distinctness (an ic_weighting spec is a NEW trial), the adaptive weighting
arithmetic (momentum-dominant, negative-IC signals dropped), the warmup
fallback to the constructor's static blend, the all-zero -> cash book, and the
engine-class routing through signal_backtest.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import research.harness as H
import research.primitives as P
from research.director import _DIRECTOR_SCHEMA, _DIRECTOR_SYSTEM
from systems.s1_quant.engine import QuantEngine

STATIC = {"momentum": 1.0, "reversal": 0.5, "low_vol": 0.5}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Ledger reads/writes go to tmp_path - never the live registry."""
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")
    return H


def _drift_histories(n_bars: int = 600, n_names: int = 8, seed: int = 11) -> dict:
    """Synthetic OHLCV panel: each name gets its own persistent daily drift AND
    its own daily vol, both increasing in the name index. Consequences (the
    whole point of the fixture): 12-1 momentum ranks names by drift, so its
    trailing IC against the realized 21-day forward return is strongly
    positive; 5-day 'buy the loser' reversal is anti-ranked, so its IC is
    strongly negative; the low-vol signal prefers exactly the low-drift names,
    so its IC is negative too. IC weighting must therefore land on momentum.
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-02", periods=n_bars)
    mus = np.linspace(-0.005, 0.005, n_names)
    sigmas = np.linspace(0.002, 0.005, n_names)
    hist = {}
    for i in range(n_names):
        rets = mus[i] + rng.normal(0.0, sigmas[i], n_bars)
        close = 100.0 * np.cumprod(1.0 + rets)
        hist[f"SYN{i}"] = pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999,
             "close": close, "volume": np.full(n_bars, 1_000_000.0)},
            index=idx)
    return hist


def _gross(weights) -> float:
    return float(np.abs(pd.Series(weights, dtype=float)).sum())


# ------------------------------------------------------------------ grammar --
def test_ic_weighting_legal_on_signal_backtest(harness):
    spec = {"type": "signal_backtest", "family": "f",
            "params": {"ic_weighting": True, "n_names": 100}}
    assert harness.validate_spec(spec) is None
    assert "ic_weighting" in harness.ALLOWED_PARAMS["signal_backtest"]


def test_ic_weighting_illegal_on_event_study(harness):
    spec = {"type": "event_study", "family": "f",
            "params": {"ic_weighting": True, "n_names": 100}}
    err = harness.validate_spec(spec)
    assert err is not None
    assert "illegal params" in err and "ic_weighting" in err


def test_ic_weighting_hashes_as_a_distinct_trial(harness):
    base = {"type": "signal_backtest", "family": "f", "params": {"n_names": 100}}
    ic = {"type": "signal_backtest", "family": "f",
          "params": {"n_names": 100, "ic_weighting": True}}
    assert harness.spec_hash(base) != harness.spec_hash(ic)


def test_director_grammar_exposes_ic_weighting():
    """E45: params is a JSON STRING on the wire (enumerating it inline made the
    schema too complex for the API and killed the director for three days), so
    the grammar lives in the PROMPT and in the harness's ALLOWED_PARAMS - which
    is the authoritative gate. Assert there, not in the schema."""
    from research.harness import ALLOWED_PARAMS
    assert "ic_weighting" in ALLOWED_PARAMS["signal_backtest"]
    assert "ic_weighting" in _DIRECTOR_SYSTEM
    assert (_DIRECTOR_SCHEMA["properties"]["new_specs"]["items"]
            ["properties"]["params"] == {"type": "string"})


# ------------------------------------------------------------------- engine --
def test_ic_weights_favor_momentum_and_drop_reversal():
    hist = _drift_histories(n_bars=600)
    eng = P.ICWeightedQuantEngine(signal_weights=dict(STATIC))
    res = eng.generate(hist)

    assert eng.last_ic is not None                  # diagnostics populated
    assert set(eng.last_ic) == {"momentum", "reversal", "low_vol"}
    assert eng.last_ic["momentum"] > 0.0            # drift -> momentum predicts
    assert eng.last_ic["reversal"] < 0.0            # buy-the-loser anti-predicts
    assert eng.signal_weights["momentum"] > 0.5
    assert eng.signal_weights["reversal"] == 0.0    # negative IC -> dropped
    assert sum(eng.signal_weights.values()) == pytest.approx(1.0)
    assert _gross(res.weights) > 0.0                # a real book, not cash


def test_warmup_falls_back_to_static_weights():
    # ~290 bars -> only the k=1 anchor clears the 260-row momentum requirement,
    # i.e. fewer usable anchors than min_ic_obs=2
    hist = _drift_histories(n_bars=290)
    eng = P.ICWeightedQuantEngine(signal_weights=dict(STATIC))
    res = eng.generate(hist)

    assert eng._ic_signal_weights(P.build_panels(hist)[0]) is None
    assert eng.signal_weights == STATIC
    assert eng.last_ic is None
    assert res is not None


def test_all_zero_ic_weights_produce_a_cash_book(monkeypatch):
    hist = _drift_histories(n_bars=600)
    eng = P.ICWeightedQuantEngine(signal_weights=dict(STATIC))
    zeros = {"momentum": 0.0, "reversal": 0.0, "low_vol": 0.0}
    monkeypatch.setattr(P.ICWeightedQuantEngine, "_ic_signal_weights",
                        lambda self, close: dict(zeros))
    res = eng.generate(hist)

    assert eng.signal_weights == zeros              # honest 'nothing predicts'
    assert _gross(res.weights) == pytest.approx(0.0, abs=1e-12)


def test_degenerate_panels_never_raise():
    eng = P.ICWeightedQuantEngine(signal_weights=dict(STATIC))
    assert eng._ic_signal_weights(pd.DataFrame()) is None
    nan_panel = pd.DataFrame(np.nan, index=range(600), columns=["A", "B", "C"])
    assert eng._ic_signal_weights(nan_panel) is None
    assert eng._ic_signal_weights(None) is None


# ------------------------------------------------------------------ routing --
def test_signal_backtest_routes_engine_class(harness, monkeypatch):
    import backtest.engine as bt_engine
    import core.data.factory as factory

    hist = _drift_histories(n_bars=300)

    class _FakeProvider:
        def history(self, syms, start=None, end=None):
            return hist

    monkeypatch.setattr(factory, "make_equity_provider",
                        lambda *a, **k: _FakeProvider())
    rng = np.random.default_rng(5)
    r = pd.Series(rng.normal(3e-4, 6e-3, 300),
                  index=pd.bdate_range("2023-07-17", periods=300))
    captured = {}

    def _fake_run_backtest(histories, engine, costs, **kw):
        captured["engine"] = engine
        return SimpleNamespace(net_returns=r, equity=(1.0 + r).cumprod() * 1e6)

    monkeypatch.setattr(bt_engine, "run_backtest", _fake_run_backtest)

    m_ic = P.signal_backtest({"ic_weighting": True, "n_names": 8},
                             start="2023-07-15", end="2024-07-15")
    assert isinstance(captured["engine"], P.ICWeightedQuantEngine)
    assert m_ic["ic_weighting"] is True
    assert m_ic["n_days"] == 300

    m_plain = P.signal_backtest({"n_names": 8},
                                start="2023-07-15", end="2024-07-15")
    assert type(captured["engine"]) is QuantEngine    # not the research subclass
    assert "ic_weighting" not in m_plain
    assert m_plain["n_days"] == 300
