"""Independent counterexamples from the 2026-09-09 quant audit; all offline."""
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.engine import run_backtest
from backtest.metrics import deflated_sharpe_ratio, paired_test, probabilistic_sharpe_ratio
from core.broker.costs import CostModel, CostParams


def _costs(spread=0, borrow=0):
    return CostModel(CostParams(commission_bps=0, half_spread_bps=spread,
                               impact_coef=0, borrow_annual_bps=borrow))


class FixedEngine:
    def __init__(self, weights):
        self.weights = weights

    def generate(self, histories):
        return SimpleNamespace(weights=pd.Series(self.weights, dtype=float))


def _history(prices):
    return pd.DataFrame({"close": prices, "volume": 1e6},
                        index=pd.bdate_range("2026-01-01", periods=len(prices)))


def test_one_trial_dsr_is_psr_not_certainty():
    r = pd.Series(np.tile([-0.02, 0.01], 80))
    assert deflated_sharpe_ratio(r, 1) == pytest.approx(probabilistic_sharpe_ratio(r))
    assert deflated_sharpe_ratio(r, 1) < 0.01


def test_hac_matches_bartlett_quadratic_form():
    d = np.random.default_rng(821).normal(0.001, 0.01, 18)
    e = d - d.mean()
    # Direct kernel matrix: PSD Bartlett kernel, common divisor n at all lags.
    lag = np.abs(np.arange(len(d))[:, None] - np.arange(len(d))[None, :])
    kernel = np.maximum(1 - lag / 7, 0)
    se = np.sqrt(e @ kernel @ e) / len(d)
    out = paired_test(pd.Series(d), pd.Series(np.zeros(len(d))), lags=6)
    assert out["t_stat"] == pytest.approx(d.mean() / se, rel=1e-12)


def test_buy_and_hold_round_trip_does_not_mint_rebalancing_profit():
    h = {"A": _history([100] * 31 + [110, 100])}
    bt = run_backtest(h, FixedEngine({"A": 0.5}), _costs(),
                      nav0=1000, warmup=30, rebalance_days=21)
    assert bt.equity.iloc[-1] == pytest.approx(1000)
    assert bt.weights.iloc[-2]["A"] == pytest.approx(550 / 1050)


def test_backtest_excludes_warmup_from_evaluation():
    h = {"A": _history([100] * 35)}
    bt = run_backtest(h, FixedEngine({"A": 0.5}), _costs(), warmup=30)
    assert len(bt.net_returns) == 5
    assert bt.net_returns.index[0] == h["A"].index[30]


def test_unchanged_target_still_pays_for_drift_at_rebalance():
    h = {"A": _history([100] * 31 + [110])}
    bt = run_backtest(h, FixedEngine({"A": 0.5}), _costs(spread=10),
                      nav0=1000, warmup=30, rebalance_days=1)
    # Last target is unchanged, but the held shares appreciated and need selling.
    assert bt.turnover.iloc[-1] > 0.02
    assert bt.total_cost > 0.52


def test_backtest_borrow_is_not_free():
    h = {"A": _history([100] * 35)}
    bt = run_backtest(h, FixedEngine({"A": -0.5}), _costs(borrow=252),
                      nav0=1000, warmup=30)
    # Four held return periods, $500 short, annual /252 convention.
    assert bt.total_cost == pytest.approx(0.2)
    assert bt.equity.iloc[-1] == pytest.approx(999.8)


def test_backtest_rejects_missing_held_price():
    h = {"A": _history([100] * 31 + [np.nan, 100])}
    with pytest.raises(ValueError, match="(?i)price|mark"):
        run_backtest(h, FixedEngine({"A": 0.5}), _costs(), warmup=30)


def test_friday_event_cannot_earn_the_monday_reaction_that_chooses_its_sign():
    from research.primitives import calendar_time_daily
    dates = pd.bdate_range("2026-01-01", periods=15)
    friday = pd.Timestamp("2026-01-09")
    monday = pd.Timestamp("2026-01-12")
    prices = np.where(dates >= monday, 110.0, 100.0)
    px = {s: pd.DataFrame({"close": prices if s != "SPY" else 100,
                           "volume": 1e6}, index=dates)
          for s in ["A", "B", "C", "D", "SPY"]}
    events = pd.DataFrame([{"ticker": s, "date": friday, "ar01": 0.1}
                           for s in ["A", "B", "C", "D"]])
    r, _ = calendar_time_daily(events, px, 2, 10, _costs())
    assert r.loc[monday] == 0, "Monday's close is needed to choose the position"
    assert r.sum() == 0, "there was no post-signal price move"


@pytest.fixture
def audit_runtime(tmp_path, monkeypatch):
    import runtime.live as live
    rt = live.LiveRuntime(str(tmp_path / "state.json"))
    rt.cache_path = tmp_path / "cache.pkl"
    rt.state["data_provider"] = "audit"
    rt._eq_provider = SimpleNamespace(PROVIDER_NAME="audit")
    rt.state["systems"]["s4"]["weights"] = {"A": 0.5}
    rt.state["prev_prices"] = {"A": 100.0}
    rt.state["last_borrow_date"] = datetime.now(timezone.utc).date().isoformat()
    monkeypatch.setattr(rt, "_fetch_light", lambda: live.LightData(
        "2026-09-08", {"A": 100.0}, 1.0, 1.0, 500.0))
    monkeypatch.setattr(rt, "_ingest_news", lambda *a: None)
    monkeypatch.setattr(rt, "_news_raw", lambda *a: pd.Series(dtype=float))
    monkeypatch.setattr(rt, "_backfill_s3_forward", lambda *a: None)
    monkeypatch.setattr(rt, "_should_rebalance", lambda *a: (True, "new bar"))
    monkeypatch.setattr(rt, "_coverage", lambda *a: live.Coverage(1, 1, True, True))
    monkeypatch.setattr(rt, "_fetch_heavy", lambda: ({}, {}, pd.Series(dtype=float), False))
    monkeypatch.setattr(rt, "_run_systems", lambda *a: (
        {k: pd.Series(v["weights"], dtype=float) for k, v in rt.state["systems"].items()},
        {}, None, None))
    return rt


@pytest.mark.parametrize("empty", [False, True])
def test_provider_guard_survives_repeated_ticks_and_restart(audit_runtime, monkeypatch, empty):
    import runtime.live as live
    rt = audit_runtime
    rt._eq_provider = SimpleNamespace(PROVIDER_NAME="fallback")
    if empty:
        rt.state["systems"]["s4"]["weights"] = {}
    for _ in range(2):
        out = rt.tick()
        assert out["no_trade"] and not out["rebalanced"]
        assert rt.state.get("data_provider_basis", rt.state["data_provider"]) == "audit"
    restored = live.LiveRuntime(str(rt.state_path))
    assert restored.state.get("data_provider_basis", restored.state["data_provider"]) == "audit"


def test_kill_switch_sees_this_ticks_loss_before_any_new_risk(audit_runtime, monkeypatch):
    import runtime.live as live
    rt = audit_runtime
    now = pd.Timestamp.now(tz="UTC")
    rt.state["equity_history"]["s4"] = [
        [(now - pd.Timedelta(days=1)).isoformat(), rt.nav0],
        [(now - pd.Timedelta(hours=1)).isoformat(), rt.nav0]]
    monkeypatch.setattr(rt, "_fetch_light", lambda: live.LightData(
        "2026-09-08", {"A": 90.0}, 1.0, 1.0, 500.0))
    rt.tick()
    assert rt.state.get("halt_latched"), "5% NAV loss must halt in this tick"
    assert rt.state["systems"]["s4"]["weights"] == {}


def test_failed_pm_response_still_counts_usage_and_budget(tmp_path, monkeypatch):
    import runtime.live as live
    from core.config import CONFIG
    rt = live.LiveRuntime(str(tmp_path / "state.json"))
    monkeypatch.setattr(live, "has_key", lambda name: True)
    monkeypatch.setitem(CONFIG.llm, "enabled", False)

    class PaidButInvalid:
        last_source = "heuristic"
        def __init__(self, **kwargs):
            self.usage_total = {"input_tokens": 1000, "output_tokens": 100,
                                "model": CONFIG.llm.s3_model}
        def propose(self, briefing):
            return {"AAA": 0.02}  # fallback after a paid response failed parsing

    monkeypatch.setattr(live, "AnthropicPM", PaidButInvalid)
    eq = {}
    for i, name in enumerate(["AAA", "BBB", "CCC", "SPY"]):
        p = 100 * np.exp(np.random.default_rng(i).normal(0, .01, 300).cumsum())
        eq[name] = pd.DataFrame({"open": p, "high": p*1.01, "low": p*.99,
                                 "close": p, "volume": 1e6},
                                index=pd.bdate_range("2025-01-01", periods=300))
    rt._run_systems(eq, {}, pd.Series(20., index=eq['SPY'].index),
                    datetime.now(timezone.utc))
    assert rt.state['llm_budget']['pm'] == 1
    assert rt.state['llm_budget']['tokens']['pm']['input'] == 1000


def test_scorer_batch_cannot_overrun_remaining_daily_budget(tmp_path, monkeypatch):
    import runtime.live as live
    from core.config import CONFIG
    from systems.s2_news.types import NewsItem
    rt = live.LiveRuntime(str(tmp_path / "state.json"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rt.state['llm_budget'] = {'date': now.date().isoformat(), 'scorer': 399, 'pm': 0}
    monkeypatch.setitem(CONFIG.llm, 'enabled', False)
    monkeypatch.setattr(live, 'has_key', lambda name: True)
    items = [NewsItem(now, 'AAA beats estimates record profit', ['AAA'], 't', 'u1'),
             NewsItem(now, 'AAA lawsuit probe bankruptcy', ['AAA'], 't', 'u2')]
    monkeypatch.setattr('systems.s2_news.feeds.fetch_rss_headlines', lambda *a, **k: items)
    monkeypatch.setattr('systems.s2_news.feeds.fetch_edgar_8k_items', lambda *a, **k: [])
    calls = []
    class Scorer:
        def __init__(self, **kw): pass
        def score(self, text):
            calls.append(text)
            return .5, .8
    monkeypatch.setattr('systems.s2_news.sentiment.AnthropicScorer', Scorer)
    rt._ingest_news(['AAA'], now)
    assert len(calls) == 1
    assert rt.state['llm_budget']['scorer'] == 400


@pytest.mark.parametrize('adv', [0, -1, float('nan'), float('inf')])
def test_paper_broker_missing_liquidity_cannot_mean_unlimited(adv):
    from core.broker.paper_broker import PaperBroker
    from core.ledger.ledger import Ledger
    ledger = Ledger(1000)
    with pytest.raises(ValueError, match='ADV|adv|liquidity'):
        PaperBroker(ledger, {'equity': _costs()}).submit(
            'A', 100, 100, 'equity', adv, .02)
    assert ledger.cash == 1000 and not ledger.fills


def test_broker_reports_requested_quantity_before_clip():
    from core.broker.paper_broker import PaperBroker
    from core.ledger.ledger import Ledger
    result = PaperBroker(Ledger(1000), {'equity': _costs()}).submit(
        'A', 100, 100, 'equity', 1000, .02)
    assert result.requested_qty == 100 and result.filled_qty == 1


@pytest.mark.parametrize('prices', [{}, {'A':float('nan')}, {'A':0}])
def test_ledger_cannot_hide_an_unmarked_position(prices):
    from core.ledger.ledger import Fill, Ledger
    ledger = Ledger(1000)
    ledger.apply_fill(Fill(None, 'A', 5, 100, 0))
    for method in [ledger.equity, ledger.gross_exposure]:
        with pytest.raises(ValueError, match='A'):
            method(prices)


def test_event_fetch_reads_historical_submission_pages(monkeypatch):
    import research.h1_event_study as h1
    import json
    monkeypatch.setattr(h1, 'ticker_to_cik', lambda: {'AAA':'0000000001'})
    monkeypatch.setattr(h1.time, 'sleep', lambda _: None)
    recent = {'filings': {'recent': {}, 'files': [
        {'name':'old.json', 'filingFrom':'2021-01-01', 'filingTo':'2021-12-31'}]}}
    page = {'form':['8-K'], 'filingDate':['2021-06-10'], 'items':['2.02'],
            'accessionNumber':['old-accession']}
    calls=[]
    def get(url):
        calls.append(url)
        return json.dumps(page if url.endswith('/old.json') else recent)
    monkeypatch.setattr(h1, '_get', get)
    result=h1.fetch_8k_events(['AAA'], start='2021-01-01', end='2021-12-31')
    assert len(result)==1 and result.iloc[0]['acc']=='old-accession'
    assert len(calls)==2


def test_research_stamp_changes_when_imported_math_changes(tmp_path, monkeypatch):
    import research.harness as harness
    root=tmp_path/'research'; root.mkdir()
    (root/'harness.py').write_text('judge')
    (root/'primitives.py').write_text('primitive')
    (tmp_path/'backtest').mkdir()
    metrics=tmp_path/'backtest'/'metrics.py'
    metrics.write_text('original math')
    monkeypatch.setattr(harness, 'ROOT', root)
    before=harness.harness_version()
    metrics.write_text('corrected math')
    assert before != harness.harness_version()


@pytest.mark.parametrize('risk', [False, None])
def test_digest_does_not_announce_superiority_without_comparable_risk(risk):
    from core.alerts import format_daily_digest
    state={'s3_vs_s1':{'n':60, 'p_value':.001, 'verdict_allowed':True,
                       'risk_comparable':risk, 'vol_ratio_s3_s1':3.3}}
    text=format_daily_digest(state,datetime.now(timezone.utc))
    assert 'no superiority claim' in text
    assert '3.30x' in text
    assert 'verdict=yes' not in text
