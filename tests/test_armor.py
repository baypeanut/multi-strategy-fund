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
