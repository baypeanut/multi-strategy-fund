"""E48d: the measurement design is frozen while the clock is running.

Two asymmetries in how the five books are sized and governed. Both are load
bearing, neither was written down, and neither was pinned. An agent reading the
tree tonight would find both, correctly judge them inconsistent, and "fix" one
in a way that silently destroys evidence that cannot be recovered.

These tests do not claim the current behaviour is right. Two of them pin an
OPEN QUESTION (see below) precisely so it stays the owner's call and cannot be
resolved by a plausible-looking diff at 04:30.

---------------------------------------------------------------------------
RESOLVED (E49): no book carries a regime multiplier.

Only S3 did. risk_scale was 0.754 live on 2026-07-31, so S3 targeted ~7.5% vol
against S1's 10%, and the pre-registered experiment compares exactly those two
books. RESEARCH_LOG had already recorded this failure mode once, in the W3
audit:

    "Unfair risk comparison - S1 vol-targeted, S3 wasn't; 'S3 lost less' was
     just lower gross. -> W3: all four books share one ex-ante 10% vol
     normalization; regime scales the *target*, not raw gross."

W3 moved the multiplier off raw gross onto the target but left it on one book,
so the bias it was written to remove survived the fix in a new place.

Head-quant call: remove it. The registered question is whether the LLM PM's
DECISIONS beat deterministic quant. A throttle on one arm makes the measured
difference the sum of selection skill and a market-timing overlay, and a
difference you cannot attribute is not evidence. Applying the throttle to all
books instead would have tied the CONTROL group's gross to a module owned by
the treatment arm, which is backwards.

Regime gating is H6 in this project's own queue, explicitly untested. An
untested overlay does not belong live inside the arm being measured; it belongs
in the harness with a bar and a window. Queued in hypotheses.yaml.

S3 still SEES the regime in its briefing, so if regime timing has value the PM
can express it through position choice, which is the thing under test. What is
gone is the code betting on the PM's behalf.

clock_start resets when this deploys, and the 14 banked trading days become
void for the paired test, exactly as W3 voided everything before 2026-07-02.
Cheapest possible moment to pay that: day 14 of 60.

---------------------------------------------------------------------------
ASYMMETRY 2 (intended, now written down): the governor assesses S4 only.

    decision = self.governor.assess(weights["s4"], ...)

S1, S2, S3 and S5 never see the sector cap, the net cap, the VaR limit or the
drawdown gates. Only the halt latch reaches them, and that zeroes everything.

This is correct and must stay: S4 is the deployed book, and the others are
measurement instruments. Clamping S5 would contaminate the forward-OOS record
on the fund's only confirmed edge, and clamping S1/S2/S3 would break
comparability with every day already recorded. But CLAUDE.md says risk limits
"are enforced by the risk governor" with no qualification, which reads as all
books, so the tree looks like a bug to anyone who has not been told otherwise.

---------------------------------------------------------------------------
ASYMMETRY 3 (E63d, measured 2026-08-12): the books are EX-ANTE equal and
REALISED unequal, and the same bias has now survived two fixes.

    book  n_held   ex-ante vol   realised vol   ratio
    s1      502       10.04%         9.94%       0.99
    s3       41       10.00%        13.45%       1.35

The sizer is doing its job: both books are scaled to the same 10% ex-ante
target and both hit it. The gap is estimation error. S3 holds ~40 names carved
out of a 500-name shrunk covariance, so its ex-ante vol is underestimated and
it is scaled up too far; S1 holds 500 and the errors average out.

Over that same window S3 "beat" S1 by 4.34pp. A raw-return win by a book
running 35% hotter is a risk difference, not a skill claim.

Read this next to ASYMMETRY 1 above, because it is the third appearance of one
bias:

    W3   found it in raw gross      - "S3 lost less" was just lower gross
    E49  found it in the regime     - a throttle on one arm only
    E63d finds it in the estimator  - a concentrated book mis-sized

Each fix was correct and each moved the bias somewhere new. That is the pattern
to expect on the fourth: the fair race is a property of the whole pipeline, and
pinning ex-ante equality does not pin it.

Head-quant call, 2026-08-12: do NOT change sizing and do NOT change the decision
rule. Both would reset the clock and relitigate a pre-registration that exists
precisely so it cannot be relitigated late. Ex-ante equality IS the registered
property and it holds. What was missing is that nothing carried the realised
gap alongside the verdict, so a day-60 raw-return win would have read as skill.
_paired_s3_vs_s1 now emits vol_ratio_s3_s1 and risk_comparable, and the tests
in test_fair_race.py pin both directions.

The open question this leaves the owner: at day 60, if S3 wins on raw returns
while risk_comparable is False, the honest report is "no superiority claim -
the books did not run at comparable risk", NOT "S3 won". Deciding that in
advance is what stops it being decided by whoever likes the answer.
"""
import inspect

import runtime.live as live


def _sizing_source() -> str:
    return inspect.getsource(live.LiveRuntime._run_systems)


def test_the_regime_scale_reaches_no_sizing_or_target_anywhere():
    """E59b. The first version of this pinned the string "reg.risk_scale"
    inside _run_systems. _realized_vol reached the same value by a different
    route - self.state["regime"]["risk_scale"] - and kept scaling S3's target
    for a week after E49 removed the multiplier from S3's real sizing, so the
    instrument built to measure the fair race misreported the one book the
    question is about.

    Pinning a spelling is not pinning a fact. This looks for the value however
    it is spelled, everywhere except the regime module that computes it and the
    governor's own unrelated scale."""
    import pathlib
    src = pathlib.Path(live.__file__).read_text()
    hits = [l.strip() for l in src.splitlines()
            if "risk_scale" in l
            and "decision.risk_scale" not in l      # governor's own, unrelated
            and not l.strip().startswith("#")]
    assert hits == [], (
        "the regime scale is reaching sizing or a target again:\n  "
        + "\n  ".join(hits))


def test_no_book_is_regime_throttled():
    """E49. Reintroducing this on any book changes what the pre-registered test
    measures and voids every day banked since the reset. If regime gating is
    worth having, it goes through the harness as H6 first."""
    src = _sizing_source()
    assert "reg.risk_scale" not in src, (
        "a regime multiplier is back in the sizing path. That makes the "
        "measured S3-vs-S1 difference the sum of selection skill and a timing "
        "overlay; see this test's docstring.")


def test_every_book_targets_the_same_vol():
    """The fair race, stated as code: one target, sourced once, for all five."""
    src = _sizing_source()
    n = src.count("target_vol=CONFIG.risk.vol_target_annual")
    assert n >= 4, f"expected every book sized off the shared target, saw {n}"


def test_s3_still_sees_the_regime_it_no_longer_obeys():
    """Removing the throttle must not blind the PM. The regime goes into the
    briefing, so the LLM can still act on it by choosing positions, which is
    the capability under test."""
    src = _sizing_source()
    assert "build_briefing(" in src and ", reg)" in src, (
        "the regime stopped reaching the briefing; S3 is now blind to it "
        "rather than merely un-throttled by it")


def test_the_governor_assesses_the_deployed_book_only():
    """Intended, and load bearing. Applying the governor to S5 would clamp the
    forward-OOS record on the only confirmed edge the fund has."""
    src = inspect.getsource(live.LiveRuntime.tick)
    assert 'weights["s4"], equity_curve=' in src and "governor.assess(" in src, (
        "the governor's scope changed. Governing the measurement books "
        "contaminates evidence that cannot be re-collected.")


def test_all_books_share_one_vol_target_constant():
    """Whatever is decided about the regime multiplier, the base target stays
    single-sourced. Two hardcoded targets is how a fair race quietly ends."""
    src = _sizing_source()
    assert "0.10" not in src and "0.1," not in src, (
        "a literal vol target appeared in the sizing path; it must come from "
        "CONFIG.risk.vol_target_annual so every book moves together")


def test_the_realised_risk_gap_travels_with_the_verdict():
    """ASYMMETRY 3 is registered above as a known, measured, deliberately
    unfixed property. What must NOT be lost is the label that keeps it visible:
    if _paired_s3_vs_s1 stops emitting the realised-vol ratio, a raw-return win
    by the hotter book reads as a skill claim again, which is the exact sentence
    W3 and E49 were each written to prevent."""
    import inspect

    from runtime.live import LiveRuntime

    src = inspect.getsource(LiveRuntime._paired_s3_vs_s1)
    assert "vol_ratio_s3_s1" in src, (
        "the paired readout no longer carries the realised-risk ratio; a "
        "day-60 verdict could be reported without it")
    assert "risk_comparable" in src


def test_the_reset_policy_is_recorded_and_the_cap_is_not_exceeded():
    """E64c. The owner asked the question that needed asking: "will you wait 60
    days and then tell me you found a mistake and need 60 more?"

    Two resets have happened, both at low n - E49's regime removal voided 14
    banked days, E64's neutrality bug voided 11. At that rate the experiment
    never concludes, and never concluding is indistinguishable from having no
    strategy. The incentive is also misaligned: a reset is cheap for whoever is
    improving and expensive for whoever is waiting for the answer.

    DECISIONS.md section 7 bounds it, and this test keeps the bound honest:
      - an IMPROVEMENT never justifies a reset, at any n (H8 and H9 are
        improvements and wait for the next window)
      - n <= 10 head quant decides; 11-30 owner only, and only for a defect that
        changes the SIGN or significance; n > 30 nobody, the defect is disclosed
        in the verdict and the clock runs
      - three resets total, ever. This was the second.

    A policy nobody counts is a suggestion, so the count is asserted here."""
    import json
    import pathlib

    policy = pathlib.Path("DECISIONS.md")
    assert policy.exists(), "the reset policy has been deleted"
    text = policy.read_text().lower()

    # Deliberately NOT a prose match. The first version of this test asserted
    # the phrase "IMPROVEMENT never justifies", which the policy does not
    # contain in those words, so the test failed on its own wording rather than
    # on anything real - the same pin-a-spelling trap this file exists to warn
    # about. Assert only that the section is present and that the COUNT, which
    # is the actual bound, holds.
    assert "reset policy" in text, "the reset-policy section is gone"
    assert "three resets total" in text, "the hard cap is gone from the policy"

    state = pathlib.Path("data/state.json")
    if not state.exists():
        return                      # offline suite on a fresh clone
    resets = json.loads(state.read_text()).get("clock_resets") or []
    assert len(resets) <= 3, (
        f"{len(resets)} clock resets on record and the cap is 3. A fourth is not "
        f"another bug fix: it means the measurement surface must be frozen "
        f"entirely BEFORE a clock starts, which is one deliberate decision "
        f"rather than a fourth reset.")
