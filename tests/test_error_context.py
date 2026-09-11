"""E59 — an ERROR verdict must carry its stored cause to the deciders.

Both director-facing views (`_context()`'s RECENT RESULTS block and
`research_liveness()`, which is embedded in BOTH the director's and the
engineer lane's context) used to slim the ledger's `error` field away, so an
errored experiment rendered identically to an empty one — N0026 sat
verdict=ERROR/metrics=null for three nights while its exception string was on
disk in the same file.

Fully hermetic: every test monkeypatches `research.director.RESULTS` at a
tmp_path ledger, so the live research/RESULTS.jsonl is never touched. No
network. `_context()` also reads RESEARCH_LOG.md, CORRECTIONS.md, the registry
and the hypotheses queue read-only, so these tests assert ONLY on the RECENT
RESULTS section and on the liveness dict.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research import director

# rows shaped exactly like the real ledger (harness.record())
ERR = {"id": "N0026", "ts": "2026-08-01T04:35:00+00:00", "phase": "discovery",
       "spec": {"name": "H7 — post-headline decay"}, "family": "news-latency",
       "verdict": "ERROR", "metrics": None,
       "error": "RuntimeError: SPY missing"}
OK = {"id": "N0027", "ts": "2026-08-02T04:35:00+00:00", "phase": "discovery",
      "spec": {"name": "true A"}, "family": "ic-adaptive",
      "verdict": "FAIL", "metrics": {"sharpe": -0.46}}


def _ledger(tmp_path, monkeypatch, rows):
    """Write a throwaway RESULTS.jsonl and point the module at it."""
    path = tmp_path / "RESULTS.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(director, "RESULTS", path)
    return path


def _recent_results(ctx: str) -> list:
    """Parse the RECENT RESULTS JSON out of a full director context string."""
    head = "=== RECENT RESULTS ===\n"
    i = ctx.rindex(head) + len(head)
    j = ctx.find("\n\n=== ", i)
    return json.loads(ctx[i:] if j == -1 else ctx[i:j])


def test_context_recent_results_carries_the_error(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [ERR, OK])
    ctx = director._context()
    assert "RuntimeError: SPY missing" in ctx
    rows = {r["id"]: r for r in _recent_results(ctx)}
    assert rows["N0026"]["error"] == "RuntimeError: SPY missing"
    assert rows["N0026"]["verdict"] == "ERROR"
    # a healthy row must serialise exactly as before — no null 'error' key
    assert "error" not in rows["N0027"]


def test_liveness_recent_errors_when_last_row_is_healthy(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [ERR, OK])
    out = director.research_liveness()
    assert "error" not in out["last_experiment"]
    assert out["recent_errors"] == [{"id": "N0026",
                                     "name": "H7 — post-headline decay",
                                     "error": "RuntimeError: SPY missing"}]


def test_liveness_last_experiment_error(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [OK, ERR])
    out = director.research_liveness()
    assert out["last_experiment"]["error"] == "RuntimeError: SPY missing"
    assert out["last_experiment"]["id"] == "N0026"
    assert [e["id"] for e in out["recent_errors"]] == ["N0026"]


def test_bounds_error_strings_truncate_at_300(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [dict(ERR, error="X" * 5000)])
    slim = _recent_results(director._context())
    assert len(slim[0]["error"]) == 300
    out = director.research_liveness()
    assert len(out["recent_errors"][0]["error"]) == 300
    assert len(out["last_experiment"]["error"]) == 300


def test_bounds_recent_errors_keeps_last_three_in_ledger_order(tmp_path,
                                                               monkeypatch):
    rows = [dict(ERR, id=f"N003{i}", error=f"RuntimeError: boom {i}")
            for i in range(1, 6)]
    _ledger(tmp_path, monkeypatch, rows)
    out = director.research_liveness()
    assert [e["id"] for e in out["recent_errors"]] == ["N0033", "N0034", "N0035"]
    assert [e["error"] for e in out["recent_errors"]] == [
        "RuntimeError: boom 3", "RuntimeError: boom 4", "RuntimeError: boom 5"]


def test_bounds_recent_errors_scans_only_the_last_15_rows(tmp_path, monkeypatch):
    rows = [ERR] + [dict(OK, id=f"N01{i:02d}") for i in range(20)]
    _ledger(tmp_path, monkeypatch, rows)
    assert director.research_liveness()["recent_errors"] == []


def test_healthy_ledger_reports_no_errors(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [OK])
    out = director.research_liveness()
    assert out["recent_errors"] == []
    assert "error" not in out["last_experiment"]
    assert all("error" not in r for r in _recent_results(director._context()))
