"""Key-readiness tests: Anthropic chain, Polygon parser, factory, sector cap.

All offline - fake clients / fake payloads. These prove that dropping API keys
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
        "effort is pinned, not defaulted - a future default change must not "
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
