"""Tests for the append-only daily-equity ledger (scripts/backup_state.py).

The fund's grading data (per-book equity curves, benchmark, common-universe
diagnostic indices) lives only inside state.json ring buffers capped at 3000
marks. update_equity_ledger() lifts every COMPLETED day's close into
data/equity_daily.jsonl before the cap evicts it. Everything here runs offline
under tmp_path with an injected clock - the real data/ tree is never touched.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.backup_state as B

NOW = datetime(2026, 7, 30, 3, 17, tzinfo=timezone.utc)
TODAY = "2026-07-30"


def _marks(date: str, vals: list[float]) -> list[list]:
    """Hourly marks for one UTC date, in chronological order."""
    return [[f"{date}T{10 + i:02d}:00:00+00:00", v] for i, v in enumerate(vals)]


def _base_state() -> dict:
    """Two books, a benchmark and one diagnostic index across three days."""
    return {
        "equity_history": {
            "s1": (_marks("2026-07-28", [100.0, 101.0, 102.0])
                   + _marks("2026-07-29", [103.0, 104.25])
                   + _marks(TODAY, [105.0, 106.0])),
            "s4": (_marks("2026-07-28", [200.0, 201.5])
                   + _marks("2026-07-29", [202.0, 203.0, 204.0])
                   + _marks(TODAY, [205.0])),
        },
        "benchmark": (_marks("2026-07-28", [400.0, 401.0])
                      + _marks("2026-07-29", [402.0])
                      + _marks(TODAY, [403.0])),
        "common_idx_history": {
            "s1": (_marks("2026-07-28", [1.0, 1.01])
                   + _marks("2026-07-29", [1.02])
                   + _marks(TODAY, [1.03])),
        },
    }


def _write_state(tmp_path: Path, state: dict) -> Path:
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state))
    return p


def _rows(text: str) -> list[dict]:
    """Parse the well-formed dict lines of a ledger, ignoring corrupt ones."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict) and "series" in row:
            out.append(row)
    return out


def test_happy_path_extracts_daily_closes(tmp_path):
    sp = _write_state(tmp_path, _base_state())
    lp = tmp_path / "data" / "equity_daily.jsonl"

    n = B.update_equity_ledger(sp, lp, now=NOW)

    rows = _rows(lp.read_text())
    assert n == len(rows) == 8                      # 2 completed days x 4 series
    assert {r["date"] for r in rows} == {"2026-07-28", "2026-07-29"}
    assert {r["series"] for r in rows} == {"s1", "s4", "bench", "common_s1"}
    vals = {(r["date"], r["series"]): r["value"] for r in rows}
    assert vals[("2026-07-28", "s1")] == 102.0      # last mark of the date wins
    assert vals[("2026-07-29", "s1")] == 104.25
    assert vals[("2026-07-28", "s4")] == 201.5
    assert vals[("2026-07-29", "s4")] == 204.0
    assert vals[("2026-07-28", "bench")] == 401.0
    assert vals[("2026-07-29", "bench")] == 402.0
    assert vals[("2026-07-28", "common_s1")] == 1.01
    assert vals[("2026-07-29", "common_s1")] == 1.02
    pairs = [(r["date"], r["series"]) for r in rows]
    assert pairs == sorted(pairs)                   # deterministic ordering


def test_today_is_never_written(tmp_path):
    sp = _write_state(tmp_path, _base_state())
    lp = tmp_path / "data" / "equity_daily.jsonl"

    B.update_equity_ledger(sp, lp, now=NOW)

    assert all(r["date"] != TODAY for r in _rows(lp.read_text()))
    assert all(r["date"] < TODAY for r in _rows(lp.read_text()))


def test_second_call_is_idempotent(tmp_path):
    sp = _write_state(tmp_path, _base_state())
    lp = tmp_path / "data" / "equity_daily.jsonl"

    assert B.update_equity_ledger(sp, lp, now=NOW) == 8
    before = lp.read_bytes()
    assert B.update_equity_ledger(sp, lp, now=NOW) == 0
    assert lp.read_bytes() == before                # not one byte rewritten


def test_incremental_and_append_only(tmp_path):
    st = _base_state()
    sp = _write_state(tmp_path, st)
    lp = tmp_path / "data" / "equity_daily.jsonl"
    assert B.update_equity_ledger(sp, lp, now=NOW) == 8
    old = lp.read_bytes()

    # One more completed day: 07-30 closes, 07-31 starts accruing.
    for book in ("s1", "s4"):
        st["equity_history"][book] = st["equity_history"][book] + _marks(
            "2026-07-31", [999.0])
    st["benchmark"] = st["benchmark"] + _marks("2026-07-31", [888.0])
    st["common_idx_history"]["s1"] = st["common_idx_history"]["s1"] + _marks(
        "2026-07-31", [7.0])
    sp.write_text(json.dumps(st))

    n = B.update_equity_ledger(sp, lp, now=NOW.replace(day=31))

    new = lp.read_bytes()
    assert new.startswith(old)                      # append-only, byte prefix
    added = _rows(new.decode())[len(_rows(old.decode())):]
    assert n == len(added) == 4
    assert {(r["date"], r["series"]) for r in added} == {
        ("2026-07-30", s) for s in ("s1", "s4", "bench", "common_s1")}
    vals = {r["series"]: r["value"] for r in added}
    assert vals["s1"] == 106.0 and vals["s4"] == 205.0
    assert all(r["date"] != "2026-07-31" for r in _rows(new.decode()))


def test_missing_state_returns_zero_and_writes_nothing(tmp_path):
    lp = tmp_path / "data" / "equity_daily.jsonl"
    assert B.update_equity_ledger(tmp_path / "nope.json", lp, now=NOW) == 0
    assert not lp.exists()
    assert not lp.parent.exists()


def test_corrupt_state_returns_zero_and_writes_nothing(tmp_path):
    sp = tmp_path / "state.json"
    sp.write_text("{not json")
    lp = tmp_path / "data" / "equity_daily.jsonl"
    assert B.update_equity_ledger(sp, lp, now=NOW) == 0
    assert not lp.exists()

    sp.write_text(json.dumps([1, 2, 3]))            # right JSON, wrong shape
    assert B.update_equity_ledger(sp, lp, now=NOW) == 0
    assert not lp.exists()


def test_corrupt_ledger_lines_are_tolerated_and_left_in_place(tmp_path):
    sp = _write_state(tmp_path, _base_state())
    lp = tmp_path / "data" / "equity_daily.jsonl"
    lp.parent.mkdir(parents=True)
    pre = ("not json\n"
           "[1, 2]\n"
           '{"date": "2026-07-28"}\n'
           "\n"
           '{"date": "2026-07-28", "series": "s1", "value": 102.0}\n')
    lp.write_text(pre)
    old = lp.read_bytes()

    n = B.update_equity_ledger(sp, lp, now=NOW)

    new = lp.read_bytes()
    assert new.startswith(old)                      # corrupt lines untouched
    assert n == 7                                   # the one valid pair deduped
    added = _rows(new.decode())[len(_rows(old.decode())):]
    assert ("2026-07-28", "s1") not in {(r["date"], r["series"]) for r in added}
    assert len(added) == 7


def test_ledger_is_in_the_backup_set():
    assert "data/equity_daily.jsonl" in B.LEDGERS


def test_malformed_history_entries_are_skipped(tmp_path):
    st = {"equity_history": {"s1": [
        None,
        ["bad"],
        {"x": 1},
        ["2026-07-28T09:00:00+00:00", "NaNstr"],
        ["2026-07-28T11:00:00+00:00", 99.5],
        ["2026-07-28T12:00:00+00:00", True],        # bool is not a mark
    ]}}
    sp = _write_state(tmp_path, st)
    lp = tmp_path / "data" / "equity_daily.jsonl"

    n = B.update_equity_ledger(sp, lp, now=NOW)

    rows = _rows(lp.read_text())
    assert n == 1 and len(rows) == 1
    assert rows[0] == {"date": "2026-07-28", "series": "s1", "value": 99.5}


def test_main_updates_ledger_before_archiving(tmp_path, monkeypatch, capsys):
    calls: list[str] = []
    fake_archive = tmp_path / "fund_ledgers_20260730T031700Z.tar.gz"
    fake_archive.write_bytes(b"x" * 2048)

    def _ledger(*a, **k):
        calls.append("ledger")
        return 3

    def _archive(*a, **k):
        calls.append("archive")
        return fake_archive

    def _prune(*a, **k):
        calls.append("prune")
        return 1

    def _push(*a, **k):
        calls.append("push")
        return True

    monkeypatch.setattr(B, "update_equity_ledger", _ledger)
    monkeypatch.setattr(B, "make_archive", _archive)
    monkeypatch.setattr(B, "prune", _prune)
    monkeypatch.setattr(B, "push_offbox", _push)

    B.main()

    assert calls.index("ledger") < calls.index("archive")
    assert calls == ["ledger", "archive", "prune", "push"]
    out = capsys.readouterr().out
    assert "equity ledger +3 rows" in out
    assert "pruned 1" in out and "off-box=ok" in out
