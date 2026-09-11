"""E34 auto-restart: the lane may now reload the service its own fix targets,
and the watchdog may restart a wedged Gateway. Both must fail SOFT — a restart
problem never un-applies a change and never crashes a pager pass."""
from datetime import datetime, timezone
from types import SimpleNamespace

import research.engineer as eng
import scripts.watch_stale_tick as w


def _fake_run(script):
    """script: {(cmd tail as tuple): (returncode, stdout)}"""
    def run(cmd, capture_output=True, text=True, timeout=None, check=False):
        key = tuple(cmd[-2:])
        rc, out = script.get(key, (1, ""))
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
    return run


def test_restart_service_success(monkeypatch):
    monkeypatch.setattr(eng.subprocess, "run", _fake_run({
        ("restart", "paper-trader"): (0, ""),
        ("is-active", "paper-trader"): (0, "active\n")}))
    monkeypatch.setattr(eng, "_restart_sleep", 0, raising=False)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert eng._restart_service() == {"ok": True, "unit": "paper-trader"}


def test_restart_service_sudo_denied_fails_soft(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(eng.subprocess, "run", _fake_run({}))  # rc=1 everywhere
    res = eng._restart_service()
    assert res["ok"] is False and "error" in res


def test_restart_service_unit_dead_after_restart(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(eng.subprocess, "run", _fake_run({
        ("restart", "paper-trader"): (0, ""),
        ("is-active", "paper-trader"): (0, "failed\n")}))
    res = eng._restart_service()
    assert res["ok"] is False and "failed" in res["error"]


def test_watchdog_restarts_gateway_when_enabled(tmp_path, monkeypatch):
    """Stale link + auto_restart_gateway -> restart attempted, page says so."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    state = tmp_path / "state.json"
    state.write_text(
        '{"ibkr": {"last_refresh": "2026-07-26T02:00:00+00:00"}}')
    sent, restarted = [], []
    monkeypatch.setattr(w, "send_telegram", lambda m: sent.append(m))
    monkeypatch.setattr(w, "_restart_unit",
                        lambda unit: restarted.append(unit) or {"ok": True})
    monkeypatch.setattr(w, "CONFIG", {
        "ibkr": {"enabled": True},
        "watchdog": {"ibkr_stale_hours": 3, "auto_restart_gateway": True}})

    res = w.check_ibkr(state_path=state, ledger_path=tmp_path / "wd.json", now=now)

    assert res["stale"] and res["alerted"]
    assert restarted == ["ib-gateway"]
    assert "Auto-restarted ib-gateway" in sent[0]


def test_watchdog_no_restart_when_disabled_or_healthy(tmp_path, monkeypatch):
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    restarted = []
    monkeypatch.setattr(w, "send_telegram", lambda m: None)
    monkeypatch.setattr(w, "_restart_unit",
                        lambda unit: restarted.append(unit) or {"ok": True})

    # disabled (code default False) -> page but no restart
    state = tmp_path / "s1.json"
    state.write_text('{"ibkr": {"last_refresh": "2026-07-26T02:00:00+00:00"}}')
    monkeypatch.setattr(w, "CONFIG", {"ibkr": {"enabled": True}, "watchdog": {}})
    assert w.check_ibkr(state_path=state, ledger_path=tmp_path / "w1.json",
                        now=now)["stale"]
    assert not restarted

    # healthy link -> nothing
    state2 = tmp_path / "s2.json"
    state2.write_text('{"ibkr": {"last_refresh": "2026-07-26T11:30:00+00:00"}}')
    monkeypatch.setattr(w, "CONFIG", {
        "ibkr": {"enabled": True},
        "watchdog": {"auto_restart_gateway": True}})
    assert not w.check_ibkr(state_path=state2, ledger_path=tmp_path / "w2.json",
                            now=now)["stale"]
    assert not restarted
