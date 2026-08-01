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
"""
import inspect

import runtime.live as live


def _sizing_source() -> str:
    return inspect.getsource(live.LiveRuntime._run_systems)


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
