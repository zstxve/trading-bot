"""Mean reversion: RSI + Bollinger dip-buying, gated by a 4h trend filter.

Long when: RSI oversold AND close below the lower Bollinger band, but ONLY
while price holds above the higher-timeframe EMA — we fade dips inside
uptrends, we do not catch falling knives in downtrends. Exits: RSI recovery,
tag of the middle band, ATR stop, or a time stop (mean reversion that hasn't
happened within N bars usually isn't coming).
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from tradingbot.data.resample import causal_htf_indicator
from tradingbot.portfolio import Position
from tradingbot.strategies.base import EntrySignal, Strategy
from tradingbot.strategies.indicators import atr, bollinger, ema, rsi

_REQUIRED = ("rsi", "bb_mid", "bb_lower", "atr", "regime_ema")


class MeanRevRsiBB(Strategy):
    name = "meanrev_rsi_bb"

    @classmethod
    def default_params(cls) -> dict:
        return {
            "rsi_n": 14, "rsi_buy": 30.0, "rsi_exit": 55.0,
            "bb_n": 20, "bb_k": 2.0,
            "regime_timeframe": "4h", "regime_ema": 50,
            "atr_n": 14, "atr_stop_mult": 2.0, "max_hold_bars": 48,
        }

    @classmethod
    def param_grid(cls) -> Dict[str, list]:
        return {"rsi_buy": [25.0, 30.0, 35.0], "bb_k": [2.0, 2.5], "atr_stop_mult": [1.5, 2.0]}

    def validate_params(self) -> None:
        if self.params["rsi_exit"] <= self.params["rsi_buy"]:
            raise ValueError("rsi_exit must be > rsi_buy")

    def warmup_bars(self) -> int:
        p = self.params
        return max(p["rsi_n"], p["bb_n"], p["atr_n"]) + p["regime_ema"] * 4 + 10

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["rsi"] = rsi(out["close"], p["rsi_n"])
        mid, _upper, lower = bollinger(out["close"], p["bb_n"], p["bb_k"])
        out["bb_mid"] = mid
        out["bb_lower"] = lower
        out["atr"] = atr(out, p["atr_n"])
        out["regime_ema"] = causal_htf_indicator(
            df, p["regime_timeframe"], lambda h: ema(h["close"], p["regime_ema"])
        )
        return out

    def _row_ok(self, row) -> bool:
        return not any(pd.isna(row[c]) for c in _REQUIRED)

    def entry_signal(self, df: pd.DataFrame, i: int) -> Optional[EntrySignal]:
        p = self.params
        row = df.iloc[i]
        if not self._row_ok(row):
            return None
        if not (row["rsi"] < p["rsi_buy"] and row["close"] < row["bb_lower"]):
            return None
        if not (row["close"] > row["regime_ema"]):  # trend filter: dips only in uptrends
            return None
        stop = row["close"] - p["atr_stop_mult"] * row["atr"]
        if stop <= 0 or stop >= row["close"]:
            return None
        return EntrySignal(stop_price=float(stop), take_profit=None, reason="meanrev_dip")

    def exit_signal(self, df: pd.DataFrame, i: int, position: Position) -> Optional[str]:
        p = self.params
        row = df.iloc[i]
        if position.bars_held >= p["max_hold_bars"]:
            return "time_stop"
        if not self._row_ok(row):
            return None
        if row["rsi"] >= p["rsi_exit"]:
            return "rsi_recovered"
        if row["close"] >= row["bb_mid"]:
            return "mean_reverted"
        return None
