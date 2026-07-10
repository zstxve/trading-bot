"""Paper/live trading loop.

Runs the SAME decision pipeline as the backtester once per completed bar:
fetch candles (dropping the still-forming one), prepare indicators, check
risk, decide, validate, execute through the broker adapter. Between bar
closes it polls tickers to enforce stop-losses.

Safety behaviour:
  - paper mode is the default; the live broker refuses to construct unless
    all three live gates pass (see execution/binance_live.py)
  - state (cash, positions, risk highs) is persisted to JSON after every
    cycle, so a restart resumes instead of forgetting its positions
  - kill switch file (state/KILL_SWITCH): engaged automatically on max
    drawdown, repeated API errors or stale data; the loop refuses to trade
    while the file exists — deleting it is a deliberate manual act
  - SIGINT/SIGTERM: finish the current step, persist state, exit cleanly
"""
from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from tradingbot.alerts import Notifier
from tradingbot.config import AppConfig
from tradingbot.costs import CostModel
from tradingbot.data.loader import TIMEFRAME_SECONDS, fetch_ohlcv, make_public_exchange, tf_timedelta
from tradingbot.execution.base import Fill, Order
from tradingbot.execution.paper import PaperBroker
from tradingbot.execution.retry import with_retries
from tradingbot.execution.validation import validate_order
from tradingbot.logging_setup import log_event
from tradingbot.pipeline import decide_bar
from tradingbot.portfolio import Portfolio, Position
from tradingbot.risk.limits import KillSwitch, RiskManager
from tradingbot.strategies.registry import build_strategy


class LiveRunner:
    def __init__(self, cfg: AppConfig, logger: logging.Logger, live_mode: bool = False,
                 cli_live_flag: bool = False):
        if cfg.data.source == "synthetic":
            raise ValueError("paper/live trading needs data.source: binance (synthetic is backtest-only)")
        self.cfg = cfg
        self.log = logger
        self.live_mode = live_mode
        self.cost = CostModel(**cfg.costs.model_dump())
        self.strategy = build_strategy(cfg.strategy.active, cfg.strategy.active_params())
        self.tf = cfg.data.timeframe
        self.tf_seconds = TIMEFRAME_SECONDS[self.tf]

        state_dir = Path(cfg.live.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        prefix = "live" if live_mode else "paper"
        self.state_path = state_dir / f"{prefix}_state.json"
        self.trades_csv = state_dir / f"{prefix}_trades.csv"
        self.equity_csv = state_dir / f"{prefix}_equity.csv"
        self.kill = KillSwitch(state_dir / "KILL_SWITCH")
        self.notifier = Notifier(cfg.alerts, logger)
        self._kill_was_engaged = self.kill.engaged()
        self._summary_day = None
        self._trades_today = 0

        self.portfolio, risk_state = self._load_state()
        prices = {}
        self.risk = RiskManager(cfg.risk, self.portfolio.equity(prices), self.kill, logger)
        if risk_state:
            self.risk.peak_equity = risk_state.get("peak_equity", self.risk.peak_equity)
            self.risk.day_start_equity = risk_state.get("day_start_equity", self.risk.day_start_equity)

        if live_mode:
            from tradingbot.execution.binance_live import BinanceLiveBroker
            self.broker = BinanceLiveBroker(cfg, cli_live_flag, self.cost, logger)
            self.log.warning("LIVE MODE: real orders WILL be sent to Binance. "
                             "Local position state is authoritative for this bot; "
                             "reconcile with the exchange before and after each session.")
        else:
            self.broker = PaperBroker(self.cost, logger)

        self.data_exchange = make_public_exchange("binance")
        self._stop = False
        signal.signal(signal.SIGINT, self._request_stop)
        signal.signal(signal.SIGTERM, self._request_stop)

    # ------------------------------------------------------------------ state
    def _request_stop(self, signum, frame) -> None:
        self.log.info("shutdown signal received — finishing current step")
        self._stop = True

    def _load_state(self):
        if not self.state_path.exists():
            return Portfolio(cash=self.cfg.backtest.initial_capital), None
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        pf = Portfolio(cash=raw["cash"])
        for p in raw.get("positions", []):
            pf.positions[p["symbol"]] = Position(
                symbol=p["symbol"], qty=p["qty"], entry_price=p["entry_price"],
                entry_time=pd.Timestamp(p["entry_time"]),
                stop_price=p.get("stop_price"), take_profit=p.get("take_profit"),
                entry_fee=p.get("entry_fee", 0.0), bars_held=p.get("bars_held", 0),
            )
        self.log.info("resumed state: cash=%.2f positions=%d", pf.cash, len(pf.positions))
        return pf, raw.get("risk")

    def _persist_state(self) -> None:
        payload = {
            "cash": self.portfolio.cash,
            "positions": [
                {"symbol": p.symbol, "qty": p.qty, "entry_price": p.entry_price,
                 "entry_time": p.entry_time.isoformat(), "stop_price": p.stop_price,
                 "take_profit": p.take_profit, "entry_fee": p.entry_fee,
                 "bars_held": p.bars_held}
                for p in self.portfolio.positions.values()
            ],
            "risk": {"peak_equity": self.risk.peak_equity,
                     "day_start_equity": self.risk.day_start_equity},
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)  # atomic on POSIX

    def _append_csv(self, path: Path, header: str, row: str) -> None:
        """Append-only history files (trades, equity) for reports/dashboard."""
        new = not path.exists()
        with path.open("a", encoding="utf-8") as fh:
            if new:
                fh.write(header + "\n")
            fh.write(row + "\n")

    def _check_kill_transition(self) -> None:
        """Alert exactly once when the kill switch flips on."""
        engaged = self.kill.engaged()
        if engaged and not self._kill_was_engaged:
            self.notifier.send(f"🔴 KILL SWITCH ENGAGED: {self.kill.reason()}\n"
                               f"Trading halted. Investigate, then delete {self.kill.path}.")
        self._kill_was_engaged = engaged

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        mode = "LIVE" if self.live_mode else "PAPER"
        log_event(self.log, "runner_start", mode=mode,
                  strategy=self.strategy.name, symbols=",".join(self.cfg.data.symbols),
                  timeframe=self.tf, cash=round(self.portfolio.cash, 2))
        self.notifier.send(f"▶️ {mode} runner started: {self.strategy.name} on "
                           f"{', '.join(self.cfg.data.symbols)} ({self.tf})")
        if self.kill.engaged():
            self.log.error("kill switch engaged (%s) — refusing to start. "
                           "Delete %s after investigating.", self.kill.reason(), self.kill.path)
            return
        try:
            while not self._stop:
                self._sleep_until_next_bar()
                if self._stop or self.kill.engaged():
                    break
                try:
                    self._on_bar()
                    self.risk.on_api_success()
                except Exception as exc:  # noqa: BLE001 — loop must survive bad cycles
                    self.log.exception("bar cycle failed: %s", exc)
                    self.risk.on_api_error()
                self._check_kill_transition()
                self._persist_state()
        finally:
            self.broker.cancel_all()
            self._persist_state()
            log_event(self.log, "runner_stop", cash=round(self.portfolio.cash, 2),
                      open_positions=len(self.portfolio.positions),
                      kill_switch=self.kill.engaged())
            self.notifier.send(f"⏹ runner stopped. cash={self.portfolio.cash:.2f} "
                               f"open_positions={len(self.portfolio.positions)} "
                               f"kill_switch={self.kill.engaged()}")

    def _sleep_until_next_bar(self) -> None:
        """Sleep until just after the next bar close, polling stops meanwhile."""
        target = ((time.time() // self.tf_seconds) + 1) * self.tf_seconds \
            + self.cfg.live.bar_close_buffer_seconds
        last_poll = 0.0
        while not self._stop and time.time() < target:
            now = time.time()
            if now - last_poll >= self.cfg.live.poll_seconds and self.portfolio.positions:
                last_poll = now
                try:
                    self._poll_stops()
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("stop poll failed: %s", exc)
                    self.risk.on_api_error()
            time.sleep(min(1.0, max(0.0, target - time.time())))

    def _poll_stops(self) -> None:
        """Between bar closes: exit any position whose stop has been pierced."""
        import ccxt
        for symbol, pos in list(self.portfolio.positions.items()):
            if pos.stop_price is None:
                continue
            ticker = with_retries(
                lambda s=symbol: self.data_exchange.fetch_ticker(s),
                retriable=(ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable),
                logger=self.log,
            )
            last = float(ticker.get("last") or 0)
            if last and last <= pos.stop_price:
                log_event(self.log, "stop_triggered_poll", symbol=symbol,
                          last=last, stop=pos.stop_price)
                order = Order(symbol=symbol, side="sell", qty=pos.qty, reason="stop_poll")
                self._execute(order, ref_price=last)

    # ------------------------------------------------------------------ bar step
    def _fetch_prepared(self) -> Dict[str, pd.DataFrame]:
        need = self.strategy.warmup_bars() + 50
        since_ms = int(time.time() * 1000) - need * self.tf_seconds * 1000
        out = {}
        now = pd.Timestamp.now(tz="UTC")
        for symbol in self.cfg.data.symbols:
            raw = fetch_ohlcv(self.data_exchange, symbol, self.tf, since_ms, logger=self.log)
            if len(raw) == 0:
                raise RuntimeError(f"no candles returned for {symbol}")
            newest_close = raw.index[-1] + tf_timedelta(self.tf)
            if not self.risk.check_data_fresh(newest_close, now, tf_timedelta(self.tf)):
                raise RuntimeError(f"stale data for {symbol} — kill switch engaged")
            out[symbol] = self.strategy.prepare(raw)
        return out

    def _on_bar(self) -> None:
        dfs = self._fetch_prepared()
        prices = {s: float(df["close"].iloc[-1]) for s, df in dfs.items()}
        ts = max(df.index[-1] for df in dfs.values()) + tf_timedelta(self.tf)

        # Bar-close stop check (poll may have missed a spike inside the bar).
        for symbol, pos in list(self.portfolio.positions.items()):
            low = float(dfs[symbol]["low"].iloc[-1])
            if pos.stop_price is not None and low <= pos.stop_price:
                self._execute(Order(symbol=symbol, side="sell", qty=pos.qty,
                                    reason="stop_bar_close"), ref_price=prices[symbol])

        equity = self.portfolio.equity(prices)
        events = self.risk.on_bar_close(ts, equity)
        for e in events:
            log_event(self.log, "risk_breach", level=logging.WARNING, breach=e,
                      equity=round(equity, 2))
            self.notifier.send(f"⚠️ risk breach: {e} at equity {equity:.2f}")
        if events:
            for symbol in list(self.portfolio.positions):
                self._execute(Order(symbol=symbol, side="sell",
                                    qty=self.portfolio.positions[symbol].qty,
                                    reason="risk_flatten"), ref_price=prices[symbol])

        for symbol, df in dfs.items():
            pos_before = symbol in self.portfolio.positions
            orders = decide_bar(symbol=symbol, df=df, i=len(df) - 1,
                                portfolio=self.portfolio, risk=self.risk,
                                strategy=self.strategy, cost_model=self.cost, prices=prices)
            for order in orders:
                log_event(self.log, "signal", symbol=symbol, side=order.side,
                          reason=order.reason, had_position=pos_before)
                self._execute(order, ref_price=prices[symbol])

        log_event(self.log, "portfolio_update", equity=round(equity, 2),
                  cash=round(self.portfolio.cash, 2),
                  positions={s: round(p.qty, 8) for s, p in self.portfolio.positions.items()},
                  halted=self.risk.trading_halted)
        self._append_csv(self.equity_csv, "timestamp,equity,cash,n_positions",
                         f"{ts.isoformat()},{equity:.2f},{self.portfolio.cash:.2f},"
                         f"{len(self.portfolio.positions)}")

        # Daily summary on UTC day rollover.
        day = ts.date()
        if self._summary_day is None:
            self._summary_day = day
        elif day != self._summary_day:
            if self.cfg.alerts.daily_summary:
                pos_txt = ", ".join(f"{s} @{p.entry_price:.2f}"
                                    for s, p in self.portfolio.positions.items()) or "none"
                self.notifier.send(
                    f"📊 daily summary {self._summary_day}: equity {equity:.2f}, "
                    f"cash {self.portfolio.cash:.2f}, trades closed {self._trades_today}, "
                    f"positions: {pos_txt}, halted={self.risk.trading_halted}"
                )
            self._summary_day = day
            self._trades_today = 0

        # Keep the read-only dashboard current (best-effort, never fatal).
        try:
            from tradingbot.dashboard import generate_dashboard
            generate_dashboard(self.cfg, prefix="live" if self.live_mode else "paper")
        except Exception as exc:  # noqa: BLE001
            self.log.debug("dashboard regeneration failed: %s", exc)

    def _execute(self, order: Order, ref_price: float) -> Optional[Fill]:
        prices = {order.symbol: ref_price}
        # include marks for all held symbols so exposure checks are correct
        for s, p in self.portfolio.positions.items():
            prices.setdefault(s, p.entry_price)
        ok, why = validate_order(order, portfolio=self.portfolio, prices=prices,
                                 risk=self.risk, cost_model=self.cost,
                                 min_notional=self.cfg.risk.min_order_notional)
        if not ok:
            log_event(self.log, "order_rejected", symbol=order.symbol, side=order.side,
                      qty=order.qty, reason=why)
            return None
        ts = pd.Timestamp.now(tz="UTC")
        fill = self.broker.submit_market(order, ref_price, ts)
        if fill.side == "buy":
            self.portfolio.buy(fill.symbol, fill.qty, fill.price, fill.fee, fill.ts,
                               stop_price=order.stop_price, take_profit=order.take_profit)
        else:
            trade = self.portfolio.sell(fill.symbol, fill.price, fill.fee, fill.ts, order.reason)
            log_event(self.log, "trade_closed", symbol=trade.symbol,
                      pnl=round(trade.pnl, 2), return_pct=round(trade.return_pct * 100, 3),
                      reason=trade.exit_reason)
            self._trades_today += 1
            self._append_csv(
                self.trades_csv,
                "symbol,entry_time,exit_time,entry_price,exit_price,qty,pnl,fees,"
                "return_pct,bars_held,exit_reason",
                f"{trade.symbol},{trade.entry_time.isoformat()},{trade.exit_time.isoformat()},"
                f"{trade.entry_price},{trade.exit_price},{trade.qty},{trade.pnl},{trade.fees},"
                f"{trade.return_pct},{trade.bars_held},{trade.exit_reason}",
            )
            if self.cfg.alerts.notify_trades:
                emoji = "🟢" if trade.pnl >= 0 else "🔻"
                self.notifier.send(f"{emoji} closed {trade.symbol}: pnl {trade.pnl:+.2f} "
                                   f"({trade.return_pct:+.2%}), reason {trade.exit_reason}")
        return fill
