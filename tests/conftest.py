"""Shared fixtures: deterministic synthetic price paths with known character
(trend, reversal, chop) so signal tests assert behaviour, not luck."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingbot.config import RiskConfig


def _df_from_close(close: np.ndarray, start="2024-01-01", freq="1h") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(close), freq=freq, tz="UTC")
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.001
    low = np.minimum(open_, close) * 0.999
    volume = np.full(len(close), 1000.0)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


@pytest.fixture
def make_df():
    return _df_from_close


@pytest.fixture
def trend_up_df():
    rng = np.random.default_rng(42)
    n = 1500
    rets = 0.0008 + 0.004 * rng.standard_normal(n)
    return _df_from_close(100 * np.exp(np.cumsum(rets)))


@pytest.fixture
def trend_reversal_df():
    """900 bars up, then 600 bars down — exercises entries AND exits."""
    rng = np.random.default_rng(43)
    up = 0.0010 + 0.004 * rng.standard_normal(900)
    down = -0.0015 + 0.004 * rng.standard_normal(600)
    return _df_from_close(100 * np.exp(np.cumsum(np.concatenate([up, down]))))


@pytest.fixture
def dip_uptrend_df():
    """Rising sawtooth: sustained up-legs with sharp pullbacks — the exact
    shape the mean-reversion strategy is designed to buy. (A flat OU chop
    never satisfies 'below the band but above the regime EMA' jointly.)"""
    rng = np.random.default_rng(44)
    n, rets = 1500, []
    while len(rets) < n:
        rets.extend(np.full(150, 0.0025) + 0.003 * rng.standard_normal(150))
        rets.extend(np.full(25, -0.006) + 0.003 * rng.standard_normal(25))
    return _df_from_close(100 * np.exp(np.cumsum(np.array(rets[:n]))))


@pytest.fixture
def downtrend_df():
    rng = np.random.default_rng(45)
    n = 1200
    rets = -0.003 + 0.002 * rng.standard_normal(n)
    return _df_from_close(100 * np.exp(np.cumsum(rets)))


@pytest.fixture
def lenient_risk():
    """Risk config that stays out of the way for engine mechanics tests."""
    return RiskConfig(
        risk_per_trade=0.01, max_drawdown=0.5, daily_loss_limit=0.1,
        max_open_positions=4, max_asset_exposure=1.0, max_total_exposure=1.0,
        min_order_notional=0.0,
    )


# Small parameter sets so tests need little warmup.
TREND_TEST_PARAMS = {
    "fast_ema": 10, "slow_ema": 30, "regime_timeframe": "4h", "regime_ema": 20,
    "atr_n": 14, "atr_stop_mult": 3.0, "trail_stop": True,
    "atr_pct_min": 0.0, "atr_pct_max": 0.5,
}
MR_TEST_PARAMS = {
    "rsi_n": 14, "rsi_buy": 35.0, "rsi_exit": 55.0, "bb_n": 20, "bb_k": 1.5,
    "regime_timeframe": "4h", "regime_ema": 30, "atr_n": 14,
    "atr_stop_mult": 2.0, "max_hold_bars": 48,
}
BREAKOUT_TEST_PARAMS = {
    "entry_n": 20, "exit_n": 10, "atr_n": 14, "atr_stop_mult": 3.0,
    "regime_timeframe": "4h", "regime_ema": 20, "regime_filter": True,
    "allow_short": False,
}
