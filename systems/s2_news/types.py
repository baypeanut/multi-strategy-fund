"""Shared types for the News/Event system."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NewsItem:
    timestamp: datetime
    text: str                      # headline / filing summary
    symbols: list[str] = field(default_factory=list)
    source: str = ""
    url: str = ""
    # filled by the pipeline
    score: float = 0.0             # sentiment in [-1, 1]
    confidence: float = 0.0        # [0, 1]
    materiality: float = 1.0       # weight multiplier


@dataclass
class FilingItem:
    timestamp: datetime
    symbol: str
    form: str                      # e.g. "8-K", "10-Q"
    accession: str = ""
    url: str = ""
