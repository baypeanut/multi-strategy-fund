"""Close-to-close backtest engine for a cross-sectional book.

At each decision date the engine sees history through that close; new holdings
earn the next bar's return. This assumes execution at the decision close with
modeled costs, not a next-open or latency-aware fill simulation. Chronological
slicing alone does not establish out-of-sample validity of the selected
universe, parameters or previously inspected evaluation period.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.broker.costs import FALLBACK_ADV, FALLBACK_DVOL, CostModel
from systems.s1_quant.engine import QuantEngine

# Same neutral assumptions the live cost path uses for a name with no volume
# data (runtime/live.py). Kept in sync deliberately: a backtest that costs
# trades differently from the live book is not measuring the live book.
# Imported from core/broker/costs.py, not restated. A backtest that costs
# trades differently from the live book is not measuring the live book, and
# sharing one definition is the only way to mean that rather than promise it.


@dataclass
class BacktestResult:
    equity: pd.Series          # equity curve ($)
    net_returns: pd.Series     # daily net returns
    gross_returns: pd.Series   # daily returns before cost
    weights: pd.DataFrame      # end-of-day held weights, including price drift
    turnover: pd.Series        # per-rebalance turnover
    total_cost: float          # total $ cost paid
    nav0: float
    n_cost_fallback: int = 0   # trades costed without ADV/vol data (E48d)

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

    if not np.isfinite(nav0) or nav0 <= 0:
        raise ValueError("nav0 must be finite and positive")
    if warmup < 1 or rebalance_days < 1:
        raise ValueError("warmup and rebalance_days must be positive")

    # A held share count stays fixed between decisions. Constant target weights
    # would silently rebalance every day for free, changing both P&L and costs.
    asset_rets = close.pct_change(fill_method=None)
    adv_panel = (close * volume).rolling(20).mean()
    vol_panel = asset_rets.rolling(30).std()
    dates = close.index[warmup:]
    W = pd.DataFrame(0.0, index=dates, columns=close.columns)
    gross_ret = pd.Series(0.0, index=dates)
    net_ret = pd.Series(0.0, index=dates)
    equity = pd.Series(0.0, index=dates)
    turnover = {}
    held = pd.Series(0.0, index=close.columns)  # signed dollar market values
    nav = float(nav0)
    total_cost = 0.0
    n_cost_fallback = 0

    for loc in range(warmup, len(close)):
        d = close.index[loc]
        opening_nav = nav
        held_mask = held.abs() > 1e-12
        r = asset_rets.iloc[loc]
        valid_price = np.isfinite(close.iloc[loc]) & (close.iloc[loc] > 0)
        if ((~np.isfinite(r) | ~valid_price) & held_mask).any():
            bad = list(close.columns[(~np.isfinite(r) | ~valid_price) & held_mask])
            raise ValueError(f"missing or invalid held price at {d}: {bad}")
        pnl = float((held[held_mask] * r[held_mask]).sum())
        borrow = cost_model.borrow_cost(float(held[held < 0].abs().sum()))
        held.loc[held_mask] *= 1 + r[held_mask]
        nav += pnl - borrow
        total_cost += borrow
        if not np.isfinite(nav) or nav <= 0:
            raise ValueError(f"portfolio insolvent at {d}")

        if (loc - warmup) % rebalance_days == 0:
            hist_slice = {sym: histories[sym].loc[:d] for sym in close.columns}
            w = engine.generate(hist_slice).weights.reindex(close.columns).fillna(0.0)
            if not np.isfinite(w).all():
                raise ValueError(f"non-finite target at {d}")
            if ((w.abs() > 1e-12) & ~valid_price).any():
                raise ValueError(f"missing or invalid target price at {d}")
            adv = adv_panel.iloc[loc].to_numpy(dtype=float)
            dvol = vol_panel.iloc[loc].to_numpy(dtype=float)
            fallback = (~np.isfinite(adv) | (adv <= 0)
                        | ~np.isfinite(dvol) | (dvol < 0))
            adv = np.where(fallback, FALLBACK_ADV, adv)
            dvol = np.where(fallback, FALLBACK_DVOL, dvol)
            target = w.to_numpy(dtype=float)
            current = held.to_numpy(dtype=float)

            def trade_cost(post_cost_nav):
                traded = np.abs(target * post_cost_nav - current)
                costs = sum(value * cost_model.estimate(value, a, v).total
                            for value, a, v in zip(traded, adv, dvol)
                            if value > 1e-9)
                return float(costs)

            # Solve NAV_after = NAV_before - cost(target*NAV_after - holdings).
            # This also pays for price drift when today's target is unchanged.
            before_cost = nav
            after_cost = before_cost
            for _ in range(100):
                candidate = before_cost - trade_cost(after_cost)
                if not np.isfinite(candidate) or candidate <= 0:
                    raise ValueError(f"transaction costs exhaust NAV at {d}")
                if abs(candidate - after_cost) <= 1e-12 * max(before_cost, 1.0):
                    after_cost = candidate
                    break
                after_cost = candidate
            else:
                raise ValueError(f"transaction cost calculation did not converge at {d}")
            traded = np.abs(target * after_cost - current)
            turnover[d] = float(traded.sum() / before_cost)
            n_cost_fallback += int(((traded > 1e-9) & fallback).sum())
            total_cost += before_cost - after_cost
            nav = after_cost
            held = w * nav

        gross_ret.loc[d] = pnl / opening_nav
        net_ret.loc[d] = nav / opening_nav - 1.0
        equity.loc[d] = nav
        W.loc[d] = held / nav

    return BacktestResult(
        equity=equity, net_returns=net_ret, gross_returns=gross_ret,
        weights=W, turnover=pd.Series(turnover, dtype=float), total_cost=total_cost,
        nav0=nav0, n_cost_fallback=n_cost_fallback,
    )
