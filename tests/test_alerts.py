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
        "ibkr": {"account": "DU000000", "nav": 985_000, "n_positions": 100,
                 "book": {"max_abs_drift": 0.001}},
    }
    msg = format_daily_digest(state, datetime(2026, 7, 20, tzinfo=timezone.utc))
    assert "IBKR" in msg and "985,000" in msg


def test_digest_includes_every_marked_book():
    """Digest lists the books state actually carries — S5 included."""
    now = datetime(2026, 8, 5, 21, 0, tzinfo=timezone.utc)
    state = {
        "nav0": 3_000_000,
        "systems": {
            "s1": {"equity": 3_010_000},
            "s2": {"equity": 2_990_000},
            "s3": {"equity": 2_980_000},
            "s4": {"equity": 2_985_000},
            "s5": {"equity": 2_910_278},
        },
    }
    msg = format_daily_digest(state, now)
    assert "S5" in msg
    assert "S5 $2,910,278 (-2.99% vs start)" in msg

    # s1..s5 order, one line each
    idx = [msg.index(f"S{i} $") for i in range(1, 6)]
    assert idx == sorted(idx)
    assert sum(1 for ln in msg.splitlines() if ln.startswith("S5 ")) == 1

    # drop s5 → no S5 line at all
    del state["systems"]["s5"]
    msg2 = format_daily_digest(state, now)
    assert "S5" not in msg2
    assert "S4 $2,985,000" in msg2

    # fresh boot (no systems yet) still prints the four core books at nav0
    msg3 = format_daily_digest({"nav0": 3_000_000}, now)
    for i in range(1, 5):
        assert f"S{i} $3,000,000 (+0.00% vs start)" in msg3
    assert "S5" not in msg3


def test_the_offline_suite_cannot_message_the_owner():
    """E67. maybe_send_daily_digest sends via core.alerts.send_telegram, but
    ticking tests patched runtime.live.send_telegram - a different reference -
    so digest messages went out over the real bot token whenever the suite ran
    on the box, flooding the owner with fresh-state $3M summaries.

    Pinned by IDENTITY rather than by attempting a send: attempting one is the
    exact spam this prevents, and on the box it would reach the owner. The
    autouse guard installs a marked no-op at the module's definition; if the
    guard is ever removed, this attribute is the real network function again and
    the marker is gone. Never sends, fails without the guard on any machine."""
    import core.alerts as alerts
    assert getattr(alerts.send_telegram, "_offline_guard", False) is True, (
        "core.alerts.send_telegram is not the offline guard - the suite can "
        "message the owner's phone again")
