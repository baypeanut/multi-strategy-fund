"""E48: the lane works rounds until it settles, and always says why it stopped.

It used to run exactly one session and withdraw, so a night's output was capped
at whatever the planner thought of on its first look at the tree, even when the
first round's own commits opened the obvious next piece of work.

Every stop path is pinned here. A loop that ends silently is the same defect
class as E40/E43/E44/E45: the mechanism was right, the silence was the bug.
"""
import pytest

import research.engineer as eng


@pytest.fixture
def lane(monkeypatch):
    """Drive the loop with scripted round outcomes."""
    def run(rounds, **cfg):
        calls = []

        def fake_run_engineer(dry_run=False):
            calls.append(1)
            return rounds[min(len(calls) - 1, len(rounds) - 1)]

        monkeypatch.setattr(eng, "run_engineer", fake_run_engineer)
        monkeypatch.setattr(eng, "_autonomy", lambda: {
            "engineer_max_rounds": cfg.get("max_rounds", 4),
            "engineer_barren_rounds": cfg.get("barren", 2)})
        out = eng.run_engineer_until_settled()
        return out, len(calls)
    return run


def _applied(n=1):
    return {"accepted": [{"auto_apply": {"ok": True}} for _ in range(n)],
            "rejected": []}


def _barren():
    return {"accepted": [], "rejected": [{"name": "x", "error": "clash"}]}


def test_it_keeps_going_while_work_lands(lane):
    out, calls = lane([_applied()], max_rounds=4)
    assert calls == 4, "a productive round must not be the last one"
    assert out["applied_total"] == 4
    assert out["stop_reason"] == "round_cap"


def test_planner_proposing_nothing_is_a_legitimate_end(lane):
    """The lane's own 'everything looks fine, wait and see'."""
    out, calls = lane([{"accepted": [], "rejected": []}])
    assert calls == 1 and out["stop_reason"] == "stood_down"


def test_it_gives_up_after_repeated_barren_rounds(lane):
    out, calls = lane([_barren()], barren=2)
    assert calls == 2, "two barren rounds, then stop"
    assert out["stop_reason"] == "no_progress" and out["applied_total"] == 0


def test_one_barren_round_is_not_enough_to_quit(lane):
    """A clean resubmit after a rejection is a real pattern (E23/E24)."""
    out, calls = lane([_barren(), _applied(), _applied(), _applied()],
                      max_rounds=4, barren=2)
    assert calls == 4 and out["applied_total"] == 3
    assert out["stop_reason"] == "round_cap"


def test_budget_exhaustion_is_named_not_silent(lane):
    out, calls = lane([{"skipped": "daily session budget spent (4/day)"}])
    assert calls == 1 and out["stop_reason"] == "budget"


def test_an_error_stops_the_loop_and_is_surfaced(lane):
    out, calls = lane([{"error": "BadRequestError: 400"}])
    assert calls == 1 and out["stop_reason"] == "blocked"
    assert out["rounds"][0]["error"].startswith("BadRequestError")


def test_no_key_returns_none_and_burns_nothing(monkeypatch):
    """A missing key is a decision not to act, distinct from every failure."""
    monkeypatch.setattr(eng, "run_engineer", lambda dry_run=False: None)
    monkeypatch.setattr(eng, "_autonomy", lambda: {"engineer_max_rounds": 4})
    assert eng.run_engineer_until_settled() is None


def test_every_round_is_reported(lane):
    """Rounds are evidence. The digest must be able to show all of them."""
    out, _ = lane([_applied(), _applied(), _barren(), _barren()], max_rounds=4)
    assert out["n_rounds"] == len(out["rounds"]) == 4


def test_budget_exhaustion_no_longer_looks_like_a_missing_key(monkeypatch):
    """The split that makes the loop's stop reasons trustworthy."""
    monkeypatch.setattr(eng, "get_key", lambda k: "sk-test")
    monkeypatch.setattr(eng, "_autonomy", lambda: {
        "engineer_enabled": True, "mode": "paper_flexible",
        "max_pending_proposals": 9, "engineer_sessions_per_day": 4})
    monkeypatch.setattr(eng, "pending_count", lambda: 0)
    monkeypatch.setattr("research.director._budget_ok", lambda kind, cap: False)

    out = eng.run_engineer()
    assert out is not None, "budget exhaustion returned None, same as no key"
    assert "budget" in out["skipped"]


def test_a_truncated_plan_is_a_bad_draw_not_the_end_of_the_night(lane):
    """E55. On 2026-08-02 one malformed planning call ended a run that had ten
    rounds of budget left. The planner had been given the smallest token budget
    of the three big stages while running at the deepest effort setting, so its
    thinking consumed the budget and the JSON truncated at 1057 chars."""
    out, calls = lane([{"retryable": "plan: malformed JSON", "accepted": [], "rejected": []},
                       _applied(), _applied(), _applied()], max_rounds=4, barren=3)
    assert calls == 4, "a bad planning draw must not end the run"
    assert out["applied_total"] == 3


def test_repeated_plan_failures_still_stop(lane):
    """Retryable is not infinite: if every round's plan fails, that is a real
    stall and burning twelve sessions on it helps nobody."""
    out, calls = lane([{"retryable": "plan: malformed JSON",
                        "accepted": [], "rejected": []}], barren=3)
    assert calls == 3 and out["stop_reason"] == "no_progress"
