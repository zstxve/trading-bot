"""Pre-flight order validation.

Every order — backtest, paper or live — passes through here before execution.
Rejections are returned as (False, reason) so the caller can log them; they
are never exceptions, because a rejected order is a normal business event.

Key property: a halted system (kill switch, drawdown breach) rejects BUYS
but always lets SELLS through — you must always be able to reduce risk.
"""
from __future__ import annotations

import math
from typing import Mapping, Tuple

from tradingbot.costs import CostModel
from tradingbot.execution.base import Order
from tradingbot.portfolio import Portfolio
from tradingbot.risk.limits import RiskManager


def validate_order(
    order: Order,
    *,
    portfolio: Portfolio,
    prices: Mapping[str, float],
    risk: RiskManager,
    cost_model: CostModel,
    min_notional: float = 10.0,
) -> Tuple[bool, str]:
    if not (math.isfinite(order.qty) and order.qty > 0):
        return False, "invalid_qty"
    last = prices.get(order.symbol)
    if last is None or not math.isfinite(last) or last <= 0:
        return False, "no_price"

    if order.side == "buy":
        if risk.trading_halted:
            return False, "trading_halted"
        est_price = cost_model.fill_price(last, "buy")
        notional = order.qty * est_price
        if notional < min_notional:
            return False, "below_min_notional"
        if notional + cost_model.fee(notional) > portfolio.cash + 1e-6:
            return False, "insufficient_cash"
        if order.stop_price is not None and order.stop_price >= last:
            return False, "invalid_stop"  # long stop must sit below price
        ok, reason = risk.can_open(order.symbol, notional, portfolio, prices)
        if not ok:
            return False, reason
        return True, "ok"

    # sells: only require a matching position — halts never block risk reduction
    pos = portfolio.positions.get(order.symbol)
    if pos is None:
        return False, "no_position"
    if order.qty > pos.qty * (1 + 1e-9):
        return False, "qty_exceeds_position"
    return True, "ok"
