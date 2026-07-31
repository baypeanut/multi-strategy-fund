"""S3 decisions ledger (S3-v2 dataset): append-only capture of every PM
decision + the next bar's forward returns.

The ledger is strictly PASSIVE - these tests pin the row schema (decided AND
held paths), hash determinism, the fail-safe write, the forward-backfill
semantics, and that a full offline _run_systems rebalance lands the decision
in the file WITHOUT changing the book it returns.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import runtime.live
from runtime.live import LiveRuntime

NOW = datetime(2026, 7, 25, 20, 5, tzinfo=timezone.utc)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.live, "send_telegram", lambda *a, **k: None)
    return LiveRuntime(state_path=str(tmp_path / "state.json"))


def hist_df(n=300, price=100.0, seed=1):
    idx = pd.date_range("2025-06-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    close = price * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1e6}, index=idx)


def read_rows(rt) -> list[dict]:
    txt = rt.s3_ledger_path.read_text().strip()
    return [json.loads(line) for line in txt.split("\n") if line]


# --- (a) decided row schema -------------------------------------------------
def test_decision_row_schema(rt):
    briefing = {"regime": {"trend": "risk_on"}, "names": [{"symbol": "AAA"}]}
    rt._log_s3_decision(briefing, "heuristic", {"AAA": 0.03}, ["clip AAA"],
                        pd.Series({"AAA": 0.02}), "2026-07-25",
                        {"AAA": 100.0}, NOW)

    rows = read_rows(rt)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "decision"
    assert row["pm_source"] == "heuristic"
    assert row["bar_date"] == "2026-07-25"
    assert row["ts"] == NOW.isoformat()
    assert row["regime"] == {"trend": "risk_on"}
    assert row["briefing_names"] == [{"symbol": "AAA"}]
    assert row["proposal"] == {"AAA": 0.03}
    assert row["violations"] == ["clip AAA"]
    assert row["final_weights"] == {"AAA": 0.02}
    assert row["decision_prices"] == {"AAA": 100.0}
    assert isinstance(row["briefing_hash"], str) and len(row["briefing_hash"]) == 16

    pending = rt.state["s3_forward_pending"]
    assert pending["bar_date"] == "2026-07-25"
    assert pending["prices"] == {"AAA": 100.0}
    assert pending["ts"] == row["ts"]


def test_zero_weights_dropped_from_row(rt):
    rt._log_s3_decision({"regime": {}, "names": []}, "heuristic", {}, [],
                        pd.Series({"AAA": 0.02, "ZZZ": 0.0}), "2026-07-25",
                        {"AAA": 100.0}, NOW)
    assert read_rows(rt)[0]["final_weights"] == {"AAA": 0.02}


# --- (b) held row -----------------------------------------------------------
def test_held_row_has_null_proposal(rt):
    rt._log_s3_decision({"regime": {}, "names": []}, "held", None, [],
                        pd.Series(dtype=float), "2026-07-25",
                        {"AAA": 100.0}, NOW)
    row = read_rows(rt)[0]
    assert row["pm_source"] == "held"
    assert row["proposal"] is None          # json null, not a missing key
    assert "proposal" in row
    assert row["violations"] == []
    assert row["final_weights"] == {}
    assert rt.state["s3_forward_pending"]["bar_date"] == "2026-07-25"


# --- (c) briefing hash determinism -----------------------------------------
def test_briefing_hash_is_deterministic(rt):
    b1 = {"regime": {"trend": "risk_on"}, "names": [{"symbol": "AAA", "z": 1.0}]}
    b1_reordered = {"names": [{"symbol": "AAA", "z": 1.0}],
                    "regime": {"trend": "risk_on"}}
    b2 = {"regime": {"trend": "risk_off"}, "names": [{"symbol": "AAA", "z": 1.0}]}
    for b in (b1, b1_reordered, b2):
        rt._log_s3_decision(b, "heuristic", None, [], pd.Series(dtype=float),
                            "2026-07-25", {"AAA": 100.0}, NOW)
    hashes = [r["briefing_hash"] for r in read_rows(rt)]
    assert hashes[0] == hashes[1]           # same content, any key order
    assert hashes[2] != hashes[0]           # changed briefing -> new hash


# --- (d) fail-safe writes ---------------------------------------------------
def test_write_failure_never_raises(rt):
    rt.s3_ledger_path.mkdir(parents=True, exist_ok=True)   # a dir, not a file
    rt._log_s3_decision({"regime": {}, "names": []}, "heuristic", {"AAA": 0.01},
                        [], pd.Series({"AAA": 0.01}), "2026-07-25",
                        {"AAA": 100.0}, NOW)
    # the pending stamp is written OUTSIDE the try: a disk hiccup must not
    # desync the forward leg from the decision it measures
    assert rt.state["s3_forward_pending"]["bar_date"] == "2026-07-25"
    rt._backfill_s3_forward("2026-07-26", {"AAA": 101.0})   # also fail-safe
    assert "s3_forward_pending" not in rt.state


def test_exotic_proposal_value_does_not_raise(rt):
    rt._log_s3_decision({"regime": {}, "names": []}, "heuristic",
                        {"AAA": np.float32(0.03)}, [], pd.Series({"AAA": 0.02}),
                        "2026-07-25", {"AAA": 100.0}, NOW)
    row = read_rows(rt)[0]
    assert str(row["proposal"]["AAA"]).startswith("0.03")


# --- (e) forward backfill ---------------------------------------------------
def test_forward_backfill_semantics(rt):
    pending = {"ts": "T0", "bar_date": "2026-07-24",
               "prices": {"AAA": 100.0, "GONE": 50.0}}
    rt.state["s3_forward_pending"] = dict(pending)
    rt._backfill_s3_forward("2026-07-25", {"AAA": 101.0})

    rows = read_rows(rt)
    assert len(rows) == 1
    assert rows[0] == {"kind": "forward", "decision_ts": "T0",
                       "decision_bar": "2026-07-24", "forward_bar": "2026-07-25",
                       "returns": {"AAA": 0.01}}   # GONE has no mark -> skipped
    assert "s3_forward_pending" not in rt.state

    # same bar -> nothing appended, pending intact (fires only on a bar change)
    rt.state["s3_forward_pending"] = dict(pending)
    rt._backfill_s3_forward("2026-07-24", {"AAA": 101.0})
    assert len(read_rows(rt)) == 1
    assert rt.state["s3_forward_pending"]["ts"] == "T0"

    # no pending -> no-op
    rt.state.pop("s3_forward_pending")
    rt._backfill_s3_forward("2026-07-26", {"AAA": 101.0})
    assert len(read_rows(rt)) == 1


# --- (f) offline integration through _run_systems (heuristic PM) ------------
def test_run_systems_logs_decision_offline(rt, monkeypatch):
    monkeypatch.setattr(runtime.live, "has_key", lambda name: False)
    monkeypatch.setitem(runtime.live.CONFIG, "llm", {"enabled": False})

    eq = {"AAA": hist_df(), "BBB": hist_df(seed=2), "CCC": hist_df(seed=3),
          "SPY": hist_df(seed=4)}
    vix = pd.Series(np.full(300, 16.0),
                    index=pd.date_range("2025-06-01", periods=300))

    weights, regime, cov, s3_source = rt._run_systems(
        eq, {}, vix, datetime.now(timezone.utc))

    decisions = [r for r in read_rows(rt) if r["kind"] == "decision"]
    assert len(decisions) == 1
    row = decisions[0]
    assert s3_source == "heuristic"
    assert row["pm_source"] == "heuristic"
    assert row["bar_date"] == str(eq["SPY"].index.max().date())

    # the ledger is observational: the returned book is untouched
    w_s3 = weights["s3"]
    assert isinstance(w_s3, pd.Series)
    assert row["final_weights"] == {s: round(float(v), 6)
                                    for s, v in w_s3.items() if abs(v) > 1e-9}

    # decision prices cover the focus names the PM was briefed on
    assert row["briefing_names"]
    assert set(row["decision_prices"]) == {"AAA", "BBB", "CCC"}
    for sym, p in row["decision_prices"].items():
        assert p == round(float(eq[sym]["close"].iloc[-1]), 4)
    assert set(row["final_weights"]) <= set(row["decision_prices"])

    pending = rt.state["s3_forward_pending"]
    assert pending["bar_date"] == row["bar_date"]
    assert pending["prices"] == row["decision_prices"]
