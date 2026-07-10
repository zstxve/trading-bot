import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest.metrics import benchmark_equity, compute_metrics, sharpe_ratio
from tradingbot.costs import CostModel
from tradingbot.portfolio import Trade

TS = pd.Timestamp("2024-01-01", tz="UTC")


def _series(values, freq="1h"):
    idx = pd.date_range(TS, periods=len(values), freq=freq)
    return pd.Series(np.array(values, dtype=float), index=idx)


def _trade(pnl, notional=1000.0, bars=10):
    return Trade(symbol="X", entry_time=TS, exit_time=TS + pd.Timedelta("10h"),
                 entry_price=100.0, exit_price=100.0 + pnl / 10, qty=10.0,
                 pnl=pnl, fees=1.0, return_pct=pnl / notional, bars_held=bars,
                 exit_reason="test")


def test_cagr_one_year_equals_total_return():
    ppy = 8760
    growth = np.full(ppy, 1.0001)
    equity = _series(10_000 * np.cumprod(growth))
    m = compute_metrics(equity, [], ppy, 10_000.0)
    expected = float(equity.iloc[-1]) / 10_000 - 1
    # len==ppy -> exactly one year -> CAGR == total return
    assert m["cagr"] == pytest.approx(expected, rel=1e-6)
    assert m["total_return"] == pytest.approx(expected, rel=1e-9)


def test_max_drawdown_known_shape():
    equity = _series([100, 120, 90, 130])
    m = compute_metrics(equity, [], 8760, 100.0)
    assert m["max_drawdown"] == pytest.approx(90 / 120 - 1)   # -25%


def test_sharpe_sign_and_zero_vol_guard():
    rng = np.random.default_rng(7)
    up = _series(10_000 * np.cumprod(1 + 0.0002 + 0.0001 * rng.standard_normal(2000)))
    assert sharpe_ratio(up, 8760) > 0
    flat = _series(np.full(100, 10_000.0))
    assert sharpe_ratio(flat, 8760) == 0.0


def test_trade_stats():
    trades = [_trade(10.0), _trade(-5.0), _trade(20.0)]
    equity = _series(np.linspace(10_000, 10_025, 200))
    m = compute_metrics(equity, trades, 8760, 10_000.0)
    assert m["n_trades"] == 3
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["profit_factor"] == pytest.approx(30 / 5)
    assert m["avg_trade_pct"] == pytest.approx((0.01 - 0.005 + 0.02) / 3)


def test_profit_factor_no_losses_is_inf():
    trades = [_trade(10.0), _trade(5.0)]
    equity = _series(np.linspace(10_000, 10_015, 100))
    m = compute_metrics(equity, trades, 8760, 10_000.0)
    assert m["profit_factor"] == float("inf")


def test_benchmark_pays_entry_costs(make_df):
    flat = np.full(500, 100.0)
    dfs = {"A": make_df(flat), "B": make_df(flat)}
    bench = benchmark_equity(dfs, 10_000.0, CostModel(10.0, 5.0, 2.0))
    # flat prices: benchmark should sit just BELOW initial capital (fees+slippage)
    assert bench.iloc[-1] < 10_000.0
    assert bench.iloc[-1] > 9_900.0
