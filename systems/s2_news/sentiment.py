"""Sentiment scoring - pluggable, free by default.

Two scorers:
- LexiconScorer: deterministic finance-sentiment word lists (Loughran-McDonald
  style subset). Offline, $0, reproducible - the default and the backtest scorer.
- OllamaScorer: optional, calls a LOCAL Ollama model (also $0, just electricity)
  for nuance. Used live if available; falls back to the lexicon.

Both return (score in [-1,1], confidence in [0,1]).
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Protocol

_TOKEN = re.compile(r"[a-z']+")

# Compact finance-sentiment lexicons (subset; extend as needed).
POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "gain",
    "gains", "upgrade", "upgraded", "outperform", "strong", "growth", "record",
    "profit", "profitable", "raises", "raised", "boost", "approval", "approved",
    "win", "wins", "exceeds", "exceeded", "bullish", "expansion", "rebound",
}
NEGATIVE = {
    "miss", "misses", "missed", "plunge", "plunges", "drop", "drops", "fall",
    "falls", "downgrade", "downgraded", "underperform", "weak", "loss", "losses",
    "cut", "cuts", "slash", "lawsuit", "probe", "investigation", "recall",
    "bankruptcy", "default", "warns", "warning", "decline", "bearish", "fraud",
    "halt", "halted", "delay", "delayed", "guidance", "layoffs",
}
NEGATORS = {"not", "no", "never", "without"}


class Scorer(Protocol):
    def score(self, text: str) -> tuple[float, float]: ...


class LexiconScorer:
    def score(self, text: str) -> tuple[float, float]:
        toks = _TOKEN.findall(text.lower())
        pos = neg = 0
        for i, t in enumerate(toks):
            flip = i > 0 and toks[i - 1] in NEGATORS
            if t in POSITIVE:
                neg += 1 if flip else 0
                pos += 0 if flip else 1
            elif t in NEGATIVE:
                pos += 1 if flip else 0
                neg += 0 if flip else 1
        total = pos + neg
        if total == 0:
            return 0.0, 0.0
        score = (pos - neg) / total
        confidence = min(total / 5.0, 1.0)   # more hits -> more confident
        return score, confidence


class TieredScorer:
    """Two-tier scoring: lexicon for everything (instant, $0); the local LLM
    only for headlines that look material (8-K or strong lexicon signal),
    under a per-tick call budget so a news flood can't stall the loop."""

    def __init__(self, llm_scorer: "Scorer | None" = None,
                 escalate_abs_score: float = 0.4, llm_budget: int = 12):
        self.lex = LexiconScorer()
        self.llm = llm_scorer
        self.escalate_abs_score = escalate_abs_score
        self.llm_budget = llm_budget
        self.llm_calls = 0

    def score(self, text: str) -> tuple[float, float]:
        s, c = self.lex.score(text)
        material = abs(s) >= self.escalate_abs_score or "8-K" in text
        if self.llm is not None and material and self.llm_calls < self.llm_budget:
            self.llm_calls += 1
            s2, c2 = self.llm.score(text)
            return s2, max(c2, c * 0.5)
        return s, c


_SCORE_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "confidence": {"type": "number"}},
    "required": ["score", "confidence"],
    "additionalProperties": False,
}


class AnthropicScorer:
    """Frontier scorer via the official Anthropic SDK (Haiku by default
    headline classification is exactly its job at ~pennies/day).

    Activated when ANTHROPIC_API_KEY exists; every failure falls back to the
    provided fallback scorer (Ollama or lexicon). Per-tick call volume is
    capped upstream by TieredScorer's llm_budget.
    """

    def __init__(self, model: str = "claude-haiku-4-5", fallback: "Scorer | None" = None,
                 client=None, timeout: float = 30.0):
        self.model = model
        self._fallback = fallback or LexiconScorer()
        self.timeout = timeout
        self._client = client          # injectable for tests

    def _get_client(self):
        if self._client is not None:
            return self._client
        from core.env import get_key
        key = get_key("ANTHROPIC_API_KEY")
        if not key:
            return None
        try:
            import anthropic
        except ImportError:
            return None
        self._client = anthropic.Anthropic(api_key=key, timeout=self.timeout)
        return self._client

    def score(self, text: str) -> tuple[float, float]:
        client = self._get_client()
        if client is None:
            return self._fallback.score(text)
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=256,
                system=("You classify financial news sentiment for a trading system. "
                        "score: -1 (very bearish for the company) to +1 (very bullish); "
                        "confidence: 0 to 1. Judge market impact, not tone."),
                output_config={"format": {"type": "json_schema", "schema": _SCORE_SCHEMA}},
                messages=[{"role": "user", "content": f"Headline: {text}"}],
            )
            if response.stop_reason == "refusal":
                raise ValueError("refused")
            payload = json.loads(next(b.text for b in response.content if b.type == "text"))
            s = max(-1.0, min(1.0, float(payload["score"])))
            c = max(0.0, min(1.0, float(payload["confidence"])))
            return s, c
        except Exception:
            return self._fallback.score(text)


class OllamaScorer:
    """Local Ollama scorer (opt-in). Requires `ollama serve` running."""

    def __init__(self, model: str = "qwen2.5:3b-instruct",
                 host: str = "http://localhost:11434", timeout: int = 90):
        self.model = model
        self.host = host
        self.timeout = timeout
        self._fallback = LexiconScorer()

    def score(self, text: str) -> tuple[float, float]:
        prompt = (
            "You are a financial news sentiment classifier. Respond ONLY with a "
            "JSON object {\"score\": float in [-1,1], \"confidence\": float in [0,1]}. "
            f"Headline: {text}"
        )
        try:
            req = urllib.request.Request(
                f"{self.host}/api/generate",
                data=json.dumps({"model": self.model, "prompt": prompt,
                                 "stream": False, "format": "json"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                out = json.loads(resp.read())
            parsed = json.loads(out["response"])
            s = max(-1.0, min(1.0, float(parsed["score"])))
            c = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
            return s, c
        except Exception:
            return self._fallback.score(text)
