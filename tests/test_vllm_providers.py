"""Opt-in provider tests use explicit injected doubles, never live model calls."""
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core.config import CONFIG
from core.llm import GenerationResult, VLLMError
from systems.s2_news.sentiment import LexiconScorer
from systems.s3_llm.engine import S3Engine
from systems.s3_llm.pm import HeuristicPM
from systems.s3_llm.wrapper import RiskWrapper
from systems.vllm_provider import VLLMPM, VLLMScorer, selected_provider
import systems.vllm_provider as provider

BRIEFING = {"names":[{"symbol":"AAA","s1_quant_z":1.0,"s2_news_z":0.5},
                        {"symbol":"BBB","s1_quant_z":-1.0,"s2_news_z":-0.5}]}


class FakeClient:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data, error, []
    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise self.error
        return GenerationResult(self.data, 30, 12, "fund-llm", "00000000-0000-0000-0000-000000000001")


def test_valid_proposal_has_vllm_attribution_and_risk_review():
    client = FakeClient({"positions":[{"symbol":"AAA","weight":0.04}]})
    pm = VLLMPM(client=client)
    engine = S3Engine(pm=pm, wrapper=RiskWrapper(universe={"AAA","BBB"},nav=10000,max_position=0.01))
    result = engine.generate(BRIEFING)
    assert result.pm_source == "vllm"
    assert result.weights.abs().max() <= 0.01
    assert pm.usage_total["input_tokens"] == 30
    assert client.calls[0]["symbols"] == ["AAA","BBB"]


@pytest.mark.parametrize("data", [
    {"positions":[{"symbol":"UNLISTED","weight":0.01}]},
    {"positions":[{"symbol":"AAA","weight":float('nan')}]},
    {"positions":[{"symbol":"AAA","weight":0.1}]},
    {"positions":[{"symbol":"AAA","weight":0.01},{"symbol":"AAA","weight":0.02}]},
])
def test_invalid_model_proposal_falls_back_without_false_attribution(data):
    pm = VLLMPM(client=FakeClient(data))
    assert pm.propose(BRIEFING) == HeuristicPM().propose(BRIEFING)
    assert pm.last_source == "heuristic" and pm.last_error_code == "invalid_output"


def test_valid_empty_proposal_is_explicit_model_abstention():
    pm = VLLMPM(client=FakeClient({"positions":[]}))
    assert pm.propose(BRIEFING) == {}
    assert pm.last_source == "vllm" and pm.last_error_code is None


def test_error_code_only_not_upstream_details_are_recorded():
    scorer = VLLMScorer(client=FakeClient(error=RuntimeError("secret request body do not store")))
    assert scorer.score("Company beats estimates") == LexiconScorer().score("Company beats estimates")
    assert scorer.last_source == "lexicon" and scorer.last_error_code == "provider_error"
    assert "secret" not in str(scorer.error_counts)


def test_scorer_accumulates_all_usage_and_successes():
    scorer = VLLMScorer(client=FakeClient({"score":0.7,"confidence":0.8}))
    for _ in range(3): assert scorer.score("Company beats estimates") == (0.7,0.8)
    assert scorer.usage_total == {"input_tokens":90,"output_tokens":36,"model":"fund-llm"}
    assert scorer.source_counts == {"vllm":3}


def test_missing_key_falls_back_without_contacting_gateway(monkeypatch):
    monkeypatch.setattr(provider,"get_key",lambda name: None)
    monkeypatch.setattr(provider,"GatewayClient",lambda **kwargs: pytest.fail("Must not contact gateway without credentials"))
    pm = VLLMPM()
    pm.propose(BRIEFING)
    assert pm.last_source == "heuristic" and pm.last_error_code == "missing_credentials"


def test_client_uses_environment_override_and_distinct_gateway_key(monkeypatch):
    captured = {}
    monkeypatch.setattr(provider,"get_key",lambda name: "gateway-key-123456789")
    monkeypatch.setenv("FUND_VLLM_BASE_URL","http://127.0.0.1:18081/v1")
    monkeypatch.setattr(provider,"GatewayClient",lambda **kwargs: captured.update(kwargs) or FakeClient())
    provider.gateway_client({"vllm":{"base_url":"http://wrong.invalid/v1","api_key_env":"FUND_VLLM_API_KEY"}})
    assert captured["base_url"] == "http://127.0.0.1:18081/v1"
    assert captured["api_key"] == "gateway-key-123456789"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from runtime.live import LiveRuntime
    import runtime.live as live
    monkeypatch.setattr(live,"has_key",lambda key: False)
    rt = LiveRuntime(state_path=str(tmp_path/"state.json"))
    rt._log_s3_decision = lambda *args,**kwargs: None
    return rt


def test_default_config_keeps_existing_auto_chain(runtime, monkeypatch):
    assert selected_provider({}) == "auto"
    assert CONFIG["llm"]["provider"] == "auto"
    pm, budgeted = runtime._make_s3_pm({"enabled":False})
    assert isinstance(pm,HeuristicPM) and not budgeted
    with pytest.raises(ValueError): selected_provider({"provider":"misspelled"})


def test_explicit_vllm_does_not_consult_or_construct_anthropic(runtime,monkeypatch):
    import runtime.live as live
    monkeypatch.setitem(CONFIG.llm,"max_scorer_calls_per_day",1)
    monkeypatch.setattr(live,"has_key",lambda *args: pytest.fail("Explicit vLLM must not consult Anthropic credentials"))
    monkeypatch.setattr(live,"AnthropicPM",lambda *args,**kwargs: pytest.fail("Wrong provider"))
    pm,budgeted = runtime._make_s3_pm({"provider":"vllm"})
    assert isinstance(pm,VLLMPM) and budgeted
    scorer,budgeted = runtime._make_news_llm({"provider":"vllm"})
    assert isinstance(scorer,VLLMScorer) and budgeted


def histories():
    index = pd.date_range("2025-01-01",periods=350,freq="B")
    rng = np.random.default_rng(4)
    result = {}
    for symbol in ("AAA","BBB","CCC","DDD","SPY"):
        close = 100*np.exp(np.cumsum(rng.normal(0.0003,0.01,len(index))))
        result[symbol] = pd.DataFrame({"open":close,"high":close*1.01,"low":close*.99,"close":close,"volume":1e7},index=index)
    return result


def prepare_runtime(runtime,monkeypatch,max_pm=1):
    monkeypatch.setitem(CONFIG,"llm",{"provider":"vllm","max_pm_calls_per_day":max_pm,"max_scorer_calls_per_day":1})
    runtime._run_s2 = lambda eq,cov:(pd.Series(dtype=float),pd.Series(0.,index=list(eq)))
    runtime.state["systems"]["s3"]["weights"] = {"AAA":0.01}
    return histories()


def test_exhausted_pm_budget_holds_existing_book_without_model_call(runtime,monkeypatch):
    eq = prepare_runtime(runtime,monkeypatch,max_pm=0)
    monkeypatch.setattr(runtime,"_make_s3_pm",lambda cfg:pytest.fail("Exhausted budget must not construct a model provider"))
    weights,_,_,source = runtime._run_systems(eq,{},pd.Series(18.,index=eq["SPY"].index),datetime.now(timezone.utc))
    assert source == "held" and weights["s3"].to_dict() == {"AAA":0.01}
    assert "clock_start" not in runtime.state


def test_failed_pm_attempt_consumes_budget_and_is_not_vllm_attributed(runtime,monkeypatch):
    eq = prepare_runtime(runtime,monkeypatch)
    pm = VLLMPM(client=FakeClient(error=VLLMError("timeout")))
    monkeypatch.setattr(runtime,"_make_s3_pm",lambda cfg:(pm,True))
    _,_,_,source = runtime._run_systems(eq,{},pd.Series(18.,index=eq["SPY"].index),datetime.now(timezone.utc))
    assert source == "heuristic"
    assert runtime.state["llm_budget"]["pm"] == 1
    assert runtime.state["s3_vllm_last_error_code"] == "timeout"
    assert "clock_start" not in runtime.state


def test_news_failure_attempts_cap_and_aggregate_fallback_attribution(runtime,monkeypatch):
    from systems.s2_news.types import NewsItem
    now = datetime.now(timezone.utc)
    monkeypatch.setitem(CONFIG,"llm",{"provider":"vllm","max_pm_calls_per_day":1,"max_scorer_calls_per_day":1})
    items = [NewsItem(now,"AAA beats record profit strong growth",["AAA"],"test","one"),
             NewsItem(now,"BBB misses guidance weak lawsuit",["BBB"],"test","two")]
    monkeypatch.setattr("systems.s2_news.feeds.fetch_rss_headlines",lambda *args,**kwargs:items)
    monkeypatch.setattr("systems.s2_news.feeds.fetch_edgar_8k_items",lambda *args,**kwargs:[])
    client = FakeClient(error=VLLMError("timeout"))
    monkeypatch.setattr(provider,"gateway_client",lambda config:client)
    runtime._ingest_news(["AAA","BBB"],now)
    assert len(client.calls) == 1 and runtime.state["llm_budget"]["scorer"] == 1
    assert runtime.state["s2_vllm_source_counts"] == {"lexicon":1}
    assert runtime.state["s2_vllm_error_counts"] == {"timeout":1}
    assert not runtime._llm_budget_ok("scorer")
