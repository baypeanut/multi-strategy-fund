"""S5EventEngine — forward replica of the confirmed 8k-drift calendar-time book.

Position logic is a byte-for-byte forward version of research primitive
`calendar_time_daily` (the confirmation instrument): each 8-K event contributes
a signed unit (+1 if its AR(0,1) reaction was positive, -1 if negative) to its
name over the trading days inside [filing+entry_lag, filing+exit_lag]; overlaps
on a name stack; the book only trades when at least `min_units` events are
active (the breadth guard). Raw per-name weight = net signed units; the shared
`scale_to_target_vol` then applies the same 10% ex-ante vol normalization and
per-name / gross caps every other book gets (fair race).

ZERO free parameters beyond the pre-registered confirmed spec
(entry_lag=2, exit_lag=10, n_names=500, min_units=4).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from systems.s1_quant.portfolio import scale_to_target_vol


@dataclass
class S5Result:
    weights: pd.Series
    n_units: int          # total active signed units across all names today
    n_names: int          # names with a non-zero position after normalization


@dataclass
class S5EventEngine:
    entry_lag: int = 2            # calendar-day offsets, snapped to the bar
    exit_lag: int = 10
    min_units: int = 4           # breadth guard (matches calendar_time_daily `live`)
    target_vol: float = 0.10
    max_position: float = 0.05
    max_gross: float = 1.00

    def generate(self, events, today, cov: pd.DataFrame) -> S5Result:
        """events: iterable of (ticker, filing_date, sign) with sign in {+1,-1}.
        today: the current bar date. cov: daily covariance for vol-normalization.
        """
        today = pd.Timestamp(today).normalize()
        raw: dict[str, float] = {}
        n_units = 0
        for ticker, filing_date, sign in events:
            fdate = pd.Timestamp(filing_date).normalize()
            lo = fdate + pd.Timedelta(days=self.entry_lag)
            hi = fdate + pd.Timedelta(days=self.exit_lag)
            if lo <= today <= hi:
                raw[ticker] = raw.get(ticker, 0.0) + float(sign)
                n_units += 1

        if n_units < self.min_units or not raw:
            return S5Result(weights=pd.Series(dtype=float), n_units=n_units, n_names=0)

        w_raw = pd.Series(raw, dtype=float)
        w = scale_to_target_vol(
            w_raw, cov, target_vol=self.target_vol,
            max_position=self.max_position, max_gross=self.max_gross)
        w = w[w.abs() > 1e-9]
        return S5Result(weights=w, n_units=n_units, n_names=int(len(w)))
