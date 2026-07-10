"""Portfolio-level risk limits and the kill switch.

RiskManager is the single authority on "are we allowed to trade right now":
  - hard max drawdown  -> permanent halt + kill switch (manual reset required)
  - daily loss limit   -> entry halt for the rest of the UTC day
  - exposure caps, max open positions, leverage cap
  - consecutive API errors / stale data -> kill switch (live/paper loop)

Halting NEVER blocks risk-reducing orders: sells always remain allowed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

import pandas as pd

from tradingbot.config import RiskConfig
from tradingbot.portfolio import Portfolio


class KillSwitch:
    """Latch that disables trading. File-backed when `path` is given, so the
    halt survives restarts; removing the file is a deliberate manual act."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self._engaged = False
        self._reason = ""

    def engaged(self) -> bool:
        if self._engaged:
            return True
        return bool(self.path and self.path.exists())

    def reason(self) -> str:
        if self._reason:
            return self._reason
        if self.path and self.path.exists():
            return self.path.read_text(encoding="utf-8").strip()
        return ""

    def engage(self, reason: str) -> None:
        self._engaged = True
        self._reason = reason
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat()
            self.path.write_text(f"{stamp} {reason}\n", encoding="utf-8")


class RiskManager:
    def __init__(self, cfg: RiskConfig, initial_equity: float, kill_switch: Optional[KillSwitch] = None,
                 logger: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.kill_switch = kill_switch or KillSwitch(None)
        self.log = logger
        self.peak_equity = initial_equity
        self.day_start_equity = initial_equity
        self._day = None
        self.daily_halted = False
        self.hard_halted = False
        self._consecutive_api_errors = 0

    # ---- state queries ---------------------------------------------------
    @property
    def trading_halted(self) -> bool:
        """True = no NEW entries. Exits/flattening always remain allowed."""
        return self.hard_halted or self.daily_halted or self.kill_switch.engaged()

    # ---- per-bar checks --------------------------------------------------
    def on_bar_close(self, ts: pd.Timestamp, equity: float) -> List[str]:
        """Update drawdown/daily state; return NEWLY tripped breach events."""
        events: List[str] = []
        day = ts.tz_convert("UTC").date() if ts.tzinfo else ts.date()
        if self._day is None or day != self._day:
            self._day = day
            self.day_start_equity = equity
            self.daily_halted = False  # daily halt resets at UTC midnight

        self.peak_equity = max(self.peak_equity, equity)
        drawdown = equity / self.peak_equity - 1.0
        if not self.hard_halted and drawdown <= -self.cfg.max_drawdown:
            self.hard_halted = True
            self.kill_switch.engage(f"max_drawdown breached: {drawdown:.2%}")
            events.append("max_drawdown")

        daily = equity / self.day_start_equity - 1.0 if self.day_start_equity > 0 else 0.0
        if not self.daily_halted and daily <= -self.cfg.daily_loss_limit:
            self.daily_halted = True
            events.append("daily_loss_limit")

        return events

    # ---- pre-trade gate ----------------------------------------------------
    def can_open(self, symbol: str, add_notional: float, portfolio: Portfolio,
                 prices: Mapping[str, float]) -> Tuple[bool, str]:
        if self.trading_halted:
            return False, "trading_halted"
        equity = portfolio.equity(prices)
        if equity <= 0:
            return False, "no_equity"
        if symbol not in portfolio.positions and len(portfolio.positions) >= self.cfg.max_open_positions:
            return False, "max_open_positions"
        if (portfolio.notional(symbol, prices) + add_notional) / equity > self.cfg.max_asset_exposure + 1e-9:
            return False, "max_asset_exposure"
        total = portfolio.total_notional(prices) + add_notional
        if total / equity > self.cfg.max_total_exposure + 1e-9:
            return False, "max_total_exposure"
        if total > equity * self.cfg.leverage + 1e-9:
            return False, "leverage_cap"
        return True, "ok"

    # ---- operational failures (live/paper loop) ---------------------------
    def on_api_error(self) -> None:
        self._consecutive_api_errors += 1
        if self._consecutive_api_errors >= self.cfg.max_consecutive_api_errors:
            self.kill_switch.engage(
                f"{self._consecutive_api_errors} consecutive API errors"
            )

    def on_api_success(self) -> None:
        self._consecutive_api_errors = 0

    def check_data_fresh(self, newest_bar_close: pd.Timestamp, now: pd.Timestamp,
                         timeframe_delta: pd.Timedelta) -> bool:
        """Engage kill switch if data is stale (feed died, exchange outage)."""
        if now - newest_bar_close > self.cfg.stale_data_factor * timeframe_delta:
            self.kill_switch.engage(
                f"stale data: newest bar close {newest_bar_close}, now {now}"
            )
            return False
        return True
