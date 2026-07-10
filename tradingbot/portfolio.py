"""Portfolio accounting: cash, open positions, closed trades.

Pure bookkeeping — no market opinions, no order routing. Long-only, full
position exits (v1). All fees are charged to cash and attributed to the
round-trip trade record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

import pandas as pd


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float           # includes slippage/spread, excludes fee
    entry_time: pd.Timestamp
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    entry_fee: float = 0.0
    bars_held: int = 0


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float                   # net of all fees
    fees: float
    return_pct: float            # pnl / entry notional
    bars_held: int
    exit_reason: str


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    trades: List[Trade] = field(default_factory=list)

    def buy(
        self,
        symbol: str,
        qty: float,
        price: float,
        fee: float,
        ts: pd.Timestamp,
        stop_price: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Position:
        if symbol in self.positions:
            raise ValueError(f"already long {symbol}; pyramiding not supported")
        cost = qty * price + fee
        if cost > self.cash + 1e-6:
            raise ValueError(f"insufficient cash for {symbol}: need {cost:.2f}, have {self.cash:.2f}")
        self.cash -= cost
        pos = Position(
            symbol=symbol, qty=qty, entry_price=price, entry_time=ts,
            stop_price=stop_price, take_profit=take_profit, entry_fee=fee,
        )
        self.positions[symbol] = pos
        return pos

    def sell(self, symbol: str, price: float, fee: float, ts: pd.Timestamp, reason: str) -> Trade:
        """Close the full position at `price` (already cost-adjusted)."""
        pos = self.positions.pop(symbol)
        self.cash += pos.qty * price - fee
        fees = pos.entry_fee + fee
        pnl = (price - pos.entry_price) * pos.qty - fees
        entry_notional = pos.entry_price * pos.qty
        trade = Trade(
            symbol=symbol, entry_time=pos.entry_time, exit_time=ts,
            entry_price=pos.entry_price, exit_price=price, qty=pos.qty,
            pnl=pnl, fees=fees,
            return_pct=pnl / entry_notional if entry_notional > 0 else 0.0,
            bars_held=pos.bars_held, exit_reason=reason,
        )
        self.trades.append(trade)
        return trade

    # ---- valuation -------------------------------------------------------
    def notional(self, symbol: str, prices: Mapping[str, float]) -> float:
        pos = self.positions.get(symbol)
        if pos is None:
            return 0.0
        return pos.qty * prices.get(symbol, pos.entry_price)

    def total_notional(self, prices: Mapping[str, float]) -> float:
        return sum(self.notional(s, prices) for s in self.positions)

    def equity(self, prices: Mapping[str, float]) -> float:
        """Cash + mark-to-market of open positions (last known price fallback: entry)."""
        return self.cash + self.total_notional(prices)
