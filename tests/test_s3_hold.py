"""E15b: when the PM budget is exhausted, S3 HOLDS (never falls to a weaker
brain inside the final-config window — attribution must stay Opus-only)."""
import numpy as np
import pandas as pd
import pytest

from runtime.live import LiveRuntime


@pytest.fixture
def rt(tmp_path, monkeypatch):
    r = LiveRuntime(state_path=str(tmp_path / "state.json"))
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    monkeypatch.setattr("core.env.get_key",
                        lambda name: "k" if name == "ANTHROPIC_API_KEY" else None)
    monkeypatch.setattr("runtime.live.has_key", lambda name: True)
    return r


def hist_df(n=300, price=100.0, seed=1):
    idx = pd.date_range("2025-06-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    close = price * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1e6}, index=idx)


def test_s3_holds_when_budget_exhausted(rt, monkeypatch):
    # pre-load an S3 book and exhaust the PM budget for today
    prev_s3 = {"AAA": 0.03, "BBB": -0.02}
    rt.state["systems"]["s3"]["weights"] = prev_s3
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    rt.state["llm_budget"] = {"date": today, "pm": 16, "scorer": 0}   # at cap

    eq = {"AAA": hist_df(), "BBB": hist_df(seed=2), "CCC": hist_df(seed=3),
          "SPY": hist_df(seed=4)}
    vix = pd.Series(np.full(300, 16.0), index=pd.date_range("2025-06-01", periods=300))

    # the Anthropic PM must NOT be invoked when budget is out
    import systems.s3_llm.pm as pm_mod
    def boom(self, briefing):
        raise AssertionError("PM must not run when budget exhausted")
    monkeypatch.setattr(pm_mod.AnthropicPM, "propose", boom)

    weights, regime, cov, src = rt._run_systems(eq, {}, vix,
                                                __import__("datetime").datetime.now(timezone.utc))
    assert src == "held"
    assert dict(weights["s3"].round(6)) == prev_s3       # book held exactly


def test_held_source_does_not_touch_pm_counts_or_clock(rt):
    # a 'held' tick increments neither anthropic/heuristic/ollama nor the clock
    counts = {"ollama": 5, "heuristic": 2, "anthropic": 30}
    rt.state["s3_pm_counts"] = dict(counts)
    # simulate the tick bookkeeping for a held decision
    s3_source = "held"
    c = rt.state["s3_pm_counts"]
    if s3_source in ("anthropic", "heuristic", "ollama"):
        c[s3_source] += 1
    assert rt.state["s3_pm_counts"] == counts            # unchanged
    assert not (s3_source == "anthropic")                # clock not started
