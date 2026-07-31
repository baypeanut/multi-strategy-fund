"""Forward-OOS tracker: the confirmed family's live record vs the lockbox
expectation it was confirmed on (director wish, twice repeated).

What these tests pin: the arithmetic is hand-checkable, only CONFIRMED
families are reported, the ledger is preferred over state.json with a working
fallback, and missing/corrupt data is treated as DATA rather than as an error
- that last one is what lets the section render in the proposal sandbox, which
strips data/ entirely.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research import director

# the shape of N0021 - the row forward_oos must pull the expectation from,
# rather than carrying hardcoded numbers of its own
CONFIRMED_ROW = {
    "id": "N0021", "ts": "2026-07-25T00:00:00+00:00",
    "spec": {"name": "8k drift confirmation"}, "family": "8k-drift",
    "phase": "confirmation", "verdict": "CONFIRMED",
    "metrics": {"sharpe": 1.467, "ann_return": 0.1263, "max_dd": -0.0627,
                "n_days": 252},
}


def _write_lines(path: Path, rows: list) -> Path:
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n"
                            for r in rows))
    return path


def _only_8k_confirmed():
    return lambda: {"families": {"8k-drift": {"status": "confirmed",
                                              "trials": 5}}}


def test_happy_path_is_hand_computable(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "RESULTS",
                        _write_lines(tmp_path / "RESULTS.jsonl", [CONFIRMED_ROW]))
    monkeypatch.setattr(director, "load_registry", _only_8k_confirmed())
    ledger = _write_lines(tmp_path / "equity_daily.jsonl", [
        {"date": "2026-07-27", "series": "s5", "value": 100.0},
        {"date": "2026-07-28", "series": "s5", "value": 101.0},
        {"date": "2026-07-29", "series": "s5", "value": 102.01},
        {"date": "2026-07-29", "series": "s1", "value": 999.0},   # other book
    ])

    out = director.forward_oos(state_path=tmp_path / "no_state.json",
                               equity_ledger_path=ledger)

    assert out["diagnostic_only"] is True
    assert "no decision rule reads this" in out["note"].lower()
    (e,) = out["families"]
    assert e["family"] == "8k-drift" and e["book"] == "s5"
    assert e["source"] == "equity_daily.jsonl"
    assert e["n_days"] == 2                      # 3 closes -> 2 daily returns
    assert e["first_date"] == "2026-07-27"
    assert e["last_date"] == "2026-07-29"
    assert e["fwd"]["total_ret_pct"] == pytest.approx(2.01)
    assert e["fwd"]["ann_return"] == pytest.approx(2.52, rel=1e-6)  # 1%/day
    assert e["fwd"]["sharpe"] is None            # 2 < 8 forward days
    assert e["fwd"]["max_dd"] == pytest.approx(0.0, abs=1e-9)  # monotone up
    assert e["lockbox"] == {"sharpe": 1.467, "ann_return": 0.1263,
                            "max_dd": -0.0627, "n_days": 252}


def test_sharpe_appears_once_there_are_eight_forward_days(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "RESULTS",
                        _write_lines(tmp_path / "RESULTS.jsonl", [CONFIRMED_ROW]))
    monkeypatch.setattr(director, "load_registry", _only_8k_confirmed())
    closes = [100.0, 101.0, 100.5, 102.0, 101.5, 103.0, 102.5, 104.0, 105.0]
    ledger = _write_lines(tmp_path / "equity_daily.jsonl", [
        {"date": f"2026-08-{i + 1:02d}", "series": "s5", "value": v}
        for i, v in enumerate(closes)])

    (e,) = director.forward_oos(state_path=tmp_path / "no_state.json",
                                equity_ledger_path=ledger)["families"]

    assert e["n_days"] == 8
    assert e["fwd"]["sharpe"] is not None
    assert isinstance(e["fwd"]["sharpe"], float)
    assert e["fwd"]["ann_vol"] is not None
    # this curve really draws down (101.0 -> 100.5), so a drawdown measured on
    # the CLOSES cannot be zero
    assert e["fwd"]["max_dd"] != 0


def test_open_and_burned_families_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "RESULTS",
                        _write_lines(tmp_path / "RESULTS.jsonl", [CONFIRMED_ROW]))
    monkeypatch.setattr(director, "load_registry", lambda: {"families": {
        "price-factors": {"status": "open", "trials": 11},
        "overnight-gap": {"status": "burned", "trials": 2},
        "8k-drift": {"status": "confirmed", "trials": 5}}})

    out = director.forward_oos(state_path=tmp_path / "no_state.json",
                               equity_ledger_path=tmp_path / "no_ledger.jsonl")

    assert [e["family"] for e in out["families"]] == ["8k-drift"]


def test_no_confirmed_families_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "RESULTS", tmp_path / "missing.jsonl")
    monkeypatch.setattr(director, "load_registry", lambda: {"families": {
        "price-factors": {"status": "open", "trials": 11}}})

    out = director.forward_oos(state_path=tmp_path / "no_state.json",
                               equity_ledger_path=tmp_path / "no_ledger.jsonl")

    assert out == {"diagnostic_only": True, "families": [],
                   "note": "no confirmed families"}


def test_missing_ledger_falls_back_to_state_daily_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "RESULTS",
                        _write_lines(tmp_path / "RESULTS.jsonl", [CONFIRMED_ROW]))
    monkeypatch.setattr(director, "load_registry", _only_8k_confirmed())
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"equity_history": {
        "s5": [["2026-07-27T14:00:00+00:00", 100.0],
               ["2026-07-27T20:00:00+00:00", 100.5],   # last mark of the date
               ["2026-07-28T14:00:00+00:00", 101.0],
               ["2026-07-28T20:00:00+00:00", 101.5]],
        "s1": [["2026-07-28T20:00:00+00:00", 999.0]]}}))

    (e,) = director.forward_oos(
        state_path=state,
        equity_ledger_path=tmp_path / "no_ledger.jsonl")["families"]

    assert e["source"] == "state.json:equity_history"
    assert e["n_days"] == 1                      # two dates -> one return
    assert e["first_date"] == "2026-07-27"
    assert e["last_date"] == "2026-07-28"
    # 101.5/100.5 - 1, i.e. the 20:00 mark won both days (100.0 would give 1.5)
    assert e["fwd"]["total_ret_pct"] == pytest.approx(0.995, abs=1e-3)


def test_both_sources_missing_is_data_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "RESULTS", tmp_path / "missing.jsonl")
    monkeypatch.setattr(director, "load_registry", _only_8k_confirmed())

    out = director.forward_oos(state_path=tmp_path / "no_state.json",
                               equity_ledger_path=tmp_path / "no_ledger.jsonl")

    (e,) = out["families"]
    assert e["family"] == "8k-drift" and e["book"] == "s5"
    assert e["n_days"] == 0
    assert e["note"] == "no equity data yet"
    assert e["lockbox"] is None                  # no RESULTS file either
    assert "fwd" not in e


def test_corrupt_ledger_lines_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "RESULTS",
                        _write_lines(tmp_path / "RESULTS.jsonl", [CONFIRMED_ROW]))
    monkeypatch.setattr(director, "load_registry", _only_8k_confirmed())
    ledger = _write_lines(tmp_path / "equity_daily.jsonl", [
        "not json at all",
        json.dumps([1, 2, 3]),                                    # wrong shape
        {"date": "2026-07-27", "series": "s5", "value": "abc"},   # non-numeric
        {"date": "2026-07-27", "series": "s5", "value": None},
        {"series": "s5", "value": 100.0},                         # no date
        {"date": "2026-07-27", "series": "s5", "value": 100.0},   # the good one
        "",
        {"date": "2026-07-28", "series": "s5", "value": 103.0},
    ])

    (e,) = director.forward_oos(state_path=tmp_path / "no_state.json",
                                equity_ledger_path=ledger)["families"]

    assert e["n_days"] == 1
    assert e["first_date"] == "2026-07-27" and e["last_date"] == "2026-07-28"
    assert e["fwd"]["total_ret_pct"] == pytest.approx(3.0)


def test_confirmed_family_with_no_book_is_reported_not_dropped(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(director, "RESULTS", tmp_path / "missing.jsonl")
    monkeypatch.setattr(director, "load_registry", lambda: {"families": {
        "ic-weighting": {"status": "confirmed", "trials": 1}}})

    (e,) = director.forward_oos(
        state_path=tmp_path / "no_state.json",
        equity_ledger_path=tmp_path / "no_ledger.jsonl")["families"]

    assert e["family"] == "ic-weighting"
    assert e["book"] is None
    assert e["n_days"] == 0
    assert "FORWARD_BOOKS" in e["note"]


def test_forward_books_maps_the_confirmed_family_to_s5():
    assert director.FORWARD_BOOKS["8k-drift"] == "s5"


def test_the_director_context_carries_it():
    """The director's context had no book performance at all - this is the
    whole point of the wish. Renders on the real repo AND in the sandbox."""
    blob = director._context()
    assert "=== FORWARD OOS (confirmed families - diagnostic only) ===" in blob
    assert "FORWARD OOS === unavailable" not in blob


