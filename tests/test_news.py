"""Tests for System 2 (News/Event), offline & deterministic."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from systems.s2_news.dedup import dedup
from systems.s2_news.engine import NewsEngine
from systems.s2_news.events import (
    market_model_car,
    standardized_unexpected_earnings,
    time_decay_aggregate,
)
from systems.s2_news.sentiment import LexiconScorer
from systems.s2_news.types import NewsItem

NOW = datetime(2026, 6, 17, 12, 0, 0)


def item(text, syms, hours_ago=0):
    return NewsItem(timestamp=NOW - timedelta(hours=hours_ago), text=text, symbols=syms)


# --- dedup ---------------------------------------------------------------
def test_dedup_removes_near_duplicates():
    items = [
        item("Apple beats earnings expectations and raises guidance", ["AAPL"], 2),
        item("Apple beats earnings expectations and raises guidance!", ["AAPL"], 1),
        item("Tesla recalls vehicles over software fault", ["TSLA"], 1),
    ]
    out = dedup(items, threshold=0.9)
    assert len(out) == 2


# --- sentiment -----------------------------------------------------------
def test_lexicon_sign():
    sc = LexiconScorer()
    pos, _ = sc.score("Company beats estimates, strong growth, record profit")
    neg, _ = sc.score("Company misses, weak guidance, lawsuit and probe")
    neu, c = sc.score("Company holds annual meeting on Tuesday")
    assert pos > 0 and neg < 0 and neu == 0.0 and c == 0.0


def test_lexicon_negation():
    sc = LexiconScorer()
    s, _ = sc.score("did not beat")
    assert s < 0  # negated positive -> negative


# --- events --------------------------------------------------------------
def test_sue():
    # zero estimate dispersion -> SUE undefined -> 0.0 by contract
    assert standardized_unexpected_earnings(1.2, [1.0, 1.0, 1.0, 1.0]) == 0.0
    sue = standardized_unexpected_earnings(1.5, [1.0, 1.1, 0.9, 1.0])
    assert sue > 0


def test_market_model_car_positive_jump():
    idx = pd.date_range("2025-01-01", periods=300, freq="D")
    mkt = pd.Series(np.random.default_rng(1).normal(0, 0.01, 300), index=idx)
    asset = 1.0 * mkt + np.random.default_rng(2).normal(0, 0.002, 300)
    asset = pd.Series(asset, index=idx)
    event_date = idx[280]
    asset.iloc[280:286] += 0.02  # positive abnormal jump
    res = market_model_car(asset, mkt, event_date, event_window=(0, 5))
    assert res["car"] > 0.05


def test_time_decay_recent_dominates():
    agg = time_decay_aggregate(
        [(NOW, "A", 1.0, 1.0), (NOW - timedelta(hours=96), "A", 1.0, 1.0)],
        now=NOW, tau_hours=48,
    )
    # recent contributes 1.0, old contributes exp(-2) ~ 0.135
    assert abs(agg["A"] - (1.0 + np.exp(-2))) < 1e-6


# --- engine --------------------------------------------------------------
def test_engine_universe_filter_and_signal():
    items = [
        item("AAPL beats estimates with strong record profit and growth", ["AAPL"], 1),
        item("MSFT misses badly, weak guidance, lawsuit", ["MSFT"], 1),
        item("Some company nobody tracks plunges", ["ZZZZ"], 1),  # out of universe
    ]
    eng = NewsEngine()
    res = eng.generate(items, universe=["AAPL", "MSFT", "GOOGL"], now=NOW)
    assert res.n_used == 2  # ZZZZ filtered out
    assert res.signal["AAPL"] > res.signal["MSFT"]
