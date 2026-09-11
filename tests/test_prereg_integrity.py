"""Pre-registration integrity: a queued spec must run byte-identical to its
registration (the N0025 silent-corruption class).

N0025 (2026-07-31): the two ic-adaptive specs were manually recovered into
research/hypotheses.yaml carrying their prereg hashes but WITHOUT their params
blocks. Nothing verified a queued spec against its registration, so spec A ran
with params={} — the default STATIC blend — and was graded in the append-only
ledger under the ic-adaptive label; spec B, also stripped, hashed identical to
stripped-A and was marked 'done' as a duplicate without ever running. These
tests pin the guard that makes both shapes mechanically impossible.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import research.harness as H


@pytest.fixture(autouse=True)
def tmp_ledger(tmp_path, monkeypatch):
    """Every test runs against a throwaway registry/ledger — the live
    research/registry.json and RESULTS.jsonl are never written."""
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")
    yield


def _full_spec() -> dict:
    """The ic-adaptive shape as it was registered (params intact)."""
    return {"name": "IC-adaptive full blend at breadth (A)", "family": "famZ",
            "type": "signal_backtest",
            "params": {"w_momentum": 1.0, "ic_weighting": True, "n_names": 120}}


def test_stripped_params_no_longer_validates():
    """N0025 spec A: prereg hash kept, params block lost -> must not run."""
    spec = _full_spec()
    spec["prereg"] = H.register_spec(spec)
    assert H.validate_spec(spec) is None

    corrupted = dict(spec)
    del corrupted["params"]          # exactly the yaml-recovery corruption
    err = H.validate_spec(corrupted)
    assert err is not None
    assert "pre-registration" in err
    assert corrupted["prereg"] in err   # names the registered hash


def test_spec_without_prereg_key_validates_as_before():
    """Seed-style specs carry no prereg hash — the guard is additive only."""
    spec = {"name": "seed static blend", "family": "famZ",
            "type": "signal_backtest",
            "params": {"w_momentum": 1.0, "w_reversal": 0.5, "n_names": 120}}
    assert "prereg" not in spec
    assert H.validate_spec(spec) is None


def test_bookkeeping_keys_do_not_trip_the_check():
    """spec_hash pins type/params/family only: the N0025 guard must not fire on
    status/result_id/source/rationale/name churn."""
    spec = _full_spec()
    spec["prereg"] = H.register_spec(spec)
    spec.update({"status": "pending", "result_id": None, "source": "director",
                 "rationale": "adaptive combining vs static blend",
                 "name": "renamed by a human after registration"})
    assert H.validate_spec(spec) is None


def test_queue_rejects_stripped_spec_instead_of_deduping_it(tmp_path, monkeypatch):
    """N0025 spec B end-to-end: a stripped spec whose hash collides with a
    prior run is now REJECTED, not silently marked done as a duplicate — and it
    consumes no family trial. The healthy spec behind it still runs."""
    import research.nightly as N

    # nightly imports HYPOTHESES into its own namespace -> patch it there
    monkeypatch.setattr(N, "HYPOTHESES", tmp_path / "hypotheses.yaml")

    # a prior run of the STRIPPED shape (this is what stripped-A became)
    stripped_prior = {"name": "default static blend (ran as A)",
                      "type": "signal_backtest", "family": "famZ", "params": {}}
    H.register_spec(stripped_prior)
    prior = H.record(stripped_prior, {"dsr": 0.1, "sharpe": 0.1, "n_days": 100},
                     None, phase="discovery")

    # old spec B: registered full, then stripped -> hashes like the prior run
    full = _full_spec()
    corrupted = {k: v for k, v in full.items() if k != "params"}
    corrupted["prereg"] = H.register_spec(full)
    corrupted["status"] = "pending"
    assert H.spec_hash(corrupted) == H.spec_hash(stripped_prior)
    assert H.already_run_discovery(corrupted) == prior["id"]

    healthy = {"name": "IC-adaptive, reversal excluded (B — mechanism control)",
               "family": "famZ", "type": "signal_backtest",
               "params": {"w_momentum": 1.0, "ic_weighting": True, "n_names": 90},
               "status": "pending"}
    healthy["prereg"] = H.register_spec(healthy)

    monkeypatch.setattr(
        "research.primitives.signal_backtest",
        lambda params, start=None, end=None: {"dsr": 0.2, "sharpe": 0.3,
                                             "n_days": 100})

    trials_before = H.load_registry()["families"]["famZ"]["trials"]
    entry = N.run_one_from_queue([corrupted, healthy])

    # the corrupted spec never became a ledger row and never became a duplicate
    assert corrupted["status"] == "rejected"
    assert "pre-registration" in corrupted["reject_reason"]
    assert "skipped_duplicate" not in corrupted
    assert "result_id" not in corrupted

    # the healthy spec behind it still ran, exactly as registered
    assert entry is not None
    assert healthy["status"] == "done"
    assert healthy["result_id"] == entry["id"]
    assert entry["spec"]["params"]["ic_weighting"] is True

    # exactly one trial consumed: the rejected spec cost the family nothing
    assert H.load_registry()["families"]["famZ"]["trials"] == trials_before + 1


def test_live_queue_pending_specs_match_their_registration():
    """Guard on the REAL queue (read-only): every pending pre-registered spec
    must still hash to its registration, so tomorrow's spec B runs as
    registered instead of landing 'rejected' — the N0025 shape, live."""
    if not H.HYPOTHESES.exists():
        pytest.skip("no live hypotheses.yaml")
    queue = yaml.safe_load(H.HYPOTHESES.read_text()) or []

    checked = 0
    for hypo in queue:
        if not isinstance(hypo, dict):
            continue
        if hypo.get("status", "pending") != "pending" or not hypo.get("prereg"):
            continue
        checked += 1
        assert H.spec_hash(hypo) == hypo["prereg"], (
            f"queued spec {hypo.get('name')!r} no longer matches its "
            f"pre-registration ({H.spec_hash(hypo)} != {hypo['prereg']}) — "
            "it would be rejected tonight; restore its params block or "
            "re-propose it as a new spec")
    if not checked:
        pytest.skip("no pending pre-registered specs in the live queue")
