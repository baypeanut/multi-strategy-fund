"""Offline, deterministic tests for the cost model, ledger, and paper broker.

These need no network and verify the accounting + cost math is correct.
Run: pytest tests/test_costs_and_broker.py -q
"""
import math

from core.broker.costs import CostModel, CostParams
from core.broker.paper_broker import PaperBroker
from core.ledger.ledger import Fill, Ledger


def make_cost_model():
    return CostModel(CostParams(commission_bps=0.0, half_spread_bps=1.5,
                                impact_coef=0.10, borrow_annual_bps=50.0))


# --- cost model ----------------------------------------------------------
def test_square_root_impact():
    cm = make_cost_model()
    # participation = 100k / 1M = 0.1; impact = 0.10 * 0.02 * sqrt(0.1)
    b = cm.estimate(trade_value=100_000, adv=1_000_000, daily_vol=0.02)
    expected_impact = 0.10 * 0.02 * math.sqrt(0.1)
    assert math.isclose(b.impact, expected_impact, rel_tol=1e-9)
    assert math.isclose(b.half_spread, 1.5e-4, rel_tol=1e-9)


def test_impact_scales_with_size():
    cm = make_cost_model()
    small = cm.estimate(10_000, 1_000_000, 0.02).impact
    big = cm.estimate(160_000, 1_000_000, 0.02).impact
    # 16x the value -> 4x the impact (sqrt law)
    assert math.isclose(big / small, 4.0, rel_tol=1e-9)


def test_borrow_cost():
    cm = make_cost_model()
    # 50 bps annual / 252 on $1M for 1 day
    c = cm.borrow_cost(1_000_000, days=1.0)
    assert math.isclose(c, 1_000_000 * 50e-4 / 252.0, rel_tol=1e-9)


# --- ledger --------------------------------------------------------------
def test_ledger_realized_pnl_roundtrip():
    led = Ledger(starting_cash=1_000_000)
    led.apply_fill(Fill(ts=None, symbol="AAPL", qty=100, price=100.0, commission=0.0))
    assert led.cash == 1_000_000 - 100 * 100.0
    led.apply_fill(Fill(ts=None, symbol="AAPL", qty=-100, price=110.0, commission=0.0))
    pos = led.positions["AAPL"]
    assert math.isclose(pos.realized_pnl, (110.0 - 100.0) * 100)
    assert pos.qty == 0
    # cash back to start + profit
    assert math.isclose(led.cash, 1_000_000 + 1000.0)


def test_ledger_average_price():
    led = Ledger(starting_cash=1_000_000)
    led.apply_fill(Fill(ts=None, symbol="X", qty=10, price=100.0, commission=0.0))
    led.apply_fill(Fill(ts=None, symbol="X", qty=10, price=120.0, commission=0.0))
    assert math.isclose(led.positions["X"].avg_price, 110.0)


def test_ledger_equity():
    led = Ledger(starting_cash=1_000_000)
    led.apply_fill(Fill(ts=None, symbol="X", qty=100, price=50.0, commission=0.0))
    eq = led.equity({"X": 55.0})
    # cash 995000 + 100*55 = 1,000,500
    assert math.isclose(eq, 1_000_500)


# --- paper broker --------------------------------------------------------
def test_broker_slippage_direction():
    led = Ledger(starting_cash=1_000_000)
    broker = PaperBroker(led, {"equity": make_cost_model()})
    buy = broker.submit("AAPL", qty=100, mid_price=100.0, asset_class="equity",
                        adv=100_000_000, daily_vol=0.02)
    # buy fills above mid
    assert buy.fill_price > 100.0


def test_broker_liquidity_clip():
    led = Ledger(starting_cash=100_000_000)
    broker = PaperBroker(led, {"equity": make_cost_model()}, liquidity_adv_cap=0.10)
    # order worth 50M against ADV 100M, cap 10% -> clipped to 10M
    res = broker.submit("AAPL", qty=500_000, mid_price=100.0, asset_class="equity",
                        adv=100_000_000, daily_vol=0.02)
    assert res.clipped
    assert math.isclose(abs(res.filled_qty) * res.mid_price, 10_000_000, rel_tol=1e-6)
