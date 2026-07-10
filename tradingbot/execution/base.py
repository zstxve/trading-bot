"""Broker adapter interface and order/fill types.

The engine, the paper broker and the (default-disabled) live broker all speak
these types. v1 supports market orders only; stop-loss and take-profit are
managed by the system itself (engine intrabar checks / live price polling),
not as resting exchange orders — one code path, identical in backtest and
paper trading.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    reason: str
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    ts: pd.Timestamp
    reason: str


class BrokerAdapter(ABC):
    name: str = "abstract"

    @abstractmethod
    def submit_market(self, order: Order, ref_price: float, ts: pd.Timestamp) -> Fill:
        """Execute a market order. Must either return a Fill or raise —
        never silently swallow an order."""

    def cancel_all(self) -> None:
        """Cancel resting orders on shutdown (no-op for market-only v1)."""
