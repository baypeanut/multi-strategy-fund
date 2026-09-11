"""E24 IBKR mirror: the pure planner and safety rails must hold offline.

The connection layer is thin by design; everything that decides WHAT to trade
is in plan_equity_mirror and is tested here without ib_async or a network.
"""
import pytest

from core.broker.ibkr_broker import (MirrorAborted, assert_paper_account,
                                     ib_symbol, is_equity, our_symbol,
                                     plan_equity_mirror)


def test_paper_account_guard():
    assert_paper_account("DU000000", "DU000000")           # ok
    with pytest.raises(MirrorAborted):
        assert_paper_account("U1234567", "U1234567")         # live acct: never
    with pytest.raises(MirrorAborted):
        assert_paper_account("DU000000", "DUQ999999")       # wrong account


def test_symbol_mapping_roundtrip():
    assert ib_symbol("BRK.B") == "BRK B"
    assert our_symbol("BRK B") == "BRK.B"
    assert ib_symbol("AAPL") == "AAPL"
    assert is_equity("AAPL") and not is_equity("BTC/USDT")


def test_crypto_excluded_and_topn_slice():
    w = {"AAPL": 0.03, "MSFT": -0.02, "BTC/USDT": 0.01, "GE": 0.001}
    orders, info = plan_equity_mirror(
        w, nav=1_000_000, prices={"AAPL": 200.0, "MSFT": 400.0, "GE": 100.0},
        current_shares={}, top_n=2, min_trade_usd=100)
    syms = {o.symbol for o in orders}
    assert syms == {"AAPL", "MSFT"}          # crypto out, GE out of top-2
    assert info["slice_names"] == 2
    aapl = next(o for o in orders if o.symbol == "AAPL")
    assert aapl.action == "BUY" and aapl.quantity == 150   # 30k/200
    msft = next(o for o in orders if o.symbol == "MSFT")
    assert msft.action == "SELL" and msft.quantity == 50   # short 20k/400


def test_deltas_dust_and_exits():
    w = {"AAPL": 0.03}
    cur = {"AAPL": 149, "XOM": 80}           # XOM fell out of the slice
    orders, info = plan_equity_mirror(
        w, nav=1_000_000, prices={"AAPL": 200.0, "XOM": 100.0},
        current_shares=cur, top_n=50, min_trade_usd=300)
    # AAPL delta = 150-149 = 1 share = $200 < 300 dust -> skipped
    assert all(o.symbol != "AAPL" for o in orders)
    assert info["skipped_dust"] == 1
    xom = next(o for o in orders if o.symbol == "XOM")
    assert xom.action == "SELL" and xom.quantity == 80


def test_flatten_on_empty_targets_even_without_price():
    orders, _ = plan_equity_mirror(
        {}, nav=1_000_000, prices={},        # halt: no weights, no prices
        current_shares={"AAPL": 100, "MSFT": -40}, top_n=100)
    acts = {o.symbol: (o.action, o.quantity) for o in orders}
    assert acts["AAPL"] == ("SELL", 100)
    assert acts["MSFT"] == ("BUY", 40)       # buy back the short


def test_missing_price_entry_skipped_and_reported():
    orders, info = plan_equity_mirror(
        {"NEWCO": 0.02}, nav=1_000_000, prices={}, current_shares={})
    assert orders == [] and info["skipped_no_price"] == ["NEWCO"]


def test_position_clip_and_gross_fuse():
    # per-name clip to max_position
    orders, _ = plan_equity_mirror(
        {"AAPL": 0.50}, nav=1_000_000, prices={"AAPL": 100.0},
        current_shares={}, max_position=0.05)
    assert orders[0].quantity == 500          # clipped to 5% -> 50k/100
    # slice gross above cap aborts entirely
    w = {f"N{i}": 0.05 for i in range(30)}    # 150% gross
    with pytest.raises(MirrorAborted):
        plan_equity_mirror(w, nav=1_000_000,
                           prices={f"N{i}": 100.0 for i in range(30)},
                           current_shares={}, max_position=0.05, max_gross=1.0)


def test_max_orders_fuse():
    w = {f"N{i}": 0.004 for i in range(120)}
    px = {f"N{i}": 100.0 for i in range(120)}
    with pytest.raises(MirrorAborted):
        plan_equity_mirror(w, nav=1_000_000, prices=px, current_shares={},
                           top_n=120, min_trade_usd=100, max_orders=50)


def test_book_view_matches_shares_and_drift():
    from core.broker.ibkr_broker import book_view
    view = book_view(
        nav=1_000_000,
        positions={"AAPL": 100, "MSFT": -50, "XOM": 10},  # XOM stray
        prices={"AAPL": 200.0, "MSFT": 400.0, "XOM": 100.0},
        target_weights={"AAPL": 0.02, "MSFT": -0.02, "BTC/USDT": 0.05},
        top_n=100,
    )
    by = {r["symbol"]: r for r in view["positions"]}
    assert by["AAPL"]["shares"] == 100
    assert by["AAPL"]["weight"] == pytest.approx(0.02, abs=1e-6)   # 20k/1M
    assert by["AAPL"]["target_w"] == pytest.approx(0.02)
    assert by["AAPL"]["drift_w"] == pytest.approx(0.0, abs=1e-6)
    assert by["MSFT"]["shares"] == -50
    assert by["MSFT"]["weight"] == pytest.approx(-0.02, abs=1e-6)
    assert "XOM" in by and by["XOM"]["target_w"] == 0.0          # out-of-slice
    assert "BTC/USDT" not in by                                   # crypto excluded from targets
    assert view["n_long"] == 2 and view["n_short"] == 1
    assert view["gross"] == pytest.approx(0.041, abs=1e-4)        # 20k+20k+1k


def test_the_shipped_config_names_an_account():
    """E48f: `expected=""` means "no configured expectation", and then ANY DU
    account passes the gate. config.yaml is agent-writable, so a blanked field
    would silently widen the one boundary that decides which account gets
    orders. Still DU-only, so not a money path today, but this is the guard
    that has to hold on the day it is."""
    from core.config import CONFIG
    acct = CONFIG.get("ibkr", {}).get("account", "")
    assert acct and acct.startswith("DU"), (
        "ibkr.account must name a specific DU* paper account; empty turns the "
        "identity check into a prefix check")
