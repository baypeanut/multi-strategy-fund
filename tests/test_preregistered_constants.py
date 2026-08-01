"""E48c: the pre-registered numbers are pinned as literals, on purpose.

Found by mutation-testing the armor before the first unattended settle-loop
night. The existing armor tests verify MECHANISM (a family gets Bonferroni'd, a
lockbox window gets injected, a burned family is closed) but they compare
against the constants themselves:

    assert (seen["start"], seen["end"]) == H.LOCKBOX

That assertion is tautological. It passes whatever `LOCKBOX` happens to say, so
it can never catch a change to `LOCKBOX`. Measured, not argued:

    DSR_BAR      0.95 -> 0.50   suite goes red      (caught)
    DSR_BAR      0.95 -> 0.94   suite stays GREEN   (missed)
    FAMILY_ALPHA 0.05 -> 0.50   suite goes red      (caught)
    CONFIRM_T_BAR 2.0 -> 0.5    suite goes red      (caught)
    EVENT_T_BAR   2.5 -> 1.0    suite stays GREEN   (missed)
    LOCKBOX      moved 6 years  suite stays GREEN   (missed)

EVENT_T_BAR and LOCKBOX are the two that matter most. The fund's only confirmed
edge is an event study, and the lockbox is the one-shot window that makes a
confirmation mean anything. Under the settle loop, an agent could lower the
event bar or slide the window, keep the suite green, pass review on a diff that
reads plausibly, and auto-commit. Every later event study would then clear a
bar nobody chose.

These numbers are not implementation details. They are the pre-registration.
Their only value is that they were fixed BEFORE the results were seen, so a
test that reads them from the code it is testing protects nothing.

This file is the one place they appear as literals. Changing a bar now means
editing this file too, which is a deliberate act with a diff that says exactly
what it is, rather than a one-character edit buried in a refactor. That is the
point: not to forbid change, but to make it impossible to do quietly.
"""
import research.harness as H


def test_discovery_bars_are_what_was_registered():
    assert H.DSR_BAR == 0.95, "Deflated Sharpe bar for signal backtests"
    assert H.EVENT_T_BAR == 2.5, "event-study t bar, pre-family-penalty"


def test_family_and_confirmation_bars_are_what_was_registered():
    assert H.FAMILY_ALPHA == 0.05, "within-family Bonferroni budget"
    assert H.CONFIRM_T_BAR == 2.0, "lockbox calendar-time portfolio, one shot"


def test_the_lockbox_window_has_not_moved():
    """One shot per family. If the window slides, every confirmation ever
    granted was measured against a different question than the one asked."""
    assert H.LOCKBOX == ("2021-07-15", "2023-07-14")


def test_exploration_starts_where_the_lockbox_ends():
    """No overlap. Exploration must never see lockbox data, or the one shot is
    spent before it is fired."""
    assert H.EXPLORE[0] == "2023-07-15"
    assert H.LOCKBOX[1] < H.EXPLORE[0], "windows overlap: exploration is contaminated"


def test_the_book_comparison_rule_is_what_was_registered():
    """RESEARCH_LOG E7: no S3-vs-S1 superiority claim before 60 trading days
    AND p < 0.05. Pinned here because the whole horse race is judged by it."""
    import inspect

    from runtime.live import LiveRuntime
    src = inspect.getsource(LiveRuntime._paired_s3_vs_s1)
    assert 'res["n"] >= 60' in src, "the 60-day minimum is no longer enforced"
    assert 'res["p_value"] < 0.05' in src, "the p<0.05 threshold is no longer enforced"


def test_the_vol_target_is_what_was_registered():
    """W3: all books share ONE ex-ante vol normalization. If this drifts, the
    horse race stops being a fair race and every past comparison is void."""
    from core.config import CONFIG
    assert CONFIG["risk"]["vol_target_annual"] == 0.10
    assert CONFIG["risk"]["max_gross"] == 1.00, "no leverage in the proof phase"
