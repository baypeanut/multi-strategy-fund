"""Persistent sentiment ledger (H1 stage 1 — director wish 2026-07-19): every
fresh universe-tagged headline's score must be appended to an append-only
JSONL that outlives the 7-day operational buffer, so future event studies can
condition on filing-adjacent tone. All offline — feeds are monkeypatched at
systems.s2_news.feeds, no network, deterministic lexicon scoring."""
import json
from datetime import datetime

import pytest

import runtime.live as live_mod
from runtime.live import LiveRuntime
from systems.s2_news.types import NewsItem

NOW = datetime(2026, 7, 25, 12, 0)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    monkeypatch.setattr(live_mod, "has_key", lambda name: False)   # no Anthropic
    monkeypatch.setitem(live_mod.CONFIG, "llm", {"enabled": False})  # lexicon only
    return LiveRuntime(state_path=str(tmp_path / "state.json"))


def _item(url, sym, text, source="yahoo_rss"):
    return NewsItem(timestamp=NOW, text=text, symbols=[sym],
                    source=source, url=url)


def _wire(monkeypatch, items):
    monkeypatch.setattr("systems.s2_news.feeds.fetch_rss_headlines",
                        lambda syms, per_symbol_limit=8: list(items))
    monkeypatch.setattr("systems.s2_news.feeds.fetch_edgar_8k_items",
                        lambda syms, limit_per_symbol=2: [])


def _lines(rt):
    p = rt.sent_ledger_path
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_scored_headlines_persisted_with_source(rt, monkeypatch):
    _wire(monkeypatch, [
        _item("u1", "AAPL", "AAPL beats estimates, record profit and strong growth"),
        _item("u2", "MSFT", "MSFT schedules quarterly dividend"),
        _item("u3", "ZZZZ", "ZZZZ misses on weak guidance"),
    ])
    rt._ingest_news(["AAPL", "MSFT"], NOW)
    rows = _lines(rt)
    by_sym = {r[1]: r for r in rows}
    assert set(by_sym) == {"AAPL", "MSFT"}       # ZZZZ outside the universe
    ts, sym, score, weight, source = by_sym["AAPL"]
    assert ts == NOW.isoformat() and score > 0 and weight > 0
    assert source == "yahoo_rss"
    # neutral headline persisted too — 'no tone' is itself data for H1
    assert by_sym["MSFT"][2] == 0.0 and by_sym["MSFT"][3] == 0.0


def test_dedup_across_ticks_no_double_append(rt, monkeypatch):
    _wire(monkeypatch, [_item("u1", "AAPL", "AAPL beats estimates, record profit")])
    rt._ingest_news(["AAPL"], NOW)
    rt._ingest_news(["AAPL"], NOW)          # same guid -> nothing fresh -> no write
    assert len(_lines(rt)) == 1


def test_ledger_accumulates_across_ticks(rt, monkeypatch):
    _wire(monkeypatch, [_item("u1", "AAPL", "AAPL beats estimates")])
    rt._ingest_news(["AAPL"], NOW)
    _wire(monkeypatch, [_item("u2", "AAPL", "AAPL faces lawsuit and probe")])
    rt._ingest_news(["AAPL"], NOW)
    rows = _lines(rt)
    assert len(rows) == 2                    # append, never overwrite
    assert rows[0][2] > 0 and rows[1][2] < 0


def test_operational_store_semantics_unchanged(rt, monkeypatch):
    # zero-weight items enter the LEDGER but must stay OUT of s2_scored —
    # the operational path (decayed aggregate, rebalance gate) is untouched
    _wire(monkeypatch, [
        _item("u1", "AAPL", "AAPL beats estimates, record profit"),
        _item("u2", "MSFT", "MSFT schedules quarterly dividend"),
    ])
    rt._ingest_news(["AAPL", "MSFT"], NOW)
    stored_syms = {row[1] for row in rt.state["s2_scored"]}
    assert stored_syms == {"AAPL"}
    assert {r[1] for r in _lines(rt)} == {"AAPL", "MSFT"}


def test_write_failure_never_takes_the_tick_down(rt, monkeypatch):
    rt.sent_ledger_path.mkdir()              # open(..., 'a') -> IsADirectoryError
    _wire(monkeypatch, [_item("u1", "AAPL", "AAPL beats estimates")])
    rt._ingest_news(["AAPL"], NOW)           # must not raise
    # the operational path still completed: score stored, counter advanced
    assert rt.state["s2_scored"]
    assert rt.state["s2_news_processed"] == 1
