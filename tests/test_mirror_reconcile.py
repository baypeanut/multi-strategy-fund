"""Tests for scripts/mirror_reconcile.py -- fully offline and deterministic.

Pins the two defect shapes the script exists to catch: E37 (97% round-trip
churn on 2026-07-27) and E36 (669 of 4019 broker executions captured).
"""

import importlib.util
import json
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "mirror_reconcile.py"
_spec = importlib.util.spec_from_file_location("mirror_reconcile", _SCRIPT)
mr = importlib.util.module_from_spec(_spec)
sys.modules["mirror_reconcile"] = mr
_spec.loader.exec_module(mr)


# --- helpers ---------------------------------------------------------------

def _fill(fid, symbol, side, shares, price,
          time="2026-07-27 14:30:01+00:00", commission=0.35):
    return {"id": fid, "symbol": symbol, "side": side, "shares": shares,
            "price": price, "time": time, "commission": commission}


def _write_lines(path, lines):
    text = "\n".join(json.dumps(l) if isinstance(l, (dict, list)) else l
                     for l in lines)
    path.write_text(text + "\n", encoding="utf-8")


def _e37_day(day="2026-07-27"):
    """The 2026-07-27 pathology shape: huge gross, tiny net repositioning."""
    t = day + " 14:30:01+00:00"
    spec = [
        ("GLW", "BOT", 2924, 50.0),
        ("GLW", "SLD", 1005, 50.0),
        ("GLW", "SLD", 1951, 50.0),
        ("T", "BOT", 5000, 25.0),
        ("T", "SLD", 4900, 25.0),
        ("F", "BOT", 20000, 12.0),
        ("F", "SLD", 19800, 12.0),
    ]
    return [_fill(f"e{i}", sym, side, sh, px, time=t)
            for i, (sym, side, sh, px) in enumerate(spec)]


# --- (a) the E37 shape -----------------------------------------------------

def test_e37_pathology_day_is_flagged_churn():
    day = mr.reconcile_day(_e37_day())
    # GLW 294k + T 247.5k + F 477.6k gross, only $6.5k of net repositioning
    assert day["gross_notional"] == pytest.approx(1_019_100.0)
    assert day["net_notional"] == pytest.approx(6_500.0)
    assert day["round_trip_frac"] > 0.9
    assert day["gross_notional"] > mr.CHURN_GROSS_FLOOR
    assert "CHURN" in day["flags"]


def test_e37_top_churn_ordered_by_wasted_turnover():
    day = mr.reconcile_day(_e37_day())
    assert [r[0] for r in day["top_churn"]] == ["F", "GLW", "T"]
    sym, gross, net, n_execs = day["top_churn"][0]
    assert sym == "F"
    assert gross == pytest.approx(477_600.0)
    assert net == pytest.approx(2_400.0)
    assert n_execs == 2


def test_top_churn_capped_at_ten_rows():
    fills = []
    for i in range(12):
        fills.append(_fill(f"b{i}", f"S{i}", "BOT", 1000, 10.0))
        fills.append(_fill(f"s{i}", f"S{i}", "SLD", 990, 10.0))
    day = mr.reconcile_day(fills)
    assert day["n_symbols"] == 12
    assert len(day["top_churn"]) == 10


# --- (b) a healthy day -----------------------------------------------------

def test_healthy_day_passes_clean():
    fills = [
        _fill("h1", "AAPL", "BOT", 10, 100.0),
        _fill("h2", "MSFT", "BOT", 5, 200.0),
        _fill("h3", "KO", "SLD", 20, 50.0),
    ]
    day = mr.reconcile_day(fills, nav=979_000.0)
    assert day["gross_notional"] == pytest.approx(3_000.0)
    assert day["net_notional"] == pytest.approx(3_000.0)
    assert day["round_trip_frac"] == pytest.approx(0.0)
    assert day["gross_over_nav"] < 1.0
    assert day["flags"] == []


def test_empty_day_does_not_divide_by_zero():
    day = mr.reconcile_day([])
    assert day["gross_notional"] == 0.0
    assert day["round_trip_frac"] == 0.0
    assert day["flags"] == []


# --- (c) hand-computable arithmetic ---------------------------------------

def test_gross_net_round_trip_arithmetic():
    fills = [
        _fill("a1", "XYZ", "BOT", 100, 10.0, commission=1.0),
        _fill("a2", "XYZ", "SLD", 40, 10.0, commission=None),
    ]
    day = mr.reconcile_day(fills)
    assert day["n_execs"] == 2
    assert day["gross_notional"] == pytest.approx(1400.0)
    assert day["net_notional"] == pytest.approx(600.0)
    assert day["round_trip_frac"] == pytest.approx(1 - 600 / 1400)
    assert day["commissions"] == pytest.approx(1.0)  # None commission ignored
    assert day["gross_over_nav"] is None


def test_buy_sell_side_aliases_are_signed_like_bot_sld():
    fills = [
        _fill("x1", "XYZ", "BUY", 100, 10.0),
        _fill("x2", "XYZ", "SELL", 40, 10.0),
    ]
    day = mr.reconcile_day(fills)
    assert day["gross_notional"] == pytest.approx(1400.0)
    assert day["net_notional"] == pytest.approx(600.0)


# --- (d) corrupt / duplicate tolerance ------------------------------------

def test_load_ledger_skips_corrupt_blank_and_duplicate_rows(tmp_path):
    ledger = tmp_path / "fills_history.jsonl"
    _write_lines(ledger, [
        _fill("k1", "AAPL", "BOT", 10, 100.0),
        "",
        "   ",
        "{not json",
        ["a", "list", "not", "a", "dict"],
        {"id": "k2", "symbol": "MSFT", "side": "BOT", "shares": 5},  # no price
        {"id": "k3", "symbol": "MSFT", "side": "BOT", "shares": None, "price": 1.0},
        {"symbol": "MSFT", "side": "BOT", "shares": 5, "price": 1.0},  # no id
        {"id": "k4", "symbol": "", "side": "BOT", "shares": 5, "price": 1.0},
        {"id": "k5", "symbol": "F", "side": "BOT", "shares": "junk", "price": 1.0},
        _fill("k1", "AAPL", "BOT", 999, 100.0),  # duplicate execId, later row
        _fill("k6", "KO", "SLD", 20, 50.0),
    ])
    rows = mr.load_ledger(ledger)
    assert [r["id"] for r in rows] == ["k1", "k6"]
    assert rows[0]["shares"] == 10  # first occurrence wins


def test_load_ledger_missing_file_returns_empty(tmp_path):
    assert mr.load_ledger(tmp_path / "nope.jsonl") == []


def test_load_state_tolerates_missing_and_corrupt(tmp_path):
    assert mr.load_state(tmp_path / "nope.json") == {}
    bad = tmp_path / "state.json"
    bad.write_text("{not json", encoding="utf-8")
    assert mr.load_state(bad) == {}


# --- (e) multi-day grouping ------------------------------------------------

def test_day_of_and_multi_day_grouping():
    assert mr.day_of({"time": "2026-07-27 14:30:01+00:00"}) == "2026-07-27"
    assert mr.day_of({}) == ""

    fills = [
        _fill("d1", "AAPL", "BOT", 10, 100.0, time="2026-07-24 15:00:00+00:00"),
        _fill("d2", "AAPL", "BOT", 10, 100.0, time="2026-07-27 15:00:00+00:00"),
        _fill("d3", "AAPL", "SLD", 4, 100.0, time="2026-07-27 15:05:00+00:00"),
        {"id": "d4", "symbol": "KO", "side": "BOT", "shares": 1, "price": 1.0},
    ]
    summary = mr.summarize(fills)
    assert set(summary["days"]) == {"2026-07-24", "2026-07-27", "unknown"}
    assert summary["days_desc"] == ["2026-07-27", "2026-07-24", "unknown"]
    assert summary["newest_day"] == "2026-07-27"
    assert summary["days"]["2026-07-27"]["n_execs"] == 2
    assert summary["days"]["2026-07-27"]["net_notional"] == pytest.approx(600.0)


# --- (f) sink-vs-source capture check (E36) --------------------------------

def _state(n_fills_today=10, last_refresh="2026-07-27 20:05:00+00:00", nav=979_000.0):
    ib = {"nav": nav, "last_refresh": last_refresh}
    if n_fills_today is not None:
        ib["n_fills_today"] = n_fills_today
    return {"ibkr": ib}


def test_capture_check_detects_gap():
    cap = mr.check_capture("2026-07-27", 3, _state(n_fills_today=10))
    assert cap == {"comparable": True, "ledger": 3, "broker": 10, "gap": True}


def test_capture_check_no_gap_when_counts_match():
    cap = mr.check_capture("2026-07-27", 10, _state(n_fills_today=10))
    assert cap["comparable"] is True
    assert cap["gap"] is False


def test_capture_check_not_comparable_on_missing_or_stale_state():
    assert mr.check_capture("2026-07-27", 3, None)["comparable"] is False
    assert mr.check_capture("2026-07-27", 3, {})["comparable"] is False
    assert mr.check_capture("2026-07-27", 3, {"ibkr": "nope"})["comparable"] is False
    assert mr.check_capture("2026-07-27", 3, _state(n_fills_today=None))["comparable"] is False
    stale = _state(last_refresh="2026-07-28 20:05:00+00:00")
    cap = mr.check_capture("2026-07-27", 3, stale)
    assert cap["comparable"] is False
    assert cap["gap"] is False
    assert cap["broker"] is None
    assert mr.check_capture(None, 0, _state())["comparable"] is False


def test_summarize_runs_capture_check_on_newest_day():
    fills = _e37_day()[:3]  # 3 ledger rows on 2026-07-27
    summary = mr.summarize(fills, _state(n_fills_today=10))
    assert summary["capture"]["ledger"] == 3
    assert summary["capture"]["broker"] == 10
    assert summary["capture"]["gap"] is True
    assert summary["nav"] == pytest.approx(979_000.0)


# --- (g) gross / NAV flag path --------------------------------------------

def test_gross_over_nav_flag_even_with_low_round_trip():
    fills = [_fill("n1", "SPY", "BOT", 60000, 50.0)]  # $3.0M one-way
    day = mr.reconcile_day(fills, nav=979_000.0)
    assert day["round_trip_frac"] == pytest.approx(0.0)
    assert day["gross_over_nav"] > mr.NAV_MULT_BAR
    assert "CHURN" in day["flags"]


def test_big_round_trip_below_gross_floor_is_not_flagged():
    fills = [
        _fill("s1", "XYZ", "BOT", 1000, 10.0),
        _fill("s2", "XYZ", "SLD", 990, 10.0),
    ]
    day = mr.reconcile_day(fills)  # 99.5% round-trip but only $19.9k gross
    assert day["round_trip_frac"] > mr.CHURN_FRAC_BAR
    assert day["gross_notional"] < mr.CHURN_GROSS_FLOOR
    assert day["flags"] == []


def test_named_thresholds_are_pinned():
    assert mr.CHURN_FRAC_BAR == 0.30
    assert mr.CHURN_GROSS_FLOOR == 1_000_000.0
    assert mr.NAV_MULT_BAR == 3.0


# --- main(): read-only posture --------------------------------------------

def test_main_missing_ledger_exits_zero(tmp_path, capsys):
    rc = mr.main(["--ledger", str(tmp_path / "nope.jsonl"),
                  "--state", str(tmp_path / "state.json")])
    assert rc == 0
    assert "no fills ledger" in capsys.readouterr().out


def test_main_prints_table_and_writes_nothing_without_json_flag(tmp_path, capsys):
    ledger = tmp_path / "fills_history.jsonl"
    _write_lines(ledger, _e37_day())
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state(n_fills_today=4019)), encoding="utf-8")
    before = state_path.read_text(encoding="utf-8")

    rc = mr.main(["--ledger", str(ledger), "--state", str(state_path), "--days", "5"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-07-27" in out
    assert "CHURN" in out
    assert "CAPTURE GAP" in out
    assert "top churn 2026-07-27" in out
    # read-only: no new files, state untouched
    assert not (tmp_path / mr.JSON_BASENAME).exists()
    assert state_path.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "fills_history.jsonl", "state.json"]


def test_main_json_flag_is_the_only_write_path(tmp_path):
    ledger = tmp_path / "fills_history.jsonl"
    _write_lines(ledger, _e37_day())
    rc = mr.main(["--ledger", str(ledger), "--state", str(tmp_path / "state.json"),
                  "--json"])
    assert rc == 0
    out_path = tmp_path / mr.JSON_BASENAME
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert "CHURN" in payload["days"]["2026-07-27"]["flags"]
    assert payload["newest_day"] == "2026-07-27"
    assert "generated_at" in payload


def test_main_handles_empty_ledger(tmp_path, capsys):
    ledger = tmp_path / "fills_history.jsonl"
    ledger.write_text("", encoding="utf-8")
    rc = mr.main(["--ledger", str(ledger), "--state", str(tmp_path / "state.json")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(no fills)" in out
    assert "no dated fills" in out


# --- armor: the script stays standalone and stdlib-only --------------------

def test_script_imports_nothing_from_runtime_or_broker_layer():
    forbidden = ("runtime", "core", "ib_insync", "ibapi", "requests",
                 "pandas", "numpy", "urllib", "socket")
    for line in _SCRIPT.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not (s.startswith("import ") or s.startswith("from ")):
            continue
        for mod in forbidden:
            assert mod not in s, f"forbidden import in mirror_reconcile.py: {s}"
