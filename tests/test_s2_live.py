"""W4b tests: live news pipeline - feeds, tiered scorer, cross-tick S2 book."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import systems.s2_news.feeds as feeds
from systems.s2_news.feeds import fetch_rss_headlines, item_id
from systems.s2_news.sentiment import LexiconScorer, TieredScorer
from systems.s2_news.types import NewsItem

NOW = datetime(2026, 7, 2, 12, 0, 0)

RSS_XML = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Acme beats estimates, raises guidance</title>
<pubDate>Wed, 01 Jul 2026 14:00:00 +0000</pubDate>
<guid>https://x/1</guid></item>
<item><title>Acme faces lawsuit over defect</title>
<pubDate>Wed, 01 Jul 2026 15:00:00 +0000</pubDate>
<guid>https://x/2</guid></item>
</channel></rss>"""


def test_rss_parsing(monkeypatch):
    monkeypatch.setattr(feeds, "_get", lambda url, timeout=15: RSS_XML)
    items = fetch_rss_headlines(["ACME"])
    assert len(items) == 2
    assert items[0].symbols == ["ACME"] and items[0].source == "yahoo_rss"
    assert "beats" in items[0].text
    # stable ids for cross-tick dedup
    assert item_id(items[0]) != item_id(items[1])


def test_rss_failure_is_silent(monkeypatch):
    def boom(url, timeout=15):
        raise OSError("feed down")
    monkeypatch.setattr(feeds, "_get", boom)
    assert fetch_rss_headlines(["ACME"]) == []


# --- TieredScorer -----------------------------------------------------------
class CountingLLM:
    def __init__(self):
        self.calls = 0

    def score(self, text):
        self.calls += 1
        return 0.9, 0.9


def test_tiered_scorer_escalates_material_only():
    llm = CountingLLM()
    sc = TieredScorer(llm_scorer=llm, escalate_abs_score=0.4, llm_budget=10)
    sc.score("Company holds annual meeting")            # neutral -> no LLM
    assert llm.calls == 0
    s, c = sc.score("Company beats estimates, record profit, strong growth")
    assert llm.calls == 1 and s == 0.9                  # material -> LLM wins


def test_tiered_scorer_respects_budget():
    llm = CountingLLM()
    sc = TieredScorer(llm_scorer=llm, llm_budget=2)
    for _ in range(5):
        sc.score("beats beats beats record profit strong")
    assert llm.calls == 2                               # capped


def test_tiered_scorer_without_llm_is_lexicon():
    sc = TieredScorer(llm_scorer=None)
    lex = LexiconScorer()
    text = "misses badly, weak guidance, lawsuit"
    assert sc.score(text) == lex.score(text)


# --- S2 book end-to-end (no network) -----------------------------------------
@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from runtime.live import LiveRuntime
    import runtime.live as live_mod
    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    return rt


def make_hist(n=300, price=100.0):
    idx = pd.date_range("2025-06-01", periods=n, freq="D")
    rng = np.random.default_rng(5)
    close = price * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1e6}, index=idx)


def test_run_s2_builds_book_from_fresh_news(runtime, monkeypatch):
    syms = ["AAA", "BBB", "CCC", "DDD"]
    eq_hist = {s: make_hist(price=50 + i * 10) for i, s in enumerate(syms)}
    from systems.s1_quant.covariance import ledoit_wolf_cov
    close = pd.DataFrame({s: d["close"] for s, d in eq_hist.items()})
    cov = ledoit_wolf_cov(np.log(close / close.shift(1)).dropna())

    fresh_now = datetime.now()
    fake_items = [
        NewsItem(fresh_now, "AAA beats estimates, record profit, strong growth", ["AAA"], "t", "u1"),
        NewsItem(fresh_now, "BBB faces lawsuit and probe, weak outlook", ["BBB"], "t", "u2"),
        NewsItem(fresh_now, "CCC upgraded, strong growth and approval win", ["CCC"], "t", "u3"),
    ]
    monkeypatch.setattr("systems.s2_news.feeds.fetch_rss_headlines",
                        lambda s, per_symbol_limit=8: list(fake_items))
    monkeypatch.setattr("systems.s2_news.feeds.fetch_edgar_8k_items",
                        lambda s, limit_per_symbol=2: [])

    w, z = runtime._run_s2(eq_hist, cov)
    assert not w.empty
    assert w["AAA"] > 0 and w["BBB"] < 0          # direction follows sentiment
    assert w.abs().max() <= 0.05 + 1e-9           # same caps as every book
    assert runtime.state["s2_news_processed"] == 3

    # second tick with SAME items: all seen -> no reprocessing, state decays on
    w2, _ = runtime._run_s2(eq_hist, cov)
    assert runtime.state["s2_news_processed"] == 3
    assert not w2.empty                            # decayed signal persists


def test_run_s2_cash_when_too_few_names(runtime, monkeypatch):
    syms = ["AAA", "BBB"]
    eq_hist = {s: make_hist() for s in syms}
    from systems.s1_quant.covariance import ledoit_wolf_cov
    close = pd.DataFrame({s: d["close"] for s, d in eq_hist.items()})
    cov = ledoit_wolf_cov(np.log(close / close.shift(1)).dropna())
    monkeypatch.setattr("systems.s2_news.feeds.fetch_rss_headlines",
                        lambda s, per_symbol_limit=8: [
                            NewsItem(datetime.now(), "AAA beats estimates strongly",
                                     ["AAA"], "t", "u9")])
    monkeypatch.setattr("systems.s2_news.feeds.fetch_edgar_8k_items",
                        lambda s, limit_per_symbol=2: [])
    w, z = runtime._run_s2(eq_hist, cov)
    assert w.empty                                 # <3 names with signal -> cash
