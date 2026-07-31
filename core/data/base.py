"""Core data types shared across providers.

We keep price history as pandas DataFrames (OHLCV) because every downstream
quant computation (vol estimators, momentum, covariance) is vectorized over
them. Lightweight dataclasses describe assets and the universe.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AssetClass(str, Enum):
    EQUITY = "equity"
    CRYPTO = "crypto"


@dataclass(frozen=True)
class Asset:
    symbol: str               # canonical symbol, e.g. "AAPL" or "BTC/USDT"
    asset_class: AssetClass
    name: str = ""

    @property
    def is_crypto(self) -> bool:
        return self.asset_class is AssetClass.CRYPTO


# Standard OHLCV column contract every provider must return.
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
