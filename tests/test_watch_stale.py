"""Stale-tick pager (E27 follow-up): the independent cron monitor must page
on a wedged-but-alive loop, dedupe re-pages, and send one recovery all-clear.
All offline - send_telegram is captured, the clock is injected."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.watch_stale_tick as W

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(W, "send_telegram",
                        lambda m, **k: sent.append(m) or True)
    return sent


def state_file(tmp_path, minutes_ago, tz=True):
    p = tmp_path / "state.json"
    ts = NOW - timedelta(minutes=minutes_ago)
    if not tz:
        ts = ts.replace(tzinfo=None)
    p.write_text(json.dumps({"last_tick": ts.isoformat()}))
    return p


def test_fresh_tick_is_silent(tmp_path, alerts):
    out = W.check(state_file(tmp_path, 30), tmp_path / "wd.json", now=NOW)
    assert out["stale"] is False and out["alerted"] is False
    assert alerts == []


def test_stale_tick_pages(tmp_path, alerts):
    # 5h stale vs the 150-min default threshold - the E27 scenario
    out = W.check(state_file(tmp_path, 300), tmp_path / "wd.json", now=NOW)
    assert out["stale"] and out["alerted"]
    assert alerts and "STALE TICK" in alerts[0]


def test_repage_deduped_within_window(tmp_path, alerts):
    st = state_file(tmp_path, 300)
    led = tmp_path / "wd.json"
    W.check(st, led, now=NOW)
    out2 = W.check(st, led, now=NOW + timedelta(hours=1))
    assert out2["stale"] and not out2["alerted"]   # still stale, no spam
    assert len(alerts) == 1


def test_repage_after_realert_interval(tmp_path, alerts):
    st = state_file(tmp_path, 300)
    led = tmp_path / "wd.json"
    W.check(st, led, now=NOW)
    out = W.check(st, led, now=NOW + timedelta(hours=7))
    assert out["alerted"]
    assert len(alerts) == 2


def test_recovery_sends_one_all_clear(tmp_path, alerts):
    led = tmp_path / "wd.json"
    W.check(state_file(tmp_path, 300), led, now=NOW)          # page
    # heartbeat resumes: state written 30min before NOW, checked at NOW+1h
    # -> 90 min age, under the 150-min threshold
    out = W.check(state_file(tmp_path, 30), led,
                  now=NOW + timedelta(hours=1))
    assert out.get("recovered") is True
    assert len(alerts) == 2 and "resumed" in alerts[1]
    # second healthy pass: silence, no duplicate all-clear
    out2 = W.check(state_file(tmp_path, 30), led,
                   now=NOW + timedelta(hours=1))
    assert not out2.get("recovered")
    assert len(alerts) == 2


def test_missing_state_pages(tmp_path, alerts):
    out = W.check(tmp_path / "nope.json", tmp_path / "wd.json", now=NOW)
    assert out["stale"] and out["alerted"]
    assert alerts and "missing" in alerts[0]


def test_naive_timestamp_assumed_utc(tmp_path, alerts):
    # a tz-naive heartbeat must not crash the monitor or false-page
    out = W.check(state_file(tmp_path, 30, tz=False),
                  tmp_path / "wd.json", now=NOW)
    assert out["stale"] is False
    assert alerts == []


def test_threshold_configurable(tmp_path, alerts, monkeypatch):
    monkeypatch.setitem(W.CONFIG, "watchdog", {"stale_tick_minutes": 10})
    out = W.check(state_file(tmp_path, 30), tmp_path / "wd.json", now=NOW)
    assert out["stale"] and out["alerted"]
    assert len(alerts) == 1
