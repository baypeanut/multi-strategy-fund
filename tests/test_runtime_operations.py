"""Operational safety without live APIs, accounts, orders or state mutation."""

import json
import stat

import pytest
from runtime.live import LiveRuntime


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_invalid_runtime_interval_is_rejected_before_tick(interval, tmp_path, monkeypatch):
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    monkeypatch.setattr(rt, "tick", lambda: pytest.fail("Invalid scheduling must not run a tick"))
    with pytest.raises(ValueError):
        rt.run(interval=interval, max_ticks=1)


def test_runtime_errors_are_visible_without_upstream_secret_text(tmp_path, monkeypatch, capsys):
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))

    def failed():
        raise RuntimeError("token=secret-not-for-logs")

    monkeypatch.setattr(rt, "tick", failed)
    rt.run(interval=1, max_ticks=1)
    result = json.loads(capsys.readouterr().out)
    assert result == {"event": "tick_failed", "error_type": "RuntimeError"}
    assert not (tmp_path / "state.json").exists()


def test_atomic_state_save_is_private_and_round_trips(tmp_path):
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    rt._save()
    assert json.loads(rt.state_path.read_text()) == rt.state
    assert stat.S_IMODE(rt.state_path.stat().st_mode) == 0o600
    assert not rt.state_path.with_suffix(".json.tmp").exists()


def test_state_save_does_not_follow_a_preexisting_predictable_temp_symlink(tmp_path):
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    victim = tmp_path / "untouched.txt"
    victim.write_text("untouched")
    rt.state_path.with_suffix(".json.tmp").symlink_to(victim)
    rt._save()
    assert victim.read_text() == "untouched"
    assert json.loads(rt.state_path.read_text()) == rt.state


def test_failed_atomic_replace_keeps_original_state_and_removes_only_own_temp(tmp_path, monkeypatch):
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    rt.state_path.write_text('{"original": true}')

    def failed_replace(*args):
        raise OSError("simulated rename failure")

    monkeypatch.setattr("runtime.live.os.replace", failed_replace)
    with pytest.raises(OSError):
        rt._save()
    assert json.loads(rt.state_path.read_text()) == {"original": True}
    assert not list(tmp_path.glob(".state.json-*"))
