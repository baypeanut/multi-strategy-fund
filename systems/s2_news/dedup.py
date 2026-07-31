"""Near-duplicate detection for news items.

Free, offline bag-of-words cosine similarity. Items whose cosine similarity
exceeds a threshold are treated as the same story; only the earliest is kept.
(In production this can be swapped for sentence embeddings; the interface is
the same.)
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .types import NewsItem

_TOKEN = re.compile(r"[a-z0-9]+")


def _vec(text: str) -> Counter:
    return Counter(_TOKEN.findall(text.lower()))


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def dedup(items: list[NewsItem], threshold: float = 0.9) -> list[NewsItem]:
    """Return items with near-duplicates removed (earliest kept)."""
    ordered = sorted(items, key=lambda x: x.timestamp)
    kept: list[NewsItem] = []
    kept_vecs: list[Counter] = []
    for item in ordered:
        v = _vec(item.text)
        if any(cosine(v, kv) >= threshold for kv in kept_vecs):
            continue
        kept.append(item)
        kept_vecs.append(v)
    return kept
