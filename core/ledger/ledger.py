"""Portfolio ledger — cash, positions, P&L, and the equity curve.

Single source of truth for accounting. Every fill mutates exactly one position
and cash. Equity is marked to provided prices on demand. Realized P&L uses
average-cost basis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math


@dataclass
class Position:
    symbol: str
    qty: float = 0.0            # signed: + long, - short
    avg_price: float = 0.0     # average entry price of the open position
    realized_pnl: float = 0.0

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.qty


@dataclass
class Fill:
    ts: datetime
    symbol: str
    qty: float                 # signed
    price: float               # execution price incl. slippage
    commission: float          # dollar commission
    note: str = ""


@dataclass
class Ledger:
    starting_cash: float
    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.starting_cash

    # --- mutations -------------------------------------------------------
    def apply_fill(self, fill: Fill) -> None:
        pos = self.positions.setdefault(fill.symbol, Position(fill.symbol))
        old_qty = pos.qty
        new_qty = old_qty + fill.qty

        # Cash impact: buying spends cash, selling raises it; commission always paid.
        self.cash -= fill.qty * fill.price
        self.cash -= fill.commission

        if old_qty == 0 or (old_qty > 0) == (fill.qty > 0):
            # opening or adding in same direction -> weighted-average price
            total_cost = pos.avg_price * abs(old_qty) + fill.price * abs(fill.qty)
            pos.avg_price = total_cost / abs(new_qty) if new_qty != 0 else 0.0
        else:
            # reducing / closing / flipping -> realize P&L on the closed amount
            closed = min(abs(fill.qty), abs(old_qty))
            direction = 1.0 if old_qty > 0 else -1.0
            pos.realized_pnl += direction * (fill.price - pos.avg_price) * closed
            if abs(fill.qty) > abs(old_qty):
                # position flipped; new avg price is the fill price
                pos.avg_price = fill.price
            elif new_qty == 0:
                pos.avg_price = 0.0
            # else: partial reduce, avg_price unchanged

        pos.qty = new_qty
        self.fills.append(fill)

    # --- valuation -------------------------------------------------------
    def _marked_values(self, prices: dict[str, float]) -> list[float]:
        values = []
        for sym, pos in self.positions.items():
            if pos.qty == 0:
                continue
            px = prices.get(sym)
            if px is None or not math.isfinite(px) or px <= 0:
                raise ValueError(f"missing or invalid price for open position {sym}")
            values.append(pos.market_value(px))
        return values

    def positions_value(self, prices: dict[str, float]) -> float:
        return sum(self._marked_values(prices))

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.positions_value(prices)

    def gross_exposure(self, prices: dict[str, float]) -> float:
        return sum(abs(value) for value in self._marked_values(prices))

    def mark(self, prices: dict[str, float], ts: datetime | None = None) -> float:
        eq = self.equity(prices)
        self.equity_curve.append((ts or datetime.now(timezone.utc), eq))
        return eq
