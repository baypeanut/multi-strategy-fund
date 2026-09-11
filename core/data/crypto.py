"""Crypto data provider (free) — ccxt against a public exchange.

Public OHLCV endpoints require no API key. Default exchange is binanceus for
US accessibility; override via config. Funding-rate reads (for the carry
signal) are added in the quant step.
"""
from __future__ import annotations

import pandas as pd

from .base import OHLCV_COLUMNS

# ccxt timeframe -> milliseconds, for paging if ever needed
_TF_MS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000}


class CryptoDataProvider:
    def __init__(self, exchange: str = "binanceus") -> None:
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover
            raise ImportError("ccxt not installed. `pip install ccxt`") from exc
        self._exchange = getattr(ccxt, exchange)({"enableRateLimit": True})

    def history(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        limit: int = 500,
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                ohlcv = self._exchange.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)
            except Exception:  # network/symbol issues — skip, don't crash the loop
                continue
            if not ohlcv:
                continue
            df = pd.DataFrame(
                ohlcv, columns=["ts", *OHLCV_COLUMNS]
            )
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            df = df.set_index("ts")[OHLCV_COLUMNS].astype(float)
            out[sym] = df
        return out

    def last_price(self, symbol: str) -> float | None:
        try:
            ticker = self._exchange.fetch_ticker(symbol)
            return float(ticker["last"])
        except Exception:
            return None

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        """Light-tick marking prices — one ticker call per symbol (8 names)."""
        out: dict[str, float] = {}
        for sym in symbols:
            px = self.last_price(sym)
            if px is not None:
                out[sym] = px
        return out
