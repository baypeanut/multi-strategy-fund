"""Run System 1 (Quant) on the live universe and print the target book.

Fetches free data for the equity + crypto universe, runs the QuantEngine
separately per asset class (different vol regimes), blends 80/20, and prints
target weights. No orders are placed - this shows the book the engine wants.

Run: python scripts/run_quant.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.config import CONFIG
from core.data.crypto import CryptoDataProvider
from core.data.equities import EquityDataProvider
from core.data.universe import EQUITY_UNIVERSE, CRYPTO_UNIVERSE
from systems.s1_quant.engine import QuantEngine


def show(title, res, scale):
    print(f"\n=== {title} (scaled x{scale:.2f}) ===")
    print(f"gross={res.gross:.3f} net={res.net:+.3f}  (pre-scale)")
    w = (res.weights * scale).sort_values(ascending=False)
    longs = w[w > 1e-4].head(6)
    shorts = w[w < -1e-4].tail(6)
    print("  LONGS:")
    for s, val in longs.items():
        print(f"    {s:10s} {val:+.3%}  vol={res.vols.get(s, float('nan')):.1%}")
    print("  SHORTS:")
    for s, val in shorts.items():
        print(f"    {s:10s} {val:+.3%}  vol={res.vols.get(s, float('nan')):.1%}")


def main():
    eq_prov = EquityDataProvider()
    cx_prov = CryptoDataProvider(exchange=CONFIG.data.crypto_exchange)

    print("Fetching equity history (yfinance)...")
    eq_syms = [a.symbol for a in EQUITY_UNIVERSE]
    eq_hist = eq_prov.history(eq_syms, period="2y", interval="1d")
    print(f"  got {len(eq_hist)}/{len(eq_syms)} names")

    print("Fetching crypto history (ccxt)...")
    cx_syms = [a.symbol for a in CRYPTO_UNIVERSE]
    cx_hist = cx_prov.history(cx_syms, timeframe="1d", limit=500)
    print(f"  got {len(cx_hist)}/{len(cx_syms)} names")

    eng_eq = QuantEngine(target_vol=CONFIG.risk.vol_target_annual,
                         max_position=CONFIG.risk.max_position,
                         max_gross=CONFIG.risk.max_gross, market_neutral=True)
    eng_cx = QuantEngine(target_vol=CONFIG.risk.vol_target_annual,
                         max_position=CONFIG.risk.max_position,
                         max_gross=CONFIG.risk.max_gross, market_neutral=False)

    res_eq = eng_eq.generate(eq_hist)
    res_cx = eng_cx.generate(cx_hist) if cx_hist else None

    show("EQUITIES", res_eq, CONFIG.allocation.equities)
    if res_cx is not None:
        show("CRYPTO", res_cx, CONFIG.allocation.crypto)

    # combined book notional summary
    eq_gross = res_eq.gross * CONFIG.allocation.equities
    cx_gross = (res_cx.gross * CONFIG.allocation.crypto) if res_cx else 0.0
    print(f"\n=== COMBINED BOOK ===")
    print(f"  equity gross {eq_gross:.3f} + crypto gross {cx_gross:.3f} "
          f"= total gross {eq_gross + cx_gross:.3f} (<= 1.0 limit)")
    print(f"  paper NAV: ${CONFIG.fund.paper_capital:,.0f}")
    print("\nNOTE: target book only. No edge claim yet - that is step 3 (validation).")


if __name__ == "__main__":
    main()
