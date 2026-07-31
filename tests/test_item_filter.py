"""Item-EXCLUSION filter for event primitives (director wish 2026-07-19).

The decisive PEAD-vs-novel read is 'all 8-Ks EXCEPT earnings items' in ONE
pre-registered run. item_not must be admissible grammar on the three event
types, hash as a DISTINCT trial, filter the event table by exact complement,
and survive into a lockbox confirmation spec. All offline - no network."""
import pandas as pd
import pytest

import research.harness as H
import research.primitives as P


@pytest.fixture(autouse=True)
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(H, "RESULTS", tmp_path / "RESULTS.jsonl")


# --- grammar ------------------------------------------------------------------
def test_item_not_is_admissible_on_event_types():
    for t in ("event_study", "event_study_costnet", "event_portfolio"):
        spec = {"name": "x", "family": "famNew", "type": t,
                "params": {"n_names": 100, "item_not": "2.02"}}
        assert H.validate_spec(spec) is None


def test_item_not_rejected_on_signal_backtest():
    spec = {"name": "x", "family": "famNew", "type": "signal_backtest",
            "params": {"item_not": "2.02"}}
    assert "illegal" in (H.validate_spec(spec) or "")


def test_item_vs_item_not_are_distinct_trials():
    # the subset spec and its complement must never collide in the immutable
    # discovery-dedupe set - they are different experiments
    a = {"type": "event_study", "family": "f",
         "params": {"n_names": 100, "item": "2.02"}}
    b = {"type": "event_study", "family": "f",
         "params": {"n_names": 100, "item_not": "2.02"}}
    assert H.spec_hash(a) != H.spec_hash(b)


# --- complement filter -----------------------------------------------------------
EVENTS = pd.DataFrame([
    {"ticker": "AAA", "date": pd.Timestamp("2024-01-05"), "items": "2.02",
     "acc": "a1"},
    {"ticker": "BBB", "date": pd.Timestamp("2024-01-08"), "items": "5.02",
     "acc": "a2"},
    {"ticker": "CCC", "date": pd.Timestamp("2024-01-09"), "items": "1.01,9.01",
     "acc": "a3"},
])


def _wire(monkeypatch, seen):
    """Fully mocked EDGAR + price environment so event_table runs offline;
    compute_event_table is intercepted to record which events survived the
    filter stage."""
    monkeypatch.setattr("research.h1_event_study.fetch_8k_events",
                        lambda syms, start=None, end=None: EVENTS.copy())
    idx = pd.bdate_range("2024-01-01", periods=20)
    spy = pd.DataFrame({"close": [100.0 + i for i in range(20)]}, index=idx)

    class FakeProv:
        def history(self, syms, start=None, end=None, **kw):
            return {"SPY": spy}

    monkeypatch.setattr("core.data.factory.make_equity_provider",
                        lambda: FakeProv())

    def fake_cet(events, px, spy_ret):
        seen["tickers"] = set(events["ticker"])
        return pd.DataFrame({"ticker": events["ticker"],
                             "date": events["date"],
                             "item": events["items"],
                             "ar01": 0.01, "car2_10": 0.02})
    monkeypatch.setattr("research.h1_event_study.compute_event_table", fake_cet)


def test_event_table_excludes_complement(monkeypatch):
    seen = {}
    _wire(monkeypatch, seen)
    P.event_table({"n_names": 3, "item_not": "2.02"},
                  start="2024-01-01", end="2024-02-01")
    assert seen["tickers"] == {"BBB", "CCC"}      # everything EXCEPT earnings


def test_event_table_item_filter_unchanged(monkeypatch):
    seen = {}
    _wire(monkeypatch, seen)
    P.event_table({"n_names": 3, "item": "2.02"},
                  start="2024-01-01", end="2024-02-01")
    assert seen["tickers"] == {"AAA"}             # positive filter untouched


def test_event_table_no_filter_keeps_all(monkeypatch):
    seen = {}
    _wire(monkeypatch, seen)
    P.event_table({"n_names": 3}, start="2024-01-01", end="2024-02-01")
    assert seen["tickers"] == {"AAA", "BBB", "CCC"}


# --- confirmation propagation --------------------------------------------------
def test_confirmation_carries_item_not(monkeypatch):
    """A candidate discovered WITH the exclusion must be confirmed WITH the
    exclusion - run_confirmation's param filter now admits item_not into the
    calendar-time instrument."""
    spec = {"name": "x", "family": "famC", "type": "event_study",
            "params": {"n_names": 100, "item_not": "2.02"}}
    H.record(spec, {"t": 4.0, "n_events": 1000}, None)
    used = {}
    monkeypatch.setattr("research.primitives.event_portfolio",
                        lambda p, start, end: used.update(params=dict(p)) or
                        {"t": 2.5, "ann_return": 0.05, "sharpe": 1.2,
                         "n_days": 400})
    H.run_confirmation("famC")
    assert used["params"].get("item_not") == "2.02"


def test_director_grammar_exposes_item_not():
    """E45: the schema no longer enumerates params (it made the request 400
    with 'Schema is too complex'). The grammar is the prompt + ALLOWED_PARAMS."""
    import research.director as D
    from research.harness import ALLOWED_PARAMS
    assert "item_not" in ALLOWED_PARAMS["event_study"]
    assert "item_not" in D._DIRECTOR_SYSTEM
