"""E42: an implementing engineer must never be handed a truncated file.

The engineers emit COMPLETE replacement files. `_gen_user` used to pass
`f.read_text()[:60_000]` with no marker, so for any larger file the engineer
was asked to rewrite something it had only partly seen and its emission would
delete or hallucinate the tail. Both of the platform's most-edited files were
over that cap when the lane found it (runtime/live.py 63,935 chars,
research/engineer.py 65,284) — and the tails hold the mirror gating, the run
loop, and `_run_pipeline` itself. A red sandbox would *probably* catch it;
a tail of comments or untested code would pass green, which is silent code
deletion — the E36 shape.

Truncation is therefore never acceptable in a brief. Too big is a REJECTED
SPEC with a loud reason.
"""
import research.engineer as eng

SPEC = {"title": "t", "kind": "ops", "risk_class": "low", "rationale": "r",
        "instructions": "i", "acceptance": "a"}


def _spec(*paths):
    return {**SPEC, "files_to_touch": list(paths)}


def test_a_large_real_file_is_handed_over_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    big = tmp_path / "runtime"
    big.mkdir()
    body = "".join(f"line {i}\n" for i in range(12000))       # ~111KB, over the raised 100k cap
    (big / "live.py").write_text(body)
    assert len(body) > eng._MAX_FILE_CHARS, "fixture must exceed the old cap"

    user, err = eng._gen_user(_spec("runtime/live.py"))

    assert err is None
    assert body in user, "the engineer was handed a truncated file"
    assert "TRUNCATED" not in user and "truncated" not in user


def test_the_real_repos_hot_files_fit(tmp_path):
    """Regression guard against the exact condition that triggered E42: the
    two files the lane edits most must be handable whole."""
    for rel in ("runtime/live.py", "research/engineer.py"):
        user, err = eng._gen_user(_spec(rel))
        assert err is None, f"{rel} can no longer be implemented: {err}"
        assert (eng.ROOT / rel).read_text() in user


def test_over_budget_spec_is_rejected_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    monkeypatch.setattr(eng, "_GEN_USER_BUDGET", 5_000)
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "a.py").write_text("x\n" * 4_000)

    user, err = eng._gen_user(_spec("scripts/a.py"))

    assert user is None and err is not None
    assert "too large" in err and "scripts/a.py" in err
    assert "Split the spec" in err


def test_new_files_are_marked_not_faked(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    user, err = eng._gen_user(_spec("tests/brand_new.py"))
    assert err is None and "NEW FILE" in user


def test_unreadable_file_rejects_the_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    d = tmp_path / "scripts"
    d.mkdir()
    p = d / "bin.py"
    p.write_bytes(b"\xff\xfe\x00binary")

    user, err = eng._gen_user(_spec("scripts/bin.py"))
    assert user is None and "unreadable" in err


def test_context_truncation_marker_reports_chars_cut(tmp_path, monkeypatch):
    """The planner's own view still truncates at _MAX_FILE_CHARS, which is
    fine — it does not rewrite files. But the marker used to print the file's
    TOTAL size, reading as 'N chars were cut' and understating the loss."""
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    monkeypatch.setattr(eng, "_CTX_DIRS", ["runtime"])
    monkeypatch.setattr(eng, "_MAX_FILE_CHARS", 100)
    d = tmp_path / "runtime"
    d.mkdir()
    (d / "big.py").write_text("z" * 350)

    ctx = eng.build_context()

    assert "250 chars CUT of 350" in ctx
    assert "TRUNCATED" in ctx
