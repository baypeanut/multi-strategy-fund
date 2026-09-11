"""Key-readiness tests: Anthropic chain, Polygon parser, factory, sector cap.

All offline — fake clients / fake payloads. These prove that dropping API keys
into .env is the ONLY remaining step to activate the paid stack.
"""
import json

import numpy as np
import pandas as pd
import pytest

from core.risk.governor import RiskGovernor
from systems.s2_news.sentiment import AnthropicScorer, LexiconScorer
from systems.s3_llm.pm import AnthropicPM, HeuristicPM


# --- fake Anthropic SDK objects --------------------------------------------
class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn", raise_exc=None):
        self.payload, self.stop_reason, self.raise_exc = payload, stop_reason, raise_exc
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.raise_exc:
            raise self.raise_exc
        return FakeResponse(json.dumps(self.payload), self.stop_reason)


class FakeClient:
    def __init__(self, payload, stop_reason="end_turn", raise_exc=None):
        self.messages = FakeMessages(payload, stop_reason, raise_exc)


BRIEFING = {"regime": {"risk_scale": 1.0},
            "names": [{"symbol": "AAPL", "s1_quant_z": 1.0, "s2_news_z": 0.5},
                      {"symbol": "MSFT", "s1_quant_z": -1.0, "s2_news_z": 0.0}]}


# --- AnthropicPM ------------------------------------------------------------
def test_anthropic_pm_parses_structured_output():
    client = FakeClient({"positions": [{"symbol": "AAPL", "weight": 0.04},
                                       {"symbol": "MSFT", "weight": -0.03}]})
    pm = AnthropicPM(client=client)
    out = pm.propose(BRIEFING)
    assert out == {"AAPL": 0.04, "MSFT": -0.03}
    assert pm.last_source == "anthropic"
    # request used structured outputs + the configured model
    kw = client.messages.last_kwargs
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["model"] == "claude-opus-5", (
        "E50: the PM runs the strongest available judgment. A 'no' on the "
        "registered question is only worth having if it was a no against the "
        "best model, and a model swap costs a full clock reset, so this moves "
        "only when the clock does.")
    assert kw["thinking"] == {"type": "adaptive"}, "this book must reason"
    assert kw["output_config"]["effort"] == "high", (
        "effort is pinned, not defaulted — a future default change must not "
        "quietly move what the experiment measures")
    assert kw["max_tokens"] >= 16000, (
        "thinking and the response share this budget on Opus 5. Truncation "
        "fails the json parse and drops the book to a heuristic, visible only "
        "in pm_source.")
    assert kw["thinking"] == {"type": "adaptive"}


def test_anthropic_pm_falls_back_on_api_error():
    client = FakeClient({}, raise_exc=RuntimeError("rate limited"))
    pm = AnthropicPM(client=client, fallback=HeuristicPM())
    out = pm.propose(BRIEFING)
    assert out and pm.last_source == "heuristic"     # loop never stalls


def test_anthropic_pm_falls_back_on_refusal():
    client = FakeClient({"positions": []}, stop_reason="refusal")
    pm = AnthropicPM(client=client)
    pm.propose(BRIEFING)
    assert pm.last_source == "heuristic"


def test_anthropic_pm_no_key_no_client_falls_back(monkeypatch):
    monkeypatch.setattr("systems.s3_llm.pm.get_key", lambda name: None)
    pm = AnthropicPM()
    out = pm.propose(BRIEFING)
    assert out and pm.last_source == "heuristic"


# --- AnthropicScorer ---------------------------------------------------------
def test_anthropic_scorer_parses_and_clamps():
    sc = AnthropicScorer(client=FakeClient({"score": 1.7, "confidence": 0.9}))
    s, c = sc.score("Company beats estimates")
    assert s == 1.0 and c == 0.9                     # clamped to [-1, 1]


def test_anthropic_scorer_falls_back():
    sc = AnthropicScorer(client=FakeClient({}, raise_exc=OSError("down")),
                         fallback=LexiconScorer())
    s, c = sc.score("misses badly, weak guidance, lawsuit")
    assert s < 0                                     # lexicon took over


# --- Polygon parser ----------------------------------------------------------
def test_polygon_parses_aggs(monkeypatch):
    import core.data.polygon as pg
    fake = {"results": [
        {"t": 1719878400000, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 5e6},
        {"t": 1719964800000, "o": 101.0, "h": 103.0, "l": 100.0, "c": 102.5, "v": 6e6},
    ]}
    monkeypatch.setattr(pg, "_get", lambda url, timeout=30: fake)
    prov = pg.PolygonDataProvider(api_key="test")
    hist = prov.history(["AAPL"])
    df = hist["AAPL"]
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2 and df["close"].iloc[-1] == 102.5


def test_polygon_requires_key(monkeypatch):
    import core.data.polygon as pg
    monkeypatch.setattr(pg, "get_key", lambda name: None)
    with pytest.raises(RuntimeError):
        pg.PolygonDataProvider()


# --- factory ------------------------------------------------------------------
def test_factory_defaults_to_yfinance(monkeypatch):
    import core.data.factory as f
    monkeypatch.setattr(f, "has_key", lambda name: False)
    from core.data.equities import EquityDataProvider
    assert isinstance(f.make_equity_provider(), EquityDataProvider)
    assert f.equity_provider_name() == "yfinance"


def test_factory_upgrades_with_key(monkeypatch):
    import core.data.factory as f
    monkeypatch.setattr(f, "has_key", lambda name: True)
    monkeypatch.setenv("POLYGON_API_KEY", "test")
    assert f.equity_provider_name() == "polygon"
    from core.data.polygon import PolygonDataProvider
    assert isinstance(f.make_equity_provider(), PolygonDataProvider)


# --- sector cap ----------------------------------------------------------------
def test_governor_sector_cap_clips():
    sectors = {"A": "tech", "B": "tech", "C": "energy"}
    gov = RiskGovernor(max_sector=0.25, sectors=sectors)
    w = pd.Series({"A": 0.20, "B": 0.20, "C": 0.10})   # tech gross 0.40 > 0.25
    d = gov.assess(w)
    tech = abs(d.weights["A"]) + abs(d.weights["B"])
    assert tech <= 0.25 + 1e-9
    assert abs(d.weights["C"] - 0.10) < 1e-9           # other sector untouched
    assert any("sector tech" in a for a in d.actions)


def test_governor_sector_cap_ignores_when_within():
    gov = RiskGovernor(max_sector=0.25, sectors={"A": "tech"})
    d = gov.assess(pd.Series({"A": 0.10}))
    assert abs(d.weights["A"] - 0.10) < 1e-9 and not d.actions


# --- E63f: measured tokens, declared prices, no invented dollars --------------
def test_the_scorer_records_measured_token_usage():
    """Before this, `.usage` appeared in NO source file while every call had it
    in hand. That is why a $30 night was invisible and why the estimate of it
    was 3x low."""
    from systems.s2_news.sentiment import AnthropicScorer

    class Usage:
        input_tokens, output_tokens = 1234, 56

    class Resp:
        stop_reason = "end_turn"
        usage = Usage()
        content = [type("B", (), {"type": "text",
                                  "text": '{"score":0.5,"confidence":0.8}'})()]

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                return Resp()

    sc = AnthropicScorer(client=Client())
    sc.score("MSFT beats on earnings")
    assert sc.last_usage == {"input_tokens": 1234, "output_tokens": 56,
                             "model": "claude-haiku-4-5"}


def test_dollars_are_tokens_times_a_declared_price_and_never_invented(tmp_path):
    """Tokens are measured. The price is an owner input in config. A model with
    no declared price contributes tokens and NO dollars, because a fabricated
    price is exactly how the last spend estimate came out 3x low."""
    from core.config import CONFIG
    from runtime.live import LiveRuntime

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    rt.state["llm_budget"] = {"date": "2026-08-12"}

    # opus-5 has a declared price: 1M in + 1M out = 5.0 + 25.0
    rt._record_llm_usage("pm", {"input_tokens": 1_000_000,
                                "output_tokens": 1_000_000,
                                "model": "claude-opus-5"})
    assert abs(rt.state["llm_budget"]["usd"] - 30.0) < 1e-9
    assert rt.state["llm_budget"]["tokens"]["pm"]["input"] == 1_000_000

    # haiku has none: tokens counted, no dollars invented, model surfaced
    before = rt.state["llm_budget"]["usd"]
    rt._record_llm_usage("scorer", {"input_tokens": 500_000,
                                    "output_tokens": 10_000,
                                    "model": "claude-haiku-4-5"})
    assert rt.state["llm_budget"]["usd"] == before, "a price was invented"
    assert rt.state["llm_budget"]["tokens"]["scorer"]["input"] == 500_000
    assert "claude-haiku-4-5" in rt.state["llm_budget"]["unpriced_models"]
    assert CONFIG["llm"]["prices_usd_per_mtok"]["claude-opus-5"]["input"] == 5.0


def test_scorer_usage_accumulates_across_every_call_in_a_tick():
    """E65. The first version of this accounting recorded `last_usage` and the
    runtime read it once per tick, but TieredScorer calls the scorer up to
    llm_budget times in that tick - so only the final call was ever counted.

    Caught by the arithmetic, not by a test: the live book showed 264 scorer
    calls against 5,472 input tokens, which is 20.7 tokens per call. No headline
    prompt plus system prompt is 20 tokens. The instrument built to measure spend
    was itself understating it, which is the same defect class it was built to
    close."""
    from systems.s2_news.sentiment import AnthropicScorer

    class Usage:
        input_tokens, output_tokens = 300, 25

    class Resp:
        stop_reason = "end_turn"
        usage = Usage()
        content = [type("B", (), {"type": "text",
                                  "text": '{"score":0.4,"confidence":0.7}'})()]

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                return Resp()

    sc = AnthropicScorer(client=Client())
    for _ in range(5):
        sc.score("some headline")

    assert sc.last_usage["input_tokens"] == 300, "last call still reported"
    assert sc.usage_total["input_tokens"] == 1500, (
        f"five calls at 300 input tokens must total 1500, got "
        f"{sc.usage_total['input_tokens']} - the accumulator is sampling, not summing")
    assert sc.usage_total["output_tokens"] == 125
    assert sc.usage_total["model"] == "claude-haiku-4-5"


def test_the_runtime_drains_the_total_not_the_last_call():
    """The other half: an accumulator nobody drains is the same bug in a new
    place. Pinned behaviourally would need a full news tick; pinned here at the
    seam, because the failure mode is reading the wrong attribute name."""
    import inspect

    import runtime.live as live_mod

    src = inspect.getsource(live_mod.LiveRuntime)
    assert 'getattr(llm, "usage_total"' in src, (
        "the scorer's accumulated usage is no longer drained; spend will be "
        "undercounted by roughly the number of calls per tick")
    assert 'getattr(pm, "usage_total"' in src
