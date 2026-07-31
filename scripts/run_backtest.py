"""Backtest System 1 (Quant) out-of-sample on the live universe + honest stats.

Runs a walk-forward backtest (each rebalance uses only past data), charges
realistic costs, then reports Sharpe/t-stat/drawdown AND the overfit-aware
metrics: Deflated Sharpe Ratio (multiple-testing penalty) and PBO (CSCV).

Run: python scripts/run_backtest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from backtest.engine import run_backtest
from backtest.metrics import (
    annualized_return,
    annualized_vol,
    cscv_pbo,
    deflated_sharpe_ratio,
    max_drawdown,
    sharpe_ratio,
    t_statistic,
)
from core.broker.costs import CostModel, CostParams
from core.config import CONFIG
from core.data.crypto import CryptoDataProvider
from core.data.equities import EquityDataProvider
from core.data.universe import EQUITY_UNIVERSE, CRYPTO_UNIVERSE
from systems.s1_quant.engine import QuantEngine

# Config grid for PBO / DSR (multiple-testing honesty)
CONFIGS = {
    "mom":      {"momentum": 1.0, "reversal": 0.0, "low_vol": 0.0},
    "mom+rev":  {"momentum": 1.0, "reversal": 0.5, "low_vol": 0.0},
    "mom+lv":   {"momentum": 1.0, "reversal": 0.0, "low_vol": 0.5},
    "all":      {"momentum": 1.0, "reversal": 0.5, "low_vol": 0.5},
    "rev":      {"momentum": 0.0, "reversal": 1.0, "low_vol": 0.0},
    "rev+lv":   {"momentum": 0.0, "reversal": 1.0, "low_vol": 0.5},
}


def report(name, bt, n_trials):
    r = bt.net_returns
    print(f"\n=== {name} ===")
    print(f"  Ann. return : {annualized_return(r):+.2%}")
    print(f"  Ann. vol    : {annualized_vol(r):.2%}")
    print(f"  Sharpe      : {sharpe_ratio(r):.2f}   (t-stat {t_statistic(r):.2f})")
    print(f"  Max DD      : {max_drawdown(bt.equity):.2%}")
    print(f"  Cost drag   : {bt.cost_drag_annual:.2%}/yr  (total ${bt.total_cost:,.0f})")
    print(f"  Final equity: ${bt.equity.iloc[-1]:,.0f}  (from ${bt.nav0:,.0f})")
    dsr = deflated_sharpe_ratio(r, n_trials=n_trials)
    print(f"  Deflated Sharpe (N={n_trials} trials): {dsr:.3f}  "
          f"{'PASS >0.95' if dsr > 0.95 else 'FAIL - not significant'}")


def backtest_book(histories, cost_params, nav, label):
    cm = CostModel(CostParams(**cost_params))
    returns_by_cfg = {}
    chosen_bt = None
    for cfg_name, weights in CONFIGS.items():
        eng = QuantEngine(target_vol=CONFIG.risk.vol_target_annual,
                          max_position=CONFIG.risk.max_position,
                          max_gross=CONFIG.risk.max_gross,
                          market_neutral=(label == "EQUITIES"),
                          signal_weights=weights)
        try:
            bt = run_backtest(histories, eng, cm, nav0=nav, rebalance_days=21, warmup=252)
        except ValueError:
            continue
        returns_by_cfg[cfg_name] = bt.net_returns
        if cfg_name == "all":
            chosen_bt = bt
    perf = pd.DataFrame(returns_by_cfg).dropna()
    pbo = cscv_pbo(perf, s=8) if perf.shape[1] >= 2 else float("nan")
    report(label, chosen_bt, n_trials=len(CONFIGS))
    print(f"  PBO (overfit prob, {perf.shape[1]} configs): {pbo:.2f}  "
          f"{'OK <0.5' if pbo < 0.5 else 'WARN - likely overfit'}")
    return chosen_bt


def main():
    print("Fetching data (free)...")
    eq = EquityDataProvider().history([a.symbol for a in EQUITY_UNIVERSE], period="2y")
    cx = CryptoDataProvider(exchange=CONFIG.data.crypto_exchange).history(
        [a.symbol for a in CRYPTO_UNIVERSE], timeframe="1d", limit=730)
    print(f"  equities {len(eq)}, crypto {len(cx)}")

    nav = CONFIG.fund.paper_capital
    bt_eq = backtest_book(eq, CONFIG.costs.equities, nav * CONFIG.allocation.equities, "EQUITIES")
    bt_cx = backtest_book(cx, CONFIG.costs.crypto, nav * CONFIG.allocation.crypto, "CRYPTO")

    # combined 80/20 book
    if bt_eq is not None and bt_cx is not None:
        combined = (CONFIG.allocation.equities * bt_eq.net_returns
                    .add(CONFIG.allocation.crypto * bt_cx.net_returns, fill_value=0.0))
        print("\n=== COMBINED 80/20 BOOK ===")
        print(f"  Ann. return : {annualized_return(combined):+.2%}")
        print(f"  Ann. vol    : {annualized_vol(combined):.2%}")
        print(f"  Sharpe      : {sharpe_ratio(combined):.2f}")
        print(f"  Max DD      : {max_drawdown((1+combined).cumprod()):.2%}")

    print("\nHONEST READ: Sharpe alone is not proof. DSR>0.95 AND PBO<0.5 is the bar.")
    print("This is 2y of mostly in-distribution data - treat as a first signal, not a verdict.")


if __name__ == "__main__":
    main()
