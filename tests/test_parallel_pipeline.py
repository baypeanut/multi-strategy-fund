"""E41: concurrent Opus 5 engineers, and the file-collision rule that makes
concurrency safe.

Engineers run in parallel and cannot see each other or the rest of the repo.
Two specs touching the same file would therefore race — the second engineer's
"current contents" are already stale and its emission silently clobbers the
first. The planner is told to keep specs independent; this pins the mechanical
guard for when it doesn't.
"""
import threading
from types import SimpleNamespace

import pytest

import research.engineer as eng

SPEC_A = {"title": "spec a", "kind": "ops", "risk_class": "low",
          "rationale": "why", "instructions": "do", "acceptance": "see",
          "files_to_touch": ["scripts/a_e41.py"]}
SPEC_B = {**SPEC_A, "title": "spec b", "files_to_touch": ["scripts/b_e41.py"]}
FILES_A = [{"path": "scripts/a_e41.py", "content": "A = 1\n"}]
FILES_B = [{"path": "scripts/b_e41.py", "content": "B = 1\n"}]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(eng, "ENGINEER_MEMOS", tmp_path / "MEMOS.md")
    monkeypatch.setattr(eng, "sandbox_test",
                        lambda pdir, timeout=420: {"passed": True, "note": ""})
    monkeypatch.setattr(eng, "apply_proposal",
                        lambda pid, force_armor=False, run_tests=True: {
                            "ok": True, "touched": [], "needs_restart": False,
                            "note": ""})
    monkeypatch.setattr(eng, "git_commit", lambda paths, msg: {"ok": True, "sha": "e41"})
    return SimpleNamespace()


def _script(monkeypatch, plan, gens, reviews, record=None):
    """Thread-safe fake: results are queued PER STAGE, because generation now
    runs concurrently and call order is no longer deterministic."""
    calls, lock = [], threading.Lock()
    queues = {eng.PLAN_MODEL: [plan], eng.GEN_MODEL: list(gens)}
    reviews = list(reviews)

    def fake(client, *, model, system, user, schema, max_tokens,
             effort=None, fallback=eng.FALLBACK_MODEL):
        with lock:
            calls.append({"model": model, "effort": effort})
            # both judgment stages are Fable; the plan is always the first
            q = queues[model] if (model == eng.GEN_MODEL or queues[eng.PLAN_MODEL]) else reviews
            item = q.pop(0) if q else (None, "exhausted")
        if record is not None:
            record(model)
        data, err = item
        return data, SimpleNamespace(model=model, stop_reason="end_turn"), err

    monkeypatch.setattr(eng, "_stream_json", fake)
    return calls


def test_two_specs_generate_concurrently(rig, monkeypatch):
    """The point of the change: wall clock is the slowest spec, not the sum."""
    inflight, peak, lock = 0, [0], threading.Lock()

    def track(model):
        nonlocal inflight
        if model != eng.GEN_MODEL:
            return
        with lock:
            inflight += 1
            peak[0] = max(peak[0], inflight)
        threading.Event().wait(0.05)
        with lock:
            inflight -= 1

    _script(monkeypatch,
            plan=({"memo": "m", "proposals": [SPEC_A, SPEC_B]}, None),
            gens=[({"files": FILES_A, "test_note": "t"}, None),
                  ({"files": FILES_B, "test_note": "t"}, None)],
            reviews=[({"verdict": "approve", "reasons": "ok", "revision_instructions": None}, None), ({"verdict": "approve", "reasons": "ok", "revision_instructions": None}, None)], record=track)

    out = eng._run_pipeline(None, "CTX", {"auto_apply": True, "auto_commit": True}, 3)

    assert peak[0] == 2, "engineers did not overlap — generation ran serially"
    assert len(out["accepted"]) == 2


def test_specs_sharing_a_file_are_rejected_before_generation(rig, monkeypatch):
    """The collision guard: a stale-read clobber must never reach the repo."""
    clash = {**SPEC_B, "files_to_touch": ["scripts/a_e41.py"]}
    calls = _script(monkeypatch,
                    plan=({"memo": "m", "proposals": [SPEC_A, clash]}, None),
                    gens=[({"files": FILES_A, "test_note": "t"}, None)],
                    reviews=[({"verdict": "approve", "reasons": "ok", "revision_instructions": None}, None)])

    out = eng._run_pipeline(None, "CTX", {"auto_apply": True, "auto_commit": True}, 3)

    assert len(out["accepted"]) == 1, "the colliding spec was implemented anyway"
    assert "claimed by an earlier spec" in out["rejected"][0]["error"]
    # plan + one generate + one review — the second engineer never ran
    assert [c["model"] for c in calls] == [
        eng.PLAN_MODEL, eng.GEN_MODEL, eng.REVIEW_MODEL]


def test_one_engineer_failing_does_not_sink_the_others(rig, monkeypatch):
    _script(monkeypatch,
            plan=({"memo": "m", "proposals": [SPEC_A, SPEC_B]}, None),
            gens=[(None, "timeout"),             # engineer A dies
                  ({"files": FILES_B, "test_note": "t"}, None)],
            reviews=[({"verdict": "approve", "reasons": "ok", "revision_instructions": None}, None)])

    out = eng._run_pipeline(None, "CTX", {"auto_apply": True, "auto_commit": True}, 3)

    assert len(out["accepted"]) == 1 and len(out["rejected"]) == 1
    assert "generate:" in out["rejected"][0]["error"]


def test_effort_levels_reach_the_right_stages(rig, monkeypatch):
    calls = _script(monkeypatch,
                    plan=({"memo": "m", "proposals": [SPEC_A]}, None),
                    gens=[({"files": FILES_A, "test_note": "t"}, None)],
                    reviews=[({"verdict": "approve", "reasons": "ok", "revision_instructions": None}, None)])

    eng._run_pipeline(None, "CTX", {"auto_apply": True, "auto_commit": True}, 3)

    by_model = {c["model"]: c["effort"] for c in calls}
    assert by_model[eng.GEN_MODEL] == eng.GEN_EFFORT == "xhigh"
    assert eng.PLAN_EFFORT == "max" and eng.REVIEW_EFFORT == "max"
    assert calls[0]["effort"] == "max" and calls[-1]["effort"] == "max"


def test_sessions_per_day_is_configurable_not_hardcoded(monkeypatch):
    """E44: 'open the budget' raised effort, parallelism and specs-per-night,
    but a hardcoded one-session-per-day cap kept the lane at the old cadence —
    and a supervised test run could consume the whole day's allowance."""
    import inspect
    src = inspect.getsource(eng.run_engineer)
    assert '_budget_ok("engineer", 1)' not in src, "the hardcoded cap is back"
    assert "engineer_sessions_per_day" in src

    seen = {}

    def fake_ok(kind, cap):
        seen[kind] = cap
        return False           # stop the run right after the budget check

    monkeypatch.setattr("research.director._budget_ok", fake_ok)
    monkeypatch.setattr(eng, "_autonomy", lambda: {
        "engineer_enabled": True, "mode": "paper_flexible",
        "engineer_sessions_per_day": 7, "max_pending_proposals": 9})
    monkeypatch.setattr(eng, "pending_count", lambda: 0)
    monkeypatch.setattr(eng, "get_key", lambda k: "sk-test")

    eng.run_engineer()
    assert seen["engineer"] == 7, "config value did not reach the budget check"
