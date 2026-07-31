"""H1 - reaction/sentiment-conditioned 8-K event study (research lane).

Pre-registered (RESEARCH_LOG E9, written before running):
  H1a: |AR(0,1)| on 8-K days exceeds non-event days.
  H1b: sign of AR(0,1) predicts drift: mean CAR(2,10)|pos - CAR(2,10)|neg
       >= 50bps net, month-block bootstrap t >= 2.5 (Bonferroni), stable
       across sample halves.
  H1c: (stage B) Item 2.02 press-release lexicon score predicts AR(0,1).

Run: python research/h1_event_study.py
Touches NOTHING in production. All data free (EDGAR + yfinance).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from core.data.equities import EquityDataProvider
from core.data.universe import EQUITY_UNIVERSE
from systems.s2_news.events import market_model_car
from systems.s2_news.ingest import _get, ticker_to_cik

YEARS_BACK = 3
DRIFT_WINDOWS = [(2, 10), (2, 20)]
N_BOOT = 500
RNG = np.random.default_rng(42)


# ---------------------------------------------------------------- events ----
def fetch_8k_events(tickers: list[str], start: str | None = None,
                    end: str | None = None) -> pd.DataFrame:
    """All 8-Ks (with item codes) for tickers within [start, end].

    Date bounds are how the harness enforces the exploration/lockbox split
    primitives obey the window they are handed, they never choose it.
    """
    lo = pd.Timestamp(start) if start else pd.Timestamp.now() - pd.DateOffset(years=YEARS_BACK)
    hi = pd.Timestamp(end) if end else pd.Timestamp.now()
    rows = []
    cik_map = ticker_to_cik()
    for i, t in enumerate(tickers):
        cik = cik_map.get(t.upper())
        if not cik:
            continue
        try:
            data = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
        except Exception:
            continue
        rec = data.get("filings", {}).get("recent", {})
        for form, date, items, acc in zip(
            rec.get("form", []), rec.get("filingDate", []),
            rec.get("items", []), rec.get("accessionNumber", []),
        ):
            if form != "8-K":
                continue
            d = pd.Timestamp(date)
            if d < lo or d > hi:
                continue
            rows.append({"ticker": t, "date": d, "items": items or "", "acc": acc})
        time.sleep(0.15)  # SEC politeness
    return pd.DataFrame(rows)


def primary_item(items: str) -> str:
    """Bucket by the most 'material' item code present."""
    codes = [c.strip() for c in items.split(",") if c.strip()]
    for want in ("2.02", "5.02", "1.01", "2.01", "8.01", "7.01"):
        if any(c.startswith(want) for c in codes):
            return want
    return "other"


# ------------------------------------------------------------- event study --
def compute_event_table(events: pd.DataFrame, prices: dict, spy_ret: pd.Series) -> pd.DataFrame:
    out = []
    for _, ev in events.iterrows():
        df = prices.get(ev["ticker"])
        if df is None or df.empty:
            continue
        aret = df["close"].pct_change().dropna()
        try:
            r01 = market_model_car(aret, spy_ret, ev["date"], event_window=(0, 1))
            if len(r01["ar"]) == 0:
                continue
            # NOTE: ev["items"], not ev.items - the latter is Series.items (method)
            row = {"ticker": ev["ticker"], "date": ev["date"],
                   "item": primary_item(ev["items"]), "ar01": r01["car"]}
            for (a, b) in DRIFT_WINDOWS:
                rd = market_model_car(aret, spy_ret, ev["date"], event_window=(a, b))
                row[f"car{a}_{b}"] = rd["car"] if len(rd["ar"]) > 0 else np.nan
            out.append(row)
        except Exception:
            continue
    return pd.DataFrame(out).dropna(subset=["ar01"])


def month_block_bootstrap_diff(tab: pd.DataFrame, col: str, n_boot: int = N_BOOT) -> dict:
    """Difference in mean drift (pos vs neg AR(0,1) bucket) with month-block
    bootstrap SE - events cluster in earnings months, iid SEs would lie."""
    tab = tab.dropna(subset=[col]).copy()
    tab["month"] = tab["date"].dt.to_period("M")
    months = tab["month"].unique()

    def diff_of(df):
        pos = df.loc[df.ar01 > 0, col]
        neg = df.loc[df.ar01 < 0, col]
        if len(pos) < 5 or len(neg) < 5:
            return np.nan
        return pos.mean() - neg.mean()

    point = diff_of(tab)
    boots = []
    for _ in range(n_boot):
        pick = RNG.choice(months, size=len(months), replace=True)
        sample = pd.concat([tab[tab.month == m] for m in pick])
        d = diff_of(sample)
        if not np.isnan(d):
            boots.append(d)
    boots = np.array(boots)
    se = boots.std(ddof=1) if len(boots) > 10 else np.nan
    t = point / se if se and se > 0 else np.nan
    return {"diff": point, "se": se, "t": t,
            "n_pos": int((tab.ar01 > 0).sum()), "n_neg": int((tab.ar01 < 0).sum())}


def main():
    tickers = [a.symbol for a in EQUITY_UNIVERSE]
    print(f"Universe: {len(tickers)} names, sample {YEARS_BACK}y, "
          f"pre-registered thresholds: diff>=50bps net, boot-t>=2.5 (Bonferroni), split-half stable")

    print("\n[1/4] fetching 8-K events from EDGAR...")
    events = fetch_8k_events(tickers)
    print(f"  events: {len(events)} | by item: {events['items'].map(primary_item).value_counts().to_dict()}")

    print("[2/4] fetching prices (yfinance, cached)...")
    import pickle
    cache = Path("research/h1_prices.pkl")
    px = {}
    if cache.exists():
        px = pickle.load(open(cache, "rb"))
        print(f"  cache hit: {len(px)} names")
    missing = [t for t in tickers + ["SPY"] if t not in px]
    for attempt in range(3):
        if not missing:
            break
        got = EquityDataProvider().history(missing, period=f"{YEARS_BACK + 1}y")
        px.update(got)
        missing = [t for t in missing if t not in px]
        if missing:
            print(f"  attempt {attempt + 1}: still missing {len(missing)} - backing off 30s (rate limit)")
            time.sleep(30)
    pickle.dump(px, open(cache, "wb"))
    print(f"  prices: {len(px)}/{len(tickers) + 1} names")
    if "SPY" not in px:
        sys.exit("FATAL: SPY missing - cannot run market model")
    spy_ret = px["SPY"]["close"].pct_change().dropna()

    print("[3/4] computing AR/CAR per event...")
    tab = compute_event_table(events, px, spy_ret)
    print(f"  usable events: {len(tab)}")
    if len(tab) == 0:
        sys.exit("FATAL: no usable events - inspect data")
    tab.to_csv("research/h1_events.csv", index=False)

    print("[4/4] hypothesis tests\n" + "=" * 64)

    # --- H1a: events move prices --------------------------------------
    all_ar = tab["ar01"].abs()
    # non-event baseline: typical 2-day |abnormal| move ~ sqrt(2)*resid vol;
    # approximate with each name's |2d market-model residual| median via bootstrap
    # of random dates: cheaper proxy - compare to |ar01| on shuffled dates
    print(f"H1a  |AR(0,1)| on events: mean {all_ar.mean():.2%}, median {all_ar.median():.2%} "
          f"(E2 replication on n={len(tab)})")

    # --- H1b: reaction-conditioned drift -------------------------------
    n_tests = len(DRIFT_WINDOWS) * (1 + 3)  # all + 3 major item buckets
    bonferroni_p = 0.05 / n_tests
    print(f"\nH1b  drift by AR(0,1) sign - {n_tests} tests, Bonferroni alpha={bonferroni_p:.4f}")
    results = {}
    for (a, b) in DRIFT_WINDOWS:
        col = f"car{a}_{b}"
        r = month_block_bootstrap_diff(tab, col)
        results[f"ALL {col}"] = r
        print(f"  ALL    {col:9s} diff={r['diff']:+.3%} se={r['se']:.3%} t={r['t']:+.2f} "
              f"(n+={r['n_pos']}, n-={r['n_neg']})")
        for item in ("2.02", "5.02", "1.01"):
            sub = tab[tab.item == item]
            if len(sub) < 40:
                continue
            ri = month_block_bootstrap_diff(sub, col)
            results[f"{item} {col}"] = ri
            print(f"  {item:6s} {col:9s} diff={ri['diff']:+.3%} se={ri['se']:.3%} t={ri['t']:+.2f} "
                  f"(n+={ri['n_pos']}, n-={ri['n_neg']})")

    # split-half stability on the primary spec (ALL, car2_10)
    tab_sorted = tab.sort_values("date")
    half = len(tab_sorted) // 2
    r1 = month_block_bootstrap_diff(tab_sorted.iloc[:half], "car2_10", n_boot=200)
    r2 = month_block_bootstrap_diff(tab_sorted.iloc[half:], "car2_10", n_boot=200)
    print(f"\n  split-half car2_10: H1 {r1['diff']:+.3%} (t={r1['t']:+.2f}) | "
          f"H2 {r2['diff']:+.3%} (t={r2['t']:+.2f}) | same sign: {np.sign(r1['diff']) == np.sign(r2['diff'])}")

    # --- verdict against pre-registered thresholds ----------------------
    main_r = results.get("ALL car2_10", {})
    passed = (main_r.get("diff", 0) >= 0.005 and (main_r.get("t") or 0) >= 2.5
              and np.sign(r1["diff"]) == np.sign(r2["diff"]))
    print("\n" + "=" * 64)
    print(f"PRE-REGISTERED VERDICT (ALL car2_10): "
          f"{'PASS - candidate for next config window' if passed else 'FAIL - no tradable reaction-drift at our thresholds'}")
    print("Full table saved to research/h1_events.csv")


if __name__ == "__main__":
    main()
