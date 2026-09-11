"""Equity data provider (free) — yfinance for history, ADV, and last price.

yfinance is free and sufficient for daily/4h-cadence strategies. We never use
it for sub-minute data. History is split/dividend-adjusted via auto_adjust.
"""
from __future__ import annotations

import pandas as pd

from .base import OHLCV_COLUMNS


class EquityDataProvider:
    PROVIDER_NAME = "yfinance"
    def __init__(self) -> None:
        try:
            import yfinance as yf  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "yfinance not installed. `pip install yfinance`"
            ) from exc

    def history(
        self,
        symbols: list[str],
        start: str | None = None,
        end: str | None = None,
        period: str = "2y",
        interval: str = "1d",
    ) -> dict[str, pd.DataFrame]:
        """Return {symbol: OHLCV DataFrame} with normalized lowercase columns."""
        import yfinance as yf

        out: dict[str, pd.DataFrame] = {}
        kwargs = dict(interval=interval, auto_adjust=True, progress=False)
        if start:
            kwargs.update(start=start, end=end)
        else:
            kwargs.update(period=period)

        raw = yf.download(symbols, group_by="ticker", **kwargs)
        for sym in symbols:
            try:
                df = raw[sym] if len(symbols) > 1 else raw
                df = df.rename(columns=str.lower)[OHLCV_COLUMNS].dropna()
                if not df.empty:
                    out[sym] = df
            except (KeyError, ValueError):
                continue
        return out

    def last_price(self, symbol: str) -> float | None:
        hist = self.history([symbol], period="5d")
        df = hist.get(symbol)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])
