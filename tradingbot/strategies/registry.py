"""Strategy registry: config name -> class."""
from __future__ import annotations

from typing import Dict, Optional, Type

from tradingbot.strategies.base import Strategy
from tradingbot.strategies.breakout_donchian import BreakoutDonchian
from tradingbot.strategies.meanrev_rsi_bb import MeanRevRsiBB
from tradingbot.strategies.trend_ma_cross import TrendMACross

STRATEGIES: Dict[str, Type[Strategy]] = {
    TrendMACross.name: TrendMACross,
    MeanRevRsiBB.name: MeanRevRsiBB,
    BreakoutDonchian.name: BreakoutDonchian,
}


def build_strategy(name: str, params: Optional[dict] = None) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise KeyError(f"unknown strategy '{name}'; available: {sorted(STRATEGIES)}") from None
    return cls(params)
