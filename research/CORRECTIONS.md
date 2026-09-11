# Record corrections

Append-only. RESULTS.jsonl and registry.json are mechanically unwritable by
every agent — the graded ledger stays closed at any authority level — so when
a ledger row's LABEL is known to be wrong, the correction lives here and is
tailed into the research director's nightly context. Corrections state facts
about what actually ran; they never re-grade a verdict and never edit a row.
Newest entries at the bottom.

## 2026-08-01 — E49: N0025 is mislabeled; the experiment it names never ran

- N0025 sits in the ledger as "IC-adaptive full blend at breadth (A)", family
  ic-adaptive, verdict FAIL (Sharpe -0.179). The label is wrong.
- What actually ran: the DEFAULT static blend (momentum 1.0 / reversal 0.5 /
  low_vol 0.5, n_names 120, 21d rebalance, NO ic_weighting). The E45 yaml
  recovery restored the two ic-adaptive specs by prereg hash but WITHOUT
  their params blocks, so spec A executed with params={}. Two smoking guns in
  N0025's own metrics: signal_backtest always emits "ic_weighting": true when
  the flag is on (it is absent), and universe_names=120 is the bare default.
- The original spec B ("IC-adaptive without reversal — mechanism control"),
  equally stripped to params={}, hashed identical to stripped-A and was
  marked done as a duplicate of N0025 without ever running.
- What this means for design:
  * NO genuine ic-adaptive experiment has run yet. The re-registered B
    (prereg f3fb81e509d84e1c, params intact) is trial #1 IN SUBSTANCE,
    whatever the family's trial counter says.
  * The family counter includes one non-member run — conservative direction
    only (it raises the Bonferroni bar); no false positive was minted.
  * There is no true A beside B. Whether to spend a further trial on true A
    (full blend WITH ic_weighting) is a fresh multiplicity decision, not a
    re-run of something already tried.
  * N0025's Sharpe -0.179 is a static-blend datapoint consistent with the
    known-toxic reversal leg (N0020 -0.54, N0024 -1.37). It says nothing
    about adaptive combining.
  * The rationale registered with the re-queued B repeats the phantom-A
    claim ("spec A ... failed at Sharpe -0.18"); that sentence will travel
    into N0026's ledger row. This entry is the correction of record for it.
- Class fix already shipped 2026-08-01 (P0022, @0392b1d): validate_spec now
  enforces the prereg hash — a registered spec runs byte-identical to its
  registration or is rejected loudly — and the queue validates BEFORE the
  duplicate-dedupe consult. RESULTS.jsonl itself stays append-only and
  uncorrected, by design.

## 2026-08-03 — E58: N0028 did not run its registered exclusion; A and B were the same experiment

- N0028 sits in the ledger as "IC-adaptive without reversal (B) — mechanism
  control", family ic-adaptive, verdict FAIL (Sharpe -0.465). The label is
  wrong in substance: the registered exclusion (w_reversal: 0.0) bound only
  the warmup rebalances.
- Mechanism: ICWeightedQuantEngine._ic_signal_weights computed trailing ICs
  over a HARDCODED three-signal set and generate() replaced signal_weights
  wholesale after warmup, so a signal registered at static weight 0
  re-entered the book whenever its trailing IC was positive. N0028's own
  ic_diag records reversal frac_active = 0.545 under w_reversal 0.0.
- Consequence: N0027 (true A) and N0028 (B) were the same experiment outside
  warmup — Sharpe -0.460 vs -0.465, max_dd IDENTICAL at -0.1888, identical
  per-signal mean ICs (mom 0.0272, rev 0.0031, lv -0.1635). The A-vs-B
  question ("does negative-IC dropping handle a toxic leg?") is UNANSWERED.
- P0022's prereg-hash armor could not catch this class: the spec ran
  byte-identical to its registration; the CODE dishonored it.
- Class fix shipped 2026-08-03 (engineer lane): the adaptive set is now
  exactly the signals with registered static weight > 0; an excluded
  signal's IC is still measured for diagnostics but it can never be
  allocated. harness_version() covers primitives.py, so post-fix rows are
  mechanically distinguishable from N0027/N0028.
- Both rows stand in the family trial count (conservative — the Bonferroni
  bar only rises). The pending spec C ("IC-gate on momentum alone", prereg
  5305ed3243bebb18) is the first trial that will execute exactly as
  registered; without this fix it would have re-run the full-blend gate a
  third time and the family would have been closed on evidence the design
  never produced.

## 2026-09-10 — Quant audit: event timing, backtest accounting, and research identity

- The old calendar_time_daily selected direction using AR(0,1), then credited
  returns from calendar date + entry_lag. A Friday filing could earn Monday's
  return with a direction unavailable until Monday's close. A synthetic four-
  name fixture with a 10% Monday reaction and no later movement earned 10%
  under the old code and zero under the corrected code.
- On the existing h1_events/h1_prices cache (1,529 events, 752 price dates,
  2023-07-05 through 2026-07-02), keeping entry/exit 2/10 and the approximate
  costs unchanged, the legacy calculation had Sharpe 1.520544 and cumulative
  return 0.789133; the corrected timing has Sharpe 0.649735 and return 0.260420.
  This is a cache diagnostic, NOT a rerun of N0021's 2021–2023 lockbox and NOT
  a new registered strategy trial. It does not quantify N0021's exact bias.
- Old confirmation labels cannot establish live S5 profitability: besides
  the timing defect, the unit-weight calendar calculation does not reproduce
  live covariance sizing, caps, actual turnover, execution timing, or borrow.
  Historical SEC retrieval also ignored extra submission files. The local
  reader now follows relevant historical files, but old samples were not
  automatically expanded or re-graded.
- The general backtest used fixed target weights between rebalance dates,
  implying free daily rebalancing; costs used static initial NAV, warmup
  returns were included, and shorts accrued no borrow. Local corrections
  carry marked holdings, charge drift/current-NAV turnover and borrow, and
  exclude warmup. This does NOT repair the separate live shadow accounting.
- DSR at one trial returned false certainty through an undefined extreme-
  value approximation. It now reduces to PSR against zero. HAC autocovariances
  now use the common sample-size divisor. Neither correction by itself
  establishes that every previously recorded verdict was wrong.
- New harness hashes include imported maths, signals, costs and config,
  which the old two-file hash missed. Existing stamps, RESULTS.jsonl,
  registry.json, trial counts, thresholds and clock history remain intact.
- Status: tested LOCAL changes only; production c2d8d9c was not patched or
  restarted. See QUANT_AUDIT_2026-09-10.md and research/audit_20260909 for
  evidence, reproduction commands, limitations and remaining defects.

## 2026-09-11 UTC — Quant corrections deployed to the paper service

The preceding local corrections were deployed as server aa3f4a1. The writer
now retains client ID 18; per-request timeouts, cooperative job expiry and
non-overlapping broker work replace rotating abandoned writers. Existing
orders already dispatched still require account reconciliation.

On-server baseline 608 passed/1 skipped; candidate 641 passed/1 skipped.
Five execution tests and three additional caught mutations validate the new
failure paths. The first completed tick advanced 2022 to 2023, provider and
accepted basis both polygon, no halt or broker refresh error. No clock reset,
model/strategy change or historical re-grade. See DEPLOYMENT_2026-09-11.md.
