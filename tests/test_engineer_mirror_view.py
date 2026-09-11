"""E38: the mirror reconciliation section of the nightly engineer context.

Offline and deterministic — every case writes its own ledger/state into a tmp
dir; nothing is created under the real repo. What these tests pin: the section
is parsed as JSON (not just grepped), it is COMPACT (<=7 sessions, per-symbol
detail only on flagged days), and it can NEVER take a nightly session down —
a missing, corrupt or empty ledger degrades to "".
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research import engineer as eng

# Split literal on purpose: build_context reads tests/ into its SOURCE TREE, so
# the contiguous header must appear only in the RENDERED section — otherwise
# the "marker absent" branch below would match this file's own source.
HEADER = ("=== MIRROR RECONCILIATION (per session, from "
          "fills_history.jsonl — E37 instrument) ===")
RENDERED = HEADER + "\n{"       # header line followed by the json body


def _fill(i, symbol, side, shares, price, day="2026-07-27"):
    return {"id": f"exec{i}", "symbol": symbol, "side": side,
            "shares": shares, "price": price,
            "time": f"{day} 14:30:{i % 60:02d}+00:00", "commission": 0.35}


def _write_ledger(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _write_state(path, nav=979000.0, last_refresh="2026-07-27 20:00:00+00:00",
                 n_fills_today=7):
    path.write_text(json.dumps({"ibkr": {"nav": nav,
                                         "last_refresh": last_refresh,
                                         "n_fills_today": n_fills_today}}))


def _parse(section):
    """Header line off, JSON body parsed — assert on the dict, not substrings."""
    assert section.startswith(HEADER), section[:160]
    return json.loads(section.split("\n", 1)[1])


def test_missing_ledger_returns_empty(tmp_path):
    assert eng._mirror_view(ledger_path=tmp_path / "nope.jsonl",
                            state_path=tmp_path / "state.json") == ""


def test_churn_day_is_rendered(tmp_path):
    # E37 shape: the same exposure bought and sold back within one session
    rows = [
        _fill(1, "GLW", "BOT", 2924, 50.0),
        _fill(2, "GLW", "SLD", 1005, 50.0),
        _fill(3, "GLW", "SLD", 1951, 50.0),
        _fill(4, "AAPL", "BOT", 5000, 100.0),
        _fill(5, "AAPL", "SLD", 4990, 100.0),
        _fill(6, "MSFT", "BOT", 4000, 100.0),
        _fill(7, "MSFT", "SLD", 3990, 100.0),
    ]
    led, st = tmp_path / "fills.jsonl", tmp_path / "state.json"
    _write_ledger(led, rows)
    _write_state(st)

    sec = eng._mirror_view(ledger_path=led, state_path=st)
    assert HEADER in sec and "CHURN" in sec and "2026-07-27" in sec
    assert len(sec) < 4_000            # compact enough for the context budget

    data = _parse(sec)
    assert data["n_fills_ledger"] == 7
    assert data["newest_day"] == "2026-07-27"
    assert [s["date"] for s in data["sessions"]] == ["2026-07-27"]

    day = data["sessions"][0]
    assert day["n_execs"] == 7
    assert day["gross_usd"] == 2_092_000
    assert day["net_usd"] == 3_600
    assert day["rt_frac"] > 0.9
    assert day["flags"] == ["CHURN"]
    assert day["gross_over_nav"] == round(2_092_000 / 979_000.0, 2)

    # per-symbol detail only on a flagged day, worst wasted turnover first,
    # capped at 3 rows and rounded to whole dollars
    assert "top_churn" in day
    assert len(day["top_churn"]) == 3
    assert day["top_churn"][0][:3] == ["AAPL", 999_000, 1_000]
    assert [r[0] for r in day["top_churn"]] == ["AAPL", "MSFT", "GLW"]

    # E36 sink-vs-source check: same session date -> comparable, no gap
    cap = data["capture_check_newest_day"]
    assert cap["comparable"] is True
    assert cap["ledger"] == 7 and cap["broker"] == 7 and cap["gap"] is False


def test_clean_day_has_no_top_churn(tmp_path):
    rows = [_fill(1, "KO", "BOT", 100, 20.0, day="2026-07-28"),
            _fill(2, "PEP", "BOT", 50, 30.0, day="2026-07-28"),
            _fill(3, "XOM", "SLD", 40, 25.0, day="2026-07-28")]
    led, st = tmp_path / "fills.jsonl", tmp_path / "state.json"
    _write_ledger(led, rows)
    _write_state(st, last_refresh="2026-07-28 20:00:00+00:00", n_fills_today=3)

    sec = eng._mirror_view(ledger_path=led, state_path=st)
    assert sec and "CHURN" not in sec

    day = _parse(sec)["sessions"][0]
    assert day["date"] == "2026-07-28"
    assert day["gross_usd"] == 4_500 and day["net_usd"] == 4_500
    assert day["rt_frac"] == 0.0
    assert day["flags"] == []
    assert "top_churn" not in day


def test_sessions_capped_at_seven(tmp_path):
    days = [f"2026-07-{d:02d}" for d in range(15, 24)]      # 9 sessions
    rows = [_fill(i, "AAPL", "BOT", 10, 100.0, day=d)
            for i, d in enumerate(days, start=1)]
    led, st = tmp_path / "fills.jsonl", tmp_path / "state.json"
    _write_ledger(led, rows)
    _write_state(st, last_refresh="2026-07-23 20:00:00+00:00", n_fills_today=1)

    data = _parse(eng._mirror_view(ledger_path=led, state_path=st))
    dates = [s["date"] for s in data["sessions"]]
    assert len(dates) == 7
    assert dates == sorted(dates, reverse=True)             # newest first
    assert dates[0] == "2026-07-23"
    assert data["n_fills_ledger"] == 9                      # all days counted


def test_corrupt_ledger_never_raises(tmp_path):
    # (a) the ledger path is a DIRECTORY: open() raises, the helper swallows it
    as_dir = tmp_path / "fills.jsonl"
    as_dir.mkdir()
    assert eng._mirror_view(ledger_path=as_dir,
                            state_path=tmp_path / "state.json") == ""

    # (b) nothing but garbage lines: load_ledger yields no usable fills
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json at all\n{{{\n\n[1,2,3]\n")
    assert eng._mirror_view(ledger_path=garbage,
                            state_path=tmp_path / "state.json") == ""


def test_build_context_still_fits():
    ctx = eng.build_context()
    assert len(ctx) <= eng._MAX_CTX_CHARS
    # wiring, not content: whenever the helper renders a section on the real
    # repo the context must carry it (an absent/empty ledger renders nothing)
    section = eng._mirror_view()
    if (eng.ROOT / "data" / "fills_history.jsonl").exists() and section:
        assert RENDERED in ctx
    else:
        assert RENDERED not in ctx


def test_helper_is_read_only(tmp_path):
    led, st = tmp_path / "fills.jsonl", tmp_path / "state.json"
    _write_ledger(led, [_fill(1, "GLW", "BOT", 2924, 50.0),
                        _fill(2, "GLW", "SLD", 2900, 50.0)])
    _write_state(st, n_fills_today=2)

    assert eng._mirror_view(ledger_path=led, state_path=st)
    # the --json summary write path belongs to the CLI only
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fills.jsonl",
                                                          "state.json"]
