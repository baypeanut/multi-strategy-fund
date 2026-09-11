"""IC-weighted signal combining — research-only grammar primitive (E35).

Offline and deterministic: no network, seeded synthetic panels, the harness
ledger redirected to tmp_path so no test ever touches the real registry or
RESULTS.jsonl. Pins the grammar admissibility (both directions), spec-hash
distinctness (an ic_weighting spec is a NEW trial), the adaptive weighting
arithmetic (momentum-dominant, negative-IC signals dropped), the warmup
fallback to the constructor's static blend, the all-zero -> cash book, the
engine-class routing through signal_backtest, and (director wish 2026-08-01)
the per-rebalance ic_diag summary + sidecar artifact, including its fail-soft
write path and the untouched static path.
"""
from __future__ import annotations

import json
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
    """Ledger reads/writes go to tmp_path — never the live registry."""
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
    the grammar lives in the PROMPT and in the harness's ALLOWED_PARAMS — which
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


def test_generate_accumulates_one_history_row_per_decision():
    """The engine object persists across the walk-forward loop, so the rows are
    what signal_backtest later summarizes. Warmup rows carry mean_ic None."""
    eng = P.ICWeightedQuantEngine(signal_weights=dict(STATIC))
    eng.generate(_drift_histories(n_bars=290))      # warmup decision
    eng.generate(_drift_histories(n_bars=600))      # adaptive decision

    assert len(eng.ic_history) == 2
    warm, adaptive = eng.ic_history
    assert warm["mean_ic"] is None and warm["weights"] == STATIC
    assert adaptive["mean_ic"]["reversal"] < 0.0
    assert adaptive["weights"]["reversal"] == 0.0
    assert all(e["date"] is not None for e in eng.ic_history)


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


# -------------------------------------------------------------- diagnostics --
@pytest.fixture
def provider_fake(monkeypatch):
    """Serve the drift panel through the real provider entry point, so the REAL
    run_backtest drives the engine (that is what builds ic_history)."""
    import core.data.factory as factory

    hist = _drift_histories(n_bars=600)

    class _FakeProvider:
        def history(self, syms, start=None, end=None):
            return hist

    monkeypatch.setattr(factory, "make_equity_provider",
                        lambda *a, **k: _FakeProvider())
    return hist


def test_ic_diag_end_to_end_over_real_walk_forward(harness, monkeypatch,
                                                   tmp_path, provider_fake):
    art = tmp_path / "artifacts"
    monkeypatch.setattr(P, "IC_ARTIFACT_DIR", art)

    m = P.signal_backtest({"ic_weighting": True, "n_names": 8,
                           "rebalance_days": 42},
                          start="2023-07-15", end="2026-07-15")
    d = m["ic_diag"]

    assert m["ic_weighting"] is True
    assert d["n_rebalances"] >= 2
    assert d["n_warmup"] >= 1                       # early rebalances lack anchors
    assert d["signals"]["momentum"]["mean_ic"] > 0.0
    assert d["signals"]["reversal"]["mean_ic"] < 0.0
    assert d["signals"]["reversal"]["frac_active"] <= 0.2   # negative-IC drop rule
    assert isinstance(d["weight_churn_l1"], float)
    assert d["weight_churn_l1"] >= 0.0

    files = list(art.glob("ic_diag_*.json"))
    assert len(files) == 1
    assert d["artifact"] and d["artifact"].endswith(files[0].name)
    payload = json.loads(files[0].read_text())
    assert payload["window"] == ["2023-07-15", "2026-07-15"]
    assert len(payload["history"]) == d["n_rebalances"]
    for entry in payload["history"]:
        assert {"date", "mean_ic", "weights"} <= set(entry)


def test_static_path_emits_no_ic_diag_and_no_artifact(harness, monkeypatch,
                                                      tmp_path, provider_fake):
    """The non-ic path is byte-identical to before: no ic_diag, no writes."""
    art = tmp_path / "artifacts"
    monkeypatch.setattr(P, "IC_ARTIFACT_DIR", art)

    m = P.signal_backtest({"n_names": 8, "rebalance_days": 42},
                          start="2023-07-15", end="2026-07-15")

    assert "ic_diag" not in m
    assert "ic_weighting" not in m
    assert not art.exists()


def test_artifact_write_failure_is_soft(harness, monkeypatch, tmp_path,
                                        provider_fake):
    """Sidecar dir under a regular FILE -> mkdir raises OSError; the experiment
    still returns its metrics with the summary intact and artifact None."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr(P, "IC_ARTIFACT_DIR", blocked / "sub")

    m = P.signal_backtest({"ic_weighting": True, "n_names": 8,
                           "rebalance_days": 42},
                          start="2023-07-15", end="2026-07-15")
    d = m["ic_diag"]

    assert d["artifact"] is None
    assert d["n_rebalances"] >= 2
    assert d["signals"]["momentum"]["mean_ic"] > 0.0
    assert isinstance(d["weight_churn_l1"], float)


def test_empty_history_still_emits_ic_diag(harness, monkeypatch, tmp_path,
                                           provider_fake):
    """A faked run_backtest never calls generate(): the summary is emitted with
    n_rebalances 0 / all-None fields and nothing is written."""
    import backtest.engine as bt_engine

    art = tmp_path / "artifacts"
    monkeypatch.setattr(P, "IC_ARTIFACT_DIR", art)
    rng = np.random.default_rng(7)
    r = pd.Series(rng.normal(3e-4, 6e-3, 300),
                  index=pd.bdate_range("2023-07-17", periods=300))
    monkeypatch.setattr(bt_engine, "run_backtest",
                        lambda histories, engine, costs, **kw: SimpleNamespace(
                            net_returns=r, equity=(1.0 + r).cumprod() * 1e6))

    d = P.signal_backtest({"ic_weighting": True, "n_names": 8},
                          start="2023-07-15", end="2024-07-15")["ic_diag"]

    assert d["n_rebalances"] == 0 and d["n_warmup"] == 0
    assert d["weight_churn_l1"] is None and d["artifact"] is None
    for name in ("momentum", "reversal", "low_vol"):
        assert d["signals"][name] == {"mean_ic": None, "frac_active": None}
    assert not art.exists()


# ------------------------- registered signal set (E58: N0028 re-ran N0027) --
def test_registered_exclusion_is_honored():
    """A signal REGISTERED at static weight 0 is excluded from the adaptive set
    forever — only its IC keeps being measured, for diagnostics.

    FAILS on pre-fix code: the hardcoded three-signal blend handed momentum
    essentially all the weight on this fixture (momentum is the only
    positively-predictive signal here) despite w_momentum=0 — exactly the
    mechanism by which N0028 re-ran N0027 with w_reversal registered at 0.0.
    Nothing is asserted about reversal/low_vol: their IC signs are the
    fixture's business, the exclusion is the contract under test.
    """
    hist = _drift_histories(n_bars=600)
    eng = P.ICWeightedQuantEngine(
        signal_weights={"momentum": 0.0, "reversal": 1.0, "low_vol": 0.5})
    res = eng.generate(hist)

    assert res is not None
    assert eng.signal_weights["momentum"] == 0.0    # excluded, exactly zero
    assert eng.last_ic is not None                  # still MEASURED...
    assert eng.last_ic["momentum"] > 0.0            # ...and visibly predictive


def test_spec_c_shape_is_momentum_alone():
    """Regression pin for the pending spec C shape (w_momentum 1.0 / w_reversal
    0.0 / w_low_vol 0.0): momentum alone forms the adaptive set, so it takes
    the whole book and the excluded legs stay exactly 0.0.

    Honest about discrimination: on THIS fixture the pre-fix code also zeroes
    reversal and low_vol (their ICs are negative here), so this test pins the
    shape going forward — test_registered_exclusion_is_honored is the
    discriminator against pre-fix behaviour.
    """
    hist = _drift_histories(n_bars=600)
    eng = P.ICWeightedQuantEngine(
        signal_weights={"momentum": 1.0, "reversal": 0.0, "low_vol": 0.0})
    res = eng.generate(hist)

    # momentum's mean IC is pinned > 0 on this fixture by the tests above, so
    # the single survivor normalizes to exactly 1.0
    assert eng.signal_weights == {"momentum": 1.0, "reversal": 0.0,
                                  "low_vol": 0.0}
    assert _gross(res.weights) > 0.0                # a real book, not cash


def test_excluded_signal_never_activates_end_to_end(harness, monkeypatch,
                                                    tmp_path, provider_fake):
    """Walk-forward discriminator: with w_momentum 0.0 registered, momentum must
    be inactive at EVERY scored rebalance while its trailing IC keeps being
    measured. Fails pre-fix, where momentum's positive IC activated it in
    essentially every non-warmup rebalance (frac_active > 0) — the live
    signature of the N0028 defect, read straight off ic_diag.
    """
    monkeypatch.setattr(P, "IC_ARTIFACT_DIR", tmp_path / "artifacts")

    m = P.signal_backtest({"ic_weighting": True, "n_names": 8,
                           "rebalance_days": 42, "w_momentum": 0.0,
                           "w_reversal": 1.0, "w_low_vol": 0.5},
                          start="2023-07-15", end="2026-07-15")
    d = m["ic_diag"]

    assert d["n_rebalances"] >= 2
    assert d["signals"]["momentum"]["frac_active"] == 0.0   # never allocated
    assert d["signals"]["momentum"]["mean_ic"] > 0.0        # still measured
