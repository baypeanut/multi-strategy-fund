"""Opt-in research providers through the private validated model gateway.

A failed call is attributed to the deterministic fallback, never to vLLM.
Providers propose weights or sentiment only; the existing risk wrapper and
budget governor remain in charge. No broker client or order method exists here.
"""
from __future__ import annotations

import json
import os

from core.env import get_key
from core.llm import GatewayClient, VLLMError
from core.llm.contracts import ContractError, validate_data
from systems.s2_news.sentiment import LexiconScorer
from systems.s3_llm.pm import HeuristicPM


def selected_provider(config):
    provider = config.get("provider", "auto")
    if provider not in ("auto", "vllm"):
        raise ValueError("llm.provider must be auto or vllm")
    return provider


def gateway_client(config):
    options = config.get("vllm", {}) or {}
    key_env = options.get("api_key_env", "FUND_VLLM_API_KEY")
    api_key = get_key(key_env)
    if not api_key:
        raise VLLMError("missing_credentials")
    return GatewayClient(
        base_url=os.environ.get("FUND_VLLM_BASE_URL") or options.get("base_url", "http://127.0.0.1:8081/v1"),
        model=os.environ.get("FUND_VLLM_MODEL") or options.get("model", "fund-llm"),
        api_key=api_key, timeout=options.get("timeout_seconds", 30),
        max_context_tokens=options.get("max_context_tokens", 4096),
        max_output_tokens=options.get("max_output_tokens", 1024),
        max_prompt_chars=options.get("max_prompt_chars", 24000),
    )


class _Provider:
    def __init__(self, *, config=None, client=None):
        self.config = config or {}
        self._client = client
        self.last_source = "uninitialized"
        self.last_error_code = None
        self.source_counts = {}
        self.error_counts = {}
        self.usage_total = {"input_tokens": 0, "output_tokens": 0,
                            "model": (self.config.get("vllm", {}) or {}).get("model", "fund-llm")}

    def _generate(self, **kwargs):
        if self._client is None:
            self._client = gateway_client(self.config)
        result = self._client.generate(**kwargs)
        self.usage_total["input_tokens"] += result.input_tokens
        self.usage_total["output_tokens"] += result.output_tokens
        self.usage_total["model"] = result.model
        validate_data(kwargs["task"], result.data, kwargs.get("symbols"))
        self.last_error_code = None
        self.last_source = "vllm"
        self.source_counts["vllm"] = self.source_counts.get("vllm", 0) + 1
        return result.data

    def _failure(self, exc, fallback_source):
        # Fixed codes only: never store server responses, keys or exception text.
        code = exc.code if isinstance(exc, VLLMError) else ("invalid_output" if isinstance(exc, ContractError) else "provider_error")
        self.last_error_code = code
        self.last_source = fallback_source
        self.source_counts[fallback_source] = self.source_counts.get(fallback_source, 0) + 1
        self.error_counts[code] = self.error_counts.get(code, 0) + 1


class VLLMPM(_Provider):
    def __init__(self, max_position=0.05, *, config=None, client=None):
        super().__init__(config=config, client=client)
        self._fallback = HeuristicPM(max_position=max_position)

    def propose(self, briefing):
        try:
            symbols = [row["symbol"] for row in briefing["names"]]
            data = self._generate(task="portfolio", prompt="BRIEFING:\n" + json.dumps(briefing, allow_nan=False),
                                  symbols=symbols, max_tokens=1024, temperature=0.0)
            return {row["symbol"]: float(row["weight"]) for row in data["positions"]}
        except Exception as exc:
            self._failure(exc, "heuristic")
            return self._fallback.propose(briefing)


class VLLMScorer(_Provider):
    def __init__(self, *, config=None, client=None):
        super().__init__(config=config, client=client)
        self._fallback = LexiconScorer()

    def score(self, text):
        try:
            data = self._generate(task="sentiment", prompt="Headline: " + text,
                                  max_tokens=128, temperature=0.0)
            return float(data["score"]), float(data["confidence"])
        except Exception as exc:
            self._failure(exc, "lexicon")
            return self._fallback.score(text)
