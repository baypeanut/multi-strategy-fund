"""Live capstone: run all four books and show the combined ensemble.

S1 (quant, real signals) -> briefing -> S3 (discretionary PM behind the hard
wrapper) -> S4 (ensemble). Regime from live VIX. S2 contributes only if a live
scored-news feed is wired (here it is neutral - flagged honestly).

Run: python scripts/run_combined.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.config import CONFIG
from core.data.equities import EquityDataProvider
from core.data.macro import MacroDataProvider
from core.data.universe import EQUITY_UNIVERSE, average_dollar_volume
from systems.s1_quant.engine import QuantEngine, build_panels
from systems.s2_news.engine import NewsEngine  # noqa: F401 (wired when feed available)
from systems.s3_llm.briefing import build_briefing
from systems.s3_llm.engine import S3Engine
from systems.s3_llm.pm import HeuristicPM
from systems.s3_llm.regime import compute_regime
from systems.s3_llm.wrapper import RiskWrapper
from systems.s4_combined.engine import S4Engine


def top(w, n=6):
    w = w[w.abs() > 1e-9].sort_values(ascending=False)
    return list(w.head(n).items()) + [("...", None)] + list(w.tail(3).items()) if len(w) > n else list(w.items())


def main():
    nav = CONFIG.fund.paper_capital
    syms = [a.symbol for a in EQUITY_UNIVERSE]

    print("Fetching equities + SPY + VIX (free)...")
    eq = EquityDataProvider().history(syms + ["SPY"], period="2y")
    vix = MacroDataProvider().vix()
    spy = eq["SPY"]["close"]
    histories = {s: eq[s] for s in syms if s in eq}

    # --- S1 quant (real) ---
    s1 = QuantEngine(target_vol=CONFIG.risk.vol_target_annual,
                     max_position=CONFIG.risk.max_position,
                     max_gross=CONFIG.risk.max_gross, market_neutral=True).generate(histories)
    close, _ = build_panels(histories)

    # --- regime (real) ---
    reg = compute_regime(close, spy, vix)
    print(f"\n=== REGIME ===\n  {reg.as_dict()}")

    # --- S2 news (neutral placeholder; needs live headline feed) ---
    s2_scores = pd.Series(0.0, index=s1.combined_score.index)

    # --- S3 discretionary (PM behind hard wrapper) ---
    briefing = build_briefing(
        universe=list(histories.keys()), prices=histories,
        s1_scores=s1.combined_score, s2_scores=s2_scores, vols=s1.vols, regime=reg,
    )
    adv = {s: average_dollar_volume(histories[s]) for s in histories}
    wrapper = RiskWrapper(universe={s.upper() for s in histories}, nav=nav,
                          max_position=CONFIG.risk.max_position,
                          max_gross=CONFIG.risk.max_gross,
                          adv_cap=CONFIG.risk.liquidity_adv_cap)
    s3 = S3Engine(wrapper=wrapper, pm=HeuristicPM(max_position=CONFIG.risk.max_position)).generate(
        briefing, adv=adv)

    print(f"\n=== S1 QUANT (gross {s1.gross:.2f}, net {s1.net:+.2f}) ===")
    for s, v in top(s1.weights):
        print(f"    {s:8s} {('' if v is None else f'{v:+.2%}')}")

    print(f"\n=== S3 DISCRETIONARY (gross {s3.weights.abs().sum():.2f}) ===")
    for s, v in top(s3.weights):
        print(f"    {s:8s} {('' if v is None else f'{v:+.2%}')}")
    print(f"  wrapper violations: {len(s3.violations)}"
          + (f" (e.g. {s3.violations[0]})" if s3.violations else ""))

    # --- S4 combined ensemble ---
    s4 = S4Engine(method="risk_parity").generate(
        system_weights={"s1": s1.weights, "s3": s3.weights},
        system_returns=None,  # equal alphas live; real alphas come from validation
    )
    print(f"\n=== S4 COMBINED (alphas {s4.system_alphas}, gross {s4.weights.abs().sum():.2f}) ===")
    for s, v in top(s4.weights):
        print(f"    {s:8s} {('' if v is None else f'{v:+.2%}')}")

    print("\nNOTE: full 4-book pipeline runs live on free data. S2 neutral here")
    print("(needs a live scored-news feed). LLM PM proposals pass the HARD wrapper —")
    print("no order bypasses limits. Edge still subject to the DSR/PBO bar.")


if __name__ == "__main__":
    main()
