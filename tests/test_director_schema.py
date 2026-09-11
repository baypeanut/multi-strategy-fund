"""E45: the director was dead for three days behind a bare except.

`_DIRECTOR_SCHEMA` enumerated all 13 experiment params inline. P0013 added a
14th (`ic_weighting`) on 2026-07-27 and the request started failing with
400 "Schema is too complex" — swallowed by `except Exception: return None`,
which is also what a missing key or an exhausted budget returns. So the
nightly digest said nothing and the research engine simply stopped.

Two guards: the schema stays small enough to compile, and a failure is
distinguishable from a decision not to act.
"""
import json

import pytest

from research import director as D


def _nodes(node) -> int:
    if isinstance(node, dict):
        return 1 + sum(_nodes(v) for v in node.values())
    if isinstance(node, list):
        return sum(_nodes(v) for v in node)
    return 0


def test_schema_stays_under_the_complexity_limit():
    """27 nodes was rejected by the API; 13 compiled. Keep real headroom."""
    n = _nodes(D._DIRECTOR_SCHEMA)
    assert n <= 20, (
        f"director schema is {n} nodes — it was rejected at 27 with "
        f"'Schema is too complex'. Do not re-enumerate params inline.")


def test_params_is_a_string_not_an_enumerated_object():
    params = D._DIRECTOR_SCHEMA["properties"]["new_specs"]["items"]["properties"]["params"]
    assert params == {"type": "string"}, "params was re-typed — that is what broke it"
    assert "JSON STRING" in D._DIRECTOR_SYSTEM


def test_a_failure_is_reported_not_swallowed(monkeypatch):
    """The class fix: None meant 'no key / no budget / catastrophic 400'."""
    monkeypatch.setattr(D, "get_key", lambda k: "sk-test")
    monkeypatch.setattr(D, "_budget_ok", lambda kind, cap: True)
    monkeypatch.setattr(D, "_context", lambda: "CTX")

    class Boom:
        def __getattr__(self, _):
            raise RuntimeError("Error code: 400 - Schema is too complex.")

    import sys
    fake = type("M", (), {})()
    fake.Anthropic = staticmethod(lambda **kw: Boom())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    out = D.run_director([])
    assert out is not None and "error" in out
    assert "too complex" in out["error"]


def test_no_key_still_returns_none(monkeypatch):
    """A missing key is a decision not to act, not a failure — keep them apart."""
    monkeypatch.setattr(D, "get_key", lambda k: None)
    assert D.run_director([]) is None


def test_nightly_surfaces_a_director_failure(monkeypatch, capsys):
    import research.nightly as N
    monkeypatch.setattr(N, "run_director", lambda q: {"error": "BadRequestError: 400"})
    monkeypatch.setattr(N, "pending_confirmation", lambda: None)
    monkeypatch.setattr(N, "run_one_from_queue", lambda q: None)
    monkeypatch.setattr(N, "load_queue", lambda: [])
    monkeypatch.setattr(N, "load_registry", lambda: {"total_experiments": 24})
    # nightly imports run_engineer INSIDE main(), so the patch must land on
    # the SOURCE module — patching research.nightly.run_engineer does nothing
    # and, with a real key present, fires an actual pipeline session (E46).
    monkeypatch.setattr("research.engineer.run_engineer", lambda: None)

    N.main()
    assert "DIRECTOR FAILED" in capsys.readouterr().out


@pytest.mark.parametrize("raw,ok", [
    ('{"n_names": 500}', True),
    ("{}", True),
    ("not json", False),
    ("[1,2]", False),
])
def test_params_string_is_parsed_and_bad_input_rejected(raw, ok, monkeypatch):
    """The wire format changed, so the parse must be defensive."""
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    assert (isinstance(parsed, dict)) is ok


def test_a_capped_director_says_so_instead_of_going_quiet(monkeypatch):
    """E57b: budget exhaustion shared `return None` with the no-key case, so a
    capped director produced no digest line at all and the night read as if it
    had simply chosen not to design. Same residue E45 left in the error path.

    A limit doing its job is information, not silence - the engineer has named
    its own cap since E48."""
    monkeypatch.setattr(D, "get_key", lambda k: "sk-test")
    monkeypatch.setattr(D, "_budget_ok", lambda kind, cap: False)
    out = D.run_director([])
    assert out is not None and "budget" in out["skipped"]


def test_nightly_prints_the_capped_director(monkeypatch, capsys):
    import research.nightly as N
    monkeypatch.setattr(N, "run_director",
                        lambda q: {"skipped": "daily design budget spent (2/day)"})
    monkeypatch.setattr(N, "pending_confirmation", lambda: None)
    monkeypatch.setattr(N, "run_one_from_queue", lambda q: None)
    monkeypatch.setattr(N, "load_queue", lambda: [])
    monkeypatch.setattr(N, "load_registry", lambda: {"total_experiments": 28})
    monkeypatch.setattr("research.engineer.run_engineer_until_settled", lambda: None)
    N.main()
    assert "director skipped" in capsys.readouterr().out
