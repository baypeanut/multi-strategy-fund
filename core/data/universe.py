"""Investment universe definitions and liquidity filtering.

Equities: liquid US large/mid caps only (S&P 500 core). At $100M, small caps
are excluded for market-impact reasons; we keep the same universe in the $3M
proof phase for consistency.

Crypto: BTC/ETH plus the most liquid majors. No memecoins, no low-cap alts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .base import Asset, AssetClass

# --- Equity universe: liquid mega/large caps across sectors --------------
_EQUITY_SYMBOLS = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA",
    # Healthcare
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE",
    # Consumer
    "WMT", "PG", "KO", "PEP", "COST", "HD", "MCD", "NKE",
    # Industrials / Energy
    "XOM", "CVX", "CAT", "BA", "GE", "HON",
    # Communications / Other
    "NFLX", "DIS", "CRM", "ADBE", "AMD", "INTC",
    # User additions (2026-07-03) - passed the ADV>$50M rule at add time.
    # RCKT was requested but REJECTED: 20d ADV ~$10M, 5x below the floor.
    "MU", "MRVL", "TEM", "EOSE",
]

# --- Crypto universe: BTC/ETH + most liquid majors -----------------------
_CRYPTO_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    "XRP/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT",
]

# User-requested names (E8): always force-included in any generated universe.
USER_PICKS = ["MU", "MRVL", "TEM", "EOSE"]

_UNIVERSE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "universe" / "equities.json"


def _load_dynamic(path: Path = _UNIVERSE_FILE):
    """Load the generated top-ADV universe (scripts/build_universe.py output).

    Returns (symbols, sectors, adv_map) or None if no file - in which case the
    hardcoded 45-name seed below is used. The generated universe is FROZEN for
    the duration of an experiment run (clock discipline: regeneration only at
    config windows).
    """
    try:
        data = json.loads(path.read_text())
        rows = data["symbols"]
        symbols = [r["symbol"] for r in rows]
        sectors = {r["symbol"]: r.get("sector", "other") for r in rows}
        adv_map = {r["symbol"]: float(r.get("adv", 0.0)) for r in rows}
        if len(symbols) >= 45:
            return symbols, sectors, adv_map
    except Exception:
        pass
    return None


_dyn = _load_dynamic()
if _dyn:
    _ACTIVE_EQUITY_SYMBOLS, _DYN_SECTORS, ADV_MAP = _dyn
else:
    _ACTIVE_EQUITY_SYMBOLS, _DYN_SECTORS, ADV_MAP = _EQUITY_SYMBOLS, None, {}

EQUITY_UNIVERSE = [Asset(s, AssetClass.EQUITY) for s in _ACTIVE_EQUITY_SYMBOLS]
CRYPTO_UNIVERSE = [Asset(s, AssetClass.CRYPTO) for s in _CRYPTO_SYMBOLS]
FULL_UNIVERSE = EQUITY_UNIVERSE + CRYPTO_UNIVERSE


def rss_subset(top_n: int = 150) -> list[str]:
    """Names that get hourly RSS coverage: top-ADV slice + user picks.

    Hitting Yahoo with 500 requests/hour is abusive and slow; EDGAR 8-Ks cover
    the FULL universe for material events, headlines cover the liquid core.
    """
    if ADV_MAP:
        ranked = sorted(ADV_MAP, key=ADV_MAP.get, reverse=True)[:top_n]
        return list(dict.fromkeys(ranked + [p for p in USER_PICKS if p in ADV_MAP]))
    return [a.symbol for a in EQUITY_UNIVERSE]

# GICS-style sector map - feeds the governor's 25%-per-sector cap (SPEC §4).
# Crypto names all map to "crypto" (itself capped by the 20% allocation).
SECTORS: dict[str, str] = {
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "AVGO": "tech",
    "CRM": "tech", "ADBE": "tech", "AMD": "tech", "INTC": "tech",
    "MU": "tech", "MRVL": "tech",
    "AMZN": "consumer", "TSLA": "consumer", "WMT": "consumer", "PG": "consumer",
    "KO": "consumer", "PEP": "consumer", "COST": "consumer", "HD": "consumer",
    "MCD": "consumer", "NKE": "consumer",
    "GOOGL": "comms", "META": "comms", "NFLX": "comms", "DIS": "comms",
    "JPM": "financials", "BAC": "financials", "WFC": "financials",
    "GS": "financials", "MS": "financials", "V": "financials", "MA": "financials",
    "UNH": "healthcare", "JNJ": "healthcare", "LLY": "healthcare",
    "ABBV": "healthcare", "MRK": "healthcare", "PFE": "healthcare",
    "TEM": "healthcare",
    "XOM": "energy", "CVX": "energy", "EOSE": "energy",
    "CAT": "industrials", "BA": "industrials", "GE": "industrials",
    "HON": "industrials",
}
SECTORS.update({s: "crypto" for s in _CRYPTO_SYMBOLS})

# Generated universe: SIC-derived sectors as the base, curated map above as
# override (higher quality for those names), crypto always mapped.
if _DYN_SECTORS:
    _curated = dict(SECTORS)
    SECTORS = dict(_DYN_SECTORS)
    SECTORS.update(_curated)


def average_dollar_volume(ohlcv: pd.DataFrame, window: int = 20) -> float:
    """20-day average dollar volume (ADV) = mean(close * volume)."""
    if ohlcv is None or ohlcv.empty:
        return 0.0
    dollar_vol = (ohlcv["close"] * ohlcv["volume"]).tail(window)
    return float(dollar_vol.mean())


def liquidity_filter(
    assets: list[Asset],
    histories: dict[str, pd.DataFrame],
    min_adv_usd: float = 50_000_000,
) -> list[Asset]:
    """Keep only names whose 20d ADV clears the liquidity floor."""
    keep = []
    for asset in assets:
        adv = average_dollar_volume(histories.get(asset.symbol))
        if adv >= min_adv_usd:
            keep.append(asset)
    return keep
