"""E32: the lane must be able to SEE the fund, and must be told the truth
about its own authority.

Before E32 the engineer's context carried source code and ops counters only —
no book performance, no research results — so 'reason like a quant' was
structurally impossible rather than merely un-prompted. These tests pin the
data being present and the prompt no longer describing the retired
human-review gate.
"""
import json

import research.engineer as eng


def test_curve_stats_reports_return_and_drawdown():
    curve = [["t0", 100.0], ["t1", 120.0], ["t2", 90.0], ["t3", 110.0]]
    st = eng._curve_stats(curve)
    assert st["marks"] == 4
    assert st["total_ret_pct"] == 10.0
    assert st["max_dd_pct"] == -25.0        # 120 -> 90
    assert st["start"] == 100.0 and st["now"] == 110.0


def test_curve_stats_handles_bare_numbers_and_too_short():
    assert eng._curve_stats([100.0, 50.0])["total_ret_pct"] == -50.0
    assert eng._curve_stats([]) is None
    assert eng._curve_stats([["t", 1.0]]) is None


def test_quant_view_surfaces_per_book_pnl_and_the_rule():
    state = {
        "equity_history": {"s1": [["t", 100.0], ["t", 101.0]],
                           "s3": [["t", 100.0], ["t", 97.0]]},
        "systems": {"s1": {"equity": 101.0, "weights": {"AAPL": 0.3, "MSFT": -0.2}}},
        "s3_vs_s1": {"n": 14, "p_value": 0.11},
        "s3_vs_s1_common": {"n": 14, "diagnostic_only": True},
        "regime": {"trend": "up"},
    }
    view = eng._quant_view(state)
    assert "FUND PERFORMANCE" in view
    payload = json.loads(view.split("===\n", 1)[1])

    assert payload["books_since_inception"]["s1"]["total_ret_pct"] == 1.0
    assert payload["books_since_inception"]["s3"]["total_ret_pct"] == -3.0
    assert payload["books_now"]["s1"]["n_positions"] == 2
    assert payload["books_now"]["s1"]["gross"] == 0.5
    assert payload["regime"] == {"trend": "up"}

    # the pre-registered rule and the diagnostic must stay distinguishable —
    # conflating them is exactly what E28 pre-registered against
    assert payload["s3_vs_s1_PREREGISTERED_RULE"]["n"] == 14
    assert payload["s3_vs_s1_common_DIAGNOSTIC_ONLY"]["diagnostic_only"] is True


def test_quant_view_survives_an_empty_state():
    payload = json.loads(eng._quant_view({}).split("===\n", 1)[1])
    assert payload["books_since_inception"] == {}
    assert payload["books_now"] == {}


def test_research_ledger_is_readable_but_never_writable():
    """The agent must be able to read results to judge research, while the
    write-path validator still refuses them — read/write asymmetry is the
    point: it can see its exam, never edit it."""
    ledger = eng._research_ledger()
    if ledger:                                    # present in a real checkout
        assert "RESEARCH RESULTS" in ledger or "FAMILIES" in ledger
    assert eng.validate_rel_path("research/RESULTS.jsonl") is not None
    assert eng.validate_rel_path("research/registry.json") is not None


def test_context_is_whole_and_free_of_proposal_duplicates(tmp_path):
    """Regression for the E32 crowd-out: 728KB of full file copies under
    research/proposals/ pushed config.yaml, every script and EVERY test out of
    the context budget. The agent must see the real tree, not its own archive.
    """
    ctx = eng.build_context()
    assert len(ctx) <= eng._MAX_CTX_CHARS

    listed = [p for p in _files_in(ctx) if "proposals" in p.split("/")]
    assert not listed, f"proposal copies leaked into context: {listed[:3]}"

    for must_see in ("config/config.yaml", "core/risk/governor.py",
                     "research/harness.py", "runtime/live.py"):
        assert f"--- FILE: {must_see} ---" in ctx, f"{must_see} missing"

    omitted = [p for p in _files_in(ctx, omitted=True) if not p.startswith("{")]
    assert not omitted, (
        f"files dropped for budget: {omitted[:5]} — the tree has outgrown "
        f"_MAX_CTX_CHARS ({eng._MAX_CTX_CHARS:,}); raise it rather than "
        f"letting the lane go blind on whatever sorts last")

    # Headroom is a WARNING, not a brake (E40). This assert used to be hard,
    # and it twice failed the apply gate on unrelated, reviewer-approved work
    # (P0014 on 2026-07-28, again on 07-29) because the tree grows every night.
    # Eviction — asserted above — is the real failure; approaching the cap is
    # something the lane should SEE, and `_mirror_view`-style context now
    # carries it. Keep the signal, drop the blocking.
    if len(ctx) >= eng._MAX_CTX_CHARS * 0.92:
        import warnings
        warnings.warn(
            f"engineer context at {100 * len(ctx) / eng._MAX_CTX_CHARS:.1f}% of "
            f"cap ({len(ctx):,}/{eng._MAX_CTX_CHARS:,}) — raise _MAX_CTX_CHARS or "
            f"tail more files before it starts evicting", stacklevel=2)


def test_append_only_logs_are_tailed_not_included_whole():
    """These grow by a memo every night. Included whole they crowded the
    context to 92% of cap and the headroom guard above then rolled back an
    approved proposal (P0014, 2026-07-28). The lane needs recent history."""
    ctx = eng.build_context()
    for rel, budget in eng._TAIL_CHARS.items():
        full = (eng.ROOT / rel)
        if not full.exists() or len(full.read_text()) <= budget:
            continue
        marker = f"--- FILE: {rel} ---"
        assert marker in ctx, f"{rel} missing from context"
        body = ctx.split(marker, 1)[1].split("\n--- FILE: ", 1)[0]
        assert "append-only log" in body, f"{rel} was not tailed"
        assert len(body) < budget * 1.2, f"{rel} tail exceeded its budget"
        # tail, not head: the newest entry must survive
        assert full.read_text()[-200:].strip()[-80:] in body, f"{rel} kept the head"


def _files_in(ctx: str, omitted: bool = False) -> list[str]:
    import re
    pat = (r"--- FILE: (\S+) --- \[omitted" if omitted
           else r"--- FILE: (\S+) ---\n")
    return re.findall(pat, ctx)


def test_system_prompt_tells_the_truth_about_authority():
    sys_p = eng._SYSTEM.lower()
    # the retired gate must not be described any more
    assert "a human\nreviews the diff next morning" not in sys_p
    assert "you cannot modify\nanything" not in sys_p
    # the real posture, the one-way doors, and the licence to abstain
    assert "applied to the live" in sys_p
    assert "lockbox" in sys_p and "one-way door" in sys_p
    assert "doing nothing is a first-class outcome" in sys_p
    assert "zero proposals" in sys_p
