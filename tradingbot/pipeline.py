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
            orders.append(Order(symbol=symbol, side="sell", qty=pos.qty, reason=reason))
        else:
            # Trailing stop: only ever tightened, never loosened.
            new_stop = strategy.trail_stop(df, i, pos)
            if new_stop is not None and (pos.stop_price is None or new_stop > pos.stop_price):
                pos.stop_price = new_stop
        return orders

    # Flat: consider a new entry (blocked entirely while halted).
    if risk.trading_halted:
        return orders
    sig = strategy.entry_signal(df, i)
    if sig is None:
        return orders

    price = float(df["close"].iloc[i])
    qty = size_position(
        equity=portfolio.equity(prices),
        cash=portfolio.cash,
        price=price,
        stop_price=sig.stop_price,
        risk_per_trade=risk.cfg.risk_per_trade,
        max_asset_exposure=risk.cfg.max_asset_exposure,
        cost_side_bps=cost_model.total_side_bps,
    )
    if qty > 0:
        orders.append(Order(symbol=symbol, side="buy", qty=qty, reason=sig.reason,
                            stop_price=sig.stop_price, take_profit=sig.take_profit))
    return orders
