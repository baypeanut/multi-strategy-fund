"""E22 staff-engineer lane: the agent's only write surface is the proposal
store; path validation, backpressure, apply/rollback, and the armor tripwire
must all hold mechanically."""
import json
from pathlib import Path

import pytest

import research.engineer as E


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "PROPOSALS_DIR", tmp_path / "proposals")
    return tmp_path


def test_path_validation_blocks_escapes_and_secrets():
    assert E.validate_rel_path("../etc/passwd") is not None
    assert E.validate_rel_path("/etc/passwd") is not None
    assert E.validate_rel_path("core/../../x.py") is not None
    assert E.validate_rel_path(".env") is not None
    assert E.validate_rel_path("research/registry.json") is not None
    assert E.validate_rel_path("research/RESULTS.jsonl") is not None
    assert E.validate_rel_path("research/proposals/P0001/x.py") is not None
    assert E.validate_rel_path("core/broker/costs.py") is None       # allowed target
    assert E.validate_rel_path("tests/test_new.py") is None
    assert E.validate_rel_path("core/evil.so") is not None           # extension
    assert E.validate_rel_path("randomdir/x.py") is not None         # outside tree


def test_write_proposal_rejects_bad_python(store):
    rec, err = E.write_proposal({
        "title": "broken", "kind": "bugfix", "risk_class": "low",
        "rationale": "r", "test_note": "t",
        "files": [{"path": "core/x.py", "content": "def f(:\n  pass"}]})
    assert rec is None and "syntax" in err


def test_write_load_and_diff_roundtrip(store):
    rec, err = E.write_proposal({
        "title": "add helper", "kind": "feature", "risk_class": "low",
        "rationale": "why", "test_note": "how",
        "files": [{"path": "scripts/new_helper.py",
                   "content": "print('hello')\n"}]})
    assert err is None and rec["id"] == "P0001" and rec["status"] == "pending"
    pdir = E.PROPOSALS_DIR / rec["dir"]
    assert (pdir / "files/scripts/new_helper.py").read_text() == "print('hello')\n"
    assert "new_helper" in (pdir / "diff.patch").read_text()
    assert E.pending_count() == 1
    # second proposal gets the next id
    rec2, _ = E.write_proposal({
        "title": "two", "kind": "test", "risk_class": "low",
        "rationale": "r", "test_note": "t",
        "files": [{"path": "tests/test_two.py", "content": "x = 1\n"}]})
    assert rec2["id"] == "P0002"


def test_apply_backs_up_writes_and_reject_feeds_back(store, tmp_path, monkeypatch):
    # aim ROOT at a fake repo so apply never touches the real tree
    fake_root = tmp_path / "repo"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "scripts/target.py").write_text("old = 1\n")
    monkeypatch.setattr(E, "ROOT", fake_root)

    rec, err = E.write_proposal({
        "title": "update target", "kind": "bugfix", "risk_class": "low",
        "rationale": "r", "test_note": "t",
        "files": [{"path": "scripts/target.py", "content": "new = 2\n"}]})
    assert err is None

    out = E.apply_proposal(rec["id"], run_tests=False)
    assert out["ok"] and (fake_root / "scripts/target.py").read_text() == "new = 2\n"
    baks = list((fake_root / "scripts").glob("target.py.bak.*"))
    assert len(baks) == 1 and baks[0].read_text() == "old = 1\n"
    # applied proposals cannot be re-applied or rejected
    assert not E.apply_proposal(rec["id"], run_tests=False)["ok"]
    assert not E.reject_proposal(rec["id"], "late")["ok"]


def test_armor_tripwire_requires_force(store, tmp_path, monkeypatch):
    fake_root = tmp_path / "repo"
    (fake_root / "core/risk").mkdir(parents=True)
    (fake_root / "core/risk/governor.py").write_text("g = 1\n")
    monkeypatch.setattr(E, "ROOT", fake_root)
    monkeypatch.setattr(E, "_autonomy", lambda: {
        "mode": "paper_flexible",
        "protected_paths": ["core/risk/governor.py"]})

    rec, _ = E.write_proposal({
        "title": "touch governor", "kind": "feature", "risk_class": "critical",
        "rationale": "r", "test_note": "t",
        "files": [{"path": "core/risk/governor.py", "content": "g = 2\n"}]})
    out = E.apply_proposal(rec["id"], run_tests=False)
    assert not out["ok"] and "force-armor" in out["error"]
    assert (fake_root / "core/risk/governor.py").read_text() == "g = 1\n"
    out = E.apply_proposal(rec["id"], force_armor=True, run_tests=False)
    assert out["ok"]


def test_real_money_lock_gates_everything(store, tmp_path, monkeypatch):
    fake_root = tmp_path / "repo"
    (fake_root / "scripts").mkdir(parents=True)
    monkeypatch.setattr(E, "ROOT", fake_root)
    monkeypatch.setattr(E, "_autonomy", lambda: {"mode": "real_money_locked",
                                                 "protected_paths": []})
    rec, _ = E.write_proposal({
        "title": "innocent doc", "kind": "docs", "risk_class": "low",
        "rationale": "r", "test_note": "t",
        "files": [{"path": "scripts/note.md", "content": "hi\n"}]})
    out = E.apply_proposal(rec["id"], run_tests=False)
    assert not out["ok"] and "real_money_locked" in out["error"]
    # and the session generator refuses to run at all
    monkeypatch.setattr(E, "_autonomy", lambda: {
        "engineer_enabled": True, "mode": "real_money_locked"})
    assert "locked" in E.run_engineer()["skipped"]


def test_backpressure_skips_session(store, monkeypatch):
    monkeypatch.setattr(E, "_autonomy", lambda: {
        "engineer_enabled": True, "mode": "paper_flexible",
        "max_pending_proposals": 1})
    E.write_proposal({
        "title": "pending one", "kind": "test", "risk_class": "low",
        "rationale": "r", "test_note": "t",
        "files": [{"path": "tests/test_x.py", "content": "x = 1\n"}]})
    out = E.run_engineer()
    assert "backpressure" in out["skipped"]


def test_sandbox_copy_excludes_secrets_but_keeps_core_data(tmp_path, monkeypatch):
    """Regression (night one): a depth-blind 'data' exclusion also dropped the
    core/data/ PACKAGE, breaking every sandbox test collection. Top-level
    data/ (state) stays out, data/universe/ (frozen config) comes along."""
    fake_root = tmp_path / "repo"
    (fake_root / "core/data").mkdir(parents=True)
    (fake_root / "core/data/crypto.py").write_text("c = 1\n")
    (fake_root / ".env").write_text("ANTHROPIC_API_KEY=secret\n")
    (fake_root / "data/universe").mkdir(parents=True)
    (fake_root / "data/state.json").write_text("{}")
    (fake_root / "data/universe/equities.json").write_text("[]")
    (fake_root / "research/proposals/P0001").mkdir(parents=True)
    (fake_root / "research/harness.py").write_text("h = 1\n")
    (fake_root / "big.pkl").write_text("x")
    monkeypatch.setattr(E, "ROOT", fake_root)

    dst = tmp_path / "sbx"
    E._copy_repo(dst)
    assert (dst / "core/data/crypto.py").exists()          # the night-one bug
    assert (dst / "research/harness.py").exists()
    assert (dst / "data/universe/equities.json").exists()  # frozen universe
    assert not (dst / ".env").exists()
    assert not (dst / "data/state.json").exists()
    assert not (dst / "research/proposals").exists()
    assert not (dst / "big.pkl").exists()
