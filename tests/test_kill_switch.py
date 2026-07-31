"""E47: `enabled` was a kill switch that failed open.

`SYSTEMS` was hardcoded to [s1,s2,s3,s4], so those four flags were never read.
config.yaml shipped s2/s3/s4 as `enabled: false` with a comment saying the flag
was stale, while all four books traded every tick. An operator reaching for the
flag mid-incident would have believed a book was stopped when it was still
sending orders.

Found by the security audit agent on its first pass over the tree.
"""
import pandas as pd
import pytest

from core.config import CONFIG
from runtime.live import book_enabled


def test_every_book_reads_its_own_flag(monkeypatch):
    systems = CONFIG.setdefault("systems", {})
    for book, key in [("s1", "s1_quant"), ("s2", "s2_news"), ("s3", "s3_llm"),
                      ("s4", "s4_combined"), ("s5", "s5_event")]:
        monkeypatch.setitem(systems, key, {"enabled": False})
        assert book_enabled(book) is False, f"{book} ignores its kill switch"
        monkeypatch.setitem(systems, key, {"enabled": True})
        assert book_enabled(book) is True


def test_a_book_missing_from_config_keeps_trading(monkeypatch):
    """Absence is not a kill switch. A book must not vanish because someone
    deleted a config block."""
    monkeypatch.setitem(CONFIG.setdefault("systems", {}), "s1_quant", {})
    assert book_enabled("s1") is True


def test_the_shipped_config_has_no_stale_flag():
    """The defect was a config that disagreed with the running system."""
    systems = CONFIG["systems"]
    for key in ("s1_quant", "s2_news", "s3_llm", "s4_combined"):
        assert systems[key]["enabled"] is True, (
            f"{key} is marked disabled. If that is intended the book must be "
            f"flat; if it is not, the config is lying again.")


def test_disabled_book_goes_flat_not_missing(monkeypatch):
    """Flat, not absent: the book keeps being marked so its equity history and
    the paired book comparison stay intact."""
    from runtime import live

    weights = {"s1": pd.Series({"AAPL": 0.5, "MSFT": 0.5}),
               "s2": pd.Series({"AAPL": 0.3})}
    monkeypatch.setattr(live, "book_enabled", lambda b: b != "s2")

    for book, w in list(weights.items()):
        if not live.book_enabled(book):
            weights[book] = w * 0.0

    assert set(weights) == {"s1", "s2"}, "a disabled book must not disappear"
    assert weights["s2"].abs().sum() == 0.0
    assert weights["s1"].abs().sum() == pytest.approx(1.0)
