# System Specification - Multi-Strategy Systematic Fund (v0.1)

Institutional-rigor design. Proof phase = $3M paper; framed for a $100M mandate
with explicit capacity awareness. Numbers below are enforced in
`config/config.yaml`.

## 1. Objectives
| Param | Target |
|---|---|
| Net return | 12–18%/yr *(if edge is real; not guaranteed)* |
| Vol target | 10% annualized |
| Sharpe | 1.0–1.5 |
| Max drawdown | −15% hard cap (halt new risk, human review) |
| Benchmark | SPX total return + cash |

Philosophy: stable, risk-adjusted, *survivable* returns - not "exponential."
Edge is rare and does not scale; targets are deliberately modest.

## 2. Universe
- **Equities (80%)**: S&P 500 + liquid mid-cap, 20d ADV > $50M. Small-cap excluded
  at $100M for impact; same universe kept in proof for consistency. Position ≤ 10% ADV.
- **Crypto (20%)**: BTC, ETH + most-liquid majors only. No memecoins/low-cap. Spot
  only in proof phase (no leverage); funding rate read for the carry signal only.

## 3. Four Books (risk-budget split, not capital)
| System | Risk wt | Decision | Signal |
|---|---|---|---|
| S1 Quant | 0.33 | deterministic code, **no LLM** | price/vol/factor |
| S2 News/Event | 0.33 | LLM as reader | news/filings/macro |
| S3 LLM Discretionary | 0.33 | LLM as PM (hard wrapper) | everything+context |
| S4 Combined | overlay | risk-parity → min-var ensemble | S1+S2+S3 |

Same universe, same limits across books (fair horse race). **S1 is the control
group.** No leverage in proof (gross ≤ 100% NAV).

## 4. Risk Framework (hard rules)
| Control | Limit | Action |
|---|---|---|
| Gross exposure | ≤ 100% NAV | clip |
| Per-name | ≤ 5% NAV | reject |
| Sector | ≤ 25% NAV | clip |
| Liquidity | ≤ 10% of 20d ADV | clip/split |
| Daily VaR (95%) | ≤ 2% NAV | de-risk |
| Single-day loss | −3% NAV | kill-switch |
| Drawdown gate 1 | −10% | sizing ×0.5 |
| Drawdown gate 2 | −15% | halt, cash, human review |

Vol-target sizing to 10%; fractional Kelly 0.25. Stress tests (2008, Mar-2020,
2022) must keep loss within the drawdown cap.

## 5. Execution & Cost (pessimistic)
`TC = half_spread + impact + commission (+ borrow if short)`; impact via
square-root law. Equities: 0 commission, 1.5bps half-spread, Y=0.10. Crypto:
5bps taker, 2.5bps half-spread, Y=0.12. Orders clipped to 10% ADV; VWAP/TWAP
slicing for size. (Implemented in `core/broker/`.)

## 6. Validation (the actual product)
1. In-sample backtest (full costs) → walk-forward OOS → live paper (2–3 months).
2. Multiple-testing penalty: **Deflated Sharpe Ratio**; overfit check via **PBO**.
3. Pre-registered kill criteria: book closed if OOS+paper Sharpe < 0.5; halt at −15% DD.
4. Honest stat-power note: in 2–3 months S1/S4 (many trades) give meaningful signal;
   S3 (few trades) stays fuzzy. Paper proves *plumbing + early edge signal*, not
   certainty.

## 7. Capacity ($3M → $100M)
At $100M, impact is material (modeled), universe shrinks to large/mega-cap and
BTC/ETH + most-liquid alts; niche names in S2/S3 are capacity-limited. Proof-phase
performance will **not** replicate 1:1 at scale - accounted for up front.

## 8. Honest risks
Overfitting (biggest), regime change, LLM model risk/hallucination, crowding/edge
decay, crypto counterparty, short statistical window, scale erosion. Free data has
rate limits / delay - fine for our daily/4h cadence, not for HFT (which we don't do).

**Survivorship bias (known, unfixed):** the equity universe is hand-picked from
TODAY'S mega-caps. Any backtest on it implicitly trades the future's winners in
the past, inflating momentum-style results. Our 2y backtest found no edge even
WITH this tailwind - the honest read is therefore worse, not better. Any future
"edge found" claim on this universe is suspect until re-tested on a
point-in-time constituent list. (See RESEARCH_LOG E7.)

**Backtest/live accounting divergence (known):** backtests charge square-root
impact via PaperBroker; the live loop uses shadow accounting with spread+
commission only. Negligible at $3M book size, material at $100M - must be
unified before any scale-up.
