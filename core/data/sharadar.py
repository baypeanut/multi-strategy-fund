"""Sharadar (Nasdaq Data Link) provider - research lane, PIT/survivorship-clean.

Activated by NASDAQ_DATA_LINK_API_KEY. Used by backtests to fix audit finding
No. 2 (survivorship bias): SEP has delisted names, TICKERS carries listing
metadata so a point-in-time universe can be constructed. NOT used by the live
loop - research only.
"""
from __future__ import annotations

import io
import ssl
import urllib.request

import certifi
import pandas as pd

from ..env import get_key

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_BASE = "https://data.nasdaq.com/api/v3/datatables/SHARADAR/{table}.csv"


class SharadarProvider:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or get_key("NASDAQ_DATA_LINK_API_KEY")
        if not self.api_key:
            raise RuntimeError("NASDAQ_DATA_LINK_API_KEY missing")

    def _fetch_csv(self, table: str, params: str, timeout: int = 60) -> pd.DataFrame:
        url = f"{_BASE.format(table=table)}?{params}&api_key={self.api_key}"
        with urllib.request.urlopen(url, timeout=timeout, context=_SSL_CTX) as resp:
            return pd.read_csv(io.BytesIO(resp.read()))

    def prices(self, tickers: list[str], start: str, end: str) -> pd.DataFrame:
        """SEP daily prices (split-adjusted closeadj included), delisted-aware."""
        frames = []
        for i in range(0, len(tickers), 50):     # keep URLs bounded
            chunk = ",".join(tickers[i:i + 50])
            frames.append(self._fetch_csv(
                "SEP", f"ticker={chunk}&date.gte={start}&date.lte={end}"))
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df

    def tickers_meta(self) -> pd.DataFrame:
        """TICKERS table: listing dates, delisting, category - the PIT backbone."""
        return self._fetch_csv("TICKERS", "table=SEP")
