"""Off-box ledger backup: archives the right files, never leaks secrets,
prunes by retention, and a broken remote alerts without raising."""
import tarfile

import scripts.backup_state as B


def test_archive_contains_ledgers_and_skips_missing(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / "research").mkdir()
    (root / "data/state.json").write_text("{}")
    (root / "research/registry.json").write_text("{}")
    a = B.make_archive(root=root, out_dir=tmp_path / "backups",
                       ledgers=["data/state.json", "research/registry.json",
                                "research/RESULTS.jsonl"])   # last one missing
    with tarfile.open(a) as tar:
        names = tar.getnames()
    assert "data/state.json" in names
    assert "research/registry.json" in names
    assert "research/RESULTS.jsonl" not in names


def test_env_never_enters_an_archive(tmp_path):
    # even a malicious/buggy ledger list cannot pull secrets into an archive
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("ANTHROPIC_API_KEY=secret")
    a = B.make_archive(root=root, out_dir=tmp_path / "b", ledgers=[".env"])
    with tarfile.open(a) as tar:
        assert tar.getnames() == []


def test_prune_keeps_the_newest(tmp_path):
    for i in range(5):
        (tmp_path / f"fund_ledgers_2026010{i}T000000Z.tar.gz").write_bytes(b"x")
    removed = B.prune(out_dir=tmp_path, retention=2)
    assert removed == 3
    left = sorted(p.name for p in tmp_path.glob("fund_ledgers_*.tar.gz"))
    assert left == ["fund_ledgers_20260103T000000Z.tar.gz",
                    "fund_ledgers_20260104T000000Z.tar.gz"]


def test_push_unconfigured_is_noop(tmp_path):
    a = tmp_path / "a.tar.gz"
    a.write_bytes(b"x")
    assert B.push_offbox(a, remote_cmd="") is False


def test_push_failure_alerts_but_never_raises(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(B, "send_telegram", lambda m, **k: alerts.append(m) or True)
    a = tmp_path / "a.tar.gz"
    a.write_bytes(b"x")
    ok = B.push_offbox(a, remote_cmd="definitely-not-a-real-binary {archive}")
    assert ok is False
    assert alerts and "FAILED" in alerts[0]


def test_push_success_returns_true(tmp_path):
    a = tmp_path / "a.tar.gz"
    a.write_bytes(b"x")
    assert B.push_offbox(a, remote_cmd="true {archive}") is True
