"""WS-B tests: harness grammar, global registry, verdict bar."""
import json

import pytest

import research.harness as H


def test_validate_spec_accepts_known_types():
    assert H.validate_spec({"type": "signal_backtest",
                            "params": {"w_momentum": 1.0}}) is None
    assert H.validate_spec({"type": "event_study",
                            "params": {"n_names": 100, "item": "2.02"}}) is None


def test_validate_spec_rejects_unknown_type_and_params():
    assert H.validate_spec({"type": "arbitrary_code"}) is not None
    err = H.validate_spec({"type": "signal_backtest",
                           "params": {"exec": "rm -rf /"}})
    assert err and "illegal" in err


def test_registry_counts_globally(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")
    reg0 = H.load_registry()
    assert reg0["total_experiments"] == 14      # seeded with hand-run history

    spec = {"name": "t", "type": "signal_backtest", "params": {}}
    e1 = H.record(spec, {"dsr": 0.10, "sharpe": 0.2}, None)
    e2 = H.record(spec, {"dsr": 0.99, "sharpe": 1.5}, None)
    assert e1["id"] == "N0015" and e2["id"] == "N0016"
    assert H.load_registry()["total_experiments"] == 16
    lines = (tmp_path / "RESULTS.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["verdict"] == "FAIL"
    assert json.loads(lines[1])["verdict"] == "PASS"


def test_verdicts_use_preregistered_bars():
    sb = {"type": "signal_backtest"}
    assert H.verdict_of(sb, {"dsr": 0.951}) == "PASS"
    assert H.verdict_of(sb, {"dsr": 0.94}) == "FAIL"
    ev = {"type": "event_study"}
    assert H.verdict_of(ev, {"t": 2.6}) == "PASS"
    assert H.verdict_of(ev, {"t": -2.7}) == "PASS"     # two-sided
    assert H.verdict_of(ev, {"t": 1.9}) == "FAIL"


def test_error_runs_still_count_against_n(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")
    e = H.record({"name": "x", "type": "event_study"}, None, "boom")
    assert e["verdict"] == "ERROR"
    assert H.load_registry()["total_experiments"] == 15


def test_discovery_dedupe_prevents_family_trial_inflation(tmp_path, monkeypatch):
    """A spec that already had a discovery run must be recognized as executed
    even after its queue status is reset to 'pending' (director/human rewrite).
    Re-running would re-increment the family trial count used in the Bonferroni
    correction - the exact snooping the armor prevents."""
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")

    spec = {"name": "mom", "type": "signal_backtest", "family": "price-factors",
            "params": {"w_momentum": 1.0, "rebalance_days": 42}}
    H.register_spec(spec)
    e1 = H.record(spec, {"dsr": 0.30, "sharpe": 0.9}, None, phase="discovery")
    assert H.load_registry()["families"]["price-factors"]["trials"] == 1
    # immutable guard sees it as already run, regardless of queue status
    assert H.already_run_discovery(spec) == e1["id"]

    # a genuinely different spec is NOT considered a duplicate
    other = {**spec, "params": {"w_momentum": 1.0, "rebalance_days": 21}}
    assert H.already_run_discovery(other) is None


def test_discovery_hashes_migrate_from_results(tmp_path, monkeypatch):
    """A registry with no discovery_hashes key must reconstruct the set from
    the append-only results ledger (so pre-fix history is recognized)."""
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")

    spec = {"name": "f1", "type": "event_study", "family": "8k-drift",
            "params": {"n_names": 500, "item": None}}
    H.register_spec(spec)
    e = H.record(spec, {"t": 1.9}, None, phase="discovery")

    # wipe the key to simulate a pre-fix registry, then reload
    reg = json.loads((tmp_path / "registry.json").read_text())
    reg.pop("discovery_hashes")
    (tmp_path / "registry.json").write_text(json.dumps(reg))
    assert H.already_run_discovery(spec) == e["id"]
