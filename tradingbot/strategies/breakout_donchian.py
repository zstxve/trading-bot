"""Donchian channel breakout (turtle-style), designed for daily bars.

Long when the close breaks above the PRIOR entry_n-bar high (the current bar
is excluded from the channel via shift(1) — including it would make every
new high a self-fulfilling breakout and leak the current close into its own
signal). Optional regime filter: only long above a slow EMA. Exit when the
close drops below the prior exit_n-bar low, or on the ATR stop.

With allow_short (research; needs risk.allow_short) the logic mirrors:
short on a break below the prior entry_n-bar low, cover above the prior
exit_n-bar high.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from tradingbot.data.resample import causal_htf_indicator
from tradingbot.portfolio import Position
from tradingbot.strategies.base import EntrySignal, Strategy
from tradingbot.strategies.indicators import atr, ema

_REQUIRED = ("entry_high", "entry_low", "exit_high", "exit_low", "atr", "regime_ema")


class BreakoutDonchian(Strategy):
    name = "breakout_donchian"

    @classmethod
    def default_params(cls) -> dict:
        return {
            "entry_n": 55, "exit_n": 20,
            "atr_n": 14, "atr_stop_mult": 3.0,
            "regime_timeframe": "1d", "regime_ema": 100, "regime_filter": True,
            "allow_short": False,
        }

    @classmethod
    def param_grid(cls) -> Dict[str, list]:
        return {"entry_n": [20, 55], "exit_n": [10, 20], "atr_stop_mult": [2.0, 3.0]}

    def validate_params(self) -> None:
        if self.params["exit_n"] >= self.params["entry_n"]:
            raise ValueError("exit_n must be < entry_n")

    def warmup_bars(self) -> int:
        p = self.params
        # regime EMA may live on a higher timeframe; *4 covers the worst
        # supported ratio (4h regime on 1h bars).
        return max(p["entry_n"], p["atr_n"]) + p["regime_ema"] * 4 + 10

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        # shift(1): channels are built from PRIOR bars only — the breakout
        # bar must not be part of the channel it breaks.
        out["entry_high"] = out["close"].rolling(p["entry_n"], min_periods=p["entry_n"]).max().shift(1)
        out["entry_low"] = out["close"].rolling(p["entry_n"], min_periods=p["entry_n"]).min().shift(1)
        out["exit_high"] = out["close"].rolling(p["exit_n"], min_periods=p["exit_n"]).max().shift(1)
        out["exit_low"] = out["close"].rolling(p["exit_n"], min_periods=p["exit_n"]).min().shift(1)
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

        regime_long_ok = (not p["regime_filter"]) or row["close"] > row["regime_ema"]
        if row["close"] > row["entry_high"] and regime_long_ok:
            stop = row["close"] - p["atr_stop_mult"] * row["atr"]
            if stop <= 0 or stop >= row["close"]:
                return None
            return EntrySignal(stop_price=float(stop), take_profit=None,
                               reason="breakout_long", side="long")

        regime_short_ok = (not p["regime_filter"]) or row["close"] < row["regime_ema"]
        if p["allow_short"] and row["close"] < row["entry_low"] and regime_short_ok:
            stop = row["close"] + p["atr_stop_mult"] * row["atr"]
            if stop <= row["close"]:
                return None
            return EntrySignal(stop_price=float(stop), take_profit=None,
                               reason="breakout_short", side="short")
        return None

    def exit_signal(self, df: pd.DataFrame, i: int, position: Position) -> Optional[str]:
        row = df.iloc[i]
        if not self._row_ok(row):
            return None
        if position.is_long:
            if row["close"] < row["exit_low"]:
                return "channel_exit"
        else:
            if row["close"] > row["exit_high"]:
                return "channel_exit"
        return None
