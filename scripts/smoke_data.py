"""Smoke test for the free data layer + paper broker (needs network).

Fetches a little equity + crypto history, computes ADV, then routes one paper
order through the broker to prove the end-to-end plumbing works.

Run: python scripts/smoke_data.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.broker.costs import CostModel, CostParams
from core.broker.paper_broker import PaperBroker
from core.config import CONFIG
from core.data.crypto import CryptoDataProvider
from core.data.equities import EquityDataProvider
from core.data.universe import average_dollar_volume
from core.ledger.ledger import Ledger


def main() -> None:
    print("=== Equity data (yfinance) ===")
    eq = EquityDataProvider()
    eq_hist = eq.history(["AAPL", "MSFT"], period="3mo")
    for sym, df in eq_hist.items():
        adv = average_dollar_volume(df)
        vol = df["close"].pct_change().std()
        print(f"  {sym}: {len(df)} bars, last={df['close'].iloc[-1]:.2f}, "
              f"ADV=${adv/1e9:.1f}B, dailyVol={vol:.3%}")

    print("=== Crypto data (ccxt) ===")
    cx = CryptoDataProvider(exchange=CONFIG.data.crypto_exchange)
    cx_hist = cx.history(["BTC/USDT", "ETH/USDT"], timeframe="1d", limit=90)
    for sym, df in cx_hist.items():
        adv = average_dollar_volume(df)
        print(f"  {sym}: {len(df)} bars, last={df['close'].iloc[-1]:.2f}, ADV~{adv/1e6:.0f}M(base*px)")

    print("=== Paper broker round-trip ===")
    led = Ledger(starting_cash=CONFIG.fund.paper_capital)
    cm_eq = CostModel(CostParams(**CONFIG.costs.equities))
    cm_cx = CostModel(CostParams(**CONFIG.costs.crypto))
    broker = PaperBroker(led, {"equity": cm_eq, "crypto": cm_cx},
                         liquidity_adv_cap=CONFIG.risk.liquidity_adv_cap)

    if "AAPL" in eq_hist:
        df = eq_hist["AAPL"]
        mid = float(df["close"].iloc[-1])
        adv = average_dollar_volume(df)
        vol = float(df["close"].pct_change().std())
        res = broker.submit("AAPL", qty=1000, mid_price=mid, asset_class="equity",
                            adv=adv, daily_vol=vol)
        print(f"  BUY 1000 AAPL @ mid {mid:.2f} -> fill {res.fill_price:.4f} "
              f"(slip {res.slippage_bps:.2f}bps, clipped={res.clipped})")
        print(f"  Equity now: ${led.equity({'AAPL': mid}):,.2f} | cash ${led.cash:,.2f}")

    print("\nSMOKE OK")


if __name__ == "__main__":
    main()
