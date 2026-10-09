"""Ledger boundary tests: invalid input must never partially mutate a book."""
from copy import deepcopy

import pytest

from core.ledger.ledger import Fill, Ledger


@pytest.mark.parametrize("field,value", [
    ("qty", float("nan")), ("qty", float("inf")), ("qty", True), ("qty", "100"),
    ("price", float("nan")), ("price", float("inf")), ("price", 0.0), ("price", -1.0),
    ("commission", float("nan")), ("commission", float("inf")), ("commission", -1.0),
    ("symbol", ""), ("symbol", " "), ("symbol", None),
])
def test_invalid_fill_rejection_is_atomic_for_existing_book(field, value):
    ledger = Ledger(10000)
    ledger.apply_fill(Fill(None, "AAPL", 10, 100.0, 1.0))
    before = deepcopy(ledger)
    existing = ledger.positions["AAPL"]
    fields = {"ts": None, "symbol": "AAPL", "qty": -2, "price": 110.0, "commission": 1.0}
    fields[field] = value
    with pytest.raises(ValueError):
        ledger.apply_fill(Fill(**fields))
    assert ledger == before
    assert ledger.positions["AAPL"] is existing


def test_rejected_new_symbol_does_not_create_a_phantom_position():
    ledger = Ledger(10000)
    with pytest.raises(ValueError):
        ledger.apply_fill(Fill(None, "NEW", 10, 0.0, 0.0))
    assert ledger.positions == {} and ledger.fills == [] and ledger.cash == 10000


def test_finite_input_overflow_is_atomic():
    ledger = Ledger(10000)
    before = deepcopy(ledger)
    with pytest.raises(ValueError, match="non-finite accounting"):
        ledger.apply_fill(Fill(None, "AAPL", 1e308, 1e308, 0.0))
    assert ledger == before


def test_valid_partial_close_and_flip_preserve_cash_and_cost_basis():
    ledger = Ledger(10000)
    ledger.apply_fill(Fill(None, "AAPL", 10, 100.0, 1.0))
    ledger.apply_fill(Fill(None, "AAPL", -4, 110.0, 1.0))
    assert ledger.positions["AAPL"].qty == 6
    assert ledger.positions["AAPL"].avg_price == 100.0
    assert ledger.positions["AAPL"].realized_pnl == 40.0
    ledger.apply_fill(Fill(None, "AAPL", -8, 120.0, 1.0))
    assert ledger.positions["AAPL"].qty == -2
    assert ledger.positions["AAPL"].avg_price == 120.0
    assert ledger.positions["AAPL"].realized_pnl == 160.0
    assert ledger.cash == 10397.0
    assert ledger.equity({"AAPL": 120.0}) == 10157.0
