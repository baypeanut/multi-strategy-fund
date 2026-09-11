"""Provider factory — key presence decides which data source runs.

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


def equity_provider_name(provider=None) -> str:
    """What is ACTUALLY pricing the book, not what the key implies.

    E59. This used to answer from `has_key("POLYGON_API_KEY")`. But
    make_equity_provider swallows any construction failure and falls back to
    yfinance, so a Polygon outage left the fund pricing off yfinance while the
    state file, the dashboard and the research record all said "polygon".
    Provenance on every price in the book, wrong and silent.

    Same shape as the realised-vol disagreement: one fact, derived twice, from
    two different things. Derived once now, from the object that was built.
    Passing no provider still answers from the key, which is only meaningful
    before anything has been constructed - callers that hold a provider should
    hand it over.
    """
    if provider is not None:
        return getattr(provider, "PROVIDER_NAME", type(provider).__name__)
    return "polygon" if has_key("POLYGON_API_KEY") else "yfinance"
