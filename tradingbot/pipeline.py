"""Shared per-bar decision step.

Both the backtest engine and the live/paper runner call `decide_bar` — one
code path for signal -> risk -> order, so paper trading behaves exactly like
the backtest instead of quietly diverging.
"""
from __future__ import annotations

from typing import List, Mapping

import pandas as pd

from tradingbot.costs import CostModel
from tradingbot.execution.base import Order
from tradingbot.portfolio import Portfolio
from tradingbot.risk.limits import RiskManager
from tradingbot.risk.sizing import size_position
from tradingbot.strategies.base import Strategy


def decide_bar(
    *,
    symbol: str,
    df: pd.DataFrame,
    i: int,
    portfolio: Portfolio,
    risk: RiskManager,
    strategy: Strategy,
    cost_model: CostModel,
    prices: Mapping[str, float],
) -> List[Order]:
    """Decisions for `symbol` at bar i's close. Returned orders execute at the
    NEXT bar's open (the engine/runner enforces the delay)."""
    orders: List[Order] = []
    pos = portfolio.positions.get(symbol)

    if pos is not None:
        reason = strategy.exit_signal(df, i, pos)
        if reason:
            # Closing side is the opposite of the held direction.
            close_side = "sell" if pos.is_long else "buy"
            orders.append(Order(symbol=symbol, side=close_side, qty=pos.abs_qty, reason=reason))
        else:
            # Trailing stop: only ever tightened, never loosened —
            # raised for longs, lowered for shorts.
            new_stop = strategy.trail_stop(df, i, pos)
            if new_stop is not None:
                if pos.stop_price is None:
                    pos.stop_price = new_stop
                elif pos.is_long and new_stop > pos.stop_price:
                    pos.stop_price = new_stop
                elif not pos.is_long and new_stop < pos.stop_price:
                    pos.stop_price = new_stop
        return orders

    # Flat: consider a new entry (blocked entirely while halted).
    if risk.trading_halted:
        return orders
    sig = strategy.entry_signal(df, i)
    if sig is None:
        return orders
    if sig.side == "short" and not risk.cfg.allow_short:
        return orders  # strategy wants short but the risk config forbids it

    price = float(df["close"].iloc[i])
    qty = size_position(
        equity=portfolio.equity(prices),
        cash=portfolio.cash,
        price=price,
        stop_price=sig.stop_price,
        risk_per_trade=risk.cfg.risk_per_trade,
        max_asset_exposure=risk.cfg.max_asset_exposure,
        cost_side_bps=cost_model.total_side_bps,
        side=sig.side,
    )
    if qty > 0:
        open_side = "buy" if sig.side == "long" else "sell"
        orders.append(Order(symbol=symbol, side=open_side, qty=qty, reason=sig.reason,
                            stop_price=sig.stop_price, take_profit=sig.take_profit))
    return orders
