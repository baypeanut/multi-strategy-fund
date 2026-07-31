"""Live news feeds for System 2 - all free.

Two sources, one NewsItem interface:
- Yahoo Finance per-ticker RSS: headlines with timestamps, symbol-tagged by
  construction (we ask per ticker). Free, no key, stable.
- SEC EDGAR recent filings (via the existing EDGARClient): 8-K events become
  NewsItems with a standard text so the scorer can weight materiality.

The runtime keeps a `seen` id set (persisted in state) so each headline is
processed exactly once across ticks.
"""
from __future__ import annotations

import ssl
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import certifi

from .ingest import EDGARClient
from .types import NewsItem

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_RSS_URL = ("https://feeds.finance.yahoo.com/rss/2.0/headline"
            "?s={symbol}&region=US&lang=en-US")
_UA = {"User-Agent": "Mozilla/5.0 (research; contact research@example.com)"}


def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read()


def fetch_rss_headlines(symbols: list[str], per_symbol_limit: int = 10) -> list[NewsItem]:
    """Pull per-ticker Yahoo Finance RSS headlines (parallel - the subset is
    ~150 names hourly). Failures skip silently - a dead feed must never take
    the tick down."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch_one(sym: str) -> bytes | None:
        try:
            return _get(_RSS_URL.format(symbol=sym))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        raw_by_sym = dict(zip(symbols, pool.map(fetch_one, symbols)))

    items: list[NewsItem] = []
    for sym in symbols:
        raw = raw_by_sym.get(sym)
        if raw is None:
            continue
        try:
            root = ET.fromstring(raw)
        except Exception:
            continue
        for entry in root.iter("item"):
            title = (entry.findtext("title") or "").strip()
            if not title:
                continue
            pub = entry.findtext("pubDate")
            try:
                ts = parsedate_to_datetime(pub).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                ts = datetime.now(timezone.utc).replace(tzinfo=None)
            guid = (entry.findtext("guid") or entry.findtext("link") or title).strip()
            items.append(NewsItem(
                timestamp=ts, text=title, symbols=[sym],
                source="yahoo_rss", url=guid,
            ))
            if sum(1 for i in items if sym in i.symbols) >= per_symbol_limit:
                break
    return items


def fetch_edgar_8k_items(symbols: list[str], limit_per_symbol: int = 3) -> list[NewsItem]:
    """Recent 8-K filings as NewsItems. An 8-K is material by construction, so
    it carries a materiality boost; direction still comes from the scorer/LLM
    reading the headline flow around it (raw 8-K dates alone showed no
    direction - see RESEARCH_LOG E2)."""
    client = EDGARClient()
    items: list[NewsItem] = []
    for sym in symbols:
        try:
            filings = client.recent_filings(sym, forms=("8-K",), limit=limit_per_symbol)
        except Exception:
            continue
        for f in filings:
            item = NewsItem(
                timestamp=f.timestamp,
                text=f"{sym} files 8-K (material corporate event) {f.accession}",
                symbols=[sym], source="edgar_8k", url=f.url,
            )
            item.materiality = 1.5
            items.append(item)
    return items


def item_id(item: NewsItem) -> str:
    """Stable dedup id across ticks."""
    return f"{item.source}|{item.url or item.text[:80]}"
