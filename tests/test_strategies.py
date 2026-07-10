import pandas as pd

from conftest import MR_TEST_PARAMS, TREND_TEST_PARAMS
from tradingbot.portfolio import Position
from tradingbot.strategies.meanrev_rsi_bb import MeanRevRsiBB
from tradingbot.strategies.trend_ma_cross import TrendMACross


def _entries(strategy, prepared):
    out = []
    for i in range(len(prepared)):
        sig = strategy.entry_signal(prepared, i)
        if sig is not None:
            out.append((i, sig))
    return out


def test_trend_enters_in_uptrend(trend_up_df):
    strat = TrendMACross(TREND_TEST_PARAMS)
    prepared = strat.prepare(trend_up_df)
    entries = _entries(strat, prepared)
    assert len(entries) > 0, "trend strategy found no entries in a strong uptrend"
    for i, sig in entries:
        assert 0 < sig.stop_price < prepared["close"].iloc[i]


def test_trend_stays_out_of_downtrend(downtrend_df):
    strat = TrendMACross(TREND_TEST_PARAMS)
    prepared = strat.prepare(downtrend_df)
    tail = range(len(prepared) - 200, len(prepared))
    assert all(strat.entry_signal(prepared, i) is None for i in tail)


def test_trend_exit_fires_after_reversal(trend_reversal_df):
    strat = TrendMACross(TREND_TEST_PARAMS)
    prepared = strat.prepare(trend_reversal_df)
    dummy = Position(symbol="X", qty=1.0, entry_price=100.0,
                     entry_time=prepared.index[0])
    # Somewhere in the down leg, the strategy must want out.
    assert any(strat.exit_signal(prepared, i, dummy) is not None
               for i in range(1000, len(prepared)))


def test_meanrev_enters_on_dips(dip_uptrend_df):
    strat = MeanRevRsiBB(MR_TEST_PARAMS)
    prepared = strat.prepare(dip_uptrend_df)
    entries = _entries(strat, prepared)
    assert len(entries) > 0, "mean reversion found no entries in dip-buy conditions"
    for i, sig in entries:
        row = prepared.iloc[i]
        assert row["rsi"] < MR_TEST_PARAMS["rsi_buy"]
        assert row["close"] < row["bb_lower"]
        assert 0 < sig.stop_price < row["close"]


def test_meanrev_trend_filter_blocks_downtrend(downtrend_df):
    strat = MeanRevRsiBB(MR_TEST_PARAMS)
    prepared = strat.prepare(downtrend_df)
    assert _entries(strat, prepared) == [], "MR must not catch falling knives"


def test_meanrev_time_stop(dip_uptrend_df):
    strat = MeanRevRsiBB(MR_TEST_PARAMS)
    prepared = strat.prepare(dip_uptrend_df)
    pos = Position(symbol="X", qty=1.0, entry_price=100.0,
                   entry_time=prepared.index[0],
                   bars_held=MR_TEST_PARAMS["max_hold_bars"])
    assert strat.exit_signal(prepared, 500, pos) == "time_stop"


def test_invalid_params_rejected():
    import pytest
    with pytest.raises(ValueError):
        TrendMACross({**TREND_TEST_PARAMS, "fast_ema": 50, "slow_ema": 20})
    with pytest.raises(ValueError):
        MeanRevRsiBB({**MR_TEST_PARAMS, "rsi_buy": 60.0, "rsi_exit": 55.0})
