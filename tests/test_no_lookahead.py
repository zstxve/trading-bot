"""The single most important test in the project.

For a causal system, the signal at bar t computed on data[0..t] must be
IDENTICAL to the signal at bar t computed on the full history. If any
indicator (including the 4h regime resample) peeked forward, truncating the
future would change the value and this test would fail.
"""
import pandas as pd
import pytest

from conftest import BREAKOUT_TEST_PARAMS, MR_TEST_PARAMS, TREND_TEST_PARAMS
from tradingbot.strategies.breakout_donchian import BreakoutDonchian
from tradingbot.strategies.meanrev_rsi_bb import MeanRevRsiBB
from tradingbot.strategies.trend_ma_cross import TrendMACross

CASES = [(TrendMACross, TREND_TEST_PARAMS), (MeanRevRsiBB, MR_TEST_PARAMS),
         (BreakoutDonchian, BREAKOUT_TEST_PARAMS)]


@pytest.mark.parametrize("strategy_cls,params", CASES)
def test_indicators_causal(strategy_cls, params, trend_reversal_df):
    strat = strategy_cls(params)
    full = strat.prepare(trend_reversal_df)
    for t in range(300, len(trend_reversal_df), 197):
        truncated = strat.prepare(trend_reversal_df.iloc[: t + 1])
        pd.testing.assert_series_equal(
            full.iloc[t], truncated.iloc[t], rtol=1e-10, atol=1e-12,
            obj=f"indicator row at t={t}",
        )


@pytest.mark.parametrize("strategy_cls,params", CASES)
def test_entry_signals_causal(strategy_cls, params, trend_reversal_df):
    strat = strategy_cls(params)
    full = strat.prepare(trend_reversal_df)
    checked_any = False
    for t in range(300, len(trend_reversal_df), 97):
        truncated = strat.prepare(trend_reversal_df.iloc[: t + 1])
        sig_full = strat.entry_signal(full, t)
        sig_trunc = strat.entry_signal(truncated, t)
        assert (sig_full is None) == (sig_trunc is None), f"signal mismatch at t={t}"
        if sig_full is not None:
            checked_any = True
            assert sig_full.stop_price == pytest.approx(sig_trunc.stop_price, rel=1e-9)
    # sanity: the test data must actually produce some signals to compare
    assert checked_any or strategy_cls is not TrendMACross
