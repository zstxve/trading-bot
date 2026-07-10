"""Paper broker: simulated fills at the reference price degraded by the cost
model. The default execution venue — nothing here talks to an exchange."""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from tradingbot.costs import CostModel
from tradingbot.execution.base import BrokerAdapter, Fill, Order
from tradingbot.logging_setup import log_event


class PaperBroker(BrokerAdapter):
    name = "paper"

    def __init__(self, cost_model: CostModel, logger: Optional[logging.Logger] = None):
        self.cost = cost_model
        self.log = logger

    def submit_market(self, order: Order, ref_price: float, ts: pd.Timestamp) -> Fill:
        price = self.cost.fill_price(ref_price, order.side)
        fee = self.cost.fee(order.qty * price)
        fill = Fill(symbol=order.symbol, side=order.side, qty=order.qty,
                    price=price, fee=fee, ts=ts, reason=order.reason)
        if self.log:
            log_event(self.log, "paper_fill", symbol=fill.symbol, side=fill.side,
                      qty=round(fill.qty, 8), price=round(fill.price, 6),
                      fee=round(fill.fee, 6), reason=fill.reason)
        return fill
