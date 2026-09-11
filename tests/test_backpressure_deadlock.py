"""E53: the backpressure cap counted corpses, and the lane stopped for good.

Under E22 a human reviewed the proposal queue each morning and drained it, so
counting every unclosed proposal toward `max_pending_proposals` was right.
Under E30's full authority nothing drains a FAILED proposal: `apply_proposal`
only ever runs on a green sandbox, and no human is reading the queue. Red
sandboxes therefore accumulate until they reach the cap, and then the lane
refuses to start at all.

It happened on 2026-08-02. The queue held P0015 (green, awaiting review), P0019
and P0020 (both red, and both superseded by P0023 which had already been
applied), and P0028 (red). Four pending against a cap of four. The digest read
"engineer skipped: 4 proposals pending review (backpressure cap 4)", the lane
did nothing, and the file-review directive written for it the previous evening
was never touched.

Fifth correct-mechanism-quiet-stop of the week, after E40's headroom assert,
E43's family rule, E44's session cap and E45's director 400. The pattern is
always the same: a limit that was right for the system it was written in, still
enforcing itself in a system that changed underneath it.

Raising the ceiling would be the wrong fix. It exists so unread work cannot
pile up without bound, and that reason still holds.
"""
import datetime as dt

import research.engineer as eng


def prop(**kw):
    base = {"id": "P9999", "status": "pending",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    base.update(kw)
    return base


def test_a_red_sandbox_is_not_work_in_progress():
    assert not eng.is_actionable(prop(sandbox={"passed": False}))


def test_a_green_sandbox_still_counts():
    assert eng.is_actionable(prop(sandbox={"passed": True}))


def test_a_proposal_written_seconds_ago_counts():
    """Not-yet-sandboxed and sandboxed-red are different states. Conflating
    them was the first cut of this fix, and it would have made the cap do
    nothing at all during a run."""
    assert eng.is_actionable(prop())


def test_a_superseded_proposal_stops_counting():
    assert not eng.is_actionable(prop(sandbox={"passed": True},
                                      superseded_by="P0023"))


def test_an_abandoned_proposal_ages_out():
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(days=eng._DEAD_AFTER_DAYS + 1)).isoformat()
    assert not eng.is_actionable(prop(sandbox={"passed": True}, created_at=old))


def test_the_exact_queue_that_deadlocked(monkeypatch):
    """P0015 green, P0019/P0020/P0028 red, cap 4. Before: 4 >= 4, lane dead.
    After: one live proposal, three corpses, lane free to work."""
    queue = [
        prop(id="P0015", sandbox={"passed": True}),
        prop(id="P0019", sandbox={"passed": False}),
        prop(id="P0020", sandbox={"passed": False}),
        prop(id="P0028", sandbox={"passed": False}),
    ]
    monkeypatch.setattr(eng, "list_proposals", lambda: queue)
    assert len([p for p in queue if p["status"] == "pending"]) == 4
    assert eng.pending_count() == 1, "corpses are still holding the gate shut"


def test_retiring_records_a_reason_and_frees_the_gate(monkeypatch):
    queue = [prop(id="P0019", sandbox={"passed": False}),
             prop(id="P0015", sandbox={"passed": True})]
    saved = []
    monkeypatch.setattr(eng, "list_proposals", lambda: queue)
    monkeypatch.setattr(eng, "save_record", lambda r: saved.append(r))

    closed = eng.retire_stale_proposals()
    assert [c["id"] for c in closed] == ["P0019"]
    assert queue[0]["status"] == "retired" and queue[0]["retire_reason"]
    assert queue[1]["status"] == "pending", "a live proposal must not be closed"
    assert saved, "a retirement that is not persisted is not a retirement"


def test_retiring_never_touches_applied_work(monkeypatch):
    queue = [prop(id="P0023", status="applied", sandbox={"passed": True})]
    monkeypatch.setattr(eng, "list_proposals", lambda: queue)
    monkeypatch.setattr(eng, "save_record", lambda r: None)
    assert eng.retire_stale_proposals() == []
    assert queue[0]["status"] == "applied"


def test_the_suite_cannot_write_to_the_live_proposal_store(monkeypatch, tmp_path):
    """E53, sixth instance of 'the offline suite touched a live system'.

    Placing retire_stale_proposals() before the backpressure check inside
    run_engineer gave the suite a path into the real store, and it took it: one
    run on the server retired all four real proposals. Benign - they were dead
    anyway - which is why it is worth a guard rather than a shrug.

    E63e changed the shape of the answer and this test follows it. The guard
    alone produced the opposite failure: three tests reach run_engineer
    legitimately, so on the BOX, where the live store holds real records, the
    guard fired and the suite stayed red for days while the identical tree was
    green anywhere the store was empty. The suite now REDIRECTS the store first
    and keeps the guard as the backstop.

    So there are two properties to hold, not one: the suite must not be pointed
    at the live store, and the guard must still bite if anything points it back.
    """
    import research.engineer as engineer

    from pathlib import Path
    live = Path("research/proposals").resolve()
    assert Path(engineer.PROPOSALS_DIR).resolve() != live, (
        "the suite is pointed at the LIVE proposal store; its results now "
        "depend on what production happens to be holding")

    # the backstop still bites when the live store is restored
    monkeypatch.setattr(engineer, "PROPOSALS_DIR", live, raising=False)
    with __import__("pytest").raises(AssertionError, match="LIVE store"):
        engineer.save_record({"id": "P9998", "dir": "P9998-x"})
