"""Portfolio accounting: cash, open positions, closed trades.

Pure bookkeeping — no market opinions, no order routing. Positions carry a
SIGNED quantity: qty > 0 long, qty < 0 short. Full position exits only (v1).
All fees (entry, exit, short carry) are charged to cash and attributed to the
round-trip trade record.

Short model (simulation/research): opening a short credits the proceeds to
cash; equity = cash + Σ qty·price stays correct because the short's signed
mark is negative. Borrow/funding carry is accrued per bar by the engine into
`Position.carry_fees`. No borrow availability limits, no forced liquidation —
real short selling is harsher than this model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

import pandas as pd


@dataclass
class Position:
    symbol: str
    qty: float                   # SIGNED: >0 long, <0 short
    entry_price: float           # includes slippage/spread, excludes fee
    entry_time: pd.Timestamp
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    entry_fee: float = 0.0
    carry_fees: float = 0.0      # accrued short borrow/funding
    bars_held: int = 0

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def abs_qty(self) -> float:
        return abs(self.qty)


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float                   # SIGNED, as held
    pnl: float                   # net of all fees incl. short carry
    fees: float
    return_pct: float            # pnl / abs entry notional
    bars_held: int
    exit_reason: str


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    trades: List[Trade] = field(default_factory=list)

    # ---- open / close ------------------------------------------------------
    def open(
        self,
        symbol: str,
        qty: float,
        price: float,
        fee: float,
        ts: pd.Timestamp,
        stop_price: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Position:
        """Open a position with SIGNED qty (>0 long, <0 short)."""
        if symbol in self.positions:
            raise ValueError(f"position already open in {symbol}; pyramiding not supported")
        if qty == 0:
            raise ValueError("qty must be nonzero")
        if qty > 0:
            cost = qty * price + fee
            if cost > self.cash + 1e-6:
                raise ValueError(
                    f"insufficient cash for {symbol}: need {cost:.2f}, have {self.cash:.2f}")
            self.cash -= cost
        else:
            # Short: proceeds credited; signed mark keeps equity correct.
            self.cash += abs(qty) * price - fee
        pos = Position(symbol=symbol, qty=qty, entry_price=price, entry_time=ts,
                       stop_price=stop_price, take_profit=take_profit, entry_fee=fee)
        self.positions[symbol] = pos
        return pos

    def close(self, symbol: str, price: float, fee: float, ts: pd.Timestamp,
              reason: str) -> Trade:
        """Close the full position at `price` (already cost-adjusted)."""
        pos = self.positions.pop(symbol)
        if pos.qty > 0:
            self.cash += pos.qty * price - fee
        else:
            self.cash -= abs(pos.qty) * price + fee   # buy back the short
        fees = pos.entry_fee + pos.carry_fees + fee
        # Signed qty makes this one formula work for both directions.
        pnl = (price - pos.entry_price) * pos.qty - fees
        entry_notional = pos.entry_price * abs(pos.qty)
        trade = Trade(
            symbol=symbol, entry_time=pos.entry_time, exit_time=ts,
            entry_price=pos.entry_price, exit_price=price, qty=pos.qty,
            pnl=pnl, fees=fees,
            return_pct=pnl / entry_notional if entry_notional > 0 else 0.0,
            bars_held=pos.bars_held, exit_reason=reason,
        )
        self.trades.append(trade)
        return trade

    # ---- long-only wrappers (backwards-compatible convenience) -------------
    def buy(self, symbol: str, qty: float, price: float, fee: float, ts: pd.Timestamp,
            stop_price: Optional[float] = None, take_profit: Optional[float] = None) -> Position:
        return self.open(symbol, abs(qty), price, fee, ts, stop_price, take_profit)

    def sell(self, symbol: str, price: float, fee: float, ts: pd.Timestamp,
             reason: str) -> Trade:
        return self.close(symbol, price, fee, ts, reason)

    # ---- valuation -------------------------------------------------------
    def notional(self, symbol: str, prices: Mapping[str, float]) -> float:
        """ABSOLUTE exposure of one symbol (for risk caps)."""
        pos = self.positions.get(symbol)
        if pos is None:
            return 0.0
        return abs(pos.qty) * prices.get(symbol, pos.entry_price)

    def total_notional(self, prices: Mapping[str, float]) -> float:
        """Sum of absolute exposures (gross, for risk caps)."""
        return sum(self.notional(s, prices) for s in self.positions)

    def equity(self, prices: Mapping[str, float]) -> float:
        """Cash + SIGNED mark-to-market of open positions."""
        marked = sum(
            p.qty * prices.get(s, p.entry_price) for s, p in self.positions.items()
        )
        return self.cash + marked
