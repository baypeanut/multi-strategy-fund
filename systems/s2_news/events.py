"""Event-study & earnings-surprise math.

- standardized_unexpected_earnings (SUE) and the PEAD drift direction.
- market_model_car: abnormal returns / CAR around an event date, using a
  market-model estimation window (alpha, beta vs a market proxy).
- time_decay_aggregate: combine many scored news items into per-symbol signals
  with exponential time decay.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd


def standardized_unexpected_earnings(actual_eps: float, estimates: list[float]) -> float:
    """SUE = (actual - mean(estimates)) / std(estimates)."""
    est = np.asarray(estimates, dtype=float)
    if est.size < 2:
        return 0.0
    sd = est.std(ddof=1)
    if sd == 0:
        return 0.0
    return float((actual_eps - est.mean()) / sd)


def market_model_car(
    asset_returns: pd.Series,
    market_returns: pd.Series,
    event_date,
    est_window: tuple[int, int] = (-250, -20),
    event_window: tuple[int, int] = (0, 5),
) -> dict:
    """Cumulative abnormal return around an event using the market model.

    Estimate alpha,beta on [event-250, event-20]; abnormal return over the
    event window is R_actual - (alpha + beta*R_market). Returns CAR and series.
    """
    a, m = asset_returns.align(market_returns, join="inner")
    a, m = a.dropna(), m.dropna()
    a, m = a.align(m, join="inner")
    idx = a.index
    if event_date not in idx:
        pos = idx.searchsorted(event_date)
    else:
        pos = idx.get_loc(event_date)
    if isinstance(pos, slice):
        pos = pos.start
    e0, e1 = est_window
    est_a = a.iloc[max(pos + e0, 0):pos + e1]
    est_m = m.iloc[max(pos + e0, 0):pos + e1]
    if len(est_a) < 30:
        return {"car": 0.0, "ar": pd.Series(dtype=float)}
    beta, alpha = np.polyfit(est_m.to_numpy(), est_a.to_numpy(), 1)

    w0, w1 = event_window
    ev_a = a.iloc[pos + w0:pos + w1 + 1]
    ev_m = m.iloc[pos + w0:pos + w1 + 1]
    ar = ev_a - (alpha + beta * ev_m)
    return {"car": float(ar.sum()), "ar": ar, "alpha": float(alpha), "beta": float(beta)}


def time_decay_aggregate(
    items: list[tuple[datetime, str, float, float]],
    now: datetime,
    tau_hours: float = 48.0,
) -> pd.Series:
    """Aggregate (timestamp, symbol, score, weight) into per-symbol signals.

    signal_i = sum_k w_k * score_k * exp(-dt_k / tau)
    """
    acc: dict[str, float] = {}
    for ts, sym, score, weight in items:
        dt_hours = max((now - ts).total_seconds() / 3600.0, 0.0)
        decay = math.exp(-dt_hours / tau_hours)
        acc[sym] = acc.get(sym, 0.0) + weight * score * decay
    return pd.Series(acc, dtype=float)
