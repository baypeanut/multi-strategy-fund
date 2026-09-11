"""E49: the record-corrections channel in the director's nightly context.

The graded ledger is mechanically unwritable forever, so a row whose LABEL is
wrong (N0025, a default static blend filed under the ic-adaptive name) can
only be corrected out-of-band. `_context()` is offline-safe — file reads only,
no API — so these run in the plain test environment.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research import director


def test_the_live_context_carries_the_correction():
    """The shipped CORRECTIONS.md reaches the director, in the right place."""
    ctx = director._context()
    assert "RECORD CORRECTIONS" in ctx
    assert "N0025" in ctx
    assert "N0025 is mislabeled" in ctx
    # renders after the RESEARCH_LOG tail and before RECENT RESULTS
    i = ctx.index("=== RECORD CORRECTIONS")
    if "=== RESEARCH_LOG (tail) ===" in ctx:
        assert ctx.index("=== RESEARCH_LOG (tail) ===") < i
    if "=== RECENT RESULTS ===" in ctx:
        assert i < ctx.index("=== RECENT RESULTS ===")


def test_absent_file_is_silence_not_failure(monkeypatch, tmp_path):
    """No corrections on record is a normal state, not a reportable fault."""
    monkeypatch.setattr(director, "CORRECTIONS", tmp_path / "nope.md")
    ctx = director._context()
    assert "RECORD CORRECTIONS" not in ctx


def test_unreadable_file_is_named_not_swallowed(monkeypatch, tmp_path):
    """E45 rule: a failed read is distinguishable from nothing to report."""
    bad = tmp_path / "corrections_dir"
    bad.mkdir()                       # read_text() raises IsADirectoryError
    monkeypatch.setattr(director, "CORRECTIONS", bad)
    ctx = director._context()
    assert "RECORD CORRECTIONS === unavailable" in ctx


def test_tail_is_bounded(monkeypatch, tmp_path):
    """Tail, not head: the newest correction is the one that must survive."""
    big = tmp_path / "CORRECTIONS.md"
    big.write_text("\n".join(f"line {i}" for i in range(400)) + "\n")
    monkeypatch.setattr(director, "CORRECTIONS", big)
    ctx = director._context()
    assert "line 399" in ctx
    assert "line 100" not in ctx
