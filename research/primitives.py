"""Vetted analysis primitives - the ONLY way research touches data.

Every primitive takes an explicit (start, end) window HANDED TO IT by the
harness; primitives never choose their own dates. This is how the
exploration/lockbox split is enforced mechanically rather than by trust.

Cost realism (real-money standard): the cost-aware primitives charge the same
square-root-impact CostModel the paper broker uses - spread + impact +
commission both legs, borrow on the short leg for the holding period.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from core.broker.costs import CostModel, CostParams
from core.config import CONFIG
from systems.s1_quant.engine import QuantEngine, build_panels
from systems.s1_quant.signals import (cross_sectional_zscore,
                                      information_coefficient, low_volatility,
                                      momentum_12_1, short_term_reversal)


def _cost_model() -> CostModel:
    return CostModel(CostParams(**CONFIG.costs.equities))


def _round_trip_cost(cm: CostModel, trade_value: float, adv: float,
                     dvol: float, holding_days: int, short: bool) -> float:
    """Fractional round-trip cost: entry + exit (spread/impact/commission)
    plus borrow if short. Sized per-event at a conservative $50k clip."""
    leg = cm.estimate(trade_value, adv=adv, daily_vol=dvol)
    cost = 2.0 * (leg.slippage + leg.commission)
    if short:
        cost += (cm.p.borrow_annual_bps * 1e-4) * (holding_days / 252.0)
    return cost


# ------------------------------------------------------------------ events --
def event_table(params: dict, start: str, end: str) -> pd.DataFrame:
    """Shared event machinery: 8-Ks in window -> AR(0,1) + drift CARs."""
    from core.data.factory import make_equity_provider
    from core.data.universe import EQUITY_UNIVERSE
    from research.h1_event_study import (compute_event_table, fetch_8k_events,
                                         primary_item)

    n_names = int(params.get("n_names", 100))
    syms = [a.symbol for a in EQUITY_UNIVERSE][:n_names]
    events = fetch_8k_events(syms, start=start, end=end)
    if params.get("item") and len(events):
        events = events[events["items"].map(primary_item) == params["item"]]
    if params.get("item_not") and len(events):
        # complement filter (director wish 2026-07-19): keep every event whose
        # primary item is NOT the excluded class - 'all 8-Ks except earnings'
        # (the PEAD-vs-novel test) becomes ONE pre-registered run instead of
        # differencing two. Same primary_item bucketing as the positive filter,
        # so item and item_not are exactly complementary partitions.
        events = events[events["items"].map(primary_item) != params["item_not"]]
    # price window pads the event window for estimation + drift horizons
    px_start = (pd.Timestamp(start) - pd.DateOffset(months=14)).date().isoformat()
    px_end = (pd.Timestamp(end) + pd.DateOffset(months=2)).date().isoformat()
    px = make_equity_provider().history(syms + ["SPY"], start=px_start, end=px_end)
    if "SPY" not in px:
        raise RuntimeError("SPY missing")
    spy_ret = px["SPY"]["close"].pct_change().dropna()
    tab = compute_event_table(events, px, spy_ret)
    tab.attrs["prices"] = px
    return tab


def event_study(params: dict, start: str, end: str) -> dict:
    """Reaction-conditioned drift (H1/F1 machinery), date-bounded."""
    from research.h1_event_study import month_block_bootstrap_diff
    tab = event_table(params, start, end)
    col = params.get("drift_col", "car2_10")
    res = month_block_bootstrap_diff(tab, col, n_boot=300)
    return {"diff": round(res["diff"], 5) if res["diff"] == res["diff"] else None,
            "t": round(res["t"], 2) if res["t"] == res["t"] else None,
            "n_events": int(len(tab)), "n_pos": res["n_pos"], "n_neg": res["n_neg"],
            "col": col, "window": [start, end]}


def event_study_costnet(params: dict, start: str, end: str) -> dict:
    """Same drift spread, NET of realistic round-trip costs per event."""
    tab = event_table(params, start, end)
    col = params.get("drift_col", "car2_10")
    hold = int(col.split("_")[1]) - int(col.split("car")[1].split("_")[0])
    px = tab.attrs["prices"]
    cm = _cost_model()

    # precompute rolling ADV / daily-vol ONCE per ticker (not per event)
    # recomputing full-history rolling inside the event loop was O(events x
    # history) and blew the 2h timeout on the lockbox window (E17b fix).
    adv_by = {s: (df["close"] * df["volume"]).rolling(20).mean() for s, df in px.items()}
    dvol_by = {s: df["close"].pct_change().rolling(30).std() for s, df in px.items()}

    costs = []
    for _, ev in tab.iterrows():
        tk = ev["ticker"]
        if tk not in adv_by:
            costs.append(np.nan)
            continue
        adv = float(adv_by[tk].reindex(index=[ev["date"]], method="ffill").iloc[0] or 0)
        dvol = float(dvol_by[tk].reindex(index=[ev["date"]], method="ffill").iloc[0] or 0.02)
        short = ev["ar01"] < 0
        costs.append(_round_trip_cost(cm, 50_000, adv, dvol, hold, short))
    tab = tab.assign(cost=costs).dropna(subset=["cost", col])

    # net drift in the direction of the AR(0,1) sign, per event
    signed = np.sign(tab["ar01"]) * tab[col] - tab["cost"]
    n = len(signed)
    if n < 30:
        return {"net_mean": None, "t": None, "n_events": n}
    t = float(signed.mean() / (signed.std(ddof=1) / np.sqrt(n)))
    return {"net_mean": round(float(signed.mean()), 5),
            "gross_mean": round(float((np.sign(tab['ar01']) * tab[col]).mean()), 5),
            "avg_cost": round(float(tab["cost"].mean()), 5),
            "t": round(t, 2), "n_events": n, "col": col, "window": [start, end]}


def calendar_time_daily(tab, px, entry_lag: int, exit_lag: int, cost_model):
    """Vectorized calendar-time long/short daily return series (E17b core).

    Instead of scanning ~17k events for every one of ~500 days (8.5M scalar
    ops -> the 2h-timeout that killed the first lockbox run), each event adds
    +1 (long) / -1 (short) to its name over the contiguous slice of trading
    days inside [date+entry_lag, date+exit_lag], found by a single vectorized
    searchsorted. Overlapping events on a name count multiply - same semantics
    as the original append-duplicates loop. Returns (daily_series, num_units).
    """
    dates = px["SPY"]["close"].index
    ret_aligned = {s: px[s]["close"].pct_change().reindex(dates).fillna(0.0).values
                   for s in px if s != "SPY"}
    num_units = np.zeros(len(dates))
    wret = np.zeros(len(dates))
    tickers = tab["ticker"].to_numpy()
    ev_dates = tab["date"].to_numpy().astype("datetime64[D]")
    signs = np.where(tab["ar01"].to_numpy() > 0, 1.0, -1.0)
    dates_arr = dates.values.astype("datetime64[D]")
    i0_all = np.searchsorted(dates_arr, ev_dates + np.timedelta64(entry_lag, "D"), side="left")
    i1_all = np.searchsorted(dates_arr, ev_dates + np.timedelta64(exit_lag, "D"), side="right")
    for k in range(len(tickers)):
        i0, i1 = int(i0_all[k]), int(i1_all[k])
        tk = tickers[k]
        if i1 <= i0 or tk not in ret_aligned:
            continue
        num_units[i0:i1] += 1.0
        wret[i0:i1] += signs[k] * ret_aligned[tk][i0:i1]

    turn = 2.0 / max(exit_lag - entry_lag, 1)
    leg = cost_model.estimate(50_000, adv=5e8, daily_vol=0.02)
    cost_per_active_day = turn * (leg.slippage + leg.commission)
    live = num_units >= 4
    with np.errstate(invalid="ignore", divide="ignore"):
        daily = np.where(live, wret / np.where(num_units == 0, 1, num_units)
                         - cost_per_active_day, 0.0)
    return pd.Series(daily, index=dates), num_units


def event_portfolio(params: dict, start: str, end: str) -> dict:
    """Calendar-time long/short portfolio - the confirmation-grade test.

    Pooled event CARs overlap in time and overstate t-stats; a calendar-time
    daily return series does not - and it doubles as the S5 sleeve's actual
    backtest.
    """
    tab = event_table(params, start, end)
    px = tab.attrs["prices"]
    entry_lag = int(params.get("entry_lag", 2))
    exit_lag = int(params.get("exit_lag", 10))
    ser, gross_positions = calendar_time_daily(tab, px, entry_lag, exit_lag, _cost_model())
    ser = ser[(ser.index >= start) & (ser.index <= end)]
    active = ser[ser != 0.0]
    from backtest.metrics import max_drawdown, sharpe_ratio
    n = len(active)
    if n < 60:
        return {"sharpe": None, "n_days": n}
    sr = sharpe_ratio(ser)
    t = sr * np.sqrt(len(ser) / 252.0)
    eq = (1 + ser).cumprod()
    return {"sharpe": round(float(sr), 3),
            "t": round(float(t), 2),
            "ann_return": round(float(ser.mean() * 252), 4),
            "max_dd": round(float(max_drawdown(eq)), 4),
            "n_days": int(len(ser)), "active_days": int(n),
            "avg_positions": round(float(np.mean(gross_positions)), 1),
            "window": [start, end]}


# ---------------------------------------------------------------- backtests --
@dataclass
class ICWeightedQuantEngine(QuantEngine):
    """S1-style book whose signal weights are trailing realized ICs (research).

    Research-only subclass (director wish 2026-07-26): the LIVE QuantEngine is
    the static-blend control group and is untouched - nothing under systems/
    changes. Weights come from rolling Spearman ICs of each raw signal against
    the realized forward return over the next ic_horizon days; negative-IC
    signals are dropped (clipped to 0) and the survivors normalized to sum 1.0.
    While still in warmup the caller's static blend is used unchanged.
    """
    ic_horizon: int = 21
    ic_lookbacks: int = 6
    min_ic_obs: int = 2

    last_ic = None            # dict | None - diagnostics, not a dataclass field
    _static_weights = None    # constructor-time blend, i.e. the warmup fallback

    def _ic_signal_weights(self, close: pd.DataFrame) -> dict[str, float] | None:
        """Trailing-IC weights, or None while still in warmup.

        No lookahead by construction: for every k >= 1 the anchor position
        p = T-1-k*ic_horizon satisfies p + ic_horizon <= T-1, so each forward
        window is fully realized inside the histories handed to us - and the
        walk-forward backtest truncates those histories at the rebalance date,
        so nothing after the decision point can ever enter the weighting.
        """
        if close is None or len(close) == 0:
            return None
        T = len(close)
        ics: dict[str, list[float]] = {"momentum": [], "reversal": [], "low_vol": []}
        used = 0
        for k in range(1, int(self.ic_lookbacks) + 1):
            p = T - 1 - k * int(self.ic_horizon)
            if p < 260:        # momentum_12_1 needs a year of history AT the anchor
                continue
            try:
                close_slice = close.iloc[:p + 1]
                # close-only vol proxy: build_panels drops OHLC, so this
                # research weighting layer measures close-to-close vol. It
                # feeds the IC weights only - the live risk model is untouched.
                vols = close_slice.pct_change().tail(30).std() * np.sqrt(252.0)
                vols = vols.replace(0.0, np.nan).dropna()
                if vols.empty:
                    continue
                fwd = close.iloc[p + int(self.ic_horizon)] / close.iloc[p] - 1.0
                raw = {"momentum": momentum_12_1(close_slice, vols),
                       "reversal": short_term_reversal(close_slice, n=5),
                       "low_vol": low_volatility(vols)}
                anchor_ic = {name: information_coefficient(
                    cross_sectional_zscore(sig), fwd) for name, sig in raw.items()}
            except Exception:
                # degenerate anchor (all-NaN slice, single name, ...) - the
                # adaptive path must never raise; treat the anchor as unusable
                continue
            for name, ic in anchor_ic.items():
                ics[name].append(float(ic))
            used += 1
        if used < int(self.min_ic_obs):
            self.last_ic = None
            return None
        mean_ic = {name: float(np.mean(v)) for name, v in ics.items()}
        self.last_ic = mean_ic
        clipped = {name: max(ic, 0.0) for name, ic in mean_ic.items()}
        total = sum(clipped.values())
        if total <= 0.0:
            # honest 'nothing predicted anything' period -> all-zero book (cash)
            return {name: 0.0 for name in clipped}
        return {name: ic / total for name, ic in clipped.items()}

    def generate(self, histories: dict, min_history: int = 60):
        # signal_weights is rewritten on every call, so the constructor-time
        # blend is stashed once and reused as the warmup fallback
        if self._static_weights is None:
            self._static_weights = dict(self.signal_weights)
        try:
            close, _volume = build_panels(histories)
        except Exception:
            close = None
        w = self._ic_signal_weights(close) if close is not None else None
        self.signal_weights = w if w is not None else dict(self._static_weights)
        return super().generate(histories, min_history)


def signal_backtest(params: dict, start: str, end: str) -> dict:
    """Walk-forward S1-style backtest, date-bounded."""
    from backtest.engine import run_backtest
    from backtest.metrics import (annualized_return, deflated_sharpe_ratio,
                                  max_drawdown, sharpe_ratio)
    from core.data.factory import make_equity_provider
    from core.data.universe import EQUITY_UNIVERSE
    from research.harness import load_registry
    from systems.s1_quant.engine import QuantEngine

    n_names = int(params.get("n_names", 120))
    syms = [a.symbol for a in EQUITY_UNIVERSE][:n_names]
    hist = make_equity_provider().history(syms, start=start, end=end)

    eng_kwargs = dict(
        target_vol=CONFIG.risk.vol_target_annual,
        max_position=CONFIG.risk.max_position,
        max_gross=CONFIG.risk.max_gross,
        market_neutral=bool(params.get("market_neutral", True)),
        signal_weights={
            "momentum": float(params.get("w_momentum", 1.0)),
            "reversal": float(params.get("w_reversal", 0.5)),
            "low_vol": float(params.get("w_low_vol", 0.5)),
        },
    )
    # ic_weighting (director wish 2026-07-26): identical kwargs, adaptive
    # combining subclass - the static blend above stays the warmup fallback
    ic_weighting = bool(params.get("ic_weighting"))
    eng = ICWeightedQuantEngine(**eng_kwargs) if ic_weighting else QuantEngine(**eng_kwargs)
    bt = run_backtest(hist, eng, _cost_model(), nav0=1_000_000,
                      rebalance_days=int(params.get("rebalance_days", 21)),
                      warmup=252)
    r = bt.net_returns
    n_trials = load_registry()["total_experiments"] + 1
    out = {"sharpe": round(sharpe_ratio(r), 3),
           "ann_return": round(annualized_return(r), 4),
           "max_dd": round(max_drawdown(bt.equity), 4),
           "dsr": round(deflated_sharpe_ratio(r, n_trials=n_trials), 4),
           "n_trials_used": n_trials, "n_days": int(len(r)),
           "universe_names": len(hist), "window": [start, end]}
    if ic_weighting:
        out["ic_weighting"] = True
    return out
