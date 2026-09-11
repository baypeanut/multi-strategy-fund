"""Polygon.io equity data provider (activated by POLYGON_API_KEY).

Drop-in replacement for EquityDataProvider: same `history()` / `last_price()`
contract (dict of lowercase-OHLCV DataFrames). Daily aggregates, split-adjusted
(`adjusted=true`). Starter tier is 15-min delayed — fine for our hourly cadence.

At 500 names the runtime uses a two-tier pattern:
- light tick: `latest_grouped()` — the WHOLE market's daily bar in ONE call
  (marking prices + bar date), instead of 500 per-symbol requests
- heavy tick (rebalance only): `history()` with a thread pool (Starter has
  unlimited calls, so parallelism is free)
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import certifi
import pandas as pd

from ..env import get_key
from .base import OHLCV_COLUMNS

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_AGGS = ("https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/"
         "{start}/{end}?adjusted=true&sort=asc&limit=50000&apiKey={key}")
_GROUPED = ("https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
            "{day}?adjusted=true&apiKey={key}")


def _get(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


class PolygonDataProvider:
    PROVIDER_NAME = "polygon"
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or get_key("POLYGON_API_KEY")
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY missing")

    def history(
        self,
        symbols: list[str],
        start: str | None = None,
        end: str | None = None,
        period: str = "2y",
        interval: str = "1d",
    ) -> dict[str, pd.DataFrame]:
        if end is None:
            end = date.today().isoformat()
        if start is None:
            years = int(period.rstrip("ymo")[0]) if period.endswith("y") else 2
            start = (date.today() - timedelta(days=365 * years + 5)).isoformat()

        def fetch_one(sym: str) -> tuple[str, pd.DataFrame | None]:
            try:
                data = _get(_AGGS.format(sym=sym, start=start, end=end, key=self.api_key))
            except Exception:
                return sym, None
            results = data.get("results") or []
            if not results:
                return sym, None
            df = pd.DataFrame(results)
            df.index = pd.to_datetime(df["t"], unit="ms").dt.normalize()
            df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                                    "c": "close", "v": "volume"})[OHLCV_COLUMNS]
            return sym, df.astype(float)

        out: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=12) as pool:
            for sym, df in pool.map(fetch_one, symbols):
                if df is not None:
                    out[sym] = df
        return out

    def grouped_daily(self, day: str) -> dict[str, dict]:
        """One call -> every US stock's OHLCV for `day` (YYYY-MM-DD).

        Returns {} on non-trading days. This is the light-tick workhorse.
        """
        data = _get(_GROUPED.format(day=day, key=self.api_key), timeout=60)
        out: dict[str, dict] = {}
        for row in data.get("results") or []:
            sym = row.get("T")
            if sym:
                out[sym] = {"open": row.get("o"), "high": row.get("h"),
                            "low": row.get("l"), "close": row.get("c"),
                            "volume": row.get("v")}
        return out

    def latest_grouped(self, max_back: int = 7) -> tuple[str, dict[str, dict]]:
        """Walk back from today to the most recent trading day with data."""
        d = date.today()
        for _ in range(max_back):
            try:
                bars = self.grouped_daily(d.isoformat())
            except Exception:
                bars = {}
            if len(bars) > 100:          # a real trading day, not a holiday
                return d.isoformat(), bars
            d -= timedelta(days=1)
        return "", {}

    def last_price(self, symbol: str) -> float | None:
        hist = self.history([symbol], period="1y",
                            start=(date.today() - timedelta(days=7)).isoformat())
        df = hist.get(symbol)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])
