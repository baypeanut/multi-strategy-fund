"""Build the top-500-ADV equity universe from Polygon (A3+A4).

Pipeline:
  1. Reference tickers (type=CS, active) -> the common-stock whitelist
     (excludes ETFs/ETNs/funds so SPY-type products can't enter the book).
  2. ~30 calendar days of grouped-daily bars -> 20-day ADV per symbol.
  3. Rank CS symbols by ADV, take top N (all clear the $50M floor by miles),
     force-include USER_PICKS.
  4. Ticker-details (parallel) -> SIC code -> coarse sector bucket.
  5. Write data/universe/equities.json - FROZEN until the next config window.

Run ON THE SERVER (needs POLYGON_API_KEY):  python scripts/build_universe.py
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.data.polygon import PolygonDataProvider, _get  # noqa: E402
from core.data.universe import USER_PICKS  # noqa: E402

TOP_N = 500
OUT = Path("data/universe/equities.json")

_REF = ("https://api.polygon.io/v3/reference/tickers?market=stocks&type=CS"
        "&active=true&limit=1000&apiKey={key}")
_DETAILS = "https://api.polygon.io/v3/reference/tickers/{sym}?apiKey={key}"

# Coarse SIC-range -> sector buckets (for the governor's 25% concentration cap;
# coarse is fine - the cap needs buckets, not GICS precision).
_SIC_BUCKETS = [
    (100, 999, "other"), (1000, 1299, "materials"), (1300, 1399, "energy"),
    (1400, 1499, "materials"), (1500, 1799, "industrials"),
    (2000, 2399, "consumer"), (2400, 2799, "materials"),
    (2800, 2833, "materials"), (2834, 2836, "healthcare"),
    (2840, 2899, "materials"), (2900, 2999, "energy"),
    (3000, 3399, "industrials"), (3400, 3569, "industrials"),
    (3570, 3579, "tech"), (3580, 3599, "industrials"),
    (3600, 3699, "tech"), (3700, 3716, "consumer"),
    (3720, 3799, "industrials"), (3800, 3839, "tech"),
    (3840, 3859, "healthcare"), (3860, 3999, "industrials"),
    (4000, 4799, "industrials"), (4800, 4899, "comms"),
    (4900, 4999, "utilities"), (5000, 5999, "consumer"),
    (6000, 6999, "financials"), (7000, 7369, "consumer"),
    (7370, 7379, "tech"), (7380, 7999, "consumer"),
    (8000, 8099, "healthcare"), (8100, 9999, "other"),
]


def sic_to_sector(sic) -> str:
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return "other"
    for lo, hi, bucket in _SIC_BUCKETS:
        if lo <= code <= hi:
            return bucket
    return "other"


def common_stock_symbols(key: str) -> set[str]:
    url, out = _REF.format(key=key), set()
    while url:
        data = _get(url, timeout=60)
        out |= {r["ticker"] for r in data.get("results", [])}
        url = data.get("next_url")
        if url:
            url += f"&apiKey={key}"
    return out


def adv20(provider: PolygonDataProvider) -> dict[str, float]:
    """20-trading-day ADV per symbol from ~30 calendar days of grouped bars."""
    dollar: dict[str, list[float]] = {}
    got_days = 0
    d = date.today()
    while got_days < 20 and (date.today() - d).days < 45:
        try:
            bars = provider.grouped_daily(d.isoformat())
        except Exception:
            bars = {}
        if len(bars) > 100:
            got_days += 1
            for sym, b in bars.items():
                if b.get("close") and b.get("volume"):
                    dollar.setdefault(sym, []).append(b["close"] * b["volume"])
        d -= timedelta(days=1)
    return {s: sum(v) / len(v) for s, v in dollar.items() if v}


def main() -> None:
    provider = PolygonDataProvider()
    key = provider.api_key

    print("[1/4] common-stock whitelist (reference tickers)...")
    cs = common_stock_symbols(key)
    print(f"  {len(cs)} common stocks")

    print("[2/4] 20d ADV from grouped-daily bars...")
    adv = adv20(provider)
    print(f"  ADV computed for {len(adv)} symbols")

    ranked = sorted((s for s in adv if s in cs), key=adv.get, reverse=True)[:TOP_N]
    for pick in USER_PICKS:               # E8 user picks: always in
        if pick not in ranked and pick in adv:
            ranked.append(pick)
    print(f"[3/4] universe: {len(ranked)} names "
          f"(ADV floor in top-{TOP_N}: ${adv[ranked[TOP_N-1]]/1e6:.0f}M)")

    print("[4/4] sectors via ticker-details (SIC)...")
    def details(sym: str) -> tuple[str, str]:
        try:
            data = _get(_DETAILS.format(sym=sym, key=key), timeout=30)
            return sym, sic_to_sector(data.get("results", {}).get("sic_code"))
        except Exception:
            return sym, "other"

    with ThreadPoolExecutor(max_workers=12) as pool:
        sectors = dict(pool.map(details, ranked))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "note": "FROZEN for the experiment run; regenerate only at config windows",
        "symbols": [{"symbol": s, "adv": round(adv[s], 2), "sector": sectors[s]}
                    for s in ranked],
    }, indent=1))
    from collections import Counter
    print(f"written {OUT} | sectors: {dict(Counter(sectors.values()))}")


if __name__ == "__main__":
    main()
