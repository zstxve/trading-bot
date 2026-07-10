"""Event-driven, bar-by-bar backtest engine.

Anti-look-ahead guarantees (structural, not aspirational):
  - signals are computed on bar i's CLOSE and execute at bar i+1's OPEN
  - stop/take-profit checks use bar i's high/low only for positions that
    already existed before bar i
  - when a stop AND a take-profit are both touched inside one bar, the STOP
    is assumed to fill first (pessimistic)
  - gaps through the stop fill at the open (worse than the stop), not at the
    stop price

Per-bar sequence:
  1. execute orders queued on the previous bar at this bar's open
  2. check stops/take-profits against this bar's high/low
  3. mark-to-market at close; update the risk manager
  4. on a risk breach: purge queued buys, queue flattening sells
  5. run strategy decisions for the next bar
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from tradingbot.config import RiskConfig
from tradingbot.costs import CostModel
from tradingbot.execution.base import Fill, Order
from tradingbot.execution.validation import validate_order
from tradingbot.logging_setup import log_event
from tradingbot.pipeline import decide_bar
from tradingbot.portfolio import Portfolio, Trade
from tradingbot.risk.limits import KillSwitch, RiskManager
from tradingbot.strategies.base import Strategy

# Queued orders older than this many bars (symbol had no data) are dropped.
_MAX_ORDER_AGE = 3


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: List[Trade]
    fills: List[Fill]
    breaches: List[Tuple[pd.Timestamp, str]]
    exposure: float              # fraction of bars with >=1 open position
    turnover_notional: float     # total traded notional (buys + sells)
    initial_capital: float
    rejected_orders: int = 0
    final_positions: int = 0

    @property
    def n_trades(self) -> int:
        return len(self.trades)


def run_backtest(
    dfs: Dict[str, pd.DataFrame],
    strategy: Strategy,
    risk_cfg: RiskConfig,
    cost_model: CostModel,
    initial_capital: float,
    logger: Optional[logging.Logger] = None,
    liquidate_at_end: bool = True,
) -> BacktestResult:
    dfs = {s: df for s, df in dfs.items() if len(df) > 0}
    if not dfs:
        raise ValueError("no data supplied to backtest")

    union_index = pd.DatetimeIndex(sorted(set().union(*[df.index for df in dfs.values()])))
    # O(1) timestamp -> row position per symbol.
    pos_of = {s: {ts: i for i, ts in enumerate(df.index)} for s, df in dfs.items()}

    portfolio = Portfolio(cash=initial_capital)
    risk = RiskManager(risk_cfg, initial_capital, KillSwitch(None), logger)

    pending: List[List] = []          # [Order, age_in_bars]
    last_close: Dict[str, float] = {}
    equity_vals: List[float] = []
    fills: List[Fill] = []
    breaches: List[Tuple[pd.Timestamp, str]] = []
    turnover = 0.0
    exposure_bars = 0
    rejected = 0

    def _log(event: str, **kw):
        if logger:
            log_event(logger, event, level=logging.DEBUG, **kw)

    def _execute_sell(symbol: str, ref_price: float, ts: pd.Timestamp, reason: str):
        nonlocal turnover
        pos = portfolio.positions.get(symbol)
        if pos is None:
            return
        price = cost_model.fill_price(ref_price, "sell")
        fee = cost_model.fee(pos.qty * price)
        qty = pos.qty
        portfolio.sell(symbol, price, fee, ts, reason)
        fills.append(Fill(symbol, "sell", qty, price, fee, ts, reason))
        turnover += qty * price
        _log("fill", symbol=symbol, side="sell", qty=qty, price=price, reason=reason)

    for ts in union_index:
        bars_today = {s: pos_of[s].get(ts) for s in dfs}

        # -- 1. execute queued orders at this bar's open ---------------------
        still_pending: List[List] = []
        for order, age in pending:
            j = bars_today.get(order.symbol)
            if j is None:
                if age + 1 < _MAX_ORDER_AGE:
                    still_pending.append([order, age + 1])
                else:
                    _log("order_expired", symbol=order.symbol, side=order.side)
                continue
            open_px = float(dfs[order.symbol]["open"].iloc[j])
            if order.side == "buy":
                if order.symbol in portfolio.positions or risk.trading_halted:
                    continue  # state changed since queuing; drop silently-but-logged
                price = cost_model.fill_price(open_px, "buy")
                fee_rate = cost_model.fee_bps / 1e4
                # Re-clip to cash: the open can differ from the close we sized on.
                qty = min(order.qty, portfolio.cash * 0.999 / (price * (1 + fee_rate)))
                if qty <= 0 or qty * price < risk_cfg.min_order_notional:
                    _log("order_dropped_unaffordable", symbol=order.symbol)
                    continue
                fee = cost_model.fee(qty * price)
                portfolio.buy(order.symbol, qty, price, fee, ts,
                              stop_price=order.stop_price, take_profit=order.take_profit)
                fills.append(Fill(order.symbol, "buy", qty, price, fee, ts, order.reason))
                turnover += qty * price
                _log("fill", symbol=order.symbol, side="buy", qty=qty, price=price, reason=order.reason)
            else:
                _execute_sell(order.symbol, open_px, ts, order.reason)
        pending = still_pending

        # -- 2. intrabar stop / take-profit ----------------------------------
        for symbol in list(portfolio.positions):
            j = bars_today.get(symbol)
            if j is None:
                continue
            pos = portfolio.positions[symbol]
            row = dfs[symbol].iloc[j]
            o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
            exit_ref, reason = None, None
            if pos.stop_price is not None:
                if o <= pos.stop_price:
                    exit_ref, reason = o, "stop_gap"       # gapped through: fill at open
                elif l <= pos.stop_price:
                    exit_ref, reason = pos.stop_price, "stop"
            if exit_ref is None and pos.take_profit is not None:
                if o >= pos.take_profit:
                    exit_ref, reason = o, "take_profit_gap"
                elif h >= pos.take_profit:
                    exit_ref, reason = pos.take_profit, "take_profit"
            if exit_ref is not None:
                _execute_sell(symbol, exit_ref, ts, reason)
                pending = [p for p in pending if p[0].symbol != symbol]

        # -- 3. mark-to-market and risk state ---------------------------------
        for symbol, j in bars_today.items():
            if j is not None:
                last_close[symbol] = float(dfs[symbol]["close"].iloc[j])
        equity = portfolio.equity(last_close)
        equity_vals.append(equity)

        events = risk.on_bar_close(ts, equity)
        if events:
            for e in events:
                breaches.append((ts, e))
                if logger:
                    log_event(logger, "risk_breach", level=logging.WARNING,
                              breach=e, ts=str(ts), equity=round(equity, 2))
            # Purge queued buys; flatten every open position at the next open.
            pending = [p for p in pending if p[0].side == "sell"]
            queued_sells = {p[0].symbol for p in pending}
            for symbol in portfolio.positions:
                if symbol not in queued_sells:
                    pending.append([Order(symbol=symbol, side="sell",
                                          qty=portfolio.positions[symbol].qty,
                                          reason="risk_flatten"), 0])

        # -- 4. strategy decisions for the next bar ---------------------------
        pending_symbols = {p[0].symbol for p in pending}
        for symbol, j in bars_today.items():
            if j is None or symbol in pending_symbols:
                continue
            orders = decide_bar(symbol=symbol, df=dfs[symbol], i=j, portfolio=portfolio,
                                risk=risk, strategy=strategy, cost_model=cost_model,
                                prices=last_close)
            for order in orders:
                ok, why = validate_order(order, portfolio=portfolio, prices=last_close,
                                         risk=risk, cost_model=cost_model,
                                         min_notional=risk_cfg.min_order_notional)
                if ok:
                    pending.append([order, 0])
                    _log("order_queued", symbol=order.symbol, side=order.side,
                         qty=order.qty, reason=order.reason)
                else:
                    rejected += 1
                    _log("order_rejected", symbol=order.symbol, side=order.side, reason=why)

        # -- 5. bookkeeping ----------------------------------------------------
        if portfolio.positions:
            exposure_bars += 1
            for symbol, pos in portfolio.positions.items():
                if bars_today.get(symbol) is not None:
                    pos.bars_held += 1

    final_positions = len(portfolio.positions)
    if liquidate_at_end:
        # Close leftovers at their last close so trade stats cover everything.
        for symbol in list(portfolio.positions):
            _execute_sell(symbol, last_close[symbol], union_index[-1], "end_of_data")

    equity_series = pd.Series(equity_vals, index=union_index, name="equity")
    return BacktestResult(
        equity=equity_series, trades=portfolio.trades, fills=fills, breaches=breaches,
        exposure=exposure_bars / max(len(union_index), 1),
        turnover_notional=turnover, initial_capital=initial_capital,
        rejected_orders=rejected, final_positions=final_positions,
    )
