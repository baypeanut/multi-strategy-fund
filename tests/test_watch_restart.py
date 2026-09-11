"""Applied-but-not-running pager (2026-07-26).

The lane ships code unattended but cannot restart a service. P0011 landed at
04:43 and was still not running at 12:37 — 8h during which the data-loss it
fixes carried on, while systemd said active, the tick was fresh and git was
clean. Nothing could see it. These tests pin the detector and, most of all,
its silence when it cannot know (an unavailable service start time must never
produce a page — a pager that cries wolf is worse than none).
"""
from datetime import datetime, timedelta, timezone

import scripts.watch_stale_tick as w

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _repo(tmp_path, mtime):
    d = tmp_path / "runtime"
    d.mkdir(parents=True)
    f = d / "live.py"
    f.write_text("x = 1\n")
    import os
    os.utime(f, (mtime.timestamp(), mtime.timestamp()))
    return tmp_path


def _patch(monkeypatch, started, sent):
    monkeypatch.setattr(w, "_service_started_at", lambda service=None: started)
    monkeypatch.setattr(w, "send_telegram", lambda m: sent.append(m))


def test_pages_when_code_is_newer_than_the_process(tmp_path, monkeypatch):
    sent = []
    _patch(monkeypatch, NOW - timedelta(hours=8), sent)
    root = _repo(tmp_path, NOW - timedelta(hours=1))     # applied 7h after start

    res = w.check_restart(ledger_path=tmp_path / "wd.json", now=NOW, root=root)

    assert res["stale"] and res["alerted"]
    assert len(sent) == 1
    assert "RESTART NEEDED" in sent[0]
    assert "systemctl restart paper-trader" in sent[0]
    assert "runtime/live.py" in sent[0]


def test_quiet_within_the_grace_window(tmp_path, monkeypatch):
    """A deploy writes files seconds before the restart it triggers; that
    ordering must not page."""
    sent = []
    started = NOW - timedelta(minutes=5)
    _patch(monkeypatch, started, sent)
    root = _repo(tmp_path, started + timedelta(minutes=2))

    res = w.check_restart(ledger_path=tmp_path / "wd.json", now=NOW, root=root)
    assert not res["stale"] and not sent


def test_silent_when_service_time_is_unknown(tmp_path, monkeypatch):
    """No systemd, or unknown unit: say nothing rather than guess."""
    sent = []
    _patch(monkeypatch, None, sent)
    root = _repo(tmp_path, NOW)

    res = w.check_restart(ledger_path=tmp_path / "wd.json", now=NOW, root=root)
    assert not res["stale"] and not res["alerted"] and not sent


def test_dedupes_then_sends_one_all_clear(tmp_path, monkeypatch):
    sent = []
    led = tmp_path / "wd.json"
    _patch(monkeypatch, NOW - timedelta(hours=8), sent)
    root = _repo(tmp_path, NOW - timedelta(hours=1))

    w.check_restart(ledger_path=led, now=NOW, root=root)
    w.check_restart(ledger_path=led, now=NOW + timedelta(hours=1), root=root)
    assert len(sent) == 1, "re-paged inside the realert window"

    # operator restarts: process start now leads the code
    _patch(monkeypatch, NOW + timedelta(hours=2), sent)
    res = w.check_restart(ledger_path=led, now=NOW + timedelta(hours=3), root=root)

    assert res.get("recovered") and len(sent) == 2 and "Restart done" in sent[1]

    w.check_restart(ledger_path=led, now=NOW + timedelta(hours=4), root=root)
    assert len(sent) == 2, "all-clear must fire exactly once"
