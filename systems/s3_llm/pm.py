"""Portfolio Manager strategies for System 3.

Fallback chain (activation is automatic, driven by key/service presence):

    AnthropicPM (frontier, needs ANTHROPIC_API_KEY)
        -> OllamaPM (local LLM, needs ollama running)
            -> HeuristicPM (deterministic, always works)

Every PM records `last_source` so attribution can prove which brain actually
made each decision. All PMs return a raw proposal dict; the RiskWrapper
sanitizes it afterward — no PM ever sends orders.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Protocol

from core.env import get_key


class PM(Protocol):
    def propose(self, briefing: dict) -> dict[str, float]: ...


class HeuristicPM:
    """Blend S1 + S2 evidence, tilt long/short, scale gross by regime."""

    def __init__(self, max_position: float = 0.05, s1_w: float = 1.0, s2_w: float = 1.0):
        self.max_position = max_position
        self.s1_w, self.s2_w = s1_w, s2_w
        self.last_source = "heuristic"   # audit field: who made the decision

    def propose(self, briefing: dict) -> dict[str, float]:
        names = briefing["names"]
        scores = {}
        for n in names:
            scores[n["symbol"]] = self.s1_w * n["s1_quant_z"] + self.s2_w * n["s2_news_z"]
        if not scores:
            return {}
        # demean (market-neutral-ish), normalize to unit gross. Regime risk
        # scaling is NOT applied here — the runtime already scales S3's target
        # vol by regime.risk_scale; doing it in the PM too double-delevered
        # every heuristic-fallback day and skewed the attribution race.
        mean = sum(scores.values()) / len(scores)
        raw = {s: v - mean for s, v in scores.items()}
        gross = sum(abs(v) for v in raw.values()) or 1.0
        out = {}
        for s, v in raw.items():
            w = v / gross
            out[s] = max(-self.max_position, min(self.max_position, w))
        return out


_PM_SYSTEM = (
    "You are a disciplined long/short equity portfolio manager at a systematic "
    "fund. You receive a structured briefing (quant scores, news scores, regime) "
    "and propose target portfolio weights. Rules: use ONLY symbols present in "
    "the briefing; weights are fractions of NAV in [-0.05, 0.05], long positive, "
    "short negative; prefer a roughly market-neutral book unless the regime "
    "strongly argues otherwise; concentrate on your highest-conviction ideas "
    "rather than spreading tiny weights everywhere."
)

_PM_SCHEMA = {
    "type": "object",
    "properties": {
        "positions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "weight": {"type": "number"},
                },
                "required": ["symbol", "weight"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["positions"],
    "additionalProperties": False,
}


class AnthropicPM:
    """Frontier-LLM PM via the official Anthropic SDK.

    Activated automatically when ANTHROPIC_API_KEY exists (server .env). Uses
    structured outputs so the proposal is guaranteed-valid JSON. Any failure
    (no key, SDK missing, rate limit, API error) falls back down the chain —
    the trading loop must never stall on an API problem.

    Cost is structurally capped by the information gate (a few calls/day) plus
    the daily budget counter. Model default is claude-opus-5 — the strongest
    judgment for the book whose whole hypothesis is "does frontier judgment
    beat the quant control"; configurable via `llm.s3_model`.

    E50: moved off claude-opus-4-8, which is a generation behind at the SAME
    price ($5/$25 per MTok). The registered question is whether an LLM PM beats
    deterministic quant, and a "no" is only worth having if it was a no against
    the best available judgment. Running a generation-behind model would make a
    negative result understate what an LLM can do, and a positive one arrive
    later than it had to.

    Timed to the E49 clock reset on purpose. Mixing brains inside a measurement
    window contaminates the attribution (E15b), so a model change costs a full
    reset of the paired test — and the clock is resetting anyway. Doing it now
    is free; doing it later is not.
    """

    def __init__(self, model: str = "claude-opus-5", max_position: float = 0.05,
                 fallback: "PM | None" = None, client=None, timeout: float = 180.0):
        self.model = model
        self.max_position = max_position
        self._fallback = fallback or HeuristicPM(max_position=max_position)
        self.timeout = timeout
        self._client = client          # injectable for tests
        self.last_source = "heuristic"

    def _get_client(self):
        if self._client is not None:
            return self._client
        key = get_key("ANTHROPIC_API_KEY")
        if not key:
            return None
        try:
            import anthropic
        except ImportError:
            return None
        self._client = anthropic.Anthropic(api_key=key, timeout=self.timeout)
        return self._client

    def propose(self, briefing: dict) -> dict[str, float]:
        client = self._get_client()
        if client is None:
            out = self._fallback.propose(briefing)
            self.last_source = getattr(self._fallback, "last_source", "heuristic")
            return out
        try:
            response = client.messages.create(
                model=self.model,
                # E50: thinking and the response share this budget, and Opus 5
                # thinks by default rather than only when asked. A truncated
                # response fails the json parse and falls silently down the
                # fallback chain, so the book would degrade to a heuristic with
                # nothing but `pm_source` to show for it. Headroom is cheaper
                # than that failure: the PM decides a handful of times a day.
                max_tokens=24000,
                # explicit rather than inherited: default-on for Opus 5, off for
                # Opus 4.8 when omitted, and this book must actually reason
                thinking={"type": "adaptive"},
                # `high` is the documented setting for intelligence-sensitive
                # work. Pinned rather than defaulted so a future default change
                # cannot quietly move what the experiment is measuring.
                output_config={"effort": "high",
                               "format": {"type": "json_schema", "schema": _PM_SCHEMA}},
                system=_PM_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": "BRIEFING:\n" + json.dumps(briefing) +
                               "\n\nPropose the target portfolio.",
                }],
            )
            # E63f. Measured, not estimated. `.usage` was in hand on every
            # call in this repo and discarded everywhere, which is why the
            # $30 night was invisible and why a previous session guessed $11
            # and was wrong by 3x. Tokens are a measurement; dollars are a
            # measurement times a price the owner declares in config.
            u = getattr(response, "usage", None)
            _u = getattr(self, "usage_total", None) or {"input_tokens": 0,
                                                        "output_tokens": 0}
            _u = {"input_tokens": _u["input_tokens"] + int(getattr(u, "input_tokens", 0) or 0),
                  "output_tokens": _u["output_tokens"] + int(getattr(u, "output_tokens", 0) or 0),
                  "model": self.model} if u is not None else _u
            self.usage_total = _u
            self.last_usage = {
                "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
                "model": self.model,
            } if u is not None else None
            if response.stop_reason == "refusal":
                raise ValueError("model refused")
            text = next(b.text for b in response.content if b.type == "text")
            positions = json.loads(text)["positions"]
            weights = {str(p["symbol"]): float(p["weight"]) for p in positions}
            if not weights:
                raise ValueError("empty proposal")
            self.last_source = "anthropic"
            return weights
        except Exception:
            out = self._fallback.propose(briefing)
            self.last_source = getattr(self._fallback, "last_source", "heuristic")
            return out


class OllamaPM:
    """Local-LLM PM. Long timeout: a 3B model on a 2-vCPU box may need minutes
    for a cold load + a multi-KB briefing; the hourly cadence tolerates it.
    Every proposal records `last_source` so attribution can prove which
    decisions actually came from the LLM (W4a audit requirement)."""

    def __init__(self, model: str = "qwen2.5:3b-instruct", host: str = "http://localhost:11434",
                 max_position: float = 0.05, timeout: int = 420):
        self.model, self.host, self.timeout = model, host, timeout
        self._fallback = HeuristicPM(max_position=max_position)
        self.last_source = "heuristic"

    def propose(self, briefing: dict) -> dict[str, float]:
        prompt = (
            "You are a disciplined long/short portfolio manager. Using ONLY the "
            "briefing JSON below, propose target portfolio weights as a JSON object "
            "{\"SYMBOL\": weight}, where weight is a fraction of NAV in [-0.05, 0.05], "
            "long positive / short negative. Only use symbols present in the briefing. "
            "Respond with JSON only.\n\nBRIEFING:\n" + json.dumps(briefing)
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
            proposal = json.loads(out["response"])
            weights = {str(k): float(v) for k, v in proposal.items()}
            if not weights:
                raise ValueError("empty proposal")
            self.last_source = "ollama"
            return weights
        except Exception:
            self.last_source = "heuristic"
            return self._fallback.propose(briefing)
