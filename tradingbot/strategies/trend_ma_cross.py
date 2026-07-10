"""Trend-following: EMA crossover with volatility filter and 4h regime filter.

Long when: fast EMA > slow EMA, price above the higher-timeframe EMA, and
ATR%% of price inside a sane band (dead markets chop you up; panic markets
gap through stops). Exit on trend flip, regime loss, or the ATR stop —
optionally trailed chandelier-style.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from tradingbot.data.resample import causal_htf_indicator
from tradingbot.portfolio import Position
from tradingbot.strategies.base import EntrySignal, Strategy
from tradingbot.strategies.indicators import atr, ema

_REQUIRED = ("ema_fast", "ema_slow", "atr", "atr_pct", "regime_ema")


class TrendMACross(Strategy):
    name = "trend_ma_cross"

    @classmethod
    def default_params(cls) -> dict:
        return {
            "fast_ema": 20, "slow_ema": 50,
            "regime_timeframe": "4h", "regime_ema": 50,
            "atr_n": 14, "atr_stop_mult": 3.0, "trail_stop": True,
            "atr_pct_min": 0.003, "atr_pct_max": 0.08,
        }

    @classmethod
    def param_grid(cls) -> Dict[str, list]:
        return {"fast_ema": [10, 20, 30], "slow_ema": [50, 100], "atr_stop_mult": [2.0, 3.0]}

    def validate_params(self) -> None:
        if self.params["fast_ema"] >= self.params["slow_ema"]:
            raise ValueError("fast_ema must be < slow_ema")

    def warmup_bars(self) -> int:
        p = self.params
        # regime EMA lives on 4h bars -> *4 in 1h base bars (conservative).
        return max(p["slow_ema"], p["atr_n"]) + p["regime_ema"] * 4 + 10

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["ema_fast"] = ema(out["close"], p["fast_ema"])
        out["ema_slow"] = ema(out["close"], p["slow_ema"])
        out["atr"] = atr(out, p["atr_n"])
        out["atr_pct"] = out["atr"] / out["close"]
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
        if not (row["ema_fast"] > row["ema_slow"]):
            return None
        if not (row["close"] > row["regime_ema"]):
            return None
        if not (p["atr_pct_min"] <= row["atr_pct"] <= p["atr_pct_max"]):
            return None
        stop = row["close"] - p["atr_stop_mult"] * row["atr"]
        if stop <= 0 or stop >= row["close"]:
            return None
        return EntrySignal(stop_price=float(stop), take_profit=None, reason="trend_long")

    def exit_signal(self, df: pd.DataFrame, i: int, position: Position) -> Optional[str]:
        row = df.iloc[i]
        if not self._row_ok(row):
            return None
        if row["ema_fast"] < row["ema_slow"]:
            return "trend_flip"
        if row["close"] < row["regime_ema"]:
            return "regime_off"
        return None

    def trail_stop(self, df: pd.DataFrame, i: int, position: Position) -> Optional[float]:
        if not self.params["trail_stop"]:
            return None
        row = df.iloc[i]
        if pd.isna(row["atr"]):
            return None
        return float(row["close"] - self.params["atr_stop_mult"] * row["atr"])
