# H1 Report - Reaction-Conditioned 8-K Event Study
**Date:** 2026-07-03 · **Verdict: FAIL** (pre-registered thresholds) · Data: `h1_events.csv`

## Design (pre-registered before running)
- Universe: our 45 tradeable names; all 8-Ks over 3y from EDGAR with item codes → **1,529 usable events** (544 earnings/2.02, 306 exec-changes/5.02, 84 agreements/1.01, …).
- Market-model AR vs SPY (est. window −250..−20). Condition: sign of announcement reaction AR(0,1). Outcome: drift CAR(2,10), CAR(2,20) - enterable after day+1 close.
- Stats: month-block bootstrap SE (earnings clustering), 8 tests → Bonferroni α=0.0063 (t≈2.5), split-half stability.
- Promotion bar: diff ≥ 50bps net of ~10bps costs AND boot-t ≥ 2.5 AND split-half same sign.

## Results
| Test | diff (pos−neg) | boot-t | n |
|---|---|---|---|
| **ALL, CAR(2,10)** | **+0.62%** | **+1.58** | 718/805 |
| 2.02 (earnings), CAR(2,10) | +0.91% | +1.64 | 250/292 |
| 5.02, CAR(2,10) | +0.24% | +0.30 | 133/170 |
| ALL, CAR(2,20) | +0.10% | +0.17 | - |
| Split-half CAR(2,10) | +0.71% / +0.54% | 1.47 / 1.00 | same sign ✓ |

H1a confirmed at scale: events move prices - |AR(0,1)| mean **3.51%** (n=1,529).

## Honest read
1. **Direction is right and stable** (+62bps, both halves positive, strongest in earnings events at +91bps) - the PEAD-style story is *suggestively* present.
2. **But t=1.6 < 2.5**: at our pre-registered bar this is **not distinguishable from luck** after multiple-testing. We do not bend thresholds after seeing data - that is the whole point of pre-registration.
3. **The effect dies by day 20** (CAR(2,20) ≈ 0): days 11–20 give back the days 2–10 gain. Economically this looks like short-lived post-event momentum followed by reversion - a fragile, timing-sensitive effect, not a robust drift.
4. **Power note:** to resolve a true +0.6% effect at t≥2.5 we would need ~4,000 events → a wider research universe (e.g., S&P 500) or longer sample. The test is honest, not final.

## Implications
- **No promotion to live candidate.** S2's premise gets weak directional support, not proof.
- **Follow-ups (queued):**
  - F1: rerun on S&P 500 universe (~10k events) for power - pure research, free data.
  - F2 (H1c): text-sentiment conditioning on 2.02 press releases - deferred until the frontier-LLM scorer exists; lexicon scoring of PR boilerplate (uniformly spun positive) is unlikely to discriminate.
  - F3: interact reaction sign with magnitude (|AR| terciles) and short-window (2,5) exit before the reversion.
