"""E52: the only way out of a latched halt had no test at all.

Coverage measurement across the repo put `scripts/clear_halt.py` at 0%. It is
the human's sole route out of a halted fund, so a break there means a halted
book stays halted with no recovery path. Zero verification meeting real
consequence is the pairing worth hunting for, and this was the clearest case.

Reading it then found a race. The script read state.json, dropped the latch
key, and wrote the WHOLE file back, while the live process holds that same
state in memory and rewrites it every tick. Nothing serialised the two. A tick
landing inside the window would have its save clobbered by the stale copy the
script had already read, taking that tick's marks, prices and equity rows with
it. The runtime only ever checked whether the latch key was ABSENT, so
rewriting 1.2MB of state to signal one absence was never needed.

Now the script writes a sentinel and the runtime - the single writer - does the
clearing. One writer, no window.
"""
import json

import pytest


@pytest.fixture
def rt(tmp_path, monkeypatch):
    from runtime.live import LiveRuntime
    r = LiveRuntime.__new__(LiveRuntime)
    r.state_path = tmp_path / "state.json"
    r.state = {"halt_latched": {"ts": "2026-08-01T00:00:00Z", "reason": "dd_gate_2"}}
    r.state_path.write_text(json.dumps(r.state))
    import runtime.live as live
    monkeypatch.setattr(live, "send_telegram", lambda *a, **k: None)
    return r


def test_a_sentinel_lifts_the_latch(rt):
    (rt.state_path.parent / "halt_clear.request").write_text("{}")
    rt._sync_halt_clear_from_disk()
    assert "halt_latched" not in rt.state


def test_the_sentinel_is_consumed_so_it_cannot_clear_twice(rt):
    s = rt.state_path.parent / "halt_clear.request"
    s.write_text("{}")
    rt._sync_halt_clear_from_disk()
    assert not s.exists(), "a leftover sentinel would clear the next halt too"

    rt.state["halt_latched"] = {"ts": "later", "reason": "daily_loss_kill"}
    rt._sync_halt_clear_from_disk()
    assert "halt_latched" in rt.state, "a new halt must survive the old request"


def test_no_sentinel_means_the_halt_stays(rt):
    rt._sync_halt_clear_from_disk()
    assert "halt_latched" in rt.state


def test_a_hand_edited_state_file_still_works(rt):
    """The legacy path. Someone editing state.json directly is a real recovery
    route when the script is unavailable, and it must keep working."""
    rt.state_path.write_text(json.dumps({}))
    rt._sync_halt_clear_from_disk()
    assert "halt_latched" not in rt.state


def test_the_script_never_writes_state(tmp_path, monkeypatch):
    """The whole point. A second process must not rewrite the fund's state."""
    import scripts.clear_halt as ch
    data = tmp_path / "data"; data.mkdir()
    state = data / "state.json"
    state.write_text(json.dumps({"halt_latched": {"ts": "t", "reason": "r"},
                                 "equity_history": {"s1": [["t", 3_000_000]]}}))
    monkeypatch.setattr(ch, "DATA", data)
    monkeypatch.setattr(ch, "STATE", state)
    monkeypatch.setattr(ch, "SENTINEL", data / "halt_clear.request")
    monkeypatch.setattr(ch, "send_telegram", lambda *a, **k: None)

    before = state.read_bytes()
    ch.main()
    assert state.read_bytes() == before, (
        "clear_halt rewrote state.json; that is the race this removed")
    assert (data / "halt_clear.request").exists()


def test_the_script_is_a_noop_when_nothing_is_latched(tmp_path, monkeypatch):
    import scripts.clear_halt as ch
    data = tmp_path / "data"; data.mkdir()
    state = data / "state.json"; state.write_text(json.dumps({}))
    monkeypatch.setattr(ch, "DATA", data); monkeypatch.setattr(ch, "STATE", state)
    monkeypatch.setattr(ch, "SENTINEL", data / "halt_clear.request")
    monkeypatch.setattr(ch, "send_telegram", lambda *a, **k: None)
    ch.main()
    assert not (data / "halt_clear.request").exists(), (
        "a sentinel written with no halt latched would clear the NEXT halt")


def test_an_unreadable_state_does_not_request_a_clear(tmp_path, monkeypatch):
    import scripts.clear_halt as ch
    data = tmp_path / "data"; data.mkdir()
    state = data / "state.json"; state.write_text("{ truncated")
    monkeypatch.setattr(ch, "DATA", data); monkeypatch.setattr(ch, "STATE", state)
    monkeypatch.setattr(ch, "SENTINEL", data / "halt_clear.request")
    monkeypatch.setattr(ch, "send_telegram", lambda *a, **k: None)
    ch.main()
    assert not (data / "halt_clear.request").exists(), (
        "guessing past an unreadable state is how a halt gets cleared blind")
