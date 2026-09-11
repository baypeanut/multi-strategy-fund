"""QuantEngine — System 1 (deterministic, no LLM).

Ties data -> volatility -> signals -> combination -> covariance -> target
weights. This is the control-group book: fully reproducible, backtestable, $0
inference cost. Operates cross-sectionally on a set of OHLCV histories.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import signals as sig
from .covariance import ledoit_wolf_cov
from .portfolio import construct_signal_portfolio
from .volatility import log_returns, yang_zhang_vol


def build_panels(histories: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (close_panel, volume_panel) aligned on a common date index."""
    closes = {s: df["close"] for s, df in histories.items() if not df.empty}
    vols = {s: df["volume"] for s, df in histories.items() if not df.empty}
    close_panel = pd.DataFrame(closes).sort_index()
    volume_panel = pd.DataFrame(vols).sort_index()
    return close_panel, volume_panel


@dataclass
class QuantResult:
    weights: pd.Series                    # target weights per symbol
    combined_score: pd.Series             # blended signal z-scores
    vols: pd.Series                       # annualized vol per symbol
    signal_scores: dict[str, pd.Series] = field(default_factory=dict)
    gross: float = 0.0
    net: float = 0.0


@dataclass
class QuantEngine:
    target_vol: float = 0.10
    max_position: float = 0.05
    max_gross: float = 1.00
    market_neutral: bool = True
    signal_weights: dict[str, float] = field(
        default_factory=lambda: {"momentum": 1.0, "reversal": 0.5, "low_vol": 0.5}
    )

    def generate(self, histories: dict[str, pd.DataFrame], min_history: int = 60) -> QuantResult:
        histories = {s: d for s, d in histories.items() if len(d) >= min_history}
        close, _ = build_panels(histories)
        rets = log_returns(close)

        # annualized vol per name (Yang-Zhang on OHLCV)
        vols = pd.Series(
            {s: yang_zhang_vol(histories[s], window=30, annualize=True) for s in histories}
        ).replace(0, pd.NA).dropna()

        # individual signals -> cross-sectional z
        raw_signals = {
            "momentum": sig.momentum_12_1(close, vols),
            "reversal": sig.short_term_reversal(close, n=5),
            "low_vol": sig.low_volatility(vols),
        }
        z_signals = {k: sig.cross_sectional_zscore(v) for k, v in raw_signals.items()}

        # weighted blend
        all_syms = close.columns
        combined = pd.Series(0.0, index=all_syms)
        wsum = 0.0
        for name, w in self.signal_weights.items():
            combined = combined.add(w * z_signals[name].reindex(all_syms).fillna(0.0), fill_value=0.0)
            wsum += abs(w)
        if wsum > 0:
            combined /= wsum

        cov = ledoit_wolf_cov(rets)

        weights = construct_signal_portfolio(
            signal_scores=combined,
            cov=cov,
            vols=vols,
            target_vol=self.target_vol,
            max_position=self.max_position,
            max_gross=self.max_gross,
            market_neutral=self.market_neutral,
        )
        return QuantResult(
            weights=weights.sort_values(ascending=False),
            combined_score=combined.reindex(weights.index),
            vols=vols,
            signal_scores=z_signals,
            gross=float(weights.abs().sum()),
            net=float(weights.sum()),
        )
