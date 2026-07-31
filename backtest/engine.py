"""Walk-forward backtest engine for a cross-sectional book.

By construction this is out-of-sample: at each rebalance date the engine sees
ONLY data up to that date, produces target weights, and those weights earn the
*next* period's returns. Realistic costs (square-root impact + spread +
commission) are charged on turnover at each rebalance.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.broker.costs import CostModel
from systems.s1_quant.engine import QuantEngine


@dataclass
class BacktestResult:
    equity: pd.Series          # equity curve ($)
    net_returns: pd.Series     # daily net returns
    gross_returns: pd.Series   # daily returns before cost
    weights: pd.DataFrame      # daily target weights (ffilled)
    turnover: pd.Series        # per-rebalance turnover
    total_cost: float          # total $ cost paid
    nav0: float

    @property
    def cost_drag_annual(self) -> float:
        years = len(self.net_returns) / 252
        if years <= 0:
            return 0.0
        return (self.total_cost / self.nav0) / years


def run_backtest(
    histories: dict[str, pd.DataFrame],
    engine: QuantEngine,
    cost_model: CostModel,
    nav0: float = 1_000_000.0,
    rebalance_days: int = 21,
    warmup: int = 252,
) -> BacktestResult:
    closes = {s: d["close"] for s, d in histories.items() if len(d) > warmup}
    volumes = {s: d["volume"] for s, d in histories.items() if len(d) > warmup}
    close = pd.DataFrame(closes).sort_index()
    volume = pd.DataFrame(volumes).sort_index()
    if close.shape[0] <= warmup or close.shape[1] == 0:
        raise ValueError("not enough history for backtest")

    asset_rets = close.pct_change()
    adv_panel = (close * volume).rolling(20).mean()
    vol_panel = asset_rets.rolling(30).std()

    dates = close.index
    rb_locs = list(range(warmup, len(dates), rebalance_days))

    W = pd.DataFrame(index=dates, columns=close.columns, dtype=float)
    turnover = {}
    prev_w = pd.Series(0.0, index=close.columns)

    for loc in rb_locs:
        d = dates[loc]
        hist_slice = {s: histories[s].loc[:d] for s in close.columns}
        res = engine.generate(hist_slice)
        w = res.weights.reindex(close.columns).fillna(0.0)
        W.loc[d] = w
        turnover[d] = float((w - prev_w).abs().sum())
        prev_w = w

    W = W.ffill().fillna(0.0)
    W_eff = W.shift(1)  # trade on next day's returns

    gross_ret = (W_eff * asset_rets).sum(axis=1)

    # realistic cost on rebalance turnover
    cost_frac = pd.Series(0.0, index=dates)
    total_cost = 0.0
    nav = nav0
    for loc in rb_locs:
        d = dates[loc]
        w_now = W.loc[d]
        w_prev = W.iloc[loc - 1] if loc > 0 else pd.Series(0.0, index=close.columns)
        dollar_cost = 0.0
        for sym in close.columns:
            dw = float(w_now[sym] - w_prev[sym])
            if abs(dw) < 1e-9:
                continue
            trade_value = abs(dw) * nav
            adv = float(adv_panel.iloc[loc].get(sym, np.nan))
            dvol = float(vol_panel.iloc[loc].get(sym, np.nan))
            if not np.isfinite(adv) or adv <= 0 or not np.isfinite(dvol):
                # fallback: spread+commission only
                bd = cost_model.estimate(trade_value, adv=1e15, daily_vol=0.0)
            else:
                bd = cost_model.estimate(trade_value, adv=adv, daily_vol=dvol)
            dollar_cost += trade_value * (bd.slippage + bd.commission)
        cost_frac[d] = dollar_cost / nav if nav > 0 else 0.0
        total_cost += dollar_cost

    net_ret = (gross_ret - cost_frac).fillna(0.0)
    equity = nav0 * (1 + net_ret).cumprod()

    return BacktestResult(
        equity=equity, net_returns=net_ret, gross_returns=gross_ret.fillna(0.0),
        weights=W, turnover=pd.Series(turnover), total_cost=total_cost, nav0=nav0,
    )
