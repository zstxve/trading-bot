"""Pre-flight order validation.

Every order — backtest, paper or live — passes through here before execution.
Rejections are returned as (False, reason) so the caller can log them; they
are never exceptions, because a rejected order is a normal business event.

Order semantics are inferred from the current position:
  buy  + short position -> cover (close short)
  buy  + no position    -> open long
  sell + long position  -> close long
  sell + no position    -> open short (requires risk.allow_short)
  same-direction add    -> rejected (no pyramiding)

Key property: a halted system (kill switch, drawdown breach) rejects
POSITION-OPENING orders but always lets closing orders through — you must
always be able to reduce risk.
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

    pos = portfolio.positions.get(order.symbol)

    # ---- closing orders: always allowed (risk reduction) --------------------
    is_close = pos is not None and (
        (order.side == "sell" and pos.is_long) or (order.side == "buy" and not pos.is_long)
    )
    if is_close:
        if order.qty > pos.abs_qty * (1 + 1e-9):
            return False, "qty_exceeds_position"
        return True, "ok"

    # ---- same-direction add: rejected ---------------------------------------
    if pos is not None:
        return False, "already_long" if pos.is_long else "already_short"

    # ---- opening orders ------------------------------------------------------
    if risk.trading_halted:
        return False, "trading_halted"
    opening_short = order.side == "sell"
    if opening_short and not risk.cfg.allow_short:
        return False, "short_disabled"

    fill_side = "buy" if order.side == "buy" else "sell"
    est_price = cost_model.fill_price(last, fill_side)
    notional = order.qty * est_price
    if notional < min_notional:
        return False, "below_min_notional"

    if not opening_short:
        if notional + cost_model.fee(notional) > portfolio.cash + 1e-6:
            return False, "insufficient_cash"
        if order.stop_price is not None and order.stop_price >= last:
            return False, "invalid_stop"  # long stop must sit below price
    else:
        if order.stop_price is not None and order.stop_price <= last:
            return False, "invalid_stop"  # short stop must sit above price

    ok, reason = risk.can_open(order.symbol, notional, portfolio, prices)
    if not ok:
        return False, reason
    return True, "ok"
