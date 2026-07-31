"""Structured briefing - the ONLY information the discretionary PM may use.

The PM (LLM or heuristic) sees this dict and nothing else. The wrapper later
enforces that proposals reference only symbols present here (citation check).
"""
from __future__ import annotations

import pandas as pd


def build_briefing(
    universe: list[str],
    prices: dict[str, pd.DataFrame],
    s1_scores: pd.Series,
    s2_scores: pd.Series,
    vols: pd.Series,
    regime,
    positions: dict[str, float] | None = None,
    risk_budget_used: float = 0.0,
) -> dict:
    positions = positions or {}
    names = []
    for sym in universe:
        df = prices.get(sym)
        if df is None or df.empty:
            continue
        names.append({
            "symbol": sym,
            "price": round(float(df["close"].iloc[-1]), 4),
            "ret_21d": round(float(df["close"].iloc[-1] / df["close"].iloc[-22] - 1), 4)
            if len(df) > 22 else None,
            "vol_annual": round(float(vols.get(sym, float("nan"))), 4),
            "s1_quant_z": round(float(s1_scores.get(sym, 0.0)), 3),
            "s2_news_z": round(float(s2_scores.get(sym, 0.0)), 3),
            "current_weight": round(float(positions.get(sym, 0.0)), 4),
        })
    return {
        "regime": regime.as_dict() if hasattr(regime, "as_dict") else regime,
        "risk_budget_used": round(risk_budget_used, 3),
        "universe_size": len(names),
        "names": names,
    }
