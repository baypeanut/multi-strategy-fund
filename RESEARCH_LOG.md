# Research Log - what we tried, what worked, what didn't

A cumulative lab notebook. Every experiment, hypothesis, and result (positive OR
negative) goes here so we never repeat a dead end and can mine past findings.
**Negative results are first-class** - knowing what does NOT work is the point.

**How to append:** add a dated entry under "Experiment Log" using the template.
Tag each with a verdict: ✅ positive · ❌ negative · ⚪ inconclusive · 🔧 engineering.
Keep the "Running Scorecard" and "Open Hypotheses" sections updated.

---

## 0. Conceptual findings (strategy design, pre-build)

| # | Question | Finding | Verdict |
|---|---|---|---|
| C1 | Can a $50 autonomous agent self-finance? | No - its own **cognition/inference burn** dominates; pure-autonomous trading dies (~65-75% in 7d). Survival comes from human-economy earning, which breaks "autonomy". | ❌ |
| C2 | What capital removes ruin risk? | **~$250k floor** (costs stop being fatal), **$1-2M** for full institutional stack. No amount guarantees "exponential". | ✅ |
| C3 | Is exponential growth achievable? | No - it's **logistic (S-curve)**. Edge and AUM are inversely related; best edges are capacity-limited. Sweet spot ~$250k-$1M. | ❌ (myth) |
| C4 | Right structure to prove edge? | **4-book horse race + ensemble** (S1 quant control / S2 news / S3 LLM / S4 combined), paper-proof before real money, judged by DSR+PBO. | ✅ (adopted) |
| C5 | Does paper trading prove profitability? | No - it proves **plumbing**. Statistical proof of edge needs ~(2/SR)² years. 2-3mo proves the system runs + gives an early signal, not a verdict. | ⚪ (manage expectations) |

---

## 1. Running Scorecard

### What works (keep)
- ✅ **Validation framework** (walk-forward + Deflated Sharpe + PBO) - correctly rejects no-edge strategies. This is the most valuable asset.
- ✅ **Pessimistic cost model** (square-root impact) - impact scales correctly (16× size → 4× cost), verified.
- ✅ **Free data stack** ($0): yfinance, ccxt, SEC EDGAR, FRED - all live-verified.
- ✅ **Hard risk wrapper** around LLM PM - drops hallucinations, enforces limits; LLM never sends orders.
- ✅ **Risk governor** circuit breakers (DD gates, daily kill, VaR, gross cap).

### What does NOT work / no edge found (avoid / needs change)
- ❌ **Naive S1 quant on liquid large-caps** (12-1 momentum + 5d reversal + low-vol, market-neutral): 2y backtest **Sharpe -0.17 equities, 0.37 crypto but PBO 0.70 (overfit)**, combined -0.06. DSR fails both. Textbook signals on crowded liquid names = no free edge. *Expected.*
- ❌ **Raw 8-K filing dates as a directional signal**: 130 real events, CAR(0,5) ~symmetric (44% positive), mean -1.3%. |CAR| 4.1% (events DO move) but **direction needs sentiment filtering** - dates alone are noise.
- ⚪ **Per-name cap binding flattens equity weights** (~all at cap): symptom of weak/undifferentiated signal, not a bug.
- ❌ **Pre-W3 book comparison was invalid** (S3 not vol-targeted; "lost less" = lower gross, not skill). Any S1-vs-S3 comparison from before 2026-07-02 is void; the clock for H-A starts at the W4 deploy.

### Engineering gotchas (🔧)
- 🔧 Python 3.14 (local AND Hetzner server) - `urllib` lacks CA certs → **must use `certifi`** SSL context for EDGAR/FRED (yfinance/ccxt unaffected).
- 🔧 Ledoit-Wolf `rho` term must use the proper constant-correlation formula (first draft was wrong; rewritten + PSD-tested).
- 🔧 `binanceus` crypto data is **thin/low-liquidity** and looked stale (BTC ~$64k in one pull) - switch exchange for real crypto signals.
- 🔧 Backtest cost computed on initial NAV (not running equity) - negligible (<0.1%) at small DDs; documented simplification.
- 🔧 Single integrated service (runtime + dashboard in one process) is cleaner than the old two-service setup.

---

## 2. Experiment Log

### 2026-06-17 - E1: Naive cross-sectional quant (S1) ❌
- **Hypothesis:** 12-1 momentum + short-term reversal + low-vol, blended & vol-targeted, has edge on 41 liquid US large-caps + 8 crypto.
- **Method:** walk-forward backtest (21d rebalance, 252d warmup, 2y), 6-config grid, realistic costs. Metrics: Sharpe, DSR (N=6), PBO (CSCV).
- **Result:** Equities Sharpe -0.17 (DSR 0.06), Crypto 0.37 (DSR 0.25, PBO 0.70), Combined -0.06.
- **Verdict:** ❌ No significant edge. Crypto looked ok on Sharpe but PBO flags overfit.
- **Implication:** S1 stays as the **control group**. Don't tune to green (DSR/PBO would punish it). Edge, if any, must come from S2/S3 beating this baseline.

### 2026-06-17 - E2: 8-K event study (S2 foundation) ❌/⚪
- **Hypothesis:** abnormal returns around 8-K filings are tradable.
- **Method:** 130 real 8-Ks (6 tickers, EDGAR), market-model CAR(0,5) vs SPY.
- **Result:** mean CAR -1.3%, median -0.3%, 44% positive, mean |CAR| 4.1%.
- **Verdict:** ⚪ Events move prices (|CAR| 4%) but **no directional edge from dates alone**.
- **Implication:** S2 needs **sentiment/materiality filtering** to separate tradable events. The scorer + dedup pipeline exists; needs a live scored-news feed to test directionally.

### 2026-06-17 - E3: Validation framework sanity ✅
- **Hypothesis:** DSR + PBO correctly distinguish real edge from luck/overfit.
- **Method:** synthetic tests - dominant config → low PBO; pure noise → PBO ~0.5; more trials → lower DSR.
- **Result:** all hold.
- **Verdict:** ✅ Framework trustworthy. This is our guard against self-deception.

### 2026-06-17 - E4: 4-book live paper deploy ✅ (plumbing)
- **Method:** deployed to Hetzner, single service, dashboard :8080, 4 shadow equity curves, governor on combined book.
- **Result:** active, NRestarts=0, tick ran (equities+crypto weights), hyrox untouched.
- **Verdict:** ✅ Plumbing works. 2-3mo paper-proof clock starts. (No edge claim.)

---

### 2026-06-23 - E5: 5-day live paper readout ⚪
- **Setup:** 126 hourly ticks, 6/18→6/23, no restarts, no governor breaches.
- **Result:** S1 +1.02%, S2 +0.00% (neutral), S3 +1.07%, S4 +1.04%. Reported Sharpe ~1.4 but t-stat ≈ SR·√(5/252) ≈ **0.20** - statistically insignificant.
- **Verdict:** ⚪ Noise, not edge. All books up ~1% but 5 days ≈ 4 trading days; needs ~2y for significance. **S3 ≈ S1 → no evidence the LLM book beats the control yet.** Does NOT contradict E1 (2y backtest: no edge); a favorable micro-period.
- **Caveat (🔧):** hourly-mark Sharpe annualized with √252 is methodologically wrong (equities only move on daily close → flat intra-day segments). Need daily-resampled returns for an honest live Sharpe. **TODO: resample equity_history to daily in the metric.**
- **Don't:** let a green week override the long-horizon backtest or the DSR/PBO bar.

### 2026-06-29 - E6: 11-day live readout - the +1% reversed ⚪
- **Setup:** 264 ticks, 6/18→6/29, no restarts, no governor breaches (max DD -3.4% vs -10% gate).
- **Result:** S1 -1.94%, S2 0.00%, S3 -1.02%, S4 -1.48%. The +1% from E5 (day 5) **fully reversed and went negative** - sign flipped.
- **Verdict:** ⚪ Textbook proof that the 5-day gain was noise, not edge. 11 days still statistically zero; don't over-read the drawdown either.
- **Note:** S3 lost less than S1 (-1.0% vs -1.9%) but this is NOT alpha - S3 runs lower gross via regime risk_scale (0.74), so smaller moves both ways. Lower DD = lower exposure, not skill.
- **Regime shift:** VIX 16.4→18.9 (72nd pct), breadth 0.54→0.49 - mild risk pressure, trend still risk_on.
- **Lesson reinforced:** a green week and a red week are equally meaningless; only months + DSR/PBO decide.

### 2026-07-02 - E7: Outside-quant audit + remediation (W0–W6) 🔧✅
**Audit findings (8), all remediated same day:**
1. 🔴 **Horse race was fake** - S3's only input was S1's scores + zero S2; no LLM call existed anywhere. S3 ≈ rescaled S1, S4 ensemble diversification ≈ 0. → Fixed by W4a (real Ollama LLM PM, `pm_source` audited) + W4b (live news into S2 and into S3's briefing).
2. 🔴 **Survivorship bias** - universe = today's mega-caps; backtests inflated by construction. → Confessed in SPEC §8; PIT universe is future work. (Note: even biased, backtest showed no edge.)
3. 🔴 **Unfair risk comparison** - S1 vol-targeted, S3 wasn't; "S3 lost less" was just lower gross. → W3: all four books share one ex-ante 10% vol normalization; regime scales the *target*, not raw gross.
4. 🟠 **"Daily" kill-switch ran on hourly ticks** - a -5% day spread over 24 ticks never triggered. → W1: governor resamples to calendar-day closes; regression-tested (slow-bleed now halts).
5. 🟠 **No data-quality guard** - partial yfinance fetch would fabricate turnover silently. → W2: coverage thresholds (eq≥0.90, cx≥0.75, SPY/VIX required), NO-TRADE tick, incidents, Telegram alerts, cache fallback, watchdog cron.
6. 🟠 **Backtest vs live accounting divergence** - PaperBroker unused live. → Documented in SPEC/appendix; unify before scale-up.
7. 🟡 **Spec-code drift** - IC-weighting/OU/pairs/Kelly/ERC on paper only. → Appendix now has an implementation-status table.
8. 🟡 **No benchmark** - → W5: SPY shadow book in state + dashboard, "vs SPY" KPI.

**Strategic point:** resources were going to books our own E1 already falsified, while the one live-edge hypothesis (news+LLM judgment) sat idle. As of today the real experiment is actually running.

**PRE-REGISTERED HYPOTHESES (locked 2026-07-02, evaluation ≥60 trading days):**
- **H-A:** S3(LLM) beats S1(control): mean daily diff > 0 with DM/Newey-West p<0.05.
- **H-B:** S2(news) alone has positive edge: Sharpe > 0 and DSR-significant.
- **H-C:** S4 ensemble Sharpe > max(S1, S2, S3) - diversification is real.
- **Decision rule (do not move):** no superiority/edge claim before 60 trading days AND p<0.05. Early looks are health checks only. Dashboard shows `verdict_allowed` computed server-side.

**Infra facts for reproducibility:** qwen2.5:3b-instruct via Ollama (CPU, keep_alive=2m, MemoryMax=2200M), 2GB swap added, OOM priority hyrox(-500)/trader(+500), Telegram alerts share hyrox bot with [FUND] prefix, state schema v2 (additive).

### 2026-07-03 - E8: Universe expansion (user request) 🔧
- **Request:** add MU, EOSE, RCKT, TEM, MRVL to the tradeable universe.
- **Liquidity check (20d ADV, rule >$50M):** MU $61.8B ✅ · MRVL $18.4B ✅ · TEM $389M ✅ · EOSE $149M ✅ · **RCKT $10M ❌ REJECTED** (5x below floor; pre-registered rule not bent).
- **Added:** MU, MRVL, TEM, EOSE → equity universe now 45 names. Note EOSE daily vol 7.2% (hot name - vol-targeting will size it small; per-name/ADV caps apply as everywhere).
- **Experiment impact:** universe change hits ALL books simultaneously → the paired S3-vs-S1 comparison stays fair (same opportunity set both sides). Clock restarts anyway at final config (frontier LLM integration).

### 2026-07-03 - E9: H1 - reaction-conditioned 8-K event study ❌ (near-miss, honest FAIL)
- **Design (pre-registered):** 1,529 8-Ks (3y, 45 names, EDGAR item codes), market-model AR vs SPY, condition = sign of AR(0,1), outcome = CAR(2,10)/(2,20), month-block bootstrap, Bonferroni (8 tests), split-half. Promotion bar: ≥50bps net AND t≥2.5 AND stable.
- **Results:** H1a ✅ events move (|AR(0,1)| mean 3.51%). H1b: ALL CAR(2,10) **+0.62% (t=1.58)**, earnings-only +0.91% (t=1.64), split-half stable (+0.71/+0.54) - right direction, **below the bar**. CAR(2,20) ≈ 0 → gain reverses by day 20 (fragile, not robust drift).
- **Verdict:** ❌ FAIL at pre-registered thresholds - thresholds not bent post-hoc. No promotion.
- **Power note:** resolving a true +0.6% at t≥2.5 needs ~4,000 events → S&P500-wide rerun queued (F1). H1c (press-release text sentiment) deferred to frontier-LLM scorer (F2) - lexicon can't discriminate uniformly-spun PR boilerplate. F3: magnitude terciles + (2,5) exit before reversion.
- **Engineering (🔧):** pandas trap - a column named `items` collides with `Series.items` method; `ev.items` returns the bound method. Use `ev["items"]`. Cost: silently zero usable events. Also: yfinance burst rate-limits on 45-name×4y fetches → research scripts must disk-cache prices (added).
- Full report: `research/H1_REPORT.md`, data: `research/h1_events.csv`.

### 2026-07-05 - E10: Key-readiness build (full audit + integration layers) 🔧✅
- **Audit result:** system healthy (473 ticks, 131/131 LLM decisions, 3,909 headlines, 0 incidents, 78 tests) but NOT plug-and-play for API keys - 5 gaps found and closed same day:
  1. **Anthropic integration built** (official SDK, structured outputs): `AnthropicPM` (S3, claude-sonnet-5, ~24 calls/day structural cap) + `AnthropicScorer` (S2, claude-haiku-4-5, TieredScorer budget-capped). Full chain: anthropic -> ollama -> heuristic/lexicon; every hop audited via `pm_source`/counts. Refusal/rate-limit/no-key all fall back - the loop can never stall on an API.
  2. **Polygon provider + factory**: `POLYGON_API_KEY` in .env auto-upgrades the data layer next tick; removal falls back to yfinance; W2 guard covers misbehavior. Dashboard shows active provider.
  3. **Sharadar provider** (research lane) for the PIT/survivorship-clean backtest (fixes audit finding #2 when key arrives).
  4. **Sector cap (25%) now ENFORCED in the governor** - was in SPEC §4 but absent from code (audit finding); sector map for all 45+8 names in universe.py; tested.
  5. **Central `core/env.py`** - key presence = activation; zero code changes needed when keys land.
- **Honest remaining blocker:** IBKR paper execution cannot be pre-built blind (needs the approved account to integrate/test against). Everything else is now literally "paste the key into .env".
- 90 tests passing. Clock note: first Anthropic-brained tick = final-config candidate; 60-day clock restarts there (per E7 rules).

### 2026-07-09 - E11: Head-quant trade-correctness & data-integrity audit 🔧✅
**Verified CLEAN (hands-on, real data):**
- **Crypto feed NOT stale** (earlier suspicion wrong): BTC $61,919 cross-checked vs Coinbase $61,974 / Kraken $61,962 - three sources within 0.1%. BinanceUS is fine; BTC really is ~$62k (bear market). Lesson: verify before "fixing".
- **Polygon == yfinance closes** (AAPL 313.39 both) - provider swap introduced no data shift.
- **S1 signals fire at the right points:** momentum manually recomputed (CAT 1.318 == signal 1.318); live positions match signal directions (CAT long = top momentum + reversal-buy ✓, ADBE short = worst momentum ✓, GE long = strongest reversal ✓). S2 direction coherent (negative AAPL headline -0.8/-1.0 → short AAPL). S3 (Claude, first decision): 12-name concentrated book, longs CAT/JPM/MS/GE consistent with briefing.
**Found & FIXED:**
1. ❌ **Hidden churn cost:** hourly ticks re-derived weights from continuously-decaying news z-scores → micro-rebalances even on closed markets (measured: -$15–20/tick on Sunday, ≈1.5–2%/yr friction). **Fix: information-gated rebalancing** - books re-decide ONLY on a new daily bar or a material news shift (z quantized at 0.25); otherwise weights held exactly (zero turnover, zero LLM calls).
2. ❌ **Claude spend:** 24 S3 calls/day ≈ $20–32/mo vs $30 budget. Gate cuts to ~1–5 calls/day (≈$2–6/mo). Plus hard daily caps in state (`pm` 8/day, `scorer` 200/day) - beyond caps the fallback chain serves.
3. ❌ **Clock contamination:** paired test counted n=21 days including the ollama era. **Fix: `clock_start`** stamped at first anthropic decision; S3-vs-S1 test now filters to days >= clock_start.
**Known & accepted (logged, not fixed):** equity marking uses price returns off adjusted closes with stale prev-price basis across ex-div dates → dividends not credited (~1.5%/yr understatement on long sleeve, near-symmetric across books so the paired comparison is barely affected). Unify with broker total-return accounting at IBKR integration.

### 2026-07-09 - E12: S3 brain upgraded to Opus 4.8 (budget raised to ~$10/wk) 🔧
- User raised the Claude budget to ~$10/week. Head-quant allocation: upgrade the
  S3 PM from claude-sonnet-5 to **claude-opus-4-8** (+ explicit adaptive
  thinking - NOT default-on for Opus when omitted). Rationale: H-A tests
  "frontier judgment vs quant control" - test it with the strongest brain; the
  original Sonnet choice was purely a cost call that the info-gate obsoleted.
- **Timing deliberate:** clock had started 2026-07-09 with n≈0 - switching the
  same day costs zero clock time. `clock_start` cleared server-side to re-stamp
  at the first Opus decision (same date). This is the FINAL final config; any
  later model change restarts the 60-day clock again.
- Caps raised: pm 12/day, scorer 400/day; s2_z_round 0.25→0.20 (slightly more
  news-responsive). Projected spend at gate cadence: **~$3-4/week** (Opus PM
  ~3-5 calls/day ≈ $0.07-0.10/call incl. thinking; Haiku scorer pennies)
  comfortable headroom under $10/wk even on news-heavy days.

### 2026-07-09 - E13: Universe 45 -> 501 names + two-tier data architecture 🔧✅
- **Breadth:** IR ≈ IC·√breadth - the single highest-leverage structural move.
  Universe now top-500-ADV US common stocks (floor $291M ADV, ETFs excluded via
  reference-tickers CS filter) + USER_PICKS force-included (EOSE was outside
  top-500). Sectors via SIC codes (ticker-details), governor's 25% cap scales.
  Universe FROZEN for the run; regeneration only at config windows.
- **Two-tier data:** light tick = ONE grouped-daily call (whole market) + 8
  crypto tickers; heavy tick (fingerprint change only) = parallel full-history
  fetch. API traffic ~20x down vs naive 500-name hourly fetching.
- **Ledoit-Wolf vectorized:** closed-form rho (theta_ii = Y³ᵀY/T − var·S);
  exact match to the loop (rtol 1e-10), n=500 in 0.01s (loop: minutes).
- **S3 briefing focused:** top-40 |S1 z| ∪ news names ∪ current holdings, cap
  60 - 500-name briefings would be ~20k tokens of noise per PM call.
- **News coverage split:** RSS = top-150 ADV subset (parallel), EDGAR 8-K =
  full universe every 6th tick. Honest note: headline breadth is top-slice.
- **Live proof (tick 510):** rebalance 60s / RSS 306MB on the existing
  2vCPU/4GB box -> the $42/mo server upgrade was declined on evidence.
  S1 npos 503, S2 npos 116 (news breadth 44->116), S3 npos 18 (focused).
- **Clock restarted (universe = config change): 2026-07-09, FINAL config =
  Opus 4.8 + Polygon + 501 names. No further config changes.**

### 2026-07-09 - E14: Nightly research agent live 🔧✅
- systemd timer 04:30 UTC: pops a hypothesis from research/hypotheses.yaml,
  runs it (signal_backtest | event_study - parameterized specs only, NEVER
  arbitrary code), records to RESULTS.jsonl, Telegram digest.
- **Global multiple-testing accounting:** registry.json counts EVERY experiment
  ever (seeded 14 for the hand-run history); DSR deflates against the
  cumulative N. Without this, an automated researcher is a data-snooping
  machine - this is the design's core honesty mechanism.
- Claude (Opus) proposes new hypotheses when the queue runs thin - inside a
  constrained parameter grammar, schema-validated, budget-capped (research
  proposals share the daily PM caps). **Agent cannot touch live config; PASS
  candidates are flagged for human review only.**
- Seeded queue: momentum-only@breadth, F1 wide earnings-drift (H1 power fix,
  ~10x events), F1b all-8K@breadth, fast-reversal@5d.

### 2026-07-10 - E15: F1 readout (major negative) + gate defect found & fixed ❌🔧
- **F1 (nightly N0016), THE power-fixed earnings-drift test:** 5,770 events
  (10.6x H1's 542), reaction-conditioned drift CAR(2,10) = **+0.63%, t=1.90 ->
  FAIL** at the pre-registered t>=2.5 bar. The H1 point estimate (+0.91%)
  SHRANK with 10x the power - the signature of a marginal effect, not a
  discovery. Honest read: earnings-reaction drift is real-ish but ~0.6% gross
  per event, borderline vs costs, NOT promotable. **This materially weakens
  the news-drift leg of the S2 thesis.** (S2 still tests LLM-scored direction
  live, which is a different claim than date+reaction conditioning.)
- **Gate defect (mine):** the quantum-crossing fingerprint failed at 501
  names - with ~1000 live scored items, decay pushed some name across a 0.20
  grid line nearly every hour, so the gate never held: 40 rebalances in ~43
  ticks, PM budget (12) exhausted by noon, **18 S3 decisions fell to the
  heuristic inside the final-config window** (attribution contamination).
- **Fix:** materiality gate - rebalance only on a new daily bar OR a name's
  decayed score moving >=0.5 vs the snapshot AT LAST REBALANCE. Pure decay
  (<0.1/h) cannot trigger; a real headline (conf x materiality >= 0.5) can.
  Expected cadence 2-6 rebalances/day. Tests added (110 total).
- **Clock reset (3rd and FINAL):** n was 1 day and that day was contaminated
  by heuristic fallback - resetting costs nothing and restores a clean
  "S3 = Opus" attribution. Any future reset requires exceptional justification.

### 2026-07-13 - E16: F1b PASS - first pre-registered bar cleared ✅ (candidate, NOT deployed)
- **N0019 (nightly, all-8K reaction-conditioned drift @ breadth):** 17,573
  events (500 names, all item types), CAR(2,10) pos-vs-neg reaction spread
  = **+1.19%, t=4.14 -> PASS** (bar t>=2.5). p≈3.5e-5, survives Bonferroni
  across the global N≈20 experiments (0.05/20=0.0025).
- **Why it passed where F1 (earnings-only) failed:** the effect is STRONGER for
  the full 8-K set than for earnings alone (+1.19% vs +0.63%). Mechanistically
  plausible - generalized PEAD: earnings are the most-scrutinized, most
  efficiently-priced events; obscure 8-Ks (item 8.01 "other", 5.02 mgmt change)
  have slower price discovery. Not a data artifact.
- **Head-quant stance: PROMISING, NOT YET BELIEVED. Three validations required
  before this becomes real capital or a live sleeve:**
  1. Net-of-cost: +1.19% gross; enter day+1 close, exit day+10 -> ~20-40bps
     round trip on these liquid names -> net ~0.8-1.0%. Needs the real cost
     model run, not a back-of-envelope.
  2. Survivorship-clean re-test (universe is today's top-500; reaction-
     conditioning is robust to level bias but not immune) - this is exactly
     what Sharadar/PIT data would settle.
  3. Recency/OOS split: does it hold in the last 12 months, not just pooled 4y?
- **This is an EVENT-DRIVEN signal - a NEW sleeve (S5?), not a fix to S2/S3.**
  S2/S3 trade LLM-scored directional news, a different claim. Do not conflate.
- Queued follow-ups for the nightly agent: F1c (recency split), F1d (per-item
  breakdown to locate the drift), cost-net variant.

### 2026-07-14 - E15b: S3 holds on budget exhaustion (attribution hygiene) 🔧
- The materiality gate (E15) still fired often enough to exhaust the 12/day PM
  cap on active days -> ~2 S3 decisions/day were falling to the heuristic brain
  INSIDE the clean-clock window (mild attribution contamination, growing
  heuristic count 19->26 over 3 days).
- **Fix:** when the PM budget is exhausted on a rebalance tick, S3 HOLDS its
  existing book (source="held") instead of computing with a weaker brain. A
  held day is a legitimate "no new decision", keeps S3 = pure Opus. PM cap
  raised 12->16 (~$8/wk, within budget) for headroom. Clock NOT reset (holding
  doesn't contaminate; past heuristic days are the only smear and they predate
  this note by <3 days - acceptable, documented). 112 tests.

### 2026-07-14 - E17: Armored harness + Fable-5 Research Director live 🔧✅
- **Snooping defense, mechanical (not trust-based):** (1) hash+timestamp
  pre-registration before any run - edits are new trials; (2) hypothesis
  FAMILIES with within-family Bonferroni (p x trials < 0.05) on top of global
  DSR deflation; (3) **LOCKBOX** 2021-07-15..2023-07-14 (untouched by every
  prior run; chosen over recent data because F1b's pooled window already
  consumed it) - candidate families get ONE confirmation shot there; fail =
  family BURNED, ledger rejects all future variants; (4) frozen bars in code,
  hashed into every result; (5) primitives own all data access, specs cannot
  choose dates; (6) append-only ledger written only by the harness.
- **Confirmation instrument (pre-registered): calendar-time long/short
  portfolio, cost-net** - pooled event CARs overlap in time and overstate t
  (F1b's t=4.14 is likely inflated: ~17 events/day over 4y); calendar-time
  daily returns don't, AND the artifact doubles as the S5 sleeve's backtest.
  Bar: t>=2.0 and positive net annualized return, one shot.
- **Fable-5 Director** (1M context reads full history) designs 0-3 next
  experiments/night inside the grammar; specs pre-registered at proposal time;
  server-side fallback to Opus 4.8; budget 2 sessions/night. **Opus Referee**
  writes an adversarial memo on every PASS/CONFIRMED (cannot block - judge is
  code). Graceful degradation: no key/budget -> v1 static-queue behavior.
- **Instruments decision:** options REJECTED (no vol signal; single-name
  option spreads 1-5% vs the ~1% effects we hunt); futures REJECTED for paper
  (SPY-short hedge is modeled equivalently; ES revisited at real capital).
- Known limitation: Polygon 5y price history clips lockbox estimation windows
  -> effective confirmable events ~2022-06..2023-07 (~1y, still ample for the
  calendar-time test).
- Cost: director ~$1-1.5/night + referee ~$0.2 => ~$35-55/mo on top of live
  (~$30) = ~$65-85/mo total, inside approved budget. 126 tests green.
- **First act: the 8k-drift family's one lockbox shot fires tonight.**

### 2026-07-14 - E17: Armored research harness + Fable director (data-snooping defense)
- Harness v2: hash-locked pre-registration, hypothesis families with within-family
  Bonferroni, **LOCKBOX** (2021-07-15..2023-07-14, untouched by any run) with a
  ONE-SHOT confirmation per family (fail => family BURNED forever), frozen bars
  hashed into every result, reproducibility stamps (spec/harness/universe/window).
  Fable-5 director designs tomorrow's queue; Opus adversarial referee memos every
  PASS/CONFIRMED. Agent never writes the ledger, never computes metrics, never
  touches live config.
- **E17b defect (mine):** the calendar-time portfolio was O(days x events) via
  iterrows() -> the first lockbox run hit the 2h systemd timeout and recorded
  NOTHING. Critically the lockbox shot was NOT consumed (crash != burn - the
  armor held). Fixed: vectorized `calendar_time_daily` (searchsorted event
  windows, column arrays) - exact match to the loop (atol 1e-12), 17k events x
  500 days in <1s (was hours). 128 tests.

### 2026-07-14 - E18: 8k-drift CONFIRMED - first double-validated edge candidate ✅✅
- **The `8k-drift` family cleared the lockbox one-shot.** Calendar-time long/short
  event portfolio on the UNTOUCHED 2021-07..2023-07 window (which no exploration
  run ever saw): **Sharpe 1.47, +12.6%/yr, max-DD -6.3%, ~109 avg positions,
  cost-net** (spread+commission+impact). Pre-registered bar t>=2.0 & ann>0 -> CONFIRMED.
- **Adversarial referee (Opus) raised 6 objections; the decisive one (#4,
  autocorrelation inflating the iid t) was TESTED and REVERSED the concern:**
  lag-1 autocorr is -0.08 (mean-reverting, not persistent), so Newey-West t =
  **2.40 > iid 2.07** - the effect is *more* robust, not less. Short-borrow drag
  (~1%/yr on the short leg) trims +12.6% -> ~+12.1%: negligible.
- **Holds across TWO regimes:** explore window 2023-07..now (t=4.14) AND the
  bear-market lockbox (t=2.40 NW). Different macro regimes, same sign.
- **Remaining honest caveat (referee #6, still open): survivorship.** Universe is
  today's top-500; delisted names absent even in the lockbox window. Reaction-
  conditioning (long positive-reaction / short negative) is partly robust to
  level bias but not immune. Only PIT data (Sharadar) fully closes it - OR a
  forward live shadow book, which has no dead-name problem by construction.
- **Verdict: genuine, robust, double-validated candidate - the FIRST this project
  has produced. NOT yet real capital.** Protocol: CONFIRMED => human review.
  Recommended next gate = S5 live forward shadow book (paper, event-driven,
  ZERO further optimization) - the true OOS test that also dissolves the
  survivorship and regime-concentration caveats going forward.

### 2026-07-15 - E19: External audit (second AI reviewer) + Phase-0 fixes 🔧
- User had an independent AI agent audit the whole project. Head-quant verdict
  after verifying every hard claim against the code: **substantially correct.**
  The lab is mature; the custody vault is not. Confirmed defects, all FIXED
  today (130/130 tests green):
  - **W3 fair-race breach:** merged S1 (80/20 eq/crypto) skipped the final
    `scale_to_target_vol` every other book gets -> ex-ante vol mismatch.
    Now normalized identically.
  - **Live S4 was equal-weight, not risk parity** (`system_returns=None`).
    Now fed realized per-book daily returns (>=20 marks) -> inverse-vol
    allocation as documented.
  - **HeuristicPM double-applied regime risk_scale** (PM gross x runtime
    target-vol) -> fallback days over-delevered. Regime now scales target vol
    only.
  - **`max_net` was config-only, unenforced.** Governor now caps net exposure
    (+ tests).
  - **No halt latch:** DD recovery auto-re-risked next tick. Halt now LATCHES
    in state until a human runs `scripts/clear_halt.py` (Telegram on both).
  - **VaR skipped on held ticks** (cov only computed on rebalance). Governor
    now reuses the last covariance in-process.
  - **Short leg was free:** live marking now accrues daily borrow
    (50bps/yr /365) on short gross. Impact on live marking remains a known
    gap (spread+commission only) - flagged for the single-cost-surface work.
  - **Silent-flat marks:** missing price used to contribute 0 return silently;
    now alerts when >2% of fund gross has no mark (STALE-MARK incident).
  - **Ops:** `state.json` writes are atomic (tmp+`os.replace`); dashboard binds
    127.0.0.1 (was 0.0.0.0, unauthenticated); tautological test (`or True`)
    fixed.
- **Accepted-but-deferred (tracked, not fixed today):** single cost surface
  across backtest/live/broker; paired S3-vs-S1 test on a common-universe
  subset; walk-forward 12/3 claim (implement or strike from SPEC); PIT
  universe for research; doc-code drift table; off-box state backup;
  IBKR-blocked items (reconciliation, dividends/CA, locate/borrow feed).
- **Effect on the race:** S1/S4 books change behavior from today -> the
  S3-vs-S1 clock keeps running (both sides get the same fix), but note the
  discontinuity in attribution around 2026-07-15.

### 2026-07-15 - E20: Halt-clear race + firm-wide latch + borrow basis 🔧
- Second-pass review of E19 found residual defects; fixed same day:
  - **`clear_halt.py` was a no-op while the daemon ran:** runtime kept
    `halt_latched` in memory and re-wrote it every `_save`, undoing the disk
    clear. Fix: `_sync_halt_clear_from_disk()` at the start of every tick
    if disk has no latch, memory drops it (no restart required).
  - **Latch was S4-only.** Halt now zeros weights for S1–S4 (firm flat).
  - **Borrow day-count** live used `/365`; `CostModel.borrow_cost` uses
    `/252`. Live now matches `/252`.
  - **STALE-MARK** only watched S4; now fires if any book has >2% unmarked
    gross.
  - Tests: `tests/test_halt_latch.py` (clear-while-alive, all-book flat,
    borrow basis, non-S4 stale mark).
- **Clock note (honest):** E19's "both sides got the same fix" framing was
  too clean - S1 final vol-norm changes only S1. Discontinuity around
  2026-07-15 remains; do not over-read the pooled S3-vs-S1 p-value across
  that date without a split readout.

### 2026-07-18 - E21: Fund Terminal dashboard redesign + ops hygiene + S5 built (dormant) 🔧✅
- **Dashboard redesign:** an outside AI design tool ("Fund Terminal") was
  given as a handoff spec. Reviewed against the codebase before touching
  anything - the visual design was sound but its README fabricated 45 days
  of random-walk history and mis-described the live system (Ollama, 48
  positions, 0 incidents) when the real state was Opus 4.8, 500+ names, real
  incidents. **Rejected as-is; rebuilt the same visual density wired to real
  `state.json`** (server-side H-A stays Newey-West via `state.s3_vs_s1`,
  correlation matrix/activity log/book drill-down all real, `held` PM-source
  bucket added, halt banner added). Also fixed a live gap found in the
  process: dashboard was bound `0.0.0.0` unauthenticated (RESEARCH_LOG E19
  claimed this was fixed - it was not); left as-is per explicit user
  decision (no real money at stake yet, revisit before going live).
- **P&L attribution bug + fix:** the new per-symbol P&L panel snapshotted a
  single tick's price delta. Equities only re-mark once/day (two-tier
  architecture) while crypto re-marks hourly, so viewing the dashboard on
  any tick after the daily bar-rollover showed equities at $0 - only
  crypto's small intraday wiggle survived, dwarfed by the real Day P&L KPI.
  Fixed: `s4_attribution` now accumulates per-symbol $ P&L across all ticks
  sharing a bar_date, reset only on a genuine new bar. Regression test added
  (`test_s4_attribution_accumulates_within_a_bar_resets_on_new_bar`).
- **Nightly research-agent queue defect (mine, from E14/E17):** the agent
  had silently re-run "momentum-only" and "fast reversal" twice each
  (N0017/N0020/N0022/N0023, N0020/N0024) because dedup relied on the
  *mutable* `hypotheses.yaml` `status` field - any rewrite (director
  appending specs, a human edit) could reset `pending` and the same spec
  would fire again, quietly re-inflating the family's Bonferroni trial
  count. This is exactly the data-snooping the armor (E17) exists to
  prevent. **Fixed at the root:** `harness.load_registry()` now maintains an
  immutable `discovery_hashes` set (spec_hash -> result id), migrated once
  from the append-only `RESULTS.jsonl`; `nightly.run_one_from_queue` skips
  (marks `done`, does not re-execute) any spec whose hash already has a
  discovery run, regardless of queue status. Verified on the live registry:
  migration recovered 6 prior discovery runs; `price-factors` family sits at
  11 real trials (not inflated further by tonight's would-be duplicate).
- **S5 - the confirmed 8k-drift edge, built as a forward shadow book,
  left DORMANT (`systems.s5_event.enabled: false`):** `systems/s5_event/`
  (engine + live EDGAR feed) reproduces the CONFIRMED calendar-time spec
  (E18: entry_lag=2, exit_lag=10, min_units=4, n_names=500) with **zero
  free parameters** - verified by a direct parity check against the
  research primitive `calendar_time_daily` across 33 synthetic days
  (exact match on signed exposure + active-unit count every day). Wired
  into `runtime.live` as a fully additive, flag-gated fifth book (own
  equity/weights/history slots, only created when enabled; the S1–S4 race
  is byte-for-byte unchanged while disabled - verified live: `systems`
  keys stayed `{s1,s2,s3,s4}` after deploy, no `s5_*` state keys leaked).
  Dashboard renders S5 as a 5th book (legend/table/correlation/drill-down)
  automatically once `state.systems.s5` exists - nothing else to build.
  **Deliberately not enabled this session** (user decision): flipping the
  config flag starts an irreversible forward-OOS clock under the "zero
  further optimization" rule (E18) and is being left as a discrete human
  action, not bundled into an infra-deploy.
- **Mobile responsiveness fix:** the redesigned dashboard had no `@media`
  rules; 5 multi-column grid rows and an 8-column table collapsed
  illegibly under ~860px. Fixed with proper breakpoints + a horizontally
  scrollable table wrapper; verified in-browser at 375px and 320px
  viewports (real device widths, not just resize) with real server state.
- 148 tests passing (+20 vs E20: 10 S5 engine/feed/runtime, 2 queue-dedupe,
  2 attribution, 1 s3 tests, plus mobile/dashboard verified via live
  browser checks, not unit tests).
- **Deploy hygiene note:** every change this session was verified locally
  against a real copy of production `state.json` (pulled via scp) in a
  browser before touching the live server, and the live server file was
  backed up (timestamped `.bak`) before every overwrite. `paper-trader`
  restarted cleanly 4 times today, `NRestarts=0` throughout - no crash-loop
  risk introduced.


## 4. Principles we've committed to (don't relitigate)
1. **Never tune to the backtest.** DSR/PBO exist to catch it. Negative results stay negative.
2. **Costs stay pessimistic.** No flattering fills.
3. **LLM never sends orders** - always behind the deterministic wrapper.
4. **Paper ≠ proof of edge.** Report the honest read every time.
5. **$0 cost discipline** - free data + local LLM; S1 uses no LLM.

### 2026-07-22 - E27: tick loop froze ~8h on an unbounded IBKR read - bounded, self-healing 🔴🔧
- **Symptom:** daily check found last_tick 456 min stale though paper-trader was
  `active`. Process was blocked on `ep_poll` on an ESTABLISHED socket to
  Gateway (127.0.0.1:4002) - the whole hourly tick loop frozen since ~09:22.
  Watchdog (>26h) had not yet fired.
- **Root cause:** the per-tick read-only IBKR refresh (Cursor's E25 dashboard
  reconciliation) connects to Gateway each tick. `ib_async`'s `connect()` has a
  timeout but its POST-connect ops (accountSummary/positions/reqExecutions/
  placeOrder) do NOT - a Gateway that goes unresponsive mid-operation (its own
  daily restart, or a stale clientId from a killed process) blocks the socket
  read forever, and the read ran on the MAIN thread → the fund stopped marking,
  rebalancing, and gating. A broker link must NEVER be able to freeze the core
  loop. (This is exactly the class of failure the W2 no-trade guard defends
  against for DATA - the same discipline was missing for the BROKER.)
- **Fix (`_ibkr_bounded`):** every IBKR call (mirror + refresh) now runs in a
  daemon thread with its own event loop, joined with a hard timeout (mirror
  120s, refresh 45s). On timeout the thread is abandoned and the tick continues
  - verified live in a worker thread (connect+refresh returns cleanly; a wedged
  socket leaves the MAIN thread in `futex_do_wait` on the join, never `ep_poll`).
  Refresh clientId now rotates (base+1..+8) so an abandoned hung connection
  can't block the next attempt on a clientId collision. Mirror timeout →
  `IBKR-SYNC-TIMEOUT` incident; refresh timeout → quiet `refresh_error` marker.
- **Recovery:** restarting paper-trader unblocked the hang; restarting
  ib-gateway cleared the killed process's stale clientId state (clientId 7 then
  reconnected clean). Post-fix: tick 823 completed with tick_secs 233s (vs the
  hung 23898s), process back in normal `hrtimer_nanosleep`, IBKR mirror/refresh
  healthy, account max-drift back to 0.15pp, no lingering 4002 socket. 196 green.
- **Follow-up (watch, not fixed):** the 2026-07-22 04:30 engineer session
  produced no proposal - Fable-5 emitted >63KB of JSON that truncated
  (`JSONDecodeError`); it's caught and degrades gracefully (nightly completes,
  no proposal that night, self-recovers next run), so it's a robustness
  watch-item, not an outage. Also: extend fund-watchdog to alert on a stale
  tick at a much tighter bound than 26h (an 8h freeze should page within ~2h).

### 2026-07-23 - E28: common-universe S3-vs-S1 DIAGNOSTIC (P0007) + pre-registration of its status ✅🔒
- **Engineer lane confirmed healthy in production:** the 04:39 run completed
  cleanly (9m37s, no JSONDecodeError) and produced P0007 - the E27 max_tokens
  fix works unattended. P0006's stale-tick pager also ran all night on its
  15-min cron, correctly reporting fresh ticks (6-53 min ages, no false page).
- **P0007 APPLIED - common-universe paired readout (E19 backlog item).**
  Motivation is structural, not post-hoc: S1 carries a 20% crypto sleeve that
  S3 *cannot* hold (its briefing is equities-only), so the headline paired test
  mixes "does frontier judgment add alpha?" with "what did crypto do?". The new
  `_mark_common` accrues an equity-only return index for s1/s3 from the SAME
  held weights and SAME marking prices, and `_paired_common` runs the identical
  DM/Newey-West test over the same `clock_start` window -> `s3_vs_s1_common`.
- **Diff audited line-by-line before merge** (not rubber-stamped): exactly three
  insertions in runtime/live.py; `_paired_s3_vs_s1` and its verdict rule are
  byte-identical context; `_mark_common` writes ONLY `common_idx` /
  `common_idx_history` and never touches `sysd["equity"]`, `realized`, or
  `cost` - it structurally cannot contaminate the live books; the readout
  carries `diagnostic_only: True` and NO verdict field, pinned by a test
  (`assert "verdict_allowed" not in out`). Additive schema. 211 tests green.
- **🔒 PRE-REGISTRATION (locked 2026-07-23 at n=11, while the numbers are still
  uninformative - this is the point):**
  1. `s3_vs_s1_common` is **DIAGNOSTIC ONLY**. It may never be cited as the
     superiority test for H-A.
  2. The registered FULL-BOOK rule (`s3_vs_s1`, n>=60 AND p<0.05) remains the
     **sole decision authority**, unchanged.
  3. At readout BOTH are reported honestly. If they materially disagree, that
     disagreement is itself the finding (and the reason to design the successor
     experiment on a common universe from the start) - it is NOT a licence to
     swap rules post-hoc.
  4. If we ever want the common-universe test to BE the decision rule, that
     requires a FRESH pre-registration and a FRESH 60-day clock.
  Rationale for allowing a second number at all: it is ONE structurally
  motivated comparison that was already on the E19 backlog before any data was
  seen - categorically different from a many-comparison sweep. (Note the
  contrast: the same night, Fable correctly DEFERRED the director's per-item
  breakdown wish as an unregistered many-comparison peek needing a head-quant
  policy call. That instinct was right; this one clears the bar, the other
  does not - and the difference is pre-identification + single comparison.)
- **Honest note on the registered test:** this exposes a real design weakness in
  H-A as originally registered (crypto confound). Discipline says we do NOT
  change the registered rule mid-flight; we run the diagnostic alongside, report
  both, and build the successor experiment better. Logged so the eventual
  readout cannot pretend the confound was unknown.

### 2026-07-24 - E29: the agent caught my incomplete E27 fix (P0008) 🔴🔧
- **Live bug, found by the engineer lane before I did.** E27 bounded the IBKR
  calls so a hung Gateway socket can't freeze the tick loop - and gave the
  read-only REFRESH path a rotating clientId precisely so an abandoned
  connection's stale id can't block the next attempt. **I did not apply the
  same rotation to the MIRROR (trade) path**, which kept the fixed base id.
  The live incident ring shows exactly what that costs:
  `19:16 IBKR-SYNC-TIMEOUT (thread abandoned, as designed)` → then
  `IBKR-SYNC-FAIL TimeoutError` at 20:19, 21:20, 23:24 - every hourly
  rebalance sync collided on "clientId already in use" against the abandoned
  thread's id and hung to ib_async's 25s connect timeout, until the Gateway's
  own ~23:45 daily restart cleared it.
- **Severity was worse than "orders didn't sync" (Fable flagged this, correctly):
  while wedged, a halt-flatten could not reach the IBKR paper account.** The
  internal books would have gone flat on a governor halt while the real paper
  positions stayed on - a genuine risk-control gap, not just missed fills.
- **P0008 APPLIED.** One hunk in `_mirror_to_ibkr`: copy the cfg (so the shared
  CONFIG dict is never mutated - the refresh path already did this) and rotate
  `client_id` to `base+11 + ticks%8`. Verified the bands are disjoint by
  construction: refresh 8-15, manual CLI 17, mirror 18-25 - no path can
  collide with another, and the fixed base id is retired from live use.
  215 tests green (4 new rotation tests, broker faked, no network).
- **Process note worth keeping:** this is the first time the engineer lane
  caught a defect in MY OWN fix from the previous day, diagnosed it from the
  live incident ring, and proposed the minimal correct patch. The E22 lane is
  earning its keep as a reviewer of the reviewer, not just a backlog worker.
- Night otherwise clean: research run 8m19s, no JSON truncation; P0006's
  stale-tick pager ran all night on its 15-min cron with **0 pages** (tick ages
  14-29 min, no false positives); runtime tick 869 at 95s; SPY -0.88% while the
  market-neutral books improved (S1 +0.45%, S2 +0.69%, S4 -0.81%).
  Registered H-A readout n=13 (mean -13.1 bps/d, p=0.12) - still noise, still
  `verdict_allowed: False`, exactly as the pre-registered rule requires.

---

