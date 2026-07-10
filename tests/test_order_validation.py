import pandas as pd

from tradingbot.config import RiskConfig
from tradingbot.costs import CostModel
from tradingbot.execution.base import Order
from tradingbot.execution.validation import validate_order
from tradingbot.portfolio import Portfolio
from tradingbot.risk.limits import KillSwitch, RiskManager

TS = pd.Timestamp("2024-01-01", tz="UTC")
COST = CostModel(10.0, 5.0, 2.0)
PRICES = {"BTC": 100.0}


def _ctx(cash=10_000.0, **risk_overrides):
    pf = Portfolio(cash=cash)
    cfg = RiskConfig(**{"max_asset_exposure": 0.25, "max_total_exposure": 0.8, **risk_overrides})
    rm = RiskManager(cfg, initial_equity=cash, kill_switch=KillSwitch(None))
    return pf, rm


def _check(order, pf, rm, prices=PRICES):
    return validate_order(order, portfolio=pf, prices=prices, risk=rm,
                          cost_model=COST, min_notional=10.0)


def test_valid_buy_passes():
    pf, rm = _ctx()
    ok, reason = _check(Order("BTC", "buy", 10.0, "t", stop_price=95.0), pf, rm)
    assert ok, reason


def test_rejects_bad_qty():
    pf, rm = _ctx()
    assert not _check(Order("BTC", "buy", 0.0, "t"), pf, rm)[0]
    assert not _check(Order("BTC", "buy", float("nan"), "t"), pf, rm)[0]


def test_rejects_unknown_symbol():
    pf, rm = _ctx()
    ok, reason = _check(Order("DOGE", "buy", 1.0, "t"), pf, rm)
    assert not ok and reason == "no_price"


def test_rejects_below_min_notional():
    pf, rm = _ctx()
    ok, reason = _check(Order("BTC", "buy", 0.05, "t"), pf, rm)
    assert not ok and reason == "below_min_notional"


def test_rejects_insufficient_cash():
    pf, rm = _ctx(max_asset_exposure=1.0, max_total_exposure=1.0)
    ok, reason = _check(Order("BTC", "buy", 200.0, "t"), pf, rm)
    assert not ok and reason == "insufficient_cash"


def test_rejects_stop_above_price():
    pf, rm = _ctx()
    ok, reason = _check(Order("BTC", "buy", 10.0, "t", stop_price=105.0), pf, rm)
    assert not ok and reason == "invalid_stop"


def test_rejects_exposure_breach():
    pf, rm = _ctx()
    ok, reason = _check(Order("BTC", "buy", 30.0, "t", stop_price=95.0), pf, rm)
    assert not ok and reason == "max_asset_exposure"     # 3000 > 25% of 10k


def test_halt_blocks_buys_but_never_sells():
    pf, rm = _ctx()
    pf.buy("BTC", qty=10, price=100, fee=0, ts=TS)
    rm.kill_switch.engage("test")
    ok, reason = _check(Order("BTC", "buy", 5.0, "t", stop_price=95.0), pf, rm)
    assert not ok and reason == "trading_halted"
    ok, reason = _check(Order("BTC", "sell", 10.0, "flatten"), pf, rm)
    assert ok, f"sells must always pass while halted, got: {reason}"


def test_sell_requires_position():
    pf, rm = _ctx()
    ok, reason = _check(Order("BTC", "sell", 1.0, "t"), pf, rm)
    assert not ok and reason == "no_position"


def test_sell_cannot_exceed_position():
    pf, rm = _ctx()
    pf.buy("BTC", qty=10, price=100, fee=0, ts=TS)
    ok, reason = _check(Order("BTC", "sell", 11.0, "t"), pf, rm)
    assert not ok and reason == "qty_exceeds_position"
