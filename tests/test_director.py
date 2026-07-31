"""Director tests: spec validation/registration path with a fake Anthropic SDK."""
import json
import sys
import types

import pytest

import research.director as D
import research.harness as H


@pytest.fixture(autouse=True)
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")
    monkeypatch.setattr(D, "MEMOS", tmp_path / "MEMOS.md")
    monkeypatch.setattr(D, "get_key", lambda name: "test-key")


def fake_anthropic(payload):
    class Block:
        type = "text"
        def __init__(self, t): self.text = t
    class Resp:
        stop_reason = "end_turn"
        model = "claude-fable-5"
        content = [Block(json.dumps(payload))]
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self): return Resp()
    class Messages:
        def stream(self, **kw): return Stream()
    class Beta:
        messages = Messages()
    class Client:
        def __init__(self, **kw): self.beta = Beta()
    mod = types.ModuleType("anthropic")
    mod.Anthropic = Client
    return mod


def test_director_registers_valid_and_rejects_invalid(monkeypatch):
    payload = {
        "memo": "test memo",
        "new_specs": [
            {"name": "good", "family": "famX", "type": "event_study",
             "params": {"n_names": 200, "item": None, "drift_col": "car2_10"},
             "rationale": "r"},
            {"name": "evil", "family": "famX", "type": "event_study",
             "params": {"n_names": 200, "start": "2021-01-01"},   # date grab
             "rationale": "r"},
        ],
    }
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(payload))
    out = D.run_director(queue=[])
    assert out is not None
    assert [s["name"] for s in out["accepted"]] == ["good"]
    assert out["rejected"][0]["name"] == "evil"
    # accepted spec is PRE-REGISTERED (immutable before it ever runs)
    reg = H.load_registry()
    assert out["accepted"][0]["prereg"] in reg["registered_specs"]


def test_director_budget_cap(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic",
                        fake_anthropic({"memo": "m", "new_specs": []}))
    assert D.run_director([]) is not None
    assert D.run_director([]) is not None
    assert D.run_director([]) is None          # 3rd call: budget (2/night) hit


def test_director_cannot_reopen_burned_family(monkeypatch):
    H.record({"name": "x", "family": "famB", "type": "event_study",
              "params": {}}, {"t": 4.0}, None)
    reg = H.load_registry()
    reg["families"]["famB"]["status"] = "burned"
    H.save_registry(reg)
    payload = {"memo": "m", "new_specs": [
        {"name": "sneaky", "family": "famB", "type": "event_study",
         "params": {"n_names": 100}, "rationale": "r"}]}
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(payload))
    out = D.run_director([])
    assert out["accepted"] == []
    assert "closed" in out["rejected"][0]["error"]
