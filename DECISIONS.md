# Owner decisions, taken

Historical decision record. The September quant audit corrects several
underlying measurement claims; see QUANT_AUDIT_2026-09-10.md. The reset cap
and preservation of existing records remain in force.

Head quant, 2026-08-12. Every item below sat "parked". A parked decision is a
decision to keep the status quo, taken by silence rather than on evidence, so
each is taken here with the measurement that makes it decidable. All are
reversible and each says what would reverse it.

---

## 1. Price basis: stay on price return (Polygon, split-adjusted)

**Measured.** Polygon requests `adjusted=true`, which is split-only. yfinance is
called with `auto_adjust=True`, which is split AND dividend adjusted. Across 58
names of the live universe over one year, dividend-adjusted exceeds split-only
by a mean of 1.17pp, a median of 0.59pp, and up to 4.67pp on the highest
yielders. The live provider is Polygon, so every registered number to date rests
on price return.

**Decision: keep price return. Do not change the basis.**

Changing it re-prices every input to every signal and voids the comparison, for
a gain of roughly a point a year on an absolute target that is not the
registered question. The registered question is S3 versus S1, and both books
are priced from the same bars, so the comparison is unaffected by the basis.

**What is now guarded rather than assumed:** the fallback used to switch bases
silently. A provider mismatch against `state["data_provider"]` now produces a
NO-TRADE tick, so the basis cannot change without a human clearing it.

**Reverses if:** the fund moves to an absolute-return mandate, or dividends
start mattering to a live allocation decision. Then it is a deliberate reset,
priced at whatever the clock is worth that day.

---

## 2. S5 stays outside the drawdown gates

**Measured.** The governor assesses S4 only. S5 is not in S4, so no drawdown
gate, VaR limit or daily-loss kill ever sees it; only the halt latch reaches it,
and that zeroes every book at once.

**Decision: correct as it stands, and it is already registered.**

`tests/test_measurement_design_frozen.py` records this as ASYMMETRY 2 with the
reasoning: S4 is the deployed book and the others are measurement instruments,
and clamping S5 would contaminate the forward-OOS record on the fund's only
confirmed edge. I escalated this as unwritten earlier in the audit and was
wrong - it was written down, in the right place, before I got here.

**Reverses if:** S5 is ever allocated real capital, at which point it stops
being an instrument and needs the same gates as S4.

---

## 3. `max_pm_calls_per_day` stays at 16

**Decision: keep 16, and measure before touching it.**

The cap was the only spend control and it counted CALLS, so the honest answer to
"is 16 right" was unavailable. It is available now: both live model paths record
measured token usage and the runtime converts it to dollars against prices
declared in config. Re-decide after a week of real numbers rather than on the
`~$8/wk headroom` comment, which was an estimate presented as a bound.

**Reverses if:** a week of measured spend shows the cap is either wasteful or
binding on days that matter.

---

## 4. The engineer lane stays off, and stays undeleted

**Decision: disabled in config and in systemd. Not deleted.**

Off, because this week three separate registered controls were each shown
movable with a fully green suite - S5's confirmed spec, the governor's
connection to the book, and the 60-day rule itself. Each was a one-line
plausible-reading diff, which is exactly what the lane's review stage was built
to wave through. All three are pinned now, but the precondition for re-enabling
is dollar accounting, which arrived today and has no track record yet.

Not deleted, because deleting 1,831 lines of auto-applying code plus the ~1,200
lines of tests that guard only it, without having read engineer.py end to end,
would be the same class of confident, unverified action the lane is being
retired for. The code is inert while both locks hold.

**Reverses if:** a month of measured spend and a clean audit of engineer.py both
land. Re-enabling `fund-research.timer` fires the missed window immediately -
it is `Persistent=true`, and that is what caused the spend event.

---

## 5. Sharadar: delete the claim, not the file, until the data is bought

**Measured.** `core/data/sharadar.py` is 48 lines of correct, unreachable code:
`SharadarProvider` is imported by nothing. It is the documented remedy for
SPEC's own biggest stated methodological risk, survivorship bias, and it needs a
paid key in a project whose hard rule is free data at ~$0/mo.

**Decision: stop implying the remedy exists.** SPEC must say survivorship bias
is unmitigated, because it is. The file stays as the implementation that would
be wired the day the data is bought; what goes is the sentence that lets a
reader believe the point-in-time re-test is available today.

**Reverses if:** the owner buys the data. Then it is wired and the SPEC language
comes back.

---

## 6. The clock is NOT reset

**Measured.** Over the registered window S3 realised 13.45% annualised vol
against S1's 9.94%, a ratio of 1.35, while beating S1 by 4.34pp.

**Decision: do not reset, do not change sizing, do not change the rule.**

Ex-ante equality is the registered property and it holds - both books target 10%
and both hit it (S3 10.00%, S1 10.04%). The realised gap is estimation error on
a concentrated book, and every remedy for it changes sizing, which resets the
clock and relitigates a pre-registration that exists precisely so it cannot be
relitigated late.

What changes instead: `vol_ratio_s3_s1` and `risk_comparable` now travel with
the verdict, and ASYMMETRY 3 is on the record with its numbers.

**The condition this puts on day 60, decided now so it cannot be decided later
by whoever likes the answer:** if S3 wins on raw returns while `risk_comparable`
is False, the honest report is "no superiority claim - the books did not run at
comparable risk". Not "S3 won".

---

## 7. The reset policy — how many more times can the clock be restarted

**Decided 2026-08-19, after the owner asked the right question: "will you wait 60
days and then tell me you found a mistake and need 60 more?"**

The worry is correct and unbounded resets are a real failure mode. Two have
happened, both at low n: E49's regime removal voided 14 banked days, and E64's
neutrality bug voided 11. At that rate the experiment never concludes, and
"never concluding" is indistinguishable from having no strategy at all.

Worse, the incentive is misaligned in my direction: a reset is cheap for whoever
is doing the improving and expensive for whoever is waiting for an answer. So
the rule below takes the decision away from me as n grows, rather than trusting
my judgement in the moment.

### The category test, which comes first

A **defect** is a finding that makes the comparison not measure the registered
question. E64 qualified: the control arm carried an unregistered net short, so
the test was measuring "LLM PM versus quant-plus-an-accidental-market-short".

An **improvement** is a finding that would make a book better. It NEVER justifies
a reset, at any n. H8 (residual momentum) and H9 (dropping low_vol) are
improvements: they are queued in the harness and they go live at the next window
after this clock concludes, never mid-clock. This is the whole point of a
pre-registration and it is where both previous resets should have been stopped
from becoming a habit.

### The ratchet: the bar rises with what a reset costs

| banked n | who decides | what qualifies |
|---|---|---|
| **n <= 10** | head quant | any defect in the category above |
| **11 - 30** | **owner only** | a defect shown to change the SIGN or the significance of the measured difference, not merely to exist |
| **n > 30** | **nobody** | no resets. The defect is DISCLOSED in the verdict as a stated limitation and the clock runs to 60 |

Past day 30 a disclosed imperfection beats an indefinitely deferred perfect
measurement. An allocator can price a limitation they were told about; they
cannot price a result that never arrives.

### The hard cap

**Three resets total. This was the second.** If a third is needed, the
conclusion is not that the code has another bug: it is that the experiment
design cannot survive contact with a system still under construction. The
answer then is to freeze the entire measurement surface first - no changes to any
book, any signal, any sizing path - and only then start a clock. That is a
deliberate decision to be taken once, not a fourth reset.

### What I cannot promise

That no further defects exist. Nobody can, and I have found eight in this repo in
two weeks. What the rule does is make the RESPONSE to a defect mechanical instead
of discretionary, so the answer to "will you ask for another 60 days" is: only if
n <= 10 and the comparison is genuinely not measuring the registered question,
only twice more at the very most, and never for an improvement.

The current clock started 2026-08-18 at n=1, which means the cheap window is
already almost closed by design.

---

## 8. Where the money is efficient — signals are exhausted, the constraint is data

**Decided 2026-08-23, after the owner asked: is the spend efficient, could it go
somewhere more useful?**

The daily $1-2 was never the point. The inefficiency was spending the scarce
resource - 60-day OOS clocks and attention - on a signal set the free, instant
harness could already rank. So I ranked it. Two screens, offline, zero model
cost, on the OOS window with deflated Sharpe:

- The price-factor family (momentum / reversal / low_vol) tops out at **DSR
  0.31** (drop low_vol), and the currently-live blend is the WORST of it at a
  negative Sharpe.
- Residual/idiosyncratic momentum, the literature's best-documented improvement
  (H8), does **not** beat it here - **DSR 0.02**. On ~200 hand-picked mega-cap
  survivors the market factor is most of each name's return, so residualising
  removes the signal.

**Nothing reachable clears DSR 0.95.** The binding constraint is not the signals.
It is the universe and the data: free, split-adjusted, survivorship-biased
mega-caps are precisely the regime where these anomalies are weakest.

### The decision

1. **Stop spending effort on new signals in this family.** Two families screened
   dead for ~$0. That is the efficient "no", reached without a single live day.

2. **The efficient dollar goes to DATA and UNIVERSE, not signals.** If the goal
   is a fundable edge, the money that currently buys a narrow "does an LLM beat a
   weak quant" comparison (~$40/mo of frontier calls) is better spent on
   point-in-time data (kills survivorship, makes EVERY backtest valid) and a
   broader small/mid-cap universe (where momentum and residual momentum actually
   pay). Same order of magnitude of money, buys validity for all future research
   instead of one low-value live answer.

3. **The one free improvement that survives: drop low_vol.** It is toxic (IC
   -0.164; it drags a +0.91 Sharpe book to -0.36). By the reset policy it is an
   improvement, not a defect, so it does not go live mid-clock - it is promoted
   at the next window. Until then the live control book knowingly carries a
   toxic leg, which is a cost of the pre-registration discipline, recorded here
   rather than quietly fixed.

### The honest bottom line for the allocation question

This exact setup - free data, hand-picked mega-cap survivors, three classic
signals plus an LLM PM - is unlikely to contain a fundable edge, and that is now
established cheaply rather than after 60 more live days. The efficient paths are:
(a) spend comparable money on data + universe to reach where edge actually lives,
or (b) treat the machine as the deliverable - an honest, hostile-tested research
and measurement platform - and stop paying for alpha it is not positioned to
find. Both are defensible. What is NOT efficient is the status quo: paying to run
the worst-ranked blend live for 57 more days to confirm what two free screens
already showed.

## 8. 2026-09-16: repair accounting; close the interrupted comparison as inconclusive

Owner authorization: make the quant decisions, improve the paper bot and push.
The live shadow books silently held constant weights between decisions, final
volatility scaling bypassed liquidity bounds, spot crypto could be short, and
S3 did not receive its own positions. Keeping those defects for another 60-day
wait would not produce usable evidence.

Deploy `self_financing_v2`: quantities drift with marks, cash pays actual modeled
trade/borrow costs, final positions obey the existing capacity/spot limits,
and S3 receives current marked positions/NAV with deterministic name selection.
No alpha parameter, model, volatility target or event-study result is retuned.

This changes accounting and portfolio decisions. The old registered comparison
is **interrupted/inconclusive, with no demonstrated S3 superiority**; it is not
restarted or extended until a favorable result appears. `clock_start` and the
two historical reset records remain intact. A dated measurement boundary stores
NAV baselines and disables superiority claims on the mixed-version history.
The continued paper run is engineering/performance observation, not a newly
registered confirmation test. A future scientific comparison needs its own
prospectively specified design; this release does not create one.

The existing three-resets-total policy is not relaxed. The original clock is
retained as history, not displayed as a promise of an interpretable day-60 test.
The engineer lane and its timer remain disabled.
