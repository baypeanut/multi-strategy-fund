"""E58: prove the gate would accept something before buying a single token.

The apply gate runs the full suite in a sandbox copy and rolls back on red. So
a suite that is ALREADY red in that copy makes every proposal dead before it is
written - and the lane cannot tell, because the same suite is green on the host
it runs on.

That is not hypothetical. On 2026-08-02 a test I had written the day before
read the live data/state.json, which the sandbox copy excludes. Green locally,
green on the server, red in every sandbox. Six proposals were generated at full
Fable-plan and Opus-generate price and all six were rolled back, none of them
for anything to do with their own quality. Roughly $25-30, and the owner found
out from me the next morning rather than from the machine that night.

The check costs one pytest run and zero tokens, against roughly $11 a session.
It does not depend on anyone remembering to run a script: the parity script is
for humans, this is the machine checking itself before it spends.
"""
import research.engineer as eng


def test_a_red_baseline_spends_nothing(monkeypatch):
    spent = []
    monkeypatch.setattr(eng, "baseline_is_green",
                        lambda: {"green": False,
                                 "failed": ["tests/test_x.py::test_y"],
                                 "summary": "1 failed"})
    monkeypatch.setattr(eng, "run_engineer",
                        lambda dry_run=False: spent.append(1) or {})
    monkeypatch.setattr(eng, "_autonomy", lambda: {"engineer_max_rounds": 4})

    out = eng.run_engineer_until_settled()
    assert spent == [], "the lane bought tokens against a gate that rejects everything"
    assert out["stop_reason"] == "baseline_red"
    assert out["applied_total"] == 0 and out["n_rounds"] == 0


def test_the_refusal_names_the_failing_test(monkeypatch):
    """A refusal nobody can act on is the silence problem in a new costume."""
    monkeypatch.setattr(eng, "baseline_is_green",
                        lambda: {"green": False,
                                 "failed": ["tests/test_ibkr_sync_cli.py::test_state_path"],
                                 "summary": "1 failed, 527 passed"})
    monkeypatch.setattr(eng, "run_engineer", lambda dry_run=False: {})
    monkeypatch.setattr(eng, "_autonomy", lambda: {"engineer_max_rounds": 4})

    out = eng.run_engineer_until_settled()
    assert "test_state_path" in out["detail"]
    assert "rolled back" in out["detail"] and "nothing was spent" in out["detail"]


def test_a_green_baseline_lets_the_night_proceed(monkeypatch):
    calls = []
    monkeypatch.setattr(eng, "baseline_is_green",
                        lambda: {"green": True, "failed": [], "summary": "ok"})
    monkeypatch.setattr(eng, "run_engineer", lambda dry_run=False: (
        calls.append(1) or {"accepted": [{"auto_apply": {"ok": True}}], "rejected": []}))
    monkeypatch.setattr(eng, "_autonomy", lambda: {
        "engineer_max_rounds": 2, "engineer_barren_rounds": 2})

    out = eng.run_engineer_until_settled()
    assert calls == [1, 1] and out["applied_total"] == 2


# DELIBERATELY ABSENT: any test that calls baseline_is_green() for real.
#
# Two attempts, two problems. The first ran the full suite inside a sandbox
# copy - and that suite contained the same test, so it built another copy, and
# so on; unbounded recursion that hung for ten minutes. The second only meant
# to prove the failure path, but still entered the function far enough to build
# a repo copy, and the suite went from four seconds to over ten minutes.
#
# I wrote one. It runs the full suite inside a sandbox copy - and the suite it
# runs contains that same test, which builds another copy, and so on. It is an
# unbounded recursion, and it hung for ten minutes before I killed it.
#
# The lesson is the owner's, made the same hour: adding a thing has a cost, and
# a check that costs more than the thing it protects is not a check. The real
# exercise of this path is the nightly run itself, where it is called once,
# outside pytest, and the run either spends or explains why it did not.
