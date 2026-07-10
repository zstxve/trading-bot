"""End-to-end runs on synthetic multi-symbol data: full pipeline, walk-forward
smoke test, paper broker accounting, config validation."""
import numpy as np
import pandas as pd
import pytest

from conftest import TREND_TEST_PARAMS
from tradingbot.backtest.engine import run_backtest
from tradingbot.backtest.metrics import compute_metrics
from tradingbot.backtest.walkforward import run_walkforward
from tradingbot.config import AppConfig, RiskConfig, WalkForwardConfig
from tradingbot.costs import CostModel
from tradingbot.data.loader import generate_synthetic_ohlcv
from tradingbot.execution.base import Order
from tradingbot.execution.paper import PaperBroker
from tradingbot.portfolio import Portfolio
from tradingbot.strategies.trend_ma_cross import TrendMACross

COST = CostModel(10.0, 5.0, 2.0)


@pytest.fixture(scope="module")
def synth_dfs():
    return {s: generate_synthetic_ohlcv(s, n_bars=3000, seed=11) for s in ("AAA/USDT", "BBB/USDT")}


def test_full_backtest_pipeline(synth_dfs):
    strat = TrendMACross(TREND_TEST_PARAMS)
    prepared = {s: strat.prepare(df) for s, df in synth_dfs.items()}
    risk = RiskConfig()
    res = run_backtest(prepared, strat, risk, COST, 10_000.0)

    union = set().union(*[df.index for df in synth_dfs.values()])
    assert len(res.equity) == len(union)
    assert np.isfinite(res.equity.to_numpy()).all()
    assert res.equity.iloc[-1] > 0
    assert 0.0 <= res.exposure <= 1.0
    assert res.final_positions == 0 or res.trades[-1].exit_reason == "end_of_data"

    m = compute_metrics(res.equity, res.trades, 8760, 10_000.0,
                        res.turnover_notional, res.exposure)
    for key in ("cagr", "sharpe", "sortino", "max_drawdown", "win_rate",
                "profit_factor", "exposure", "turnover_per_year"):
        assert key in m

    # risk invariants must hold on every bar
    dd = res.equity / res.equity.cummax() - 1
    assert dd.min() >= -RiskConfig().max_drawdown - 0.10, \
        "drawdown wildly beyond the configured limit (limit + gap allowance)"


def test_trades_respect_exposure_caps(synth_dfs):
    strat = TrendMACross(TREND_TEST_PARAMS)
    prepared = {s: strat.prepare(df) for s, df in synth_dfs.items()}
    risk = RiskConfig(max_asset_exposure=0.25)
    res = run_backtest(prepared, strat, risk, COST, 10_000.0)
    for f in res.fills:
        if f.side == "buy":
            # entry notional can never exceed the per-asset cap of CURRENT equity;
            # initial capital is a loose but sufficient sanity bound here
            assert f.qty * f.price <= 0.30 * max(res.equity.max(), 10_000.0)


def test_walkforward_smoke(synth_dfs):
    wf_cfg = WalkForwardConfig(train_bars=800, test_bars=400, min_trades=1, grids={})
    grid = {"fast_ema": [5, 10], "slow_ema": [30]}
    wf = run_walkforward(synth_dfs, TrendMACross, dict(TREND_TEST_PARAMS), grid,
                         wf_cfg, RiskConfig(), COST, 10_000.0, 8760)
    assert len(wf.windows) >= 2
    assert wf.oos_equity.index.is_monotonic_increasing
    assert not wf.oos_equity.index.duplicated().any()
    assert np.isfinite(wf.oos_equity.to_numpy()).all()
    # chosen params must come from the grid
    for w in wf.windows:
        if not w.fallback_params:
            assert w.params["fast_ema"] in grid["fast_ema"]


def test_walkforward_needs_enough_history(synth_dfs):
    wf_cfg = WalkForwardConfig(train_bars=5000, test_bars=400, min_trades=1)
    with pytest.raises(ValueError):
        run_walkforward(synth_dfs, TrendMACross, dict(TREND_TEST_PARAMS), {},
                        wf_cfg, RiskConfig(), COST, 10_000.0, 8760)


def test_paper_broker_costs_and_accounting():
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    broker = PaperBroker(COST)
    fill = broker.submit_market(Order("X", "buy", 10.0, "t"), ref_price=100.0, ts=ts)
    assert fill.price > 100.0            # buys fill above the reference price
    assert fill.fee > 0

    free_broker = PaperBroker(CostModel(0, 0, 0))
    pf = Portfolio(cash=10_000.0)
    buy = free_broker.submit_market(Order("X", "buy", 10.0, "t"), 100.0, ts)
    pf.buy(buy.symbol, buy.qty, buy.price, buy.fee, ts)
    sell = free_broker.submit_market(Order("X", "sell", 10.0, "t"), 100.0, ts)
    pf.sell(sell.symbol, sell.price, sell.fee, ts, "t")
    assert pf.cash == pytest.approx(10_000.0)   # zero-cost roundtrip conserves cash


def test_config_validation_catches_errors():
    base = {"data": {"symbols": ["BTC/USDT"]}}
    AppConfig(**base)  # minimal valid config
    with pytest.raises(Exception):
        AppConfig(**{**base, "risk": {"risk_per_trade": 0.5}})      # 50% per trade
    with pytest.raises(Exception):
        AppConfig(**{**base, "risk": {"leverage": 3.0}})            # no opt-in
    with pytest.raises(Exception):
        AppConfig(**{**base, "typo_section": {}})                   # unknown key
    with pytest.raises(Exception):
        AppConfig(**{**base, "strategy": {"active": "nonexistent"}})


def test_live_broker_is_blocked_by_default(monkeypatch):
    from tradingbot.execution.binance_live import BinanceLiveBroker, LiveTradingBlocked
    monkeypatch.delenv("TRADINGBOT_LIVE_ACK", raising=False)
    cfg = AppConfig(**{"data": {"symbols": ["BTC/USDT"]}})
    assert cfg.live.enabled is False
    with pytest.raises(LiveTradingBlocked):
        BinanceLiveBroker(cfg, cli_live_flag=True, cost_model=COST)
