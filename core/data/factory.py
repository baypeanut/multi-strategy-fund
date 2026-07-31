"""Provider factory - key presence decides which data source runs.

The runtime never hardcodes a provider: it asks the factory. When
POLYGON_API_KEY lands in .env, the next tick automatically upgrades to Polygon;
if Polygon ever errors out, the coverage guard (W2) already turns the tick into
NO-TRADE, and removing the key falls back to yfinance. Zero-code-change
activation was the whole point.
"""
from __future__ import annotations

from ..env import has_key
from .equities import EquityDataProvider


def make_equity_provider():
    """Polygon if a key is present, else the free yfinance provider."""
    if has_key("POLYGON_API_KEY"):
        from .polygon import PolygonDataProvider
        try:
            return PolygonDataProvider()
        except Exception:
            pass
    return EquityDataProvider()


def equity_provider_name() -> str:
    return "polygon" if has_key("POLYGON_API_KEY") else "yfinance"
