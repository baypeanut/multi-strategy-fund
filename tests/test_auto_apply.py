"""E30 full-authority lane: memo tagging + git-commit failure safety.

The auto-apply path itself is exercised live nightly; what is worth pinning
here is that (a) the nightly memo reports the outcome truthfully, and (b) a
broken git can never turn a green apply into a failure.
"""
import research.engineer as eng


def test_applied_tag_reports_each_outcome():
    assert _tag({}) == " (left pending)"
    assert "APPLY-FAILED" in _tag({"auto_apply": {"ok": False, "error": "tests failed"}})
    assert "tests failed" in _tag({"auto_apply": {"ok": False, "error": "tests failed"}})

    ok = _tag({"auto_apply": {"ok": True, "git": {"sha": "deadbee"}}})
    assert ok == " APPLIED @deadbee"
    assert "needs restart" in _tag(
        {"auto_apply": {"ok": True, "git": {"sha": "deadbee"}, "needs_restart": True}})


def test_applied_tag_survives_missing_git_result():
    """auto_commit off, or git blew up — still reports APPLIED, no KeyError."""
    assert _tag({"auto_apply": {"ok": True}}) == " APPLIED"
    assert _tag({"auto_apply": {"ok": True, "git": {"ok": False, "error": "x"}}}) == " APPLIED"


def test_git_commit_never_raises(tmp_path, monkeypatch):
    """A non-repo (or any git failure) returns ok=False, never an exception."""
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    res = eng.git_commit(["nope.py"], "msg")
    assert res["ok"] is False and "error" in res


def _tag(rec):
    return eng._applied_tag(rec)
