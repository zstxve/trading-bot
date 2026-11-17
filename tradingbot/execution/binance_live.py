"""LIVE Binance adapter — DISABLED BY DEFAULT.

Real orders are refused unless ALL THREE gates pass:
  1. config.yaml:  live.enabled: true
  2. environment:  TRADINGBOT_LIVE_ACK set to the exact acknowledgement string
  3. CLI:          --i-really-want-live flag

This module is deliberately minimal. Before ever enabling it, read docs/GUIDE.md
section "Going live" and complete the paper-trading protocol. Expect to need
per-symbol quantity precision handling (exchange lot size filters) and fill
reconciliation for production use.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import pandas as pd

from tradingbot.config import AppConfig
from tradingbot.costs import CostModel
from tradingbot.execution.base import BrokerAdapter, Fill, Order
from tradingbot.execution.retry import with_retries
from tradingbot.logging_setup import log_event

REQUIRED_ACK = "I-UNDERSTAND-LIVE-TRADING-RISKS-AND-ACCEPT-LOSSES"


class LiveTradingBlocked(RuntimeError):
    pass


class BinanceLiveBroker(BrokerAdapter):
    name = "binance-live"

    def __init__(self, cfg: AppConfig, cli_live_flag: bool, cost_model: CostModel,
                 logger: Optional[logging.Logger] = None):
        gates = {
            "config live.enabled=true": cfg.live.enabled,
            f"env TRADINGBOT_LIVE_ACK == '{REQUIRED_ACK}'":
                os.environ.get("TRADINGBOT_LIVE_ACK") == REQUIRED_ACK,
            "CLI flag --i-really-want-live": cli_live_flag,
        }
        missing = [name for name, ok in gates.items() if not ok]
        if missing:
            raise LiveTradingBlocked(
                "LIVE TRADING BLOCKED. Missing gates: " + "; ".join(missing)
                + ". Paper trading is the default — run `python -m tradingbot paper`."
            )

        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            raise LiveTradingBlocked("BINANCE_API_KEY / BINANCE_API_SECRET not set in environment")

        import ccxt
        self._ccxt = ccxt
        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
        })
        self.cost = cost_model
        self.log = logger

    def submit_market(self, order: Order, ref_price: float, ts: pd.Timestamp) -> Fill:
        resp = with_retries(
            lambda: self.exchange.create_order(order.symbol, "market", order.side, order.qty),
            retriable=(self._ccxt.NetworkError, self._ccxt.RequestTimeout,
                       self._ccxt.ExchangeNotAvailable),
            logger=self.log,
        )
        status = resp.get("status")
        if status not in ("closed", "filled", None):
            raise RuntimeError(f"unexpected live order status '{status}' for {order.symbol}: {resp.get('id')}")
        price = float(resp.get("average") or resp.get("price") or ref_price)
        filled_qty = float(resp.get("filled") or order.qty)
        fee_info = resp.get("fee") or {}
        fee = float(fee_info.get("cost") or self.cost.fee(filled_qty * price))
        fill = Fill(symbol=order.symbol, side=order.side, qty=filled_qty,
                    price=price, fee=fee, ts=ts, reason=order.reason)
        if self.log:
            log_event(self.log, "LIVE_FILL", symbol=fill.symbol, side=fill.side,
                      qty=fill.qty, price=fill.price, fee=fill.fee,
                      order_id=resp.get("id"), reason=fill.reason)
        return fill
