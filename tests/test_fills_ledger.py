"""Append-only realized-fills ledger (E23 calibration dataset): the broker
report must carry the full day's executions (the old [-25:] truncation lost
most of an open-auction burst permanently), the runtime must persist every
NEW fill to data/fills_history.jsonl while keeping the 300-row dashboard ring
unchanged, a write failure must never break the merge, and the calibration
script must read ring+ledger merged with execId dedupe. All offline."""
import json

import pytest

import scripts.cost_calibration as C
from core.broker.ibkr_broker import IBKRBroker
from runtime.live import LiveRuntime


@pytest.fixture
def rt(tmp_path, monkeypatch):
    import runtime.live as live_mod
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    return LiveRuntime(state_path=str(tmp_path / "state.json"))


def fill(i, sym="AAPL"):
    return {"id": f"e{i}", "symbol": sym, "side": "BOT", "shares": 10,
            "price": 100.0, "time": "2026-07-26 14:30:00+00:00",
            "commission": 0.5}


def _ledger_rows(rt):
    p = rt.fills_ledger_path
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_new_fills_appended_to_ledger_and_ring(rt):
    rt._merge_ibkr_state({"mode": "refresh",
                          "fills_today": [fill(1), fill(2)]})
    assert [r["id"] for r in _ledger_rows(rt)] == ["e1", "e2"]
    # dashboard ring untouched in behavior — same fills, same cap
    assert [f["id"] for f in rt.state["ibkr"]["fills"]] == ["e1", "e2"]


def test_ledger_dedupes_against_state_ring(rt):
    rt._merge_ibkr_state({"mode": "refresh", "fills_today": [fill(1)]})
    # reqExecutions replays the whole day each read: e1 arrives again, only
    # e2 is new — the ledger must not double-write e1
    rt._merge_ibkr_state({"mode": "refresh", "fills_today": [fill(1), fill(2)]})
    assert [r["id"] for r in _ledger_rows(rt)] == ["e1", "e2"]


def test_ledger_outlives_the_state_ring(rt):
    # push more fills than the ring holds: the ring caps at 300 (dashboard),
    # the ledger keeps everything — the whole point of the fix
    rt._merge_ibkr_state({"mode": "refresh",
                          "fills_today": [fill(i) for i in range(350)]})
    assert len(rt.state["ibkr"]["fills"]) == 300
    assert len(_ledger_rows(rt)) == 350


def test_write_failure_never_breaks_the_merge(rt):
    rt.fills_ledger_path.mkdir()          # open(..., 'a') -> IsADirectoryError
    rt._merge_ibkr_state({"mode": "refresh", "fills_today": [fill(1)]})
    # the operational path still completed: ring updated, no exception
    assert [f["id"] for f in rt.state["ibkr"]["fills"]] == ["e1"]


def test_no_fills_no_ledger_file(rt):
    rt._merge_ibkr_state({"mode": "refresh", "fills_today": []})
    assert not rt.fills_ledger_path.exists()


def test_broker_report_no_longer_truncates_to_25(monkeypatch):
    """Regression for the loss at the source: an open-auction burst fills
    100+ orders inside one refresh interval and reqExecutions only covers
    the current day — the report must carry them all (ring-sized cap),
    not the last 25."""
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: None)
    monkeypatch.setattr(IBKRBroker, "disconnect", lambda self: None)
    monkeypatch.setattr(IBKRBroker, "snapshot", lambda self: (1_000_000.0, {}))
    monkeypatch.setattr(IBKRBroker, "recent_fills",
                        lambda self: [fill(i) for i in range(120)])
    br = IBKRBroker({"account": "DU000000"})
    report = br.refresh({}, {})
    assert len(report["fills_today"]) == 120
    assert report["n_fills_today"] == 120


def test_calibration_merges_ledger_and_ring(tmp_path):
    led = tmp_path / "fills.jsonl"
    led.write_text(json.dumps(fill(1)) + "\n" + json.dumps(fill(2)) + "\n")
    # e2 overlaps both sources; e3 lives only in the ring — dedupe by execId
    state = {"ibkr": {"fills": [fill(2), fill(3)]}}
    fills = C.load_fills(state, ledger_path=led)
    rows = C.analyze_fills(fills, {}, lambda s, d: None)
    assert len(rows) == 3


def test_calibration_ledger_skips_corrupt_lines(tmp_path):
    led = tmp_path / "fills.jsonl"
    led.write_text(json.dumps(fill(1)) + "\nnot-json\n")
    fills = C.load_fills({}, ledger_path=led)
    assert len(fills) == 1 and fills[0]["id"] == "e1"


def test_calibration_missing_ledger_is_fine(tmp_path):
    state = {"ibkr": {"fills": [fill(1)]}}
    fills = C.load_fills(state, ledger_path=tmp_path / "nope.jsonl")
    assert len(fills) == 1
