"""E46: two nights of the lane's work were discarded by ENOSPC.

The sandbox copies the repo before running the suite. `_COPY_IGNORE_TOP`
excluded the `logs/` DIRECTORY, but stale runtime logs sat in the repo ROOT —
`paper_trader.2026-03-18_*.log` (163MB) and friends, 271MB in all — and every
sandbox copied the lot into a 1.9GB tmpfs. When it filled, pytest reported
dozens of unrelated ERRORs, the proposal was marked sandbox-red, and a good
change (the E40 session-gate test, twice) was left pending.

Two guards: logs never enter a sandbox copy, and the sandbox lives on disk
rather than in RAM.
"""
import os

import research.engineer as eng


def test_logs_are_never_copied_into_a_sandbox(tmp_path, monkeypatch):
    """The exact shape that filled the tmpfs: a big .log in the repo ROOT."""
    src = tmp_path / "repo"
    (src / "runtime").mkdir(parents=True)
    (src / "logs").mkdir()
    (src / "runtime" / "live.py").write_text("x = 1\n")
    (src / "paper_trader.log").write_text("A" * 5000)                  # root
    (src / "paper_trader.2026-03-18_00-25-10.log").write_text("B" * 5000)
    (src / "logs" / "nightly.log").write_text("C" * 5000)              # in logs/
    (src / "runtime" / "debug.log").write_text("D" * 5000)             # nested

    monkeypatch.setattr(eng, "ROOT", src)
    dst = tmp_path / "copy"
    eng._copy_repo(dst)

    copied = {p.name for p in dst.rglob("*") if p.is_file()}
    assert "live.py" in copied, "the sandbox must still get the source tree"
    assert not [n for n in copied if n.endswith(".log")], f"logs leaked: {copied}"


def test_the_repos_own_footprint_stays_small():
    """Regression on the real repo: a sandbox copy must not be hundreds of MB."""
    import shutil
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        eng._copy_repo(Path(d) / "repo")
        total = sum(os.path.getsize(os.path.join(r, f))
                    for r, _, fs in os.walk(d) for f in fs)
    mb = total / 1e6
    assert mb < 50, (
        f"sandbox copy is {mb:.0f} MB — it was 271 MB of dead logs when this "
        f"filled a 1.9 GB tmpfs and cost two nights of work")
    shutil  # noqa: B018  (imported for symmetry with _copy_repo's use)


def test_sandbox_prefers_a_disk_backed_tmp():
    root = eng._sandbox_tmp_root()
    assert root in (None, "/var/tmp")
    if root is not None:
        assert os.path.isdir(root) and os.access(root, os.W_OK)


def test_sandbox_tmp_falls_back_when_unusable(monkeypatch):
    """It must never be the thing that breaks the sandbox."""
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    assert eng._sandbox_tmp_root() is None
