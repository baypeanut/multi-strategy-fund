"""E36: the fills ledger must capture the WHOLE day, not one read window.

Observed 2026-07-27: the broker saw 4019 executions, the ledger captured 669.
Two caps, both silent: the broker reported only `fills[-300:]` per read, and
the runtime deduped against the 300-row display ring — so a burst larger than
the ring lost everything in between, permanently (IBKR replays only today).
These tests pin both halves: the broker reports uncapped, and dedupe is
ledger-scoped so reporting the full day cannot produce duplicates.
"""
import json

import pytest

from runtime.live import LiveRuntime


def _fill(i: int) -> dict:
    return {"id": f"exec{i:05d}", "symbol": "AAPL", "side": "BOT", "shares": 1,
            "price": 100.0 + i, "time": "2026-07-27 14:00:00+00:00",
            "commission": 0.0}


@pytest.fixture
def rt(tmp_path, monkeypatch):
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    r = LiveRuntime(state_path=str(tmp_path / "state.json"))
    r.state.setdefault("ibkr", {})
    return r


def test_a_burst_larger_than_the_ring_is_fully_persisted(rt):
    """The exact 2026-07-27 shape: one read carrying more fills than the ring."""
    burst = [_fill(i) for i in range(1000)]
    rt._merge_ibkr_state({"mode": "refresh", "fills_today": burst,
                          "n_fills_today": len(burst)})

    rows = [json.loads(l) for l in rt.fills_ledger_path.read_text().splitlines()]
    assert len(rows) == 1000, "ledger must hold the whole burst, not the ring tail"
    assert {r["id"] for r in rows} == {f["id"] for f in burst}
    assert len(rt.state["ibkr"]["fills"]) == 300, "display ring stays bounded"


def test_repeated_full_day_reports_never_duplicate(rt):
    """The broker now re-reports the whole day every read; dedupe must hold."""
    day = [_fill(i) for i in range(500)]
    for _ in range(3):
        rt._merge_ibkr_state({"mode": "refresh", "fills_today": list(day),
                              "n_fills_today": len(day)})

    rows = rt.fills_ledger_path.read_text().splitlines()
    assert len(rows) == 500, f"re-reported day duplicated into the ledger: {len(rows)}"


def test_fills_evicted_from_the_ring_are_not_re_appended(rt):
    """The old bug: dedupe was ring-scoped, so an evicted fill looked new."""
    rt._merge_ibkr_state({"mode": "refresh", "fills_today": [_fill(i) for i in range(400)],
                          "n_fills_today": 400})
    assert len(rt.fills_ledger_path.read_text().splitlines()) == 400
    # fill 0 is long gone from the 300-row ring — re-report it
    rt._merge_ibkr_state({"mode": "refresh", "fills_today": [_fill(0)],
                          "n_fills_today": 1})
    assert len(rt.fills_ledger_path.read_text().splitlines()) == 400


def test_seen_set_survives_a_restart_via_the_ledger(rt, tmp_path):
    """A restarted process must not re-append what the ledger already holds."""
    rt._merge_ibkr_state({"mode": "refresh", "fills_today": [_fill(i) for i in range(50)],
                          "n_fills_today": 50})
    fresh = LiveRuntime(state_path=str(tmp_path / "state.json"))   # cold cache
    fresh.state.setdefault("ibkr", {})
    fresh._merge_ibkr_state({"mode": "refresh",
                             "fills_today": [_fill(i) for i in range(50)],
                             "n_fills_today": 50})
    assert len(rt.fills_ledger_path.read_text().splitlines()) == 50


def test_broker_reports_the_whole_day_uncapped():
    """Guard the source-side cap: no slice may be reintroduced on fills_today."""
    import inspect

    from core.broker import ibkr_broker
    src = inspect.getsource(ibkr_broker)
    assert '"fills_today": fills,' in src
    assert "fills_today\": fills[-" not in src, "a per-read cap is back"


def test_malformed_ledger_lines_do_not_break_seeding(rt):
    rt.fills_ledger_path.write_text('{"id": "exec00001"}\nnot json\n\n')
    rt._merge_ibkr_state({"mode": "refresh",
                          "fills_today": [_fill(1), _fill(2)],
                          "n_fills_today": 2})
    ids = [json.loads(l)["id"] for l in rt.fills_ledger_path.read_text().splitlines()
           if l.strip() and l.strip() != "not json"]
    assert ids.count("exec00001") == 1 and "exec00002" in ids
