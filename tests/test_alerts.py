"""Daily-only Telegram posture."""
from __future__ import annotations

from datetime import datetime, timezone

from core.alerts import (
    format_daily_digest,
    maybe_send_daily_digest,
    queue_note,
    send_telegram,
)


def test_send_telegram_non_urgent_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr("core.alerts.get_key", lambda k: "x")
    monkeypatch.setattr(
        "core.alerts.urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")),
    )
    assert send_telegram("noise", urgent=False) is False
    assert calls == []


def test_queue_note_dedupes_and_caps():
    state: dict = {}
    queue_note(state, "a")
    queue_note(state, "a")  # dup of last
    queue_note(state, "b")
    assert state["telegram_notes"] == ["a", "b"]


def test_maybe_send_daily_digest_once_per_day(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "core.alerts.send_telegram",
        lambda msg, timeout=10, urgent=True: sent.append(msg) or True,
    )
    state = {
        "nav0": 3_000_000,
        "systems": {
            "s1": {"equity": 3_010_000},
            "s2": {"equity": 2_990_000},
            "s3": {"equity": 2_980_000},
            "s4": {"equity": 2_985_000},
        },
        "telegram_notes": ["GOVERNOR: risk_scale=0.8"],
        "ticks": 10,
        "last_tick": "2026-07-20T21:05:00+00:00",
    }
    before = datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc)
    assert maybe_send_daily_digest(state, before, hour_utc=21) is False
    assert sent == []

    after = datetime(2026, 7, 20, 21, 5, tzinfo=timezone.utc)
    assert maybe_send_daily_digest(state, after, hour_utc=21) is True
    assert len(sent) == 1
    assert "Daily summary" in sent[0]
    assert "S4" in sent[0]
    assert state["telegram_digest_date"] == "2026-07-20"
    assert state["telegram_notes"] == []

    # second call same day → no send
    assert maybe_send_daily_digest(state, after, hour_utc=21) is False
    assert len(sent) == 1


def test_format_daily_digest_includes_ibkr():
    state = {
        "nav0": 3_000_000,
        "systems": {"s1": {"equity": 3e6}, "s2": {"equity": 3e6},
                    "s3": {"equity": 3e6}, "s4": {"equity": 3e6}},
        "ibkr": {"account": "DU0000000", "nav": 985_000, "n_positions": 100,
                 "book": {"max_abs_drift": 0.001}},
    }
    msg = format_daily_digest(state, datetime(2026, 7, 20, tzinfo=timezone.utc))
    assert "IBKR" in msg and "985,000" in msg
