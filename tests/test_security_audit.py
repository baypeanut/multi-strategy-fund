"""The security audit agent must find what it claims to find, rotate so that
coverage is provable rather than assumed, and never write outside its report
directory."""
import json
import stat

import pytest

from research import security_audit as S


def test_credential_literals_are_caught():
    # Synthetic. These two literals are the bait the rule has to bite on, so
    # they have to be shaped like the real thing. Neither is, or ever was, a key.
    src = '''
API_KEY = "sk-ant-abcdefghijklmnop1234"
password = "hunter2000000000000000"
'''
    rules = {f["rule"] for f in S.scan_file(S.ROOT / "x.py", src)}
    assert "SEC002" in rules and "SEC001" in rules


def test_injection_and_execution_surfaces_are_caught():
    src = '''
import subprocess
subprocess.run(cmd, shell=True)
eval(user_input)
pickle.loads(blob)
'''
    rules = {f["rule"] for f in S.scan_file(S.ROOT / "x.py", src)}
    assert {"SEC003", "SEC004", "SEC005"} <= rules


def test_swallowed_exception_is_flagged():
    src = '''
try:
    verify_signature()
except Exception:
    pass
'''
    assert any(f["rule"] == "SEC007"
               for f in S.scan_file(S.ROOT / "x.py", src))


def test_clean_file_produces_nothing():
    src = "def add(a, b):\n    return a + b\n"
    assert S.scan_file(S.ROOT / "x.py", src) == []


def test_env_readable_by_others_is_critical(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SECRET=1\n")
    env.chmod(0o644)
    findings = S.check_permissions(tmp_path)
    assert findings and findings[0]["severity"] == "critical"

    env.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert S.check_permissions(tmp_path) == []


def test_gitignore_must_cover_secrets(tmp_path):
    (tmp_path / ".gitignore").write_text("venv/\n")
    rules = {f["rule"] for f in S.check_gitignore(tmp_path)}
    assert rules == {"SEC011"}

    (tmp_path / ".gitignore").write_text(".env\n**/config.ini\n")
    assert S.check_gitignore(tmp_path) == []


def test_rotation_reviews_the_least_recently_seen_first(tmp_path):
    files = [tmp_path / f"f{i}.py" for i in range(5)]
    state = {"reviewed": {
        "f0.py": "2026-07-01T00:00:00", "f1.py": "2026-07-30T00:00:00"}}
    import research.security_audit as mod
    old_root, mod.ROOT = mod.ROOT, tmp_path
    try:
        picked = [p.name for p in mod.next_slice(files, state, 3)]
    finally:
        mod.ROOT = old_root
    # never reviewed files come first, then oldest review
    assert picked[:2] == ["f2.py", "f3.py"] or set(picked[:3]) >= {"f2.py", "f3.py"}
    assert "f1.py" not in picked, "most recently reviewed must sort last"


def test_the_live_repo_is_clean():
    """The audit that matters: run every deterministic rule on this repo."""
    findings = []
    for f in S.source_files():
        try:
            findings += [dict(x, file=str(f.relative_to(S.ROOT)))
                         for x in S.scan_file(f, f.read_text())]
        except (OSError, UnicodeDecodeError):
            continue
    findings += S.check_gitignore()
    critical = [f for f in findings if f["severity"] == "critical"]
    assert not critical, f"critical security findings in the tree: {critical}"


def test_audit_writes_only_into_its_report_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "state.json")
    out = S.run_audit(slice_size=0, use_model=False)

    written = {p.relative_to(tmp_path).parts[0] for p in tmp_path.rglob("*")
               if p.is_file()}
    assert written <= {"reports", "state.json"}
    assert Pathish(out["report"]).exists()
    assert json.loads((tmp_path / "state.json").read_text())["passes"] == 1


def Pathish(p):
    from pathlib import Path
    return Path(p)


def test_audit_runs_without_a_key(tmp_path, monkeypatch):
    """No key means the deterministic half still runs. The agent degrades to
    its cheaper layer rather than skipping the pass."""
    monkeypatch.setattr(S, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(S, "get_key", lambda k: None)
    out = S.run_audit(slice_size=4, use_model=True)
    assert out["reviewed"] == [] and out["coverage"]["total_files"] > 0


def test_placeholder_findings_are_dropped():
    """The first live pass emitted `**critical** config.yaml (...): ...` with
    no content. An empty critical costs a reader real time and leads nowhere."""
    assert not S.is_substantive({"severity": "critical", "issue": "...",
                                 "why_it_matters": "..."})
    assert not S.is_substantive({"severity": "high", "issue": "bad",
                                 "why_it_matters": "it is very bad indeed ok"})
    assert S.is_substantive({
        "severity": "high",
        "issue": "the ADV fallback substitutes 1e15 and zeroes impact cost",
        "why_it_matters": "missing liquidity data silently produces the "
                          "cheapest possible estimate"})
