"""Market regime indicators for the discretionary briefing.

Deterministic, computed from price panel + VIX. Used to scale the LLM PM's risk
appetite (risk-off / high-vol -> reduce gross).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Regime:
    trend: str            # "risk_on" | "risk_off"
    breadth: float        # fraction of names above 50d SMA
    vix: float
    vix_percentile: float # 1y percentile of latest VIX
    risk_scale: float     # [0,1] suggested gross multiplier

    def as_dict(self) -> dict:
        return {
            "trend": self.trend, "breadth": round(self.breadth, 3),
            "vix": round(self.vix, 2), "vix_percentile": round(self.vix_percentile, 3),
            "risk_scale": round(self.risk_scale, 3),
        }


def compute_regime(close_panel: pd.DataFrame, market: pd.Series, vix: pd.Series) -> Regime:
    # trend: market above its 200d SMA
    sma200 = market.rolling(200).mean().iloc[-1]
    trend = "risk_on" if market.iloc[-1] > sma200 else "risk_off"

    # breadth: fraction of names above their 50d SMA
    above = []
    for col in close_panel.columns:
        s = close_panel[col].dropna()
        if len(s) >= 50:
            above.append(s.iloc[-1] > s.rolling(50).mean().iloc[-1])
    breadth = float(np.mean(above)) if above else 0.5

    # VIX percentile over ~1y
    v = vix.dropna()
    latest_vix = float(v.iloc[-1])
    window = v.tail(252)
    vix_pct = float((window < latest_vix).mean())

    # risk scale: cut risk in risk_off, high VIX, or weak breadth
    scale = 1.0
    if trend == "risk_off":
        scale *= 0.6
    if vix_pct > 0.8:
        scale *= 0.6
    scale *= 0.5 + 0.5 * breadth        # weak breadth -> lower
    risk_scale = float(np.clip(scale, 0.0, 1.0))

    return Regime(trend, breadth, latest_vix, vix_pct, risk_scale)
