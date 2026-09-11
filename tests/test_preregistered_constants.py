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


def _runtime_with_edge(tmp_path, n_days: int, deltas):
    """A runtime whose s3 book beats s1 by `deltas` on n_days trading days."""
    import pandas as pd

    from runtime.live import LiveRuntime

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    days = pd.bdate_range("2026-01-05", periods=n_days + 1)   # n+1 closes -> n returns
    e1, e3 = 100.0, 100.0
    h1, h3 = [], []
    for i, d in enumerate(days):
        if i:
            e1 *= 1.001
            e3 *= 1.001 + deltas[(i - 1) % len(deltas)]
        h1.append([d.isoformat(), e1])
        h3.append([d.isoformat(), e3])
    rt.state["equity_history"]["s1"] = h1
    rt.state["equity_history"]["s3"] = h3
    rt.state.pop("clock_start", None)
    return rt


def test_the_book_comparison_rule_is_what_was_registered(tmp_path):
    """RESEARCH_LOG E7: no S3-vs-S1 superiority claim before 60 trading days
    AND p < 0.05. The whole horse race is judged by this.

    E63. This test used to assert on SOURCE TEXT:

        assert 'res["n"] >= 60' in inspect.getsource(...)

    Measured 2026-08-10: changing the rule to `res["n"] >= 60 - 30` keeps that
    substring intact, so the pre-registered gate was HALVED to 30 trading days
    with all 558 tests green. `res["p_value"] < 0.05 * 4` defeats the other
    assertion the same way.

    runtime/live.py:298-301 already carries this exact lesson, written by the
    author who was bitten by it: a frozen test pinned the string
    "reg.risk_scale" and missed the fact, and the note ends "pinning a spelling
    is not pinning a fact." The lesson was recorded and then not applied to the
    file whose entire job is pinning facts.

    Now behavioural: the gate must be shut at 59 trading days and open at 60,
    on the same data."""
    strong = [0.002, 0.003]        # persistent edge -> p well under 0.05

    just_short = _runtime_with_edge(tmp_path, 59, strong)._paired_s3_vs_s1()
    assert just_short["n"] == 59
    assert just_short["p_value"] < 0.05, "fixture must isolate the n rule"
    assert not just_short["verdict_allowed"], (
        "59 trading days with a significant edge must NOT allow a verdict; "
        "the 60-day minimum is no longer enforced")

    at_sixty = _runtime_with_edge(tmp_path, 60, strong)._paired_s3_vs_s1()
    assert at_sixty["n"] == 60
    assert at_sixty["verdict_allowed"], (
        "60 trading days with p<0.05 must allow a verdict; the gate is stuck shut")


def test_a_long_but_insignificant_record_still_cannot_claim(tmp_path):
    """The other half of the rule. Length alone is not evidence.

    The fixture is deliberately MARGINAL: 90 trading days of a small positive
    edge that lands at p ~ 0.17. A record this shape is exactly what tempts a
    post-hoc threshold move, and a fixture at p ~ 1.0 would not notice one -
    verified: with pure noise, loosening the rule to `p_value < 0.05 * 4` kept
    the whole suite green. Sitting just above the line is what makes this test
    bite."""
    marginal = [0.00213, -0.00187]      # mean +1.3bps/day, t = 1.38, p = 0.168

    out = _runtime_with_edge(tmp_path, 90, marginal)._paired_s3_vs_s1()
    assert out["n"] == 90
    assert 0.05 < out["p_value"] < 0.20, (
        f"fixture must straddle the threshold to be able to detect a move; "
        f"p={out['p_value']}")
    assert not out["verdict_allowed"], (
        "90 trading days at p=0.17 must NOT allow a verdict; the p<0.05 "
        "threshold has been loosened")


def test_the_s5_confirmed_spec_has_not_moved():
    """E62. S5 is the fund's ONLY confirmed edge and its forward-OOS window is
    open, so its spec is the one thing in this repo that must not move while
    evidence accumulates against it.

    It was not pinned. Measured 2026-08-10 by mutation: `s5.entry_lag` 2 -> 5
    in config.yaml, full suite GREEN, 556 passed. tests/test_s5_event.py names
    entry_lag=2 six times but passes it as a CONSTRUCTOR ARGUMENT, so it pins
    the engine's mechanism and never the registered value - the same tautology
    this file's docstring was written to eliminate, fixed for the harness bars
    and left open here. HANDOFF.md meanwhile told the next session to see this
    file for S5's parameters, which pinned none of them.

    Without this test: any edit to the s5 block keeps the suite green, passes
    the apply gate, and S5 keeps marking days under a spec nobody registered,
    indistinguishable from evidence on the original question."""
    from core.config import CONFIG

    s5 = CONFIG["s5"]
    assert s5["entry_lag"] == 2, "calendar-day offset to entry"
    assert s5["exit_lag"] == 10, "calendar-day offset to exit"
    assert s5["min_units"] == 4, "breadth guard, matches calendar_time_daily"
    assert s5["n_names"] == 500, "full universe, as confirmed"


def test_the_vol_target_is_what_was_registered():
    """W3: all books share ONE ex-ante vol normalization. If this drifts, the
    horse race stops being a fair race and every past comparison is void."""
    from core.config import CONFIG
    assert CONFIG["risk"]["vol_target_annual"] == 0.10
    assert CONFIG["risk"]["max_gross"] == 1.00, "no leverage in the proof phase"


def test_the_confirmed_s5_spec_is_what_was_confirmed():
    """These are the parameters the 8k-drift family was CONFIRMED on (N0021,
    lockbox one-shot). The family is closed to trials forever, so S5's forward
    paper record is the only remaining evidence stream on the fund's only
    confirmed edge, and its meaning depends entirely on these four numbers
    staying at the confirmed values.

    A tuned parameter means every subsequent forward day accrues against an
    unconfirmed object while the suite stays green — and forward days are not
    restorable from git the way code is. Until tonight the only guard was a
    config comment ('do not tune'); E47/E48c both measured that a comment is
    not a control. Literals live here so a change needs a two-file diff.
    """
    from core.config import CONFIG
    s5 = CONFIG["s5"]
    assert s5["entry_lag"] == 2, "confirmed entry lag: enter t+2 after the event"
    assert s5["exit_lag"] == 10, "confirmed horizon: exit t+10"
    assert s5["min_units"] == 4, "confirmed breadth guard: >= 4 units per day"
    assert s5["n_names"] == 500, "confirmed universe breadth"


def test_the_s5_engine_defaults_match_the_confirmed_spec():
    """The engine's dataclass defaults are a second copy of the same numbers.
    If config and defaults drift apart, which one actually runs depends on a
    config block existing, and that ambiguity is exactly how a quiet change
    slips through: the forward record would silently become a mixture of two
    objects, with nothing to say which day was measured under which. Same
    literals, pinned in the same place as the config ones.
    """
    from systems.s5_event.engine import S5EventEngine
    e = S5EventEngine()
    assert (e.entry_lag, e.exit_lag, e.min_units) == (2, 10, 4)


def test_the_confirmation_instruments_breadth_guard_is_pinned():
    """calendar_time_daily is the pre-registered confirmation instrument, and it
    hardcodes the breadth guard the confirmed Sharpe was measured under. Relax
    it and the confirmed edge is re-measured under a different question — but
    the family's one shot is already spent, so there is no way to re-earn the
    confirmation and no way to recover the forward days accrued in between.

    As tests/test_measurement_design_frozen.py acknowledges for its own source
    pins: a source-string pin is weaker than a behavioral pin. It is used here
    because the guard is a literal inside a hot loop.
    """
    import inspect

    import research.primitives as P
    assert "num_units >= 4" in inspect.getsource(P.calendar_time_daily), \
        "the >= 4 units/day breadth guard the confirmation was measured under is gone"


def test_the_circuit_breakers_are_what_spec_says():
    """E63. SPEC section 4 calls these hard rules. Only three of them were
    provable.

    Measured 2026-08-10 by mutating config.yaml and running the full suite:

        vol_target_annual 0.10 -> 0.40   suite RED    (caught)
        max_gross         1.00 -> 4.00   suite RED    (caught)
        max_position      0.05 -> 0.50   suite RED    (caught)
        max_net           1.00 -> 4.00   suite GREEN  (missed)
        max_sector        0.25 -> 0.99   suite GREEN  (missed)
        liquidity_adv_cap 0.10 -> 0.90   suite GREEN  (missed)
        daily_loss_kill  -0.03 -> -0.99  suite GREEN  (missed)
        dd_gate_1        -0.10 -> -0.90  suite GREEN  (missed)
        dd_gate_2        -0.15 -> -0.95  suite GREEN  (missed)
        var_limit_95      0.02 -> 0.99   suite GREEN  (missed)

    The last four are the fund's circuit breakers. The single-day kill switch
    could be set to -99%, and the -15% halt to -95%, with every test passing.
    All of them ARE wired - runtime/live.py passes them into RiskGovernor - so
    unlike the risk_weight keys these are live controls. They were simply
    unpinned, because tests/test_governor.py builds its own RiskGovernor from
    hardcoded literals and never reads CONFIG at all.

    Pinned by VALUE, which is the method that made the harness bars survive
    every mutation including the literal-preserving ones, and not by source
    text, which is the method that let the 60-day rule be halved."""
    from core.config import CONFIG

    r = CONFIG["risk"]
    assert r["max_net"] == 1.00, "no net leverage in the proof phase"
    assert r["max_position"] == 0.05, "per-name cap, SPEC section 4"
    assert r["max_sector"] == 0.25, "sector cap, SPEC section 4"
    assert r["liquidity_adv_cap"] == 0.10, "position <= 10% of 20d ADV"
    assert r["daily_loss_kill"] == -0.03, "single-day kill switch"
    assert r["dd_gate_1"] == -0.10, "drawdown gate 1: sizing x0.5"
    assert r["dd_gate_2"] == -0.15, "drawdown gate 2: halt, cash, human review"
    assert r["var_limit_95"] == 0.02, "daily 95% VaR <= 2% NAV"


def test_the_broker_mirror_controls_are_what_was_registered():
    """E63b. These four sit on the path that sends real orders to a real
    broker account, and all four could be moved with the suite green.

    Measured 2026-08-11 by mutating config.yaml and running the full suite:

        min_trade_usd       200 -> 0        589 passed  (missed)
        max_orders_per_sync 150 -> 15000    589 passed  (missed)
        limit_buffer      0.015 -> 0.50     589 passed  (missed)
        top_n               100 -> 500      589 passed  (missed)

    All four are wired into core/broker/ibkr_broker.py, so these are live
    controls that were simply unproven. max_orders_per_sync describes itself in
    config as "fuse: abort sync if plan exceeds this" - a fuse nothing tests is
    a comment. limit_buffer at 0.50 would price marketable limits at plus or
    minus 50%, which fills at any price the book is willing to cross."""
    from core.config import CONFIG

    ib = CONFIG["ibkr"]
    assert ib["top_n"] == 100, "liquid-core slice of S4's equity book"
    assert ib["min_trade_usd"] == 200, "dust filter"
    assert ib["max_orders_per_sync"] == 150, "fuse: abort a sync larger than this"
    assert ib["limit_buffer"] == 0.015, "marketable limit at last close +/- 1.5%"


def test_the_evidence_ring_cannot_be_silently_shrunk():
    """E63b. MAX_HISTORY bounds equity_history, benchmark AND common_idx_history
    - the entire evidence record for the registered comparison.

    Measured: 3000 -> 30 kept the suite green. CLAUDE.md's own invariant is
    "State schema changes must be additive (migrate in _load, never wipe equity
    history)", and a ring size that can be shrunk with nothing noticing is
    precisely a way to wipe it. At 30 marks the S3-vs-S1 series would not reach
    the 60 trading days its own decision rule requires."""
    from runtime.live import MAX_HISTORY

    assert MAX_HISTORY == 3000, (
        "the equity/benchmark/common-index ring: 3000 hourly marks is about "
        "125 days, comfortably past the 60-trading-day registered window")


def test_the_two_annualisation_constants_agree():
    """E63b. TRADING_DAYS = 252 is defined twice, in backtest/metrics.py and in
    systems/s1_quant/volatility.py. Mutating the metrics copy is caught;
    mutating the volatility copy was NOT - it stayed green at 365, which would
    misstate every annualised vol by sqrt(365/252) = 1.20x in a fund that is
    vol-targeted end to end.

    Pinned as an identity rather than two literals, because the defect this
    repo pays for most is one quantity derived in two places."""
    from backtest.metrics import TRADING_DAYS as METRICS_TD
    from systems.s1_quant.volatility import TRADING_DAYS as VOL_TD

    assert METRICS_TD == VOL_TD == 252, (
        f"annualisation disagrees between modules: metrics={METRICS_TD}, "
        f"volatility={VOL_TD}. Every vol number depends on which one ran.")


def test_the_remaining_cost_and_spend_caps_are_what_was_registered():
    """E63b. Costs are pessimistic ON PURPOSE and spend caps are the only
    thing standing between this fund and a repeat of the spend event.

    Measured: the crypto taker commission could be zeroed and S2's daily call
    cap raised a hundredfold, both with the suite green. The equity half-spread
    and impact coefficient were already caught, so the cost surface was half
    pinned."""
    from core.config import CONFIG

    assert CONFIG["costs"]["crypto"]["commission_bps"] == 5.0, "crypto taker fee"
    assert CONFIG["llm"]["max_scorer_calls_per_day"] == 400, "S2 headline scores/day"
    assert CONFIG["llm"]["max_pm_calls_per_day"] == 16, "S3 decisions/day"


def test_the_cost_fallbacks_are_one_definition_not_three():
    """E63b. FALLBACK_ADV and FALLBACK_DVOL decide what a trade with no volume
    data costs, and they existed as three separate literal pairs in
    runtime/live.py, backtest/engine.py and scripts/cost_calibration.py - each
    with a comment promising it matched the others and nothing enforcing it.

    Measured 2026-08-11: moving any ONE copy alone kept the suite green. The
    backtest and the live book would then charge different prices for the same
    missing input, silently, which is the exact failure those comments warned
    about ("a backtest that costs trades differently from the live book is not
    measuring the live book").

    Asserted by IDENTITY rather than by value, so re-splitting them into three
    equal literals fails here too. Equal is not the property that matters;
    shared is."""
    import backtest.engine as B
    import core.broker.costs as C
    import runtime.live as L

    assert L.FALLBACK_ADV is C.FALLBACK_ADV is B.FALLBACK_ADV, (
        "the fallback ADV is no longer a single definition shared by the live "
        "book and the backtest")
    assert L.FALLBACK_DVOL is C.FALLBACK_DVOL is B.FALLBACK_DVOL
    assert C.FALLBACK_ADV == 50e6 and C.FALLBACK_DVOL == 0.02
