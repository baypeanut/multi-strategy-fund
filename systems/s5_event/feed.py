"""S5 live event feed - trailing 8-K filings + their AR(0,1) reaction sign.

Keeps a rolling cache of raw 8-K filings (ticker, filing_date) over the trailing
holding window and re-derives each event's reaction sign from the SAME price
panel the runtime already fetches. EDGAR is polled at most once per calendar day
(500 CIK submission calls are the expensive part); the AR(0,1) recompute off the
cached filings is cheap.

The reaction sign uses `market_model_car(event_window=(0,1))` - identical to the
research event table that produced the confirmed edge. An event too fresh to
have its day-after return yet (filing == latest bar) is simply skipped this
rebalance; entry_lag=2 leaves a buffer, so it is picked up before the position
would open.

`fetch_8k_events` is injected so tests never touch the network.
"""
from __future__ import annotations

import pandas as pd

from systems.s2_news.events import market_model_car

# calendar days of raw filings to retain = holding window + slack for the
# entry lag and for events awaiting their day-after price
RETAIN_SLACK_DAYS = 6


def ar01_sign(ticker: str, filing_date, price_panel: dict,
              spy_ret: pd.Series) -> float | None:
    """+1 / -1 reaction sign, or None if it cannot be computed yet."""
    df = price_panel.get(ticker)
    if df is None or df.empty:
        return None
    aret = df["close"].pct_change().dropna()
    res = market_model_car(aret, spy_ret, pd.Timestamp(filing_date).normalize(),
                           event_window=(0, 1))
    if len(res["ar"]) < 2:      # need both event-day and day-after returns
        return None
    return 1.0 if res["car"] > 0 else -1.0


def refresh_raw_filings(cached: list, universe: list[str], today: pd.Timestamp,
                        exit_lag: int, last_fetch: str | None,
                        fetch_fn) -> tuple[list, str]:
    """Prune expired filings, poll EDGAR at most once/day, return (rows, fetch_date).

    cached / return rows are [ticker, filing_date_iso, accession]. Dedup key is
    (ticker, accession) so the same filing is never double-counted.
    """
    today = pd.Timestamp(today).normalize()
    horizon = today - pd.Timedelta(days=exit_lag + RETAIN_SLACK_DAYS)
    rows = [r for r in cached
            if pd.Timestamp(r[1]) >= horizon]

    today_iso = today.date().isoformat()
    if last_fetch == today_iso:
        return rows, today_iso

    start = horizon.date().isoformat()
    fresh = fetch_fn(universe, start=start, end=today_iso)
    seen = {(r[0], r[2]) for r in rows}
    for _, ev in fresh.iterrows():
        key = (ev["ticker"], ev.get("acc", ""))
        if key in seen:
            continue
        seen.add(key)
        rows.append([ev["ticker"], pd.Timestamp(ev["date"]).date().isoformat(),
                     ev.get("acc", "")])
    return rows, today_iso


def signed_events(raw_filings: list, price_panel: dict,
                  spy_ret: pd.Series) -> list:
    """Map cached raw filings -> [(ticker, filing_date_iso, sign)] where the
    reaction sign is computable from the current price panel."""
    out = []
    for ticker, fdate, _acc in raw_filings:
        sign = ar01_sign(ticker, fdate, price_panel, spy_ret)
        if sign is not None:
            out.append((ticker, fdate, sign))
    return out
