"""IBKR-link staleness pager: the broker calls are bounded and fail-safe, so
a wedged Gateway degrades silently - observed 2026-07-25, ~15h with no
successful broker read while a halt-flatten could not have reached the paper
account (E29 gap). The independent watchdog cron must page, dedupe re-pages,
send one all-clear on recovery, keep its ledger keys disjoint from the tick
pager's, and stay silent when the mirror is disabled. All offline
send_telegram captured, clock injected, no network."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.watch_stale_tick as W

NOW = datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(W, "send_telegram",
                        lambda m, **k: sent.append(m) or True)
    return sent


@pytest.fixture
def ibkr_on(monkeypatch):
    # the offline conftest disables the live broker suite-wide; this check
    # must see the flag exactly as the server does
    monkeypatch.setitem(W.CONFIG.setdefault("ibkr", {}), "enabled", True)


def state_file(tmp_path, *, refresh_hours_ago=None, sync_hours_ago=None,
               refresh_error=None, block=True, tz=True):
    p = tmp_path / "state.json"
    ibk = {}
    if refresh_hours_ago is not None:
        ts = NOW - timedelta(hours=refresh_hours_ago)
        if not tz:
            ts = ts.replace(tzinfo=None)
        ibk["last_refresh"] = ts.isoformat()
    if sync_hours_ago is not None:
        ibk["last_sync"] = (NOW - timedelta(hours=sync_hours_ago)).isoformat()
    if refresh_error:
        ibk["refresh_error"] = refresh_error
    payload = {"last_tick": NOW.isoformat()}
    if block:
        payload["ibkr"] = ibk
    p.write_text(json.dumps(payload))
    return p


def test_disabled_mirror_is_silent(tmp_path, alerts):
    # conftest leaves ibkr.enabled False for the offline suite - the check
    # must be a no-op even against a wildly stale link
    out = W.check_ibkr(state_file(tmp_path, refresh_hours_ago=15),
                       tmp_path / "wd.json", now=NOW)
    assert out["stale"] is False and alerts == []
    assert "disabled" in out["detail"]


def test_fresh_link_is_silent(tmp_path, alerts, ibkr_on):
    out = W.check_ibkr(state_file(tmp_path, refresh_hours_ago=1),
                       tmp_path / "wd.json", now=NOW)
    assert out["stale"] is False and out["alerted"] is False
    assert alerts == []


def test_wedged_gateway_pages_with_error_detail(tmp_path, alerts, ibkr_on):
    # the live 2026-07-25 scenario: last read 03:48, refresh_error stamped
    out = W.check_ibkr(
        state_file(tmp_path, refresh_hours_ago=15,
                   refresh_error="refresh timed out (Gateway unresponsive)"),
        tmp_path / "wd.json", now=NOW)
    assert out["stale"] and out["alerted"]
    assert alerts and "IBKR LINK STALE" in alerts[0]
    assert "Gateway unresponsive" in alerts[0]


def test_newest_of_refresh_and_sync_wins(tmp_path, alerts, ibkr_on):
    # a recent SYNC counts as a live link even if refresh stamps are old
    out = W.check_ibkr(
        state_file(tmp_path, refresh_hours_ago=15, sync_hours_ago=1),
        tmp_path / "wd.json", now=NOW)
    assert out["stale"] is False
    assert alerts == []


def test_repage_deduped_then_due(tmp_path, alerts, ibkr_on):
    st = state_file(tmp_path, refresh_hours_ago=15)
    led = tmp_path / "wd.json"
    W.check_ibkr(st, led, now=NOW)
    out2 = W.check_ibkr(st, led, now=NOW + timedelta(hours=1))
    assert out2["stale"] and not out2["alerted"]   # still stale, no spam
    assert len(alerts) == 1
    out3 = W.check_ibkr(st, led, now=NOW + timedelta(hours=7))
    assert out3["alerted"]
    assert len(alerts) == 2


def test_recovery_sends_one_all_clear(tmp_path, alerts, ibkr_on):
    led = tmp_path / "wd.json"
    W.check_ibkr(state_file(tmp_path, refresh_hours_ago=15), led, now=NOW)
    out = W.check_ibkr(state_file(tmp_path, refresh_hours_ago=1), led,
                       now=NOW + timedelta(hours=1))
    assert out.get("recovered") is True
    assert len(alerts) == 2 and "recovered" in alerts[1]
    out2 = W.check_ibkr(state_file(tmp_path, refresh_hours_ago=1), led,
                        now=NOW + timedelta(hours=1))
    assert not out2.get("recovered")
    assert len(alerts) == 2


def test_enabled_but_no_ibkr_block_is_silent(tmp_path, alerts, ibkr_on):
    # fresh deploy: mirror enabled but the loop has not attempted it yet
    out = W.check_ibkr(state_file(tmp_path, block=False),
                       tmp_path / "wd.json", now=NOW)
    assert out["stale"] is False and alerts == []


def test_block_without_any_success_pages(tmp_path, alerts, ibkr_on):
    # attempts recorded (error stamped) but never a successful read
    out = W.check_ibkr(state_file(tmp_path, refresh_error="TimeoutError: "),
                       tmp_path / "wd.json", now=NOW)
    assert out["stale"] and out["alerted"]
    assert alerts and "no successful read" in alerts[0]


def test_naive_timestamp_assumed_utc(tmp_path, alerts, ibkr_on):
    out = W.check_ibkr(state_file(tmp_path, refresh_hours_ago=1, tz=False),
                       tmp_path / "wd.json", now=NOW)
    assert out["stale"] is False
    assert alerts == []


def test_unreadable_state_is_tick_pagers_job(tmp_path, alerts, ibkr_on):
    out = W.check_ibkr(tmp_path / "nope.json", tmp_path / "wd.json", now=NOW)
    assert out["stale"] is False and alerts == []


def test_ledger_keys_are_namespaced(tmp_path, alerts, ibkr_on):
    # both pagers latch on the same ledger file; recovering one must not
    # disturb the other's dedupe state
    led = tmp_path / "wd.json"
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "last_tick": (NOW - timedelta(hours=10)).isoformat(),
        "ibkr": {"last_refresh": (NOW - timedelta(hours=15)).isoformat()},
    }))
    W.check(p, led, now=NOW)
    W.check_ibkr(p, led, now=NOW)
    assert len(alerts) == 2
    p.write_text(json.dumps({
        "last_tick": (NOW - timedelta(hours=10)).isoformat(),
        "ibkr": {"last_refresh": NOW.isoformat()},
    }))
    out = W.check_ibkr(p, led, now=NOW)
    assert out.get("recovered") is True
    led_data = json.loads(led.read_text())
    assert led_data.get("alerting") is True          # tick pager still latched
    assert led_data.get("ibkr_alerting") is False


def test_ibkr_threshold_configurable(tmp_path, alerts, ibkr_on, monkeypatch):
    monkeypatch.setitem(W.CONFIG, "watchdog", {"ibkr_stale_hours": 0.5})
    out = W.check_ibkr(state_file(tmp_path, refresh_hours_ago=1),
                       tmp_path / "wd.json", now=NOW)
    assert out["stale"] and out["alerted"]
    assert len(alerts) == 1
