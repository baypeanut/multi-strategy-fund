"""E17 armor tests: pre-registration, families, lockbox one-shot, burns."""
import json

import pytest

import research.harness as H


@pytest.fixture(autouse=True)
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")


def spec(family="famA", type_="event_study", **params):
    return {"name": "x", "family": family, "type": type_,
            "params": {"n_names": 100, **params}}


# --- pre-registration ---------------------------------------------------------
def test_execute_requires_registration(monkeypatch):
    s = spec()
    with pytest.raises(PermissionError):
        H.execute_spec(s, window="explore")
    H.register_spec(s)
    called = {}
    monkeypatch.setattr("research.primitives.event_study",
                        lambda p, start, end: called.update(start=start, end=end) or {"t": 1.0})
    H.execute_spec(s, window="explore")
    assert called["start"] == H.EXPLORE[0]          # harness owns the window


def test_lockbox_window_is_injected(monkeypatch):
    s = spec()
    H.register_spec(s)
    seen = {}
    monkeypatch.setattr("research.primitives.event_study",
                        lambda p, start, end: seen.update(start=start, end=end) or {"t": 1.0})
    H.execute_spec(s, window="lockbox")
    assert (seen["start"], seen["end"]) == H.LOCKBOX


def test_specs_may_not_choose_dates():
    bad = {"name": "x", "family": "f", "type": "event_study",
           "params": {"n_names": 100, "start": "2021-01-01"}}
    assert "illegal params" in (H.validate_spec(bad) or "")
    bad2 = {"name": "x", "family": "f", "type": "event_study",
            "params": {"n_names": 100}, "window": "lockbox"}
    assert "harness-owned" in (H.validate_spec(bad2) or "")


# --- family Bonferroni ----------------------------------------------------------
def test_family_bonferroni_raises_the_bar():
    ev = {"type": "event_study"}
    m = {"t": 2.6}                                   # p ~ 0.0093
    assert H.verdict_of(ev, m, family_trials=1) == "PASS"
    assert H.verdict_of(ev, m, family_trials=10) == "FAIL"   # 0.093 > 0.05


def test_discovery_pass_marks_family_candidate():
    s = spec()
    e = H.record(s, {"t": 4.0, "n_events": 1000}, None)
    assert e["verdict"] == "PASS"
    fam = H.load_registry()["families"]["famA"]
    assert fam["status"] == "candidate"
    assert fam["candidate_spec"]["type"] == "event_study"


# --- one-shot confirmation --------------------------------------------------------
def _make_candidate():
    H.record(spec(), {"t": 4.0, "n_events": 1000}, None)


def test_confirmation_confirms_and_closes(monkeypatch):
    _make_candidate()
    monkeypatch.setattr("research.primitives.event_portfolio",
                        lambda p, start, end: {"t": 2.5, "ann_return": 0.06,
                                               "sharpe": 1.2, "n_days": 500})
    entry = H.run_confirmation("famA")
    assert entry["verdict"] == "CONFIRMED"
    assert H.load_registry()["families"]["famA"]["status"] == "confirmed"
    # closed family rejects new trials
    assert "closed" in (H.validate_spec(spec()) or "")
    # and the one shot is spent
    with pytest.raises(PermissionError):
        H.run_confirmation("famA")


def test_confirmation_burns_on_fail(monkeypatch):
    _make_candidate()
    monkeypatch.setattr("research.primitives.event_portfolio",
                        lambda p, start, end: {"t": 0.4, "ann_return": -0.01,
                                               "sharpe": 0.2, "n_days": 500})
    entry = H.run_confirmation("famA")
    assert entry["verdict"] == "BURNED"
    assert H.load_registry()["families"]["famA"]["status"] == "burned"
    assert "closed" in (H.validate_spec(spec()) or "")


def test_confirmation_requires_candidate():
    with pytest.raises(PermissionError):
        H.run_confirmation("never-heard-of-it")


def test_confirmation_instrument_is_event_portfolio(monkeypatch):
    _make_candidate()
    used = {}
    monkeypatch.setattr("research.primitives.event_portfolio",
                        lambda p, start, end: used.update(t="portfolio") or
                        {"t": 2.5, "ann_return": 0.05, "sharpe": 1, "n_days": 400})
    H.run_confirmation("famA")
    assert used.get("t") == "portfolio"     # pre-registered instrument, not agent's


def test_confirmation_verdict_derives_t_for_backtests():
    assert H.confirmation_verdict(
        {"sharpe": 1.5, "n_days": 504, "ann_return": 0.1}) == "CONFIRMED"
    assert H.confirmation_verdict(
        {"sharpe": 0.5, "n_days": 504, "ann_return": 0.05}) == "BURNED"


# --- stamping -------------------------------------------------------------------
def test_results_are_stamped():
    e = H.record(spec(), {"t": 1.0, "n_events": 50}, None)
    assert set(e["stamp"]) == {"spec", "harness", "universe", "window"}
    assert e["stamp"]["window"] == list(H.EXPLORE)


# --- ERROR semantics (E60): crashed is not answered ---
def test_confirmation_error_never_burns_and_the_retry_can_confirm(monkeypatch):
    _make_candidate()

    def outage(p, start, end):
        raise RuntimeError("Polygon outage")

    monkeypatch.setattr("research.primitives.event_portfolio", outage)
    entry = H.run_confirmation("famA")
    assert entry["verdict"] == "ERROR"
    assert "RuntimeError" in entry["error"] and " at " in entry["error"]
    fam = H.load_registry()["families"]["famA"]
    assert fam["status"] == "candidate"                  # an outage never burns
    assert fam.get("confirmations_used", 0) == 0         # nothing was spent
    assert H.pending_confirmation() == "famA"            # retried next night

    monkeypatch.setattr("research.primitives.event_portfolio",
                        lambda p, start, end: {"t": 2.5, "ann_return": 0.06,
                                               "sharpe": 1.2, "n_days": 500})
    second = H.run_confirmation("famA")
    assert second["verdict"] == "CONFIRMED"
    fam2 = H.load_registry()["families"]["famA"]
    assert fam2["status"] == "confirmed"
    assert fam2["confirmations_used"] == 1


def test_confirmation_error_attempt_is_still_ledgered(monkeypatch):
    _make_candidate()

    def outage(p, start, end):
        raise RuntimeError("Polygon outage")

    monkeypatch.setattr("research.primitives.event_portfolio", outage)
    H.run_confirmation("famA")
    rows = []
    for line in H.RESULTS.read_text().splitlines():
        rows.append(json.loads(line))
    attempts = [r for r in rows if r.get("phase") == "confirmation"
                and r.get("verdict") == "ERROR"]
    assert len(attempts) == 1        # every lockbox attempt is auditable


def test_discovery_error_is_retryable_but_still_costs_a_trial():
    s = spec(family="famE")
    e1 = H.record(s, None, "boom")
    assert e1["verdict"] == "ERROR"
    assert H.already_run_discovery(s) is None            # retryable
    assert H.load_registry()["families"]["famE"]["trials"] == 1   # trial spent
    e2 = H.record(s, {"t": 4.0, "n_events": 1000}, None)
    assert H.already_run_discovery(s) == e2["id"]        # answered -> blocked
    assert H.load_registry()["families"]["famE"]["trials"] == 2


def test_legacy_error_hash_does_not_block_a_retry():
    s = spec(family="famL")
    e1 = H.record(s, None, "boom")
    reg = H.load_registry()
    reg["discovery_hashes"][H.spec_hash(s)] = e1["id"]   # pre-fix registry state
    H.save_registry(reg)
    assert H.already_run_discovery(s) is None            # verdict consult wins


def test_migration_skips_error_rows():
    s_err = spec(family="famM")
    s_fail = spec(family="famM", n_names=300)            # distinct hash
    H.record(s_err, None, "boom")
    e_fail = H.record(s_fail, {"t": 0.3, "n_events": 40}, None)
    assert e_fail["verdict"] == "FAIL"
    raw = json.loads(H.REGISTRY.read_text())
    raw.pop("discovery_hashes")                          # force the migration
    H.REGISTRY.write_text(json.dumps(raw))
    reg2 = H.load_registry()
    assert H.spec_hash(s_fail) in reg2["discovery_hashes"]
    assert H.spec_hash(s_err) not in reg2["discovery_hashes"]


def test_format_error_is_location_first_and_bounded():
    import re

    def _inner():
        raise ValueError("The truth value of a DataFrame is ambiguous. " + "x" * 1000)

    def _outer():
        _inner()

    err = None
    try:
        _outer()
    except ValueError as exc:
        err = exc
    out = H.format_error(err)
    assert re.match(r"^ValueError at \S+\.py:\d+ in _inner", out)
    assert len(out) <= 400
    # the location survives any downstream 300-char slice
    assert re.search(r"\.py:\d+ in ", out[:120])
    assert H.format_error(ValueError("no tb")) == "ValueError: no tb"


def test_queue_error_rows_carry_the_location(tmp_path, monkeypatch):
    import re

    import research.nightly as N

    monkeypatch.setattr(N, "HYPOTHESES", tmp_path / "hypotheses.yaml")

    def crasher(p, start, end):
        raise RuntimeError("boom")

    monkeypatch.setattr("research.primitives.event_study", crasher)
    hypo = {"name": "crasher", "family": "famQ", "type": "event_study",
            "params": {"n_names": 100}, "status": "pending"}
    entry = N.run_one_from_queue([hypo])
    assert entry["verdict"] == "ERROR"
    assert re.search(r"\.py:\d+ in ", entry["error"])
    assert hypo["status"] == "done"          # no auto-retry of a crashed spec
    assert hypo["result_id"] == entry["id"]
