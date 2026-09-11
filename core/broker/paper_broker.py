"""Paper broker — simulates fills with realistic costs against the ledger.

Execution model: an order to trade `qty` (signed) of a symbol at a reference
mid price fills at a slipped price:

    fill_price = mid * (1 + sign * slippage)        # spread + impact
    commission = |qty * mid| * commission_fraction

The broker never lets an order breach the liquidity cap (configurable share of
ADV); oversized orders are clipped and flagged. All accounting goes to Ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from ..ledger.ledger import Fill, Ledger
from .costs import CostModel


@dataclass
class OrderResult:
    symbol: str
    requested_qty: float
    filled_qty: float
    mid_price: float
    fill_price: float
    commission: float
    slippage_bps: float
    clipped: bool
    note: str = ""


class PaperBroker:
    def __init__(
        self,
        ledger: Ledger,
        cost_models: dict[str, CostModel],
        liquidity_adv_cap: float = 0.10,
    ) -> None:
        self.ledger = ledger
        self.cost_models = cost_models      # {"equity": CostModel, "crypto": CostModel}
        self.liquidity_adv_cap = liquidity_adv_cap

    def submit(
        self,
        symbol: str,
        qty: float,                 # signed: + buy, - sell
        mid_price: float,
        asset_class: str,
        adv: float,
        daily_vol: float,
        ts: datetime | None = None,
    ) -> OrderResult:
        ts = ts or datetime.now(timezone.utc)
        cost_model = self.cost_models[asset_class]
        if not math.isfinite(adv) or adv <= 0:
            raise ValueError("finite positive ADV required for liquidity cap")
        if not math.isfinite(mid_price) or mid_price <= 0 or not math.isfinite(qty):
            raise ValueError("finite quantity and positive price required")
        if not math.isfinite(daily_vol) or daily_vol < 0:
            raise ValueError("finite nonnegative daily_vol required")
        requested_qty = qty

        # --- liquidity cap: clip order to a share of ADV ----------------
        clipped = False
        max_value = self.liquidity_adv_cap * adv
        req_value = abs(qty) * mid_price
        if req_value > max_value:
            clipped = True
            scale = max_value / req_value
            qty = qty * scale

        trade_value = abs(qty) * mid_price
        breakdown = cost_model.estimate(trade_value, adv, daily_vol)

        sign = 1.0 if qty > 0 else -1.0
        fill_price = mid_price * (1.0 + sign * breakdown.slippage)
        commission = trade_value * breakdown.commission

        self.ledger.apply_fill(
            Fill(ts=ts, symbol=symbol, qty=qty, price=fill_price,
                 commission=commission, note=("clipped" if clipped else ""))
        )

        return OrderResult(
            symbol=symbol,
            requested_qty=requested_qty,
            filled_qty=qty,
            mid_price=mid_price,
            fill_price=fill_price,
            commission=commission,
            slippage_bps=breakdown.slippage / 1e-4,
            clipped=clipped,
        )
