"""Engine mechanics on hand-crafted bars: known entries, known fills, known
PnL. Zero-cost models isolate the accounting; cost models must only make
results worse."""
import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest.engine import run_backtest
from tradingbot.config import RiskConfig
from tradingbot.costs import CostModel
from tradingbot.strategies.base import EntrySignal, Strategy

ZERO_COST = CostModel(0.0, 0.0, 0.0)


class StubStrategy(Strategy):
    """Enters at fixed bar indices with a fixed stop; exits at a fixed bar."""
    name = "stub"

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
            return EntrySignal(stop_price=self.stop, take_profit=None, reason="stub")
        return None

    def exit_signal(self, df, i, position):
        if self.exit_bar is not None and i >= self.exit_bar:
            return "stub_exit"
        return None


def _df(open_, high, low, close, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(close), freq="1h", tz="UTC")
    return pd.DataFrame({
        "open": np.array(open_, dtype=float), "high": np.array(high, dtype=float),
        "low": np.array(low, dtype=float), "close": np.array(close, dtype=float),
        "volume": np.full(len(close), 1000.0),
    }, index=idx)


def _flat_df(closes, start="2024-01-01"):
    closes = np.array(closes, dtype=float)
    open_ = np.concatenate([[closes[0]], closes[:-1]])
    high = np.maximum(open_, closes) + 0.05
    low = np.minimum(open_, closes) - 0.05
    return _df(open_, high, low, closes, start)


def test_signal_fills_at_next_bar_open(lenient_risk):
    closes = [100, 100, 100, 102, 102, 102, 110, 110, 110, 110]
    dfs = {"X": _flat_df(closes)}
    strat = StubStrategy(entry_bars={3}, stop_price=92.0, exit_bar=6)
    res = run_backtest(dfs, strat, lenient_risk, ZERO_COST, 10_000.0)

    assert len(res.trades) == 1
    buy = res.fills[0]
    assert buy.side == "buy"
    assert buy.ts == dfs["X"].index[4]          # next bar after the signal
    assert buy.price == pytest.approx(102.0)    # that bar's open (= prev close)
    # 0.5%... here risk_per_trade=1% of 10k = 100 over $10 stop distance -> 10 units
    assert buy.qty == pytest.approx(10.0, rel=1e-6)
    trade = res.trades[0]
    assert trade.exit_price == pytest.approx(110.0)   # open of bar 7
    assert trade.pnl == pytest.approx(80.0)           # (110-102)*10, zero costs
    assert res.equity.iloc[-1] == pytest.approx(10_080.0)


def test_stop_loss_fills_at_stop_price(lenient_risk):
    open_ = [100, 100, 100, 100, 100, 100, 100, 100]
    high = [100.1] * 8
    low = [99.9] * 8
    close = [100] * 8
    low[5] = 94.0                                  # bar 5 pierces the stop
    dfs = {"X": _df(open_, high, low, close)}
    strat = StubStrategy(entry_bars={2}, stop_price=95.0)
    res = run_backtest(dfs, strat, lenient_risk, ZERO_COST, 10_000.0)

    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(95.0)
    qty = 10_000 * 0.01 / 5.0                      # 20 units
    assert t.pnl == pytest.approx((95.0 - 100.0) * qty)


def test_gap_through_stop_fills_at_open_not_stop(lenient_risk):
    open_ = [100, 100, 100, 100, 100, 91, 92, 92]  # bar 5 gaps below the 95 stop
    high = [100.1] * 5 + [100, 92.1, 92.1]
    low = [99.9] * 5 + [90, 91.9, 91.9]
    close = [100] * 5 + [92, 92, 92]
    dfs = {"X": _df(open_, high, low, close)}
    strat = StubStrategy(entry_bars={2}, stop_price=95.0)
    res = run_backtest(dfs, strat, lenient_risk, ZERO_COST, 10_000.0)

    t = res.trades[0]
    assert t.exit_reason == "stop_gap"
    assert t.exit_price == pytest.approx(91.0)      # the open, WORSE than the stop


def test_costs_strictly_reduce_performance(lenient_risk):
    closes = [100, 100, 100, 102, 102, 102, 110, 110, 110, 110]
    dfs = {"X": _flat_df(closes)}
    free = run_backtest(dfs, StubStrategy({3}, 92.0, exit_bar=6),
                        lenient_risk, ZERO_COST, 10_000.0)
    costly = run_backtest(dfs, StubStrategy({3}, 92.0, exit_bar=6),
                          lenient_risk, CostModel(10.0, 5.0, 2.0), 10_000.0)
    assert costly.equity.iloc[-1] < free.equity.iloc[-1]
    assert costly.trades[0].fees > 0


def test_drawdown_breach_flattens_and_blocks_reentry():
    risk = RiskConfig(risk_per_trade=0.02, max_drawdown=0.02, daily_loss_limit=0.1,
                      max_asset_exposure=1.0, max_total_exposure=1.0,
                      min_order_notional=0.0)
    closes = [100.0] * 4 + [10.0] * 12             # -90% crash at bar 4
    dfs = {"X": _flat_df(closes)}
    strat = StubStrategy(entry_bars={2, 10}, stop_price=50.0)
    res = run_backtest(dfs, strat, risk, ZERO_COST, 10_000.0)

    assert any(e == "max_drawdown" for _, e in res.breaches)
    assert len(res.trades) == 1                     # second entry blocked by halt
    assert res.trades[0].exit_reason in ("stop", "stop_gap", "risk_flatten")
    assert res.final_positions == 0


def test_engine_rejects_empty_data(lenient_risk):
    with pytest.raises(ValueError):
        run_backtest({}, StubStrategy({1}, 90.0), lenient_risk, ZERO_COST, 10_000.0)
