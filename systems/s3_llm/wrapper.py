"""RiskWrapper — the hard, deterministic safety layer around the LLM PM.

EVERY proposal from the discretionary PM passes through here before it can
become a target. The LLM never sends orders directly. Checks:

  1. schema / sanity   — finite numbers only
  2. citation check    — symbols MUST exist in the briefing universe (else drop)
  3. per-name cap      — |w_i| <= max_position
  4. liquidity cap     — |w_i| * NAV <= adv_cap * ADV_i
  5. gross cap         — sum|w_i| <= max_gross
  6. turnover throttle — total change vs current book <= max_turnover

Returns sanitized weights + a list of violations (for logging/audit).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class WrapperResult:
    weights: pd.Series
    violations: list[str] = field(default_factory=list)


@dataclass
class RiskWrapper:
    universe: set[str]
    max_position: float = 0.05
    max_gross: float = 1.00
    adv_cap: float = 0.10
    max_turnover: float = 1.0
    nav: float = 3_000_000.0

    def validate(
        self,
        proposal: dict[str, float],
        adv: dict[str, float] | None = None,
        current_weights: dict[str, float] | None = None,
    ) -> WrapperResult:
        adv = adv or {}
        current = pd.Series(current_weights or {}, dtype=float)
        violations: list[str] = []

        clean: dict[str, float] = {}
        for sym, w in proposal.items():
            # 1. sanity
            try:
                w = float(w)
            except (TypeError, ValueError, OverflowError):
                w = float("nan")
            if not math.isfinite(w):
                violations.append(f"drop {sym}: non-finite weight")
                continue
            # 2. citation check — symbol must be in the briefing universe
            if sym.upper() not in self.universe:
                violations.append(f"drop {sym}: not in universe (hallucinated)")
                continue
            w = float(w)
            # 3. per-name cap
            if abs(w) > self.max_position:
                violations.append(f"clip {sym}: {w:+.3f} -> cap {self.max_position}")
                w = math.copysign(self.max_position, w)
            # 4. liquidity cap
            a = adv.get(sym.upper())
            if a is not None and a > 0:
                max_w = self.adv_cap * a / self.nav
                if abs(w) > max_w:
                    violations.append(f"clip {sym}: liquidity {abs(w):.3f}->{max_w:.3f}")
                    w = math.copysign(max_w, w)
            clean[sym.upper()] = w

        w = pd.Series(clean, dtype=float)
        if w.empty:
            return WrapperResult(weights=w, violations=violations)

        # 5. gross cap
        gross = w.abs().sum()
        if gross > self.max_gross:
            violations.append(f"scale gross {gross:.3f} -> {self.max_gross}")
            w = w * (self.max_gross / gross)

        # 6. turnover throttle (vs current book)
        all_syms = w.index.union(current.index)
        w_full = w.reindex(all_syms).fillna(0.0)
        cur_full = current.reindex(all_syms).fillna(0.0)
        turnover = (w_full - cur_full).abs().sum()
        if turnover > self.max_turnover and turnover > 0:
            scale = self.max_turnover / turnover
            violations.append(f"throttle turnover {turnover:.3f} -> {self.max_turnover}")
            w_full = cur_full + (w_full - cur_full) * scale
            w = w_full[w_full.abs() > 1e-9]

        return WrapperResult(weights=w, violations=violations)
