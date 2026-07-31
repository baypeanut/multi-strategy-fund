"""IBKR paper-execution mirror (E24) - S4's liquid equity core, real fills.

The internal $3M paper books remain the canonical experiment (their history IS
the horse race - never rescale them mid-run). This module mirrors S4's FINAL
post-governor equity weights onto the real IBKR paper account (DU*, ~$1M) to
buy execution realism: real queue-at-open fills and real commission data to
calibrate the modeled cost surface (E19/E23).

Deliberate scope (head-quant, 2026-07-20):
- Weights are dimensionless -> applied to IBKR's own NetLiquidation ($1M);
  no config/NAV change to the internal books.
- EQUITIES ONLY: our crypto universe is USDT pairs, not tradeable at IBKR;
  the crypto fraction simply sits in cash, so the IBKR book tracks the
  S4-equity-sleeve, and is reported as exactly that.
- TOP-N slice (default 100 by |w|): the liquid core gives real fill data
  where it matters; weights are NOT renormalized (risk must not change) and
  the tail stays cash. Names that drop out of the slice are exited.
- SAFETY RAILS (independent of the governor): DU-prefix account guard (this
  module refuses to talk to a non-paper account, ever), account-id match,
  gross cap abort, per-name cap clip, max-orders fuse, min-trade dust filter.
  The runtime only hands us post-governor/post-halt weights, so a halt latch
  arrives here as an all-zero book -> full flatten.
At real money, human order confirmation gets inserted here (see
agent_governance / real_money_locked in config.yaml).

`ib_async` is imported lazily so the pure planner stays testable offline.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlannedOrder:
    symbol: str          # our symbol (e.g. "BRK.B")
    action: str          # "BUY" | "SELL"
    quantity: int        # whole shares, > 0
    est_price: float | None
    est_notional: float | None


class MirrorAborted(Exception):
    """Raised when a safety rail trips - no orders may be placed."""


def ib_symbol(sym: str) -> str:
    """Polygon-style class shares ("BRK.B") -> IBKR spelling ("BRK B")."""
    return sym.replace(".", " ")


def our_symbol(ib_sym: str) -> str:
    return ib_sym.replace(" ", ".")


def is_equity(sym: str) -> bool:
    return "/" not in sym


def assert_paper_account(account: str, expected: str) -> None:
    """Hard guard: only ever touch the configured DU* (simulated) account."""
    if not account.startswith("DU"):
        raise MirrorAborted(f"account {account} is not an IBKR paper (DU*) account")
    if expected and account != expected:
        raise MirrorAborted(f"account {account} != configured {expected}")


def slice_targets(
    target_weights: dict[str, float],
    *,
    top_n: int = 100,
    max_position: float = 0.05,
) -> dict[str, float]:
    """Equity-only top-N slice with per-name clip (same selection as the planner)."""
    eq = {s: w for s, w in target_weights.items()
          if is_equity(s) and abs(w) > 1e-9}
    ranked = sorted(eq, key=lambda s: abs(eq[s]), reverse=True)[:max(top_n, 0)]
    return {s: max(-max_position, min(max_position, eq[s])) for s in ranked}


def book_view(
    nav: float,
    positions: dict[str, int],
    prices: dict[str, float],
    target_weights: dict[str, float],
    *,
    top_n: int = 100,
    max_position: float = 0.05,
) -> dict:
    """Dashboard-ready IBKR book: actual shares/MV/weights vs S4 slice targets.

    Pure function - no network. Used by sync/refresh so the frontend can show
    the same numbers the broker account holds, side-by-side with intended w.
    """
    targets = slice_targets(target_weights, top_n=top_n, max_position=max_position)
    rows: list[dict] = []
    long_mv = short_mv = 0.0
    for sym in set(positions) | set(targets):
        shares = int(positions.get(sym, 0))
        px = prices.get(sym)
        px = float(px) if px is not None and px > 0 else None
        mv = (shares * px) if (px is not None and shares != 0) else 0.0
        w_act = (mv / nav) if nav > 0 else 0.0
        w_tgt = float(targets.get(sym, 0.0))
        if shares > 0:
            long_mv += mv
        elif shares < 0:
            short_mv += abs(mv)
        if shares == 0 and abs(w_tgt) < 1e-12:
            continue
        rows.append({
            "symbol": sym,
            "shares": shares,
            "price": None if px is None else round(px, 4),
            "mv": round(mv, 2),
            "weight": round(w_act, 5),
            "target_w": round(w_tgt, 5),
            "drift_w": round(w_act - w_tgt, 5),
        })
    rows.sort(key=lambda r: (-abs(r["mv"]), -abs(r["target_w"]), r["symbol"]))
    invested = long_mv + short_mv
    return {
        "positions": rows,
        "n_positions": sum(1 for r in rows if r["shares"] != 0),
        "n_long": sum(1 for r in rows if r["shares"] > 0),
        "n_short": sum(1 for r in rows if r["shares"] < 0),
        "long_mv": round(long_mv, 2),
        "short_mv": round(short_mv, 2),
        "gross": round(invested / nav, 4) if nav > 0 else 0.0,
        "net": round((long_mv - short_mv) / nav, 4) if nav > 0 else 0.0,
        "cash_frac": round(max(0.0, 1.0 - invested / nav), 4) if nav > 0 else 1.0,
        "slice_gross_target": round(sum(abs(w) for w in targets.values()), 4),
        "max_abs_drift": round(max((abs(r["drift_w"]) for r in rows), default=0.0), 5),
    }


def plan_equity_mirror(
    target_weights: dict[str, float],
    nav: float,
    prices: dict[str, float],
    current_shares: dict[str, int],
    *,
    top_n: int = 100,
    min_trade_usd: float = 200.0,
    max_position: float = 0.05,
    max_gross: float = 1.0,
    max_orders: int = 150,
) -> tuple[list[PlannedOrder], dict]:
    """Pure order planner: FINAL S4 weights -> whole-share delta orders.

    Selection: equities only, top_n by |w|; per-name |w| clipped to
    max_position (defense in depth); slice gross must be <= max_gross.
    Held names outside the slice get exit orders. Dust (< min_trade_usd)
    is skipped - except full exits with no price, which are always sent
    (quantity is known from the position itself).
    """
    targets = slice_targets(target_weights, top_n=top_n, max_position=max_position)

    gross = sum(abs(w) for w in targets.values())
    if gross > max_gross + 1e-6:
        raise MirrorAborted(f"slice gross {gross:.3f} > cap {max_gross}")
    if nav <= 0:
        raise MirrorAborted(f"non-positive NAV {nav}")

    orders: list[PlannedOrder] = []
    skipped_dust = 0
    skipped_no_price: list[str] = []

    for sym in set(targets) | set(current_shares):
        cur = int(current_shares.get(sym, 0))
        w = targets.get(sym, 0.0)
        px = prices.get(sym)
        if w != 0.0 and (px is None or px <= 0):
            skipped_no_price.append(sym)      # can't size an entry - skip
            continue
        tgt = int((w * nav) / px) if w != 0.0 else 0
        delta = tgt - cur
        if delta == 0:
            continue
        notional = abs(delta) * px if px and px > 0 else None
        if notional is not None and notional < min_trade_usd:
            skipped_dust += 1
            continue
        orders.append(PlannedOrder(
            symbol=sym, action="BUY" if delta > 0 else "SELL",
            quantity=abs(delta), est_price=px, est_notional=notional))

    if len(orders) > max_orders:
        raise MirrorAborted(f"{len(orders)} orders > fuse {max_orders} - refusing")

    info = {"slice_names": len(targets), "slice_gross": round(gross, 4),
            "skipped_dust": skipped_dust, "skipped_no_price": skipped_no_price,
            "n_orders": len(orders)}
    return orders, info


class IBKRBroker:
    """Thin connection wrapper; all sizing logic lives in the pure planner."""

    def __init__(self, cfg: dict):
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 4002))
        self.client_id = int(cfg.get("client_id", 7))
        self.account = cfg.get("account", "")
        self.top_n = int(cfg.get("top_n", 100))
        self.min_trade_usd = float(cfg.get("min_trade_usd", 200.0))
        self.max_orders = int(cfg.get("max_orders_per_sync", 150))
        self.limit_buffer = float(cfg.get("limit_buffer", 0.015))
        self._ib = None
        self._errors: list[str] = []

    # --- connection ------------------------------------------------------
    def connect(self):
        from ib_async import IB
        ib = IB()
        ib.errorEvent += self._on_error
        ib.connect(self.host, self.port, clientId=self.client_id, timeout=25)
        accounts = ib.managedAccounts()
        if not accounts:
            ib.disconnect()
            raise MirrorAborted("no managed accounts returned")
        assert_paper_account(accounts[0], self.account)
        self._ib = ib
        return ib

    def _on_error(self, reqId, code, msg, *args):
        # informational chatter is not an error: 21xx farm-connection notices
        # and 2161 (price-band regulatory notice on resting limit orders)
        if code not in (2100, 2103, 2104, 2105, 2106, 2107, 2108, 2158, 2161):
            self._errors.append(f"{code}: {str(msg)[:90]}")

    def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()
            self._ib = None

    # --- account state ---------------------------------------------------
    def snapshot(self) -> tuple[float, dict[str, int]]:
        nav = 0.0
        for row in self._ib.accountSummary():
            if row.tag == "NetLiquidation":
                nav = float(row.value)
        positions: dict[str, int] = {}
        for p in self._ib.positions():
            if p.contract.secType == "STK":
                positions[our_symbol(p.contract.symbol)] = int(p.position)
        return nav, positions

    def recent_fills(self) -> list[dict]:
        """Today's executions (for cost-model calibration)."""
        from ib_async import ExecutionFilter
        fills = self._ib.reqExecutions(ExecutionFilter())
        out = []
        for f in fills:
            cr = getattr(f, "commissionReport", None)
            out.append({
                "id": f.execution.execId,
                "symbol": our_symbol(f.contract.symbol),
                "side": f.execution.side,
                "shares": int(f.execution.shares),
                "price": float(f.execution.price),
                "time": str(f.execution.time),
                "commission": float(cr.commission) if cr else None,
            })
        return out

    # --- orders ----------------------------------------------------------
    def place(self, orders: list[PlannedOrder]) -> dict:
        """Marketable LIMIT orders at last close ± buffer.

        Paper accounts without market-data subscriptions REJECT market orders
        outright (IBKR error 202: the simulator cannot price them) - learned
        live on night one. Marketable limits solve that AND bound slippage
        for free; anything unfilled shows up as a delta again at the next
        sync, so the mirror is self-healing. Price-less exits (halt-flatten
        with degraded data) fall back to MKT and may 202 - reported, and
        retried by the next sync.
        """
        from ib_async import LimitOrder, MarketOrder, Stock
        placed, failed = 0, []
        for o in orders:
            try:
                contract = Stock(ib_symbol(o.symbol), "SMART", "USD")
                q = self._ib.qualifyContracts(contract)
                if not q:
                    failed.append(f"{o.symbol}: unqualified")
                    continue
                if o.est_price and o.est_price > 0:
                    buf = 1.0 + self.limit_buffer if o.action == "BUY" \
                        else 1.0 - self.limit_buffer
                    lmt = round(o.est_price * buf, 2)
                    order = LimitOrder(o.action, o.quantity, lmt)
                else:
                    order = MarketOrder(o.action, o.quantity)
                order.tif = "DAY"
                order.outsideRth = False
                self._ib.placeOrder(q[0], order)
                placed += 1
            except Exception as exc:
                failed.append(f"{o.symbol}: {type(exc).__name__}: {exc}")
        self._ib.sleep(3)          # let order-state and error msgs flush
        return {"placed": placed, "failed": failed[:20],
                "api_errors": self._errors[:20]}

    def cancel_open_orders(self) -> int:
        """Cancel every working order on this account; return how many (E37).

        Only orders for the mirror's own account are touched. Fail-soft: the
        mirror must still sync if cancellation is unavailable - a failure here
        degrades to the old stacking behavior for one pass, never to a dead
        loop or an unplanned position.
        """
        n = 0
        try:
            for trade in self._ib.openTrades():
                order, contract = trade.order, trade.contract
                if getattr(order, "account", None) not in (None, "", self.account):
                    continue
                if not trade.isActive():
                    continue
                try:
                    self._ib.cancelOrder(order)
                    n += 1
                except Exception as exc:
                    self._errors.append(
                        f"cancel {getattr(contract, 'symbol', '?')}: "
                        f"{type(exc).__name__}: {exc}")
            if n:
                self._ib.sleep(2)          # let cancellations settle before snapshot
        except Exception as exc:
            self._errors.append(f"cancel_open_orders: {type(exc).__name__}: {exc}")
        return n

    # --- orchestration ---------------------------------------------------
    def refresh(self, target_weights: dict[str, float], prices: dict[str, float],
                *, max_position: float = 0.05) -> dict:
        """Read-only account snapshot for the dashboard (no orders)."""
        from datetime import datetime, timezone
        self._errors = []
        self.connect()
        try:
            nav, positions = self.snapshot()
            fills = self.recent_fills()
            view = book_view(nav, positions, prices, target_weights,
                             top_n=self.top_n, max_position=max_position)
            return {
                "last_refresh": datetime.now(timezone.utc).isoformat(),
                "account": self.account,
                "nav": round(nav, 2),
                "n_positions": view["n_positions"],
                "positions": view["positions"],
                "book": {k: v for k, v in view.items() if k != "positions"},
                # THE WHOLE DAY, uncapped (E36). reqExecutions only covers the
                # current day, so anything dropped here is unbackfillable. The
                # [-25:] cap (pre-P0011) then the [-300:] cap both truncated
                # mid-burst: 2026-07-27 saw 4019 executions against a 300 cap
                # and ~83% never reached the E23 dataset. The runtime dedupes
                # by execId against a ledger-seeded set, so reporting the full
                # day costs one pass over already-fetched objects.
                "fills_today": fills,
                "n_fills_today": len(fills),
                "api_errors": self._errors[:20],
                "mode": "refresh",
            }
        finally:
            self.disconnect()

    def sync(self, target_weights: dict[str, float], prices: dict[str, float],
             *, execute: bool, max_position: float = 0.05,
             max_gross: float = 1.0) -> dict:
        """One full mirror pass. Returns a compact, state-storable report."""
        from datetime import datetime, timezone
        self._errors = []
        self.connect()
        try:
            # E37: cancel the previous generation FIRST. The planner sizes
            # deltas against FILLED positions, so a still-live order from an
            # earlier sync is invisible to it and the same delta is ordered
            # again. Orders are tif=DAY and the mirror syncs ~every 2h, so a
            # closed-market night stacked ~9-15 copies of every order; at the
            # 13:30 open they all filled at once and the next sync violently
            # unwound the overshoot. Measured 2026-07-27: $20.25M gross traded
            # on a $979k NAV (20.7x) for $613k of net repositioning - 97% pure
            # round-trip. Cancel-then-plan makes at most one generation live,
            # which is what "unfilled shows up as a delta again" always assumed.
            cancelled = self.cancel_open_orders() if execute else 0
            nav, positions = self.snapshot()
            fills = self.recent_fills()
            orders, info = plan_equity_mirror(
                target_weights, nav, prices, positions,
                top_n=self.top_n, min_trade_usd=self.min_trade_usd,
                max_position=max_position, max_gross=max_gross,
                max_orders=self.max_orders)
            info["cancelled_stale"] = cancelled
            result = {"placed": 0, "failed": [], "api_errors": []}
            if execute and orders:
                result = self.place(orders)
                # re-read so the dashboard sees post-submit holdings / open orders effect
                nav, positions = self.snapshot()
                fills = self.recent_fills()
            view = book_view(nav, positions, prices, target_weights,
                             top_n=self.top_n, max_position=max_position)
            return {
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "last_refresh": datetime.now(timezone.utc).isoformat(),
                "account": self.account, "nav": round(nav, 2),
                "n_positions": view["n_positions"],
                "positions": view["positions"],
                "book": {k: v for k, v in view.items() if k != "positions"},
                "mode": "execute" if execute else "dry_run",
                "plan": info,
                "orders_placed": result["placed"],
                "order_preview": [
                    f"{o.action} {o.quantity} {o.symbol} @~{o.est_price}"
                    for o in orders[:12]],
                "failed": result["failed"],
                "api_errors": result["api_errors"] + self._errors[:20],
                "fills_today": fills,          # whole day, uncapped - see refresh
                "n_fills_today": len(fills),
            }
        finally:
            self.disconnect()
