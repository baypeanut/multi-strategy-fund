"""Execution constraints applied AFTER volatility scaling, without renormalizing."""
import math

import pandas as pd


def final_limits(target: pd.Series, current: pd.Series, nav: float,
                 adv: dict[str, float], max_position: float, max_gross: float,
                 adv_cap: float, max_turnover: float | None = None) -> pd.Series:
    if not math.isfinite(nav) or nav <= 0:
        raise ValueError("invalid NAV for final limits")

    def clip(weights):
        clean = {}
        for sym, raw in weights.items():
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(f"non-finite target: {sym}")
            if "/" in sym:  # configured crypto universe is spot-only
                value = max(0.0, value)
            a = adv.get(sym, 0.0)
            cap = max_position
            if a is not None and math.isfinite(a) and a > 0:
                cap = min(cap, adv_cap * a / nav)
            else:
                # No volume estimate: reductions allowed, additions/reversals blocked.
                old = float(current.get(sym, 0.0))
                cap = min(cap, abs(old)) if value * old > 0 else 0.0
            clean[sym] = math.copysign(min(abs(value), cap), value)
        result = pd.Series(clean, dtype=float)
        gross = float(result.abs().sum())
        if gross > max_gross:
            result *= max_gross / gross
        return result

    desired = clip(target)
    if max_turnover is not None:
        # Hard risk exits take priority over a turnover budget. Interpolate
        # only from a feasible current book, so old breaches cannot reappear.
        safe_current = clip(current)
        symbols = current.index.union(desired.index)
        base = safe_current.reindex(symbols, fill_value=0.0)
        old = current.reindex(symbols, fill_value=0.0)
        dest = desired.reindex(symbols, fill_value=0.0)
        forced = float((base - old).abs().sum())
        remaining = max(0.0, max_turnover - forced)
        discretionary = float((dest - base).abs().sum())
        if discretionary > remaining:
            desired = base + (dest - base) * (remaining / discretionary)
    return desired[desired.abs() > 1e-12]


def briefing_focus(positions: dict, quant: pd.Series, news: pd.Series,
                   available: set[str], top_k: int = 40, limit: int = 60) -> list[str]:
    """Stable ordering; every held, priceable name is visible to the PM."""
    held = sorted((s for s, w in positions.items() if w and s in available),
                  key=lambda s: (-abs(positions[s]), s))
    ranked_news = sorted((s for s in news.index if abs(news[s]) >= .5),
                         key=lambda s: (-abs(news[s]), s))
    ranked_quant = sorted(quant.index, key=lambda s: (-abs(quant[s]), s))[:top_k]
    result = list(held)
    for sym in ranked_news + ranked_quant:
        if len(result) >= max(limit, len(held)):
            break
        if sym in available and sym not in result:
            result.append(sym)
    return result
