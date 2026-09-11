"""NewsEngine — System 2 (LLM as fast reader).

Pipeline: filter-to-universe -> dedup -> score -> time-decay aggregate ->
cross-sectional signal. The LLM (or lexicon) is a *reader* turning text into a
structured number; it never sizes or sends orders. Guardrail: only universe
symbols are ever traded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from systems.s1_quant.signals import cross_sectional_zscore

from .dedup import dedup
from .events import time_decay_aggregate
from .sentiment import LexiconScorer, Scorer
from .types import NewsItem


@dataclass
class NewsResult:
    signal: pd.Series              # per-symbol cross-sectional z-score
    raw: pd.Series                 # pre-zscore decayed sentiment
    n_used: int                    # items after dedup/filter


@dataclass
class NewsEngine:
    scorer: Scorer = field(default_factory=LexiconScorer)
    tau_hours: float = 48.0
    dedup_threshold: float = 0.9

    def generate(
        self,
        items: list[NewsItem],
        universe: list[str],
        now: datetime | None = None,
    ) -> NewsResult:
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        uni = {s.upper() for s in universe}

        # guardrail: keep only items that touch the universe
        in_uni = [it for it in items if any(s.upper() in uni for s in it.symbols)]
        deduped = dedup(in_uni, threshold=self.dedup_threshold)

        tuples = []
        for it in deduped:
            score, conf = self.scorer.score(it.text)
            it.score, it.confidence = score, conf
            weight = conf * it.materiality
            for sym in it.symbols:
                if sym.upper() in uni:
                    tuples.append((it.timestamp, sym.upper(), score, weight))

        raw = time_decay_aggregate(tuples, now=now, tau_hours=self.tau_hours)
        signal = cross_sectional_zscore(raw) if len(raw) > 1 else raw
        return NewsResult(signal=signal, raw=raw, n_used=len(deduped))
