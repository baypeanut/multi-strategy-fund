"""Manual IBKR mirror CLI (E24).

  venv/bin/python scripts/ibkr_sync.py --recon     # account vs S4 targets, read-only
  venv/bin/python scripts/ibkr_sync.py --plan      # compute orders, print, don't send
  venv/bin/python scripts/ibkr_sync.py --execute   # place the orders (paper account)

Reads S4's FINAL weights and the latest marking prices from data/state.json
the same inputs the runtime's automatic mirror uses.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.broker.ibkr_broker import IBKRBroker, plan_equity_mirror
from core.config import CONFIG


def load_inputs() -> tuple[dict, dict]:
    state = json.loads(Path("data/state.json").read_text())
    weights = state["systems"]["s4"].get("weights", {})
    prices = state.get("prev_prices", {})
    if state.get("halt_latched"):
        print("!! halt latched - mirroring a FLAT book")
        weights = {}
    return weights, prices


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--recon", action="store_true")
    g.add_argument("--plan", action="store_true")
    g.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    cfg = dict(CONFIG.get("ibkr", {}))
    # manual CLI uses its own clientId so it can never collide with the
    # runtime's automatic mirror (which owns the configured id)
    cfg["client_id"] = int(cfg.get("client_id", 7)) + 10
    weights, prices = load_inputs()
    broker = IBKRBroker(cfg)

    if args.recon:
        broker.connect()
        try:
            nav, positions = broker.snapshot()
            fills = broker.recent_fills()
        finally:
            broker.disconnect()
        print(f"account {cfg.get('account')}  NAV ${nav:,.0f}  "
              f"positions {len(positions)}  fills today {len(fills)}")
        eq_targets = {s: w for s, w in weights.items() if "/" not in s}
        tgt_top = sorted(eq_targets, key=lambda s: abs(eq_targets[s]),
                         reverse=True)[:int(cfg.get("top_n", 100))]
        missing = [s for s in tgt_top if s not in positions]
        stray = [s for s in positions if s not in tgt_top]
        print(f"slice size {len(tgt_top)} | not yet held {len(missing)} | "
              f"held-but-out-of-slice {len(stray)}")
        for f in fills[-10:]:
            print(f"  fill {f['side']} {f['shares']} {f['symbol']} @ {f['price']}"
                  f" comm={f['commission']}")
        return

    if args.plan:
        broker.connect()
        try:
            nav, positions = broker.snapshot()
        finally:
            broker.disconnect()
        orders, info = plan_equity_mirror(
            weights, nav, prices, positions,
            top_n=int(cfg.get("top_n", 100)),
            min_trade_usd=float(cfg.get("min_trade_usd", 200.0)),
            max_position=CONFIG.risk.max_position,
            max_gross=CONFIG.risk.max_gross,
            max_orders=int(cfg.get("max_orders_per_sync", 150)))
        print(f"NAV ${nav:,.0f} | {info}")
        for o in orders[:200]:
            print(f"  {o.action:4} {o.quantity:>6} {o.symbol:<8} "
                  f"@~{o.est_price}  (${o.est_notional:,.0f})"
                  if o.est_notional else
                  f"  {o.action:4} {o.quantity:>6} {o.symbol:<8} (exit, px unknown)")
        return

    report = broker.sync(weights, prices, execute=True,
                         max_position=CONFIG.risk.max_position,
                         max_gross=CONFIG.risk.max_gross)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
