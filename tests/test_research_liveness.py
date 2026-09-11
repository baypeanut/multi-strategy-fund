"""E43: the research engine went silent for 13 days and nothing noticed.

The fund's whole purpose is finding edge through the armored harness. Between
2026-07-17 and 2026-07-30 it ran ZERO experiments: four consecutive director
specs were rejected into the confirmed `8k-drift` family (correctly — a
confirmed family is closed forever), the queue emptied, and no instrument
measured it. Same shape as E36/E37: the defect was invisible because nothing
counted it.

`research_liveness()` makes the stall a number, and it is surfaced to the
director, the engineer lane and the daily digest.
"""
import json

import pytest

from research import director as D


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "RESULTS", tmp_path / "RESULTS.jsonl")
    monkeypatch.setattr(D, "HYPOTHESES", tmp_path / "hypotheses.yaml")
    monkeypatch.setattr(D, "load_registry", lambda: {"families": {}})
    return tmp_path


def _results(rig, ts, n=1):
    (rig / "RESULTS.jsonl").write_text("\n".join(
        json.dumps({"id": f"N{i:04d}", "ts": ts, "verdict": "FAIL",
                    "spec": {"name": "probe"}}) for i in range(n)))


def _queue(rig, yaml_text):
    (rig / "hypotheses.yaml").write_text(yaml_text)


def test_stall_is_detected_and_explained(rig, monkeypatch):
    """The exact 2026-07-30 shape: old last result, empty queue, rejections."""
    _results(rig, "2026-07-17T00:00:00+00:00")
    _queue(rig, """
- name: F1b — all-8K drift at breadth
  status: rejected
  reject_reason: family '8k-drift' is confirmed — closed to new trials
- name: F1d — per-item drift localization
  status: rejected
  reject_reason: family '8k-drift' is confirmed — closed to new trials
""")
    monkeypatch.setattr(D, "load_registry", lambda: {"families": {
        "8k-drift": {"status": "confirmed", "trials": 3},
        "price-factors": {"status": "open", "trials": 11}}})

    out = D.research_liveness()

    assert out["STALLED"] is True
    assert out["days_since_last_experiment"] >= 13
    assert out["queue_pending"] == 0
    assert "8k-drift(confirmed,3 trials)" in out["closed_families"]
    assert "price-factors(open,11 trials)" in out["open_families"]
    assert len(out["recent_rejections"]) == 2
    assert "closed to new trials" in out["recent_rejections"][0]["why"]
    assert "that reason is the finding" in out["note"]


def test_a_healthy_lane_is_not_flagged(rig):
    from datetime import date
    _results(rig, f"{date.today().isoformat()}T00:00:00+00:00")
    _queue(rig, "- name: next probe\n  status: pending\n")

    out = D.research_liveness()

    assert out["STALLED"] is False
    assert out["queue_pending"] == 1
    assert out["days_since_last_experiment"] == 0
    assert "note" not in out


def test_pending_queue_alone_prevents_the_stall_flag(rig):
    """Old last result but work IS queued — that is a slow night, not a stall."""
    _results(rig, "2026-07-17T00:00:00+00:00")
    _queue(rig, "- name: queued probe\n  status: pending\n")
    assert D.research_liveness()["STALLED"] is False


def test_missing_or_broken_files_do_not_raise(rig):
    """A liveness probe must never be the thing that breaks the nightly."""
    out = D.research_liveness()          # no files at all
    assert out["days_since_last_experiment"] is None and out["queue_pending"] == 0

    (rig / "RESULTS.jsonl").write_text("not json\n")
    (rig / "hypotheses.yaml").write_text("{{{ broken yaml")
    out2 = D.research_liveness()
    assert out2["queue_pending"] == 0


def test_the_engineer_lane_can_see_it():
    """The lane reviews the fund nightly; the stall must be in front of it."""
    import research.engineer as eng
    led = eng._research_ledger()
    assert "RESEARCH LIVENESS" in led
    assert "unavailable" not in led


def test_director_prompt_teaches_the_way_out():
    low = D._DIRECTOR_SYSTEM.lower()
    assert "when your best family is closed, open a new one" in low
    assert "free text" in low                     # family naming is the lever
    assert "ic_weighting" in low                  # the available new mechanism
    assert "thirteen days" in low                 # the incident is named
