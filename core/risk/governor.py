"""RiskGovernor - the firm-wide circuit breaker.

Sits above every book. Given a proposed combined target and the equity history,
it enforces (in order): drawdown gate 2 (halt), daily-loss kill, drawdown gate 1
(halve sizing), parametric VaR limit, and the gross cap. Deterministic; nothing
trades without its sign-off.

Timescale correctness: live equity marks arrive hourly, but the loss-kill rule
is a DAILY rule. If the curve has a DatetimeIndex we resample to calendar-day
closes and measure the current mark against *yesterday's close* - so a loss
spread across 24 hourly ticks triggers exactly like a single-hour crash.
Series without a DatetimeIndex (tests, backtests on daily bars) are treated as
already-daily.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

Z_95 = 1.645  # one-sided 95% normal quantile


def resample_daily(equity: pd.Series) -> pd.Series:
    """Collapse an intraday-marked equity curve to calendar-day closes.

    The last bucket is the current (possibly partial) day, so its value is the
    latest mark. Non-datetime-indexed input is returned unchanged (assumed to
    already be one point per period).
    """
    eq = equity.dropna()
    if not isinstance(eq.index, pd.DatetimeIndex) or len(eq) < 2:
        return eq
    return eq.resample("1D").last().dropna()


@dataclass
class GovernorDecision:
    weights: pd.Series
    halted: bool = False
    risk_scale: float = 1.0
    actions: list[str] = field(default_factory=list)


@dataclass
class RiskGovernor:
    max_gross: float = 1.00
    max_net: float = 1.00
    dd_gate_1: float = -0.10
    dd_gate_2: float = -0.15
    daily_loss_kill: float = -0.03
    var_limit_95: float = 0.02
    max_sector: float = 0.25
    sectors: dict[str, str] | None = None   # symbol -> sector; None disables the cap

    def assess(
        self,
        proposed: pd.Series,
        equity_curve: pd.Series | None = None,
        cov_daily: pd.DataFrame | None = None,
    ) -> GovernorDecision:
        actions: list[str] = []
        risk_scale = 1.0

        # --- drawdown / daily-loss circuit breakers (on DAILY closes) ---
        if equity_curve is not None and len(equity_curve.dropna()) >= 2:
            daily = resample_daily(equity_curve)
            # drawdown measured on daily closes (current partial day included)
            peak = daily.cummax().iloc[-1]
            dd = daily.iloc[-1] / peak - 1.0
            # day loss: latest mark vs yesterday's close (catches both a
            # 24-tick slow bleed and a single-hour crash)
            day_loss = (daily.iloc[-1] / daily.iloc[-2] - 1.0) if len(daily) >= 2 else 0.0

            if dd <= self.dd_gate_2:
                actions.append(f"DD {dd:.1%} <= gate2 ({self.dd_gate_2:.0%}) -> HALT, go cash")
                return GovernorDecision(proposed * 0.0, True, 0.0, actions)
            if day_loss <= self.daily_loss_kill:
                actions.append(
                    f"day loss {day_loss:.1%} <= kill ({self.daily_loss_kill:.0%}) -> HALT")
                return GovernorDecision(proposed * 0.0, True, 0.0, actions)
            if dd <= self.dd_gate_1:
                risk_scale *= 0.5
                actions.append(f"DD {dd:.1%} <= gate1 ({self.dd_gate_1:.0%}) -> sizing x0.5")

        w = proposed * risk_scale

        # --- parametric VaR limit ---
        if cov_daily is not None and not w.empty:
            common = w.index.intersection(cov_daily.index)
            wv = w.reindex(common).fillna(0.0).to_numpy()
            C = cov_daily.reindex(index=common, columns=common).to_numpy()
            daily_vol = math.sqrt(max(wv @ C @ wv, 0.0))
            var95 = Z_95 * daily_vol
            if var95 > self.var_limit_95 and var95 > 0:
                scale = self.var_limit_95 / var95
                w = w * scale
                actions.append(f"VaR95 {var95:.2%} > {self.var_limit_95:.0%} -> scale x{scale:.2f}")

        # --- sector concentration cap (SPEC §4: <=25% gross per sector) ---
        if self.sectors and not w.empty:
            by_sector: dict[str, float] = {}
            for sym, wt in w.items():
                sec = self.sectors.get(str(sym))
                if sec:
                    by_sector[sec] = by_sector.get(sec, 0.0) + abs(float(wt))
            for sec, expo in by_sector.items():
                if expo > self.max_sector:
                    scale = self.max_sector / expo
                    mask = [self.sectors.get(str(s)) == sec for s in w.index]
                    w.loc[mask] = w.loc[mask] * scale
                    actions.append(f"sector {sec} {expo:.2f} -> {self.max_sector:.2f}")

        # --- net exposure cap (config max_net was previously unenforced) ---
        net = float(w.sum())
        if abs(net) > self.max_net and abs(net) > 0:
            w = w * (self.max_net / abs(net))
            actions.append(f"net {net:+.2f} -> {self.max_net:.2f} cap")

        # --- gross cap ---
        gross = w.abs().sum()
        if gross > self.max_gross and gross > 0:
            w = w * (self.max_gross / gross)
            actions.append(f"gross {gross:.2f} -> {self.max_gross:.2f}")

        return GovernorDecision(w, False, risk_scale, actions)
