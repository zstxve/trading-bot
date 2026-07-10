"""Technical indicators.

Every function here is CAUSAL: the value at index t uses only data at or
before t (rolling windows, exponentially weighted means, diffs). No centered
windows, no full-sample statistics. This property is what makes it safe to
compute indicators once on full history and then slice — and it is verified
empirically in tests/test_no_lookahead.py.
"""
from __future__ import annotations

from typing import Tuple

import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    # min_periods=n keeps the warmup region NaN instead of emitting unstable
    # early values that differ from a longer history's values.
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI (smoothed with alpha=1/n)."""
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = gains.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = losses.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + avg_gain / avg_loss)  # avg_loss=0 -> rs=inf -> 100
    return out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd
