"""Self-financing shadow books: mark held dollars, then pay for actual trades.

Weights and NAV encode signed holdings and cash at the previous mark. Persisting
the NEW weights after every mark preserves quantities between decisions, also
across restarts. Missing/quarantined marks leave that holding's value unchanged.
"""
from dataclasses import dataclass
import math
from typing import Callable

import pandas as pd


@dataclass
class MarkedBook:
    equity: float
    holdings: pd.Series
    pnl: pd.Series

    @property
    def weights(self) -> pd.Series:
        return self.holdings / self.equity


def mark_book(equity: float, weights: pd.Series, previous: dict,
              prices: dict) -> MarkedBook:
    holdings = weights.astype(float) * equity
    pnl = pd.Series(0.0, index=holdings.index)
    for sym, value in holdings.items():
        p0, p1 = previous.get(sym), prices.get(sym)
        if (p0 is not None and p1 is not None and math.isfinite(p0)
                and math.isfinite(p1) and p0 > 0 and p1 > 0):
            pnl[sym] = value * (p1 / p0 - 1.0)
    nav = equity + float(pnl.sum())
    if not math.isfinite(nav) or nav <= 0:
        raise ValueError("shadow book has non-positive/non-finite marked NAV")
    return MarkedBook(nav, holdings + pnl, pnl)


def settle_book(mark: MarkedBook, target: pd.Series,
                cost_fraction: Callable[[pd.Series, pd.Series, float], float],
                borrow_usd: float = 0.0, frozen: set[str] | None = None,
                hold_limits: tuple[float, float] | None = None) -> dict:
    """Solve post-cost target dollars; cash pays costs, held shares do not shrink.

    Unpriced holdings are frozen in dollars, even if a target requests an exit.
    A halt remains pending for them until a valid mark is available.
    """
    available = mark.equity - borrow_usd
    if not math.isfinite(available) or available <= 0 or borrow_usd < 0:
        raise ValueError("invalid NAV/borrow at settlement")
    symbols = mark.holdings.index.union(target.index)
    held = mark.holdings.reindex(symbols, fill_value=0.0)
    proposed = target.reindex(symbols, fill_value=0.0)
    if not all(math.isfinite(x) for x in proposed):
        raise ValueError("non-finite target")
    current = held / mark.equity
    frozen = frozen or set()
    if any(abs(held.get(sym, 0.0)) > 1e-8 for sym in frozen):
        # Unknown exits cannot fund new risk elsewhere. Keep valid reductions,
        # but disallow increases/reversals while any old holding is unpriced.
        for sym in symbols:
            old, desired = current[sym], proposed[sym]
            proposed[sym] = (math.copysign(min(abs(old), abs(desired)), old)
                             if old * desired > 0 else 0.0)
    hold_allowed = True
    if hold_limits and not held.empty:
        max_position, max_gross = hold_limits
        after_borrow = held.abs() / available
        hold_allowed = (float(after_borrow.max()) <= max_position + 1e-12
                        and float(after_borrow.sum()) <= max_gross + 1e-12)
    if hold_allowed and float((proposed - current).abs().sum()) < 1e-12:
        final, nav, trade_cost = held, available, 0.0
    else:
        nav = available
        for _ in range(100):
            final = proposed * nav
            for sym in frozen.intersection(symbols):
                final[sym] = held[sym]
            trade_cost = mark.equity * cost_fraction(
                current, final / mark.equity, mark.equity)
            next_nav = available - trade_cost
            if not math.isfinite(next_nav) or next_nav <= 0 or trade_cost < 0:
                raise ValueError("invalid transaction cost at settlement")
            if abs(next_nav - nav) <= max(1e-9, mark.equity * 1e-13):
                nav = next_nav
                break
            nav = next_nav
        else:
            raise ValueError("post-cost NAV did not converge")
    weights = final / nav
    return {"equity": nav,
            "weights": {str(s): float(v) for s, v in weights.items() if abs(v) > 1e-12},
            "holdings_usd": {str(s): float(v) for s, v in final.items() if abs(v) > 1e-8},
            "cash_usd": nav - float(final.sum()),
            "transaction_cost_usd": trade_cost, "borrow_cost_usd": borrow_usd,
            "turnover": float((final - held).abs().sum()) / mark.equity}
