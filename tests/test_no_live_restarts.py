"""E38: the offline suite must never operate live systemd units.

Third instance of one class of defect — E31 (the suite dialled the real IB
Gateway), E35 (it fetched SEC EDGAR live), and now E34's restart powers: any
test exercising the stale-link path issued a real `sudo systemctl restart
ib-gateway`. 98 real restarts on 2026-07-28, ~6 per suite run, DURING market
hours, including the unattended 04:30 apply-gate runs.

These tests pin the guard itself, so the next power that shells out to the
host cannot quietly repeat it.
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import research.engineer as eng
import scripts.watch_stale_tick as w
from core.config import CONFIG


def test_restart_config_flags_are_off_for_the_suite():
    assert CONFIG["watchdog"]["auto_restart_gateway"] is False
    assert CONFIG["agent_autonomy"]["auto_restart"] is False


def test_systemctl_restart_is_blocked_at_the_subprocess_boundary():
    """Even called directly, the helpers cannot reach systemd."""
    assert w._restart_unit("ib-gateway")["ok"] is False
    assert eng._restart_service()["ok"] is False


def test_other_subprocess_calls_still_work():
    """The guard must be surgical — the suite still runs pytest, git, etc."""
    import subprocess
    r = subprocess.run(["echo", "ok"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok"


def test_the_live_stale_path_shells_out_to_nothing(monkeypatch):
    """The exact path that fired: a stale link with auto_restart_gateway on."""
    monkeypatch.setattr(w, "send_telegram", lambda m: None)
    monkeypatch.setitem(CONFIG["watchdog"], "auto_restart_gateway", True)
    monkeypatch.setitem(CONFIG["ibkr"], "enabled", True)   # _no_live_broker turns it off
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "state.json"
        state.write_text(json.dumps(
            {"ibkr": {"last_refresh": (now - timedelta(hours=9)).isoformat()}}))
        res = w.check_ibkr(state_path=state, ledger_path=Path(d) / "wd.json",
                           now=now)
    assert res["stale"] and res["alerted"]      # it still pages
    # ...but the restart it attempted was refused at the boundary


def test_a_test_may_still_assert_restart_behavior(monkeypatch):
    """The guard must not make restart logic untestable: a later monkeypatch
    in the test wins over the autouse fixture."""
    seen = []
    monkeypatch.setattr(w, "_restart_unit",
                        lambda unit: seen.append(unit) or {"ok": True})
    assert w._restart_unit("ib-gateway") == {"ok": True}
    assert seen == ["ib-gateway"]


def test_the_suite_cannot_spend_money(monkeypatch):
    """E46: `nightly.main()` imports run_engineer inside the function, so a
    patch on research.nightly did nothing and a real Fable+Opus session fired
    from the test suite. The credential boundary is the fix an inner import
    cannot bypass."""
    import research.director as director
    import research.engineer as engineer

    assert engineer.get_key("ANTHROPIC_API_KEY") is None
    assert director.get_key("ANTHROPIC_API_KEY") is None
    # the lane declines to run at all without a key
    monkeypatch.setattr(engineer, "_autonomy", lambda: {
        "engineer_enabled": True, "mode": "paper_flexible",
        "max_pending_proposals": 9, "engineer_sessions_per_day": 4})
    monkeypatch.setattr(engineer, "pending_count", lambda: 0)
    assert engineer.run_engineer() is None
    assert director.run_director([]) is None
