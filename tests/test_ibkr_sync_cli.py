"""E52: the manual broker CLI had no test, and it read state by a relative path.

Coverage put scripts/ibkr_sync.py at 0%. It is a money path - `--execute`
places orders - so zero verification there is worth more attention than the
other zero-coverage scripts, which are thin wrappers.

The defect: `load_inputs()` read `Path("data/state.json")`, relative to the
caller's working directory, while every other path in the file resolved from
`__file__`. From the repo root it works. From anywhere else it either raises,
or picks up a different state.json that happens to be in the cwd - and
`--execute` would then mirror those weights onto a real broker account.
"""
import json
import pathlib
import sys

import pytest


def test_state_path_is_anchored_to_the_repo(tmp_path, monkeypatch):
    """The bug: run from elsewhere, read someone else's state.

    Deliberately does NOT touch the real data/state.json. The first version of
    this test called load_inputs() against it, which is present on the server
    and absent from the sandbox copy - so it passed locally, passed on the
    host, and went red inside every sandbox. On 2026-08-02 that single red test
    failed the gate for all five proposals Fable produced in a full autonomous
    run: none of them were rejected on their own merits.

    A test that reads the live data directory is not a test of this repo, it is
    a test of one machine's disk.
    """
    import scripts.ibkr_sync as sync
    assert sync.STATE.is_absolute(), "must not be cwd-relative"
    assert sync.STATE.parent.name == "data"
    assert sync.STATE.name == "state.json"
    # resolves from the module, not from wherever the caller happens to stand
    assert sync.STATE.parent.parent == pathlib.Path(
        sync.__file__).resolve().parent.parent

    decoy = tmp_path / "data"; decoy.mkdir()
    (decoy / "state.json").write_text(json.dumps(
        {"systems": {"s4": {"weights": {"WRONG": 1.0}}}}))
    monkeypatch.chdir(tmp_path)
    # STATE was bound at import from __file__, so chdir cannot move it. Reading
    # the decoy would mean the module went back to a relative lookup.
    assert sync.STATE != (decoy / "state.json").resolve()
    assert not str(sync.STATE).startswith(str(tmp_path))


def test_a_latched_halt_mirrors_a_flat_book(monkeypatch, tmp_path):
    """Risk control: while halted, the mirror must target nothing."""
    import scripts.ibkr_sync as sync
    s = tmp_path / "state.json"
    s.write_text(json.dumps({
        "systems": {"s4": {"weights": {"AAPL": 0.04}}},
        "prev_prices": {"AAPL": 200.0},
        "halt_latched": {"ts": "t", "reason": "dd_gate_2"}}))
    monkeypatch.setattr(sync, "STATE", s)
    weights, prices = sync.load_inputs()
    assert weights == {}, "a halted book must not be mirrored"
    assert prices, "prices still load, so a flatten can be priced"


def test_without_a_halt_the_targets_pass_through(monkeypatch, tmp_path):
    import scripts.ibkr_sync as sync
    s = tmp_path / "state.json"
    s.write_text(json.dumps({"systems": {"s4": {"weights": {"AAPL": 0.04}}},
                             "prev_prices": {"AAPL": 200.0}}))
    monkeypatch.setattr(sync, "STATE", s)
    weights, _ = sync.load_inputs()
    assert weights == {"AAPL": 0.04}


def test_execute_requires_typing_the_account(monkeypatch, tmp_path, capsys):
    """--plan and --execute are one word apart and one of them sends orders."""
    import scripts.ibkr_sync as sync
    s = tmp_path / "state.json"
    s.write_text(json.dumps({"systems": {"s4": {"weights": {"AAPL": 0.04}}},
                             "prev_prices": {"AAPL": 200.0}}))
    monkeypatch.setattr(sync, "STATE", s)
    sent = []
    monkeypatch.setattr(sync, "IBKRBroker",
                        lambda cfg: type("B", (), {
                            "sync": lambda self, *a, **k: sent.append(1) or {}})())
    monkeypatch.setattr(sys, "argv", ["ibkr_sync.py", "--execute"])
    monkeypatch.setattr("builtins.input", lambda _: "WRONG-ACCOUNT")

    sync.main()
    assert not sent, "a mistyped account still sent orders"
    assert "nothing sent" in capsys.readouterr().out


def test_yes_flag_skips_the_prompt_for_scripted_use(monkeypatch, tmp_path):
    import scripts.ibkr_sync as sync
    s = tmp_path / "state.json"
    s.write_text(json.dumps({"systems": {"s4": {"weights": {}}},
                             "prev_prices": {}}))
    monkeypatch.setattr(sync, "STATE", s)
    sent = []
    monkeypatch.setattr(sync, "IBKRBroker",
                        lambda cfg: type("B", (), {
                            "sync": lambda self, *a, **k: sent.append(1) or {}})())
    monkeypatch.setattr(sys, "argv", ["ibkr_sync.py", "--execute", "--yes"])
    sync.main()
    assert sent == [1]
