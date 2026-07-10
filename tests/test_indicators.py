import numpy as np
import pandas as pd

from tradingbot.strategies.indicators import atr, bollinger, ema, rsi, sma


def test_sma_known_values():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(s, 3)
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == 2.0
    assert out.iloc[4] == 4.0


def test_ema_warmup_is_nan():
    s = pd.Series(np.arange(50, dtype=float))
    out = ema(s, 10)
    assert out.iloc[:9].isna().all()
    assert out.iloc[9:].notna().all()


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 101, dtype=float))
    down = pd.Series(np.arange(100, 0, -1, dtype=float))
    assert rsi(up, 14).iloc[-1] > 99
    assert rsi(down, 14).iloc[-1] < 1


def test_rsi_flat_series_is_50():
    flat = pd.Series(np.full(100, 42.0))
    assert rsi(flat, 14).iloc[-1] == 50.0


def test_atr_positive(make_df):
    df = make_df(100 + np.cumsum(np.random.default_rng(1).standard_normal(200)))
    out = atr(df, 14)
    assert out.iloc[20:].gt(0).all()


def test_bollinger_ordering():
    rng = np.random.default_rng(2)
    close = pd.Series(100 + np.cumsum(rng.standard_normal(300)))
    mid, upper, lower = bollinger(close, 20, 2.0)
    valid = mid.notna()
    assert (upper[valid] >= mid[valid]).all()
    assert (lower[valid] <= mid[valid]).all()
