"""E48b: the apply gate must never leave the live tree half-changed.

Two ways out of `apply_proposal` skipped the rollback entirely:

  - an IOError partway through the write loop. The sandbox filled a tmpfs one
    day earlier (E46) and this loop writes into the LIVE tree, not a sandbox.
  - `subprocess.TimeoutExpired` from the pytest gate. The suite runs 65s
    normally but E31 recorded 518s against a hanging Gateway, so the 900s wall
    is reachable. That path was worse: the tree ends up fully written, never
    verified, never rolled back, while the proposal still reads "pending".

Plus the dormancy classifier, which decided whether an applied change actually
reaches the running process.
"""
import shutil

import pytest

import research.engineer as eng


# --- which changes are dormant until the process reloads ---------------------

@pytest.mark.parametrize("path", [
    "runtime/live.py",
    "core/risk/governor.py",
    "core/broker/costs.py",
    "dashboard/server.py",
    "systems/s1_quant/portfolio.py",   # vol scaling: was missed
    "systems/s3_llm/wrapper.py",       # the S3 risk wrapper: was missed
    "config/config.yaml",              # every risk limit: was missed
])
def test_changes_the_running_process_holds_need_a_restart(path):
    assert eng._needs_restart(path), (
        f"{path} is imported or read by the live process. Applying it without "
        f"a restart is the E33 dormant-fix gap, reported as success.")


@pytest.mark.parametrize("path", [
    "research/harness.py", "scripts/backup_state.py",
    "tests/test_quant.py", "backtest/engine.py",
])
def test_offline_changes_do_not_restart_the_book(path):
    assert not eng._needs_restart(path)


def test_an_unknown_new_package_defaults_to_restarting():
    """Deny-list on purpose: a needless restart beats a silent no-op."""
    assert eng._needs_restart("newpkg/thing.py")


# --- rollback under failure --------------------------------------------------

@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A pending proposal that edits one existing file and adds one new one."""
    root = tmp_path / "repo"
    (root / "runtime").mkdir(parents=True)
    (root / "runtime" / "live.py").write_text("ORIGINAL\n")

    pdir = tmp_path / "proposals" / "P9999-x"
    (pdir / "files" / "runtime").mkdir(parents=True)
    (pdir / "files" / "runtime" / "live.py").write_text("NEW\n")
    (pdir / "files" / "runtime" / "added.py").write_text("ADDED\n")

    rec = {"id": "P9999", "dir": "P9999-x", "status": "pending",
           "title": "t", "kind": "fix", "risk_class": "low",
           "files": [{"path": "runtime/live.py"}, {"path": "runtime/added.py"}]}

    monkeypatch.setattr(eng, "ROOT", root)
    monkeypatch.setattr(eng, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(eng, "list_proposals", lambda: [rec])
    monkeypatch.setattr(eng, "save_record", lambda r: None)
    monkeypatch.setattr(eng, "_autonomy", lambda: {"mode": "paper_flexible"})
    monkeypatch.setattr(eng, "_protected", lambda p: False)
    return root


def test_a_pytest_timeout_rolls_the_tree_back(staged, monkeypatch):
    """The worst path: fully written, never verified, never restored."""
    import subprocess

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=900)

    monkeypatch.setattr(eng.subprocess, "run", boom)
    out = eng.apply_proposal("P9999", force_armor=True, run_tests=True)

    assert out["ok"] is False and "rolled back" in out["error"]
    assert (staged / "runtime" / "live.py").read_text() == "ORIGINAL\n"
    assert not (staged / "runtime" / "added.py").exists(), (
        "a file the proposal created must not survive a rolled-back apply")


def test_an_io_error_midway_rolls_back_what_was_written(staged, monkeypatch):
    """Fails on the SECOND file, so the first is already overwritten."""
    real_copy = shutil.copy2
    seen = []

    def flaky(src, dst, *a, **kw):
        seen.append(str(dst))
        if str(dst).endswith("added.py"):
            raise OSError(28, "No space left on device")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(eng.shutil, "copy2", flaky)
    out = eng.apply_proposal("P9999", force_armor=True, run_tests=False)

    assert out["ok"] is False and "rolled back" in out["error"]
    assert (staged / "runtime" / "live.py").read_text() == "ORIGINAL\n", (
        "the first file was overwritten before the failure and must be restored")


def test_a_clean_apply_still_works(staged, monkeypatch):
    monkeypatch.setattr(eng, "_restart_service", lambda: {"ok": True})
    out = eng.apply_proposal("P9999", force_armor=True, run_tests=False)

    assert out["ok"] is True
    assert (staged / "runtime" / "live.py").read_text() == "NEW\n"
    assert (staged / "runtime" / "added.py").read_text() == "ADDED\n"
    assert out["needs_restart"] is True
