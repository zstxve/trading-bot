"""Causal higher-timeframe alignment.

The one place multi-timeframe systems usually leak the future: a 4h indicator
value must only be visible to 1h bars whose CLOSE is at or after that 4h
bar's CLOSE. We enforce this by stamping each higher-timeframe value at its
bar-close time and forward-filling onto base bar-close times.

tests/test_no_lookahead.py verifies this empirically by truncating history.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from tradingbot.data.loader import PANDAS_FREQ, tf_timedelta

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _base_delta(df: pd.DataFrame) -> pd.Timedelta:
    if len(df) < 2:
        raise ValueError("need at least 2 bars to infer base timeframe")
    return pd.Series(df.index).diff().median()


def causal_htf_indicator(
    df: pd.DataFrame,
    htf: str,
    func: Callable[[pd.DataFrame], pd.Series],
) -> pd.Series:
    """Compute `func` on `htf` bars resampled from `df`, aligned causally.

    `func` receives the resampled OHLCV frame (index = HTF bar open times)
    and must return a Series on the same index, computed with causal
    transforms only (rolling / ewm / shift).
    """
    htf_df = (
        df.resample(PANDAS_FREQ[htf], label="left", closed="left")
        .agg(_AGG)
        .dropna(subset=["close"])
    )
    values = func(htf_df)
    # Value becomes available at HTF bar close, not open.
    available_at = htf_df.index + tf_timedelta(htf)
    avail = pd.Series(np.asarray(values, dtype=float), index=available_at)
    base_close = df.index + _base_delta(df)
    aligned = avail.reindex(base_close, method="ffill")
    return pd.Series(aligned.to_numpy(), index=df.index)
