"""Macro data provider (free) — FRED CSV endpoint, no API key required.

Used for regime indicators (VIX, term structure proxies, rates). The
fredgraph.csv endpoint is a public download that returns a date/value series.
"""
from __future__ import annotations

import io
import ssl
import urllib.request

import certifi
import pandas as pd

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

# Series we care about for regime detection
SERIES = {
    "VIX": "VIXCLS",          # CBOE Volatility Index
    "DGS10": "DGS10",         # 10Y Treasury yield
    "DGS2": "DGS2",           # 2Y Treasury yield
    "DFF": "DFF",             # Fed funds effective rate
}


class MacroDataProvider:
    def series(self, series_id: str, timeout: int = 20) -> pd.Series:
        url = _FRED_CSV.format(series_id=series_id)
        with urllib.request.urlopen(url, timeout=timeout, context=_SSL_CTX) as resp:
            raw = resp.read().decode("utf-8")
        df = pd.read_csv(io.StringIO(raw))
        # FRED returns columns: DATE/observation_date, <SERIES_ID>
        date_col = df.columns[0]
        val_col = df.columns[1]
        df[date_col] = pd.to_datetime(df[date_col])
        s = pd.to_numeric(df[val_col], errors="coerce")
        s.index = df[date_col]
        return s.dropna()

    def vix(self) -> pd.Series:
        return self.series(SERIES["VIX"])

    def term_spread_10y_2y(self) -> pd.Series:
        ten = self.series(SERIES["DGS10"])
        two = self.series(SERIES["DGS2"])
        return (ten - two).dropna()
