import pytest

from tradingbot.risk.sizing import size_position

BASE = dict(equity=10_000.0, cash=10_000.0, price=100.0, risk_per_trade=0.005,
            max_asset_exposure=0.25, cost_side_bps=17.0)


def test_risk_based_size():
    # risking 0.5% of 10k = $50 over a $5 stop distance -> 10 units
    qty = size_position(stop_price=95.0, **BASE)
    assert qty == pytest.approx(10.0, rel=1e-6)


def test_wider_stop_means_smaller_size():
    tight = size_position(stop_price=98.0, **BASE)
    wide = size_position(stop_price=90.0, **BASE)
    assert wide < tight


def test_asset_exposure_cap_binds():
    args = {**BASE, "risk_per_trade": 0.02}
    qty = size_position(stop_price=99.5, **args)  # risk-based would be 400 units
    assert qty == pytest.approx(10_000 * 0.25 * 0.999 / 100.0, rel=1e-9)
    assert qty * 100.0 <= 10_000 * 0.25


def test_cash_cap_binds():
    args = {**BASE, "cash": 500.0}
    qty = size_position(stop_price=95.0, **args)
    assert qty < 5.0
    assert qty * 100.0 * (1 + 17e-4) <= 500.0


def test_no_stop_no_trade():
    assert size_position(stop_price=None, **BASE) == 0.0
    assert size_position(stop_price=100.0, **BASE) == 0.0   # stop at price
    assert size_position(stop_price=105.0, **BASE) == 0.0   # stop above price


def test_degenerate_inputs():
    assert size_position(stop_price=95.0, **{**BASE, "equity": 0.0}) == 0.0
    assert size_position(stop_price=95.0, **{**BASE, "cash": 0.0}) == 0.0
    assert size_position(stop_price=95.0, **{**BASE, "price": 0.0}) == 0.0
