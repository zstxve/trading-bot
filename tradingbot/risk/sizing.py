"""Volatility-aware position sizing.

Size is derived from the distance to the stop: risking `risk_per_trade` of
equity between entry and stop means quantity = risk_amount / stop_distance.
Wide (volatile) stops automatically produce small positions. The result is
then capped by per-asset exposure and — for longs — available cash.

Works for both directions: a long's stop sits below price, a short's stop
above. Shorts have no cash cap here (opening one credits cash in the
simplified margin model); the gross-exposure and leverage caps in RiskManager
are the binding constraints instead.
"""
from __future__ import annotations

from typing import Optional


def size_position(
    *,
    equity: float,
    cash: float,
    price: float,
    stop_price: Optional[float],
    risk_per_trade: float,
    max_asset_exposure: float,
    cost_side_bps: float,
    side: str = "long",
) -> float:
    """Return ABSOLUTE quantity to trade (0.0 if no valid size exists)."""
    if price <= 0 or equity <= 0:
        return 0.0
    if stop_price is None or stop_price <= 0:
        return 0.0  # refuse to size a trade without a meaningful stop
    if side == "long" and stop_price >= price:
        return 0.0
    if side == "short" and stop_price <= price:
        return 0.0

    stop_distance = abs(price - stop_price)
    qty = (equity * risk_per_trade) / stop_distance

    # Exposure cap per asset (0.999: keep strictly inside the limit check).
    qty = min(qty, equity * max_asset_exposure * 0.999 / price)

    if side == "long":
        # Cash cap including fees/slippage buffer — spot account, no margin.
        if cash <= 0:
            return 0.0
        cost_rate = cost_side_bps / 1e4
        qty = min(qty, cash * 0.995 / (price * (1 + cost_rate)))

    return max(0.0, qty)
