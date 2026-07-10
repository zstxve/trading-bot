"""Short-side mechanics: signed-qty accounting, mirrored stops, borrow carry,
validation gates. Same hand-crafted-bar style as test_backtest_engine."""
import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest.engine import run_backtest
from tradingbot.config import RiskConfig
from tradingbot.costs import CostModel
from tradingbot.execution.base import Order
from tradingbot.execution.validation import validate_order
from tradingbot.portfolio import Portfolio
from tradingbot.risk.limits import KillSwitch, RiskManager
from tradingbot.risk.sizing import size_position
from tradingbot.strategies.base import EntrySignal, Strategy

ZERO_COST = CostModel(0.0, 0.0, 0.0, short_borrow_bps_per_day=0.0)
TS = pd.Timestamp("2024-01-01", tz="UTC")


def _risk(**over):
    return RiskConfig(**{"risk_per_trade": 0.01, "max_drawdown": 0.5,
                         "daily_loss_limit": 0.1, "max_asset_exposure": 1.0,
                         "max_total_exposure": 1.0, "min_order_notional": 0.0,
                         "allow_short": True, **over})


class ShortStub(Strategy):
    name = "short_stub"

    def __init__(self, entry_bars, stop_price, exit_bar=None):
        super().__init__({})
        self.entry_bars = set(entry_bars)
        self.stop = stop_price
        self.exit_bar = exit_bar

    @classmethod
    def default_params(cls):
        return {}

    def warmup_bars(self):
        return 0

    def prepare(self, df):
        return df.copy()

    def entry_signal(self, df, i):
        if i in self.entry_bars:
            return EntrySignal(stop_price=self.stop, take_profit=None,
                               reason="stub_short", side="short")
        return None

    def exit_signal(self, df, i, position):
        if self.exit_bar is not None and i >= self.exit_bar:
            return "stub_exit"
        return None


def _df(open_, high, low, close):
    idx = pd.date_range("2024-01-01", periods=len(close), freq="1h", tz="UTC")
    return pd.DataFrame({
        "open": np.array(open_, float), "high": np.array(high, float),
        "low": np.array(low, float), "close": np.array(close, float),
        "volume": np.full(len(close), 1000.0)}, index=idx)


def _flat_df(closes):
    closes = np.array(closes, float)
    open_ = np.concatenate([[closes[0]], closes[:-1]])
    return _df(open_, np.maximum(open_, closes) + 0.05,
               np.minimum(open_, closes) - 0.05, closes)


# ---------------------------------------------------------------- portfolio
def test_portfolio_short_roundtrip_profit():
    pf = Portfolio(cash=10_000.0)
    pf.open("X", qty=-10.0, price=100.0, fee=0.0, ts=TS)   # short 10 @ 100
    assert pf.cash == pytest.approx(11_000.0)              # proceeds credited
    assert pf.equity({"X": 100.0}) == pytest.approx(10_000.0)
    assert pf.equity({"X": 90.0}) == pytest.approx(10_100.0)   # price falls -> gain
    trade = pf.close("X", price=90.0, fee=0.0, ts=TS, reason="t")
    assert trade.pnl == pytest.approx(100.0)               # (100-90)*10
    assert pf.cash == pytest.approx(10_100.0)


def test_portfolio_short_loss_when_price_rises():
    pf = Portfolio(cash=10_000.0)
    pf.open("X", qty=-10.0, price=100.0, fee=0.0, ts=TS)
    trade = pf.close("X", price=110.0, fee=0.0, ts=TS, reason="t")
    assert trade.pnl == pytest.approx(-100.0)
    assert pf.cash == pytest.approx(9_900.0)


def test_exposure_uses_absolute_notional():
    pf = Portfolio(cash=10_000.0)
    pf.open("X", qty=-10.0, price=100.0, fee=0.0, ts=TS)
    assert pf.notional("X", {"X": 100.0}) == pytest.approx(1_000.0)   # abs
    assert pf.total_notional({"X": 100.0}) == pytest.approx(1_000.0)


# ---------------------------------------------------------------- sizing
def test_short_sizing_needs_stop_above_price():
    base = dict(equity=10_000.0, cash=10_000.0, price=100.0, risk_per_trade=0.01,
                max_asset_exposure=1.0, cost_side_bps=0.0, side="short")
    assert size_position(stop_price=105.0, **base) == pytest.approx(20.0)  # 100/5
    assert size_position(stop_price=95.0, **base) == 0.0    # stop below price: invalid


# ---------------------------------------------------------------- validation
def test_short_open_gated_by_allow_short():
    pf = Portfolio(cash=10_000.0)
    prices = {"X": 100.0}
    order = Order("X", "sell", 10.0, "t", stop_price=105.0)

    rm_no = RiskManager(_risk(allow_short=False), 10_000.0, KillSwitch(None))
    ok, reason = validate_order(order, portfolio=pf, prices=prices, risk=rm_no,
                                cost_model=ZERO_COST, min_notional=0.0)
    assert not ok and reason == "short_disabled"

    rm_yes = RiskManager(_risk(), 10_000.0, KillSwitch(None))
    ok, reason = validate_order(order, portfolio=pf, prices=prices, risk=rm_yes,
                                cost_model=ZERO_COST, min_notional=0.0)
    assert ok, reason


def test_short_stop_must_sit_above_price():
    pf = Portfolio(cash=10_000.0)
    rm = RiskManager(_risk(), 10_000.0, KillSwitch(None))
    order = Order("X", "sell", 10.0, "t", stop_price=95.0)
    ok, reason = validate_order(order, portfolio=pf, prices={"X": 100.0}, risk=rm,
                                cost_model=ZERO_COST, min_notional=0.0)
    assert not ok and reason == "invalid_stop"


def test_cover_always_allowed_even_halted():
    pf = Portfolio(cash=10_000.0)
    pf.open("X", qty=-10.0, price=100.0, fee=0.0, ts=TS)
    rm = RiskManager(_risk(), 10_000.0, KillSwitch(None))
    rm.kill_switch.engage("test")
    ok, reason = validate_order(Order("X", "buy", 10.0, "cover"), portfolio=pf,
                                prices={"X": 100.0}, risk=rm,
                                cost_model=ZERO_COST, min_notional=0.0)
    assert ok, f"covering a short is risk reduction, must pass: {reason}"


# ---------------------------------------------------------------- engine
def test_engine_short_profits_when_price_falls():
    closes = [100, 100, 100, 100, 90, 90, 80, 80, 80, 80]
    dfs = {"X": _flat_df(closes)}
    strat = ShortStub(entry_bars={3}, stop_price=110.0, exit_bar=7)
    res = run_backtest(dfs, strat, _risk(), ZERO_COST, 10_000.0)

    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.qty < 0
    # entry at open[4]=100, exit at open[8]=80 -> (100-80)*qty_abs
    assert t.entry_price == pytest.approx(100.0)
    assert t.exit_price == pytest.approx(80.0)
    assert t.pnl > 0
    assert res.equity.iloc[-1] > 10_000.0


def test_engine_short_stop_triggers_on_high():
    open_ = [100] * 8
    high = [100.1] * 8
    low = [99.9] * 8
    close = [100] * 8
    high[5] = 106.0                                # bar 5 spikes through the 105 stop
    dfs = {"X": _df(open_, high, low, close)}
    strat = ShortStub(entry_bars={2}, stop_price=105.0)
    res = run_backtest(dfs, strat, _risk(), ZERO_COST, 10_000.0)

    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(105.0)
    assert t.pnl < 0


def test_engine_short_gap_up_fills_at_open():
    open_ = [100, 100, 100, 100, 100, 108, 108, 108]   # bar 5 gaps above the 105 stop
    high = [100.1] * 5 + [109, 108.1, 108.1]
    low = [99.9] * 5 + [107, 107.9, 107.9]
    close = [100] * 5 + [108, 108, 108]
    dfs = {"X": _df(open_, high, low, close)}
    strat = ShortStub(entry_bars={2}, stop_price=105.0)
    res = run_backtest(dfs, strat, _risk(), ZERO_COST, 10_000.0)

    t = res.trades[0]
    assert t.exit_reason == "stop_gap"
    assert t.exit_price == pytest.approx(108.0)     # the open, WORSE than the stop


def test_engine_shorts_blocked_without_allow_short():
    closes = [100] * 10
    dfs = {"X": _flat_df(closes)}
    strat = ShortStub(entry_bars={3}, stop_price=110.0)
    res = run_backtest(dfs, strat, _risk(allow_short=False), ZERO_COST, 10_000.0)
    # the pipeline gate drops the signal before an order even exists
    assert res.n_trades == 0
    assert res.fills == []
    assert res.equity.iloc[-1] == pytest.approx(10_000.0)


def test_short_borrow_carry_charged():
    closes = [100.0] * 60
    dfs = {"X": _flat_df(closes)}
    strat = ShortStub(entry_bars={3}, stop_price=200.0, exit_bar=50)
    cost = CostModel(0.0, 0.0, 0.0, short_borrow_bps_per_day=24.0)  # 1 bp/hour: visible
    res = run_backtest(dfs, strat, _risk(), cost, 10_000.0)

    t = res.trades[0]
    assert t.fees > 0                               # pure carry (no fees/slippage)
    # flat prices: the ONLY pnl impact is carry, so equity ends below start
    assert res.equity.iloc[-1] < 10_000.0
    assert t.pnl == pytest.approx(-t.fees)
