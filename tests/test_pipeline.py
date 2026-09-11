"""E34 three-stage pipeline: plan (Fable) -> implement (Opus 5) -> review
(Fable) -> gated auto-apply.

The seam is `_stream_json` — every model call goes through it, so the whole
pipeline is scripted here without touching the API. What these tests pin:
the review is a REAL gate (only "approve" applies; "discard" rejects;
"revise"/error leaves pending), and the generator is mechanically confined to
the files the plan named.
"""
from types import SimpleNamespace

import pytest

import research.engineer as eng

SPEC = {"title": "add helper", "kind": "ops", "risk_class": "low",
        "rationale": "why", "instructions": "do it", "acceptance": "see it",
        "files_to_touch": ["scripts/fake_e34.py"]}
FILES = [{"path": "scripts/fake_e34.py", "content": "X = 1\n"}]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Isolated proposal store + stubbed sandbox/apply/git, scripted LLM."""
    monkeypatch.setattr(eng, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(eng, "ENGINEER_MEMOS", tmp_path / "MEMOS.md")
    monkeypatch.setattr(eng, "sandbox_test",
                        lambda pdir, timeout=420: {"passed": True, "note": "stub"})
    applied = []

    def fake_apply(pid, force_armor=False, run_tests=True):
        applied.append(pid)
        return {"ok": True, "touched": ["scripts/fake_e34.py"],
                "needs_restart": False, "note": ""}

    monkeypatch.setattr(eng, "apply_proposal", fake_apply)
    monkeypatch.setattr(eng, "git_commit",
                        lambda paths, msg: {"ok": True, "sha": "e34test"})

    calls = []

    def script(*stages):
        """stages: list of (data, err) consumed per _stream_json call."""
        def fake(client, *, model, system, user, schema, max_tokens,
                 effort=None, fallback=eng.FALLBACK_MODEL):
            calls.append({"model": model, "effort": effort, "user": user})
            data, err = stages[len(calls) - 1]
            return data, SimpleNamespace(model=model, stop_reason="end_turn"), err
        monkeypatch.setattr(eng, "_stream_json", fake)

    return SimpleNamespace(script=script, calls=calls, applied=applied)


CFG = {"auto_apply": True, "auto_commit": True}


def test_approve_path_applies_and_uses_the_right_models(rig):
    rig.script(
        ({"memo": "m", "proposals": [SPEC]}, None),                    # plan
        ({"files": FILES, "test_note": "tn"}, None),                   # generate
        ({"verdict": "approve", "reasons": "clean",
          "revision_instructions": None}, None),                       # review
    )
    out = eng._run_pipeline(None, "CTX", CFG, 2)

    assert [c["model"] for c in rig.calls] == [
        eng.PLAN_MODEL, eng.GEN_MODEL, eng.REVIEW_MODEL]
    assert rig.calls[1]["effort"] == "xhigh"          # coder runs hot
    assert rig.applied == [out["accepted"][0]["id"]]
    assert out["accepted"][0]["review"]["verdict"] == "approve"


def test_discard_rejects_and_never_applies(rig):
    rig.script(
        ({"memo": "m", "proposals": [SPEC]}, None),
        ({"files": FILES, "test_note": "tn"}, None),
        ({"verdict": "discard", "reasons": "not worth the risk",
          "revision_instructions": None}, None),
    )
    out = eng._run_pipeline(None, "CTX", CFG, 2)

    assert not rig.applied
    rec = out["accepted"][0]
    assert rec["status"] == "rejected"
    assert "not worth the risk" in rec["reject_reason"]


def test_revise_and_review_error_both_leave_pending(rig):
    # E41: engineers run concurrently, so both generates land before either
    # review. Second spec must claim a DIFFERENT file (collision guard).
    spec2 = {**SPEC, "title": "second", "files_to_touch": ["scripts/fake2_e34.py"]}
    rig.script(
        ({"memo": "m", "proposals": [SPEC, spec2]}, None),
        ({"files": FILES, "test_note": "tn"}, None),
        ({"files": [{"path": "scripts/fake2_e34.py", "content": "Y = 2\n"}],
          "test_note": "tn"}, None),
        ({"verdict": "revise", "reasons": "close",
          "revision_instructions": "tighten the test"}, None),
        (None, "timeout"),                             # review call failed
    )
    out = eng._run_pipeline(None, "CTX", CFG, 2)

    assert not rig.applied, "neither revise nor a review error may auto-apply"
    assert all(r["status"] == "pending" for r in out["accepted"])
    assert out["accepted"][0]["review"]["revision_instructions"] == "tighten the test"
    assert out["accepted"][1]["review"]["error"] == "timeout"


def test_generator_cannot_write_outside_the_spec(rig):
    rig.script(
        ({"memo": "m", "proposals": [SPEC]}, None),
        ({"files": FILES + [{"path": "core/risk/governor.py", "content": "x=1\n"}],
          "test_note": "tn"}, None),
        # review must never be reached
    )
    out = eng._run_pipeline(None, "CTX", CFG, 2)

    assert len(rig.calls) == 2, "review must not run on a confined-violation"
    assert not out["accepted"] and not rig.applied
    assert "outside spec" in out["rejected"][0]["error"]


def test_zero_specs_is_a_clean_night(rig):
    rig.script(({"memo": "all healthy, reassess tomorrow", "proposals": []}, None))
    out = eng._run_pipeline(None, "CTX", CFG, 2)

    assert len(rig.calls) == 1 and not rig.applied
    assert out["accepted"] == [] and "reassess" in out["memo"]
    assert "all healthy" in eng.ENGINEER_MEMOS.read_text()


def test_invalid_files_to_touch_rejected_before_any_generation(rig):
    rig.script(
        ({"memo": "m", "proposals": [
            {**SPEC, "files_to_touch": ["../../etc/passwd"]}]}, None),
    )
    out = eng._run_pipeline(None, "CTX", CFG, 2)

    assert len(rig.calls) == 1, "generator must not be invoked"
    assert "traversal" in out["rejected"][0]["error"]


def test_a_malformed_plan_is_retryable_not_terminal(rig):
    """E55: this used to assert the plan failure ENDED the night, and on
    2026-08-02 that contract cost a full autonomous run - one truncated
    planning call stopped a night with ten rounds of budget left. A bad draw is
    a bad draw; the settle loop now counts it as barren and re-plans with fresh
    context, and repeated failures still stop it."""
    rig.script((None, "malformed JSON (truncated at max_tokens); 12 chars"))
    out = eng._run_pipeline(None, "CTX", CFG, 2)
    assert "retryable" in out and "plan:" in out["retryable"]
    assert "skipped" not in out, "skipped is read by the loop as blocking"
    assert out["accepted"] == [] and out["rejected"] == []


def test_prompts_carry_the_mandate_and_the_gate():
    for prompt in (eng._PLAN_SYSTEM, eng._SYSTEM):
        low = prompt.lower()
        assert "one-way door" in low and "owner-level authority" in low
    plan_low = eng._PLAN_SYSTEM.lower()
    assert "do not emit file" in plan_low and "in parallel" in plan_low
    assert "independent" in plan_low            # the collision rule is stated
    assert "try to break the implementation" in eng._REVIEW_SYSTEM.lower()
    assert "only the files the spec lists" in eng._GEN_SYSTEM.lower()
    # the standing order that replaced the freeze
    assert "fix the class, not the instance" in eng._SYSTEM.lower()
    assert "verify against real data" in eng._SYSTEM.lower()
