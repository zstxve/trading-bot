"""Strategy interface.

Contract (enforced by the engine and verified by tests):
  - `prepare(df)` may only add columns computed with CAUSAL transforms.
  - `entry_signal(df, i)` / `exit_signal(df, i, pos)` may only read rows <= i.
  - Signals generated at bar i are executed by the engine at bar i+1's open.
    A strategy never fills its own orders.
  - Any NaN in required indicator columns must yield "no signal".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Dict, Optional

import pandas as pd

from tradingbot.portfolio import Position


@dataclass(frozen=True)
class EntrySignal:
    stop_price: float                 # mandatory: no entry without a stop
    take_profit: Optional[float]
    reason: str
    side: str = "long"                # "long" | "short" (short needs risk.allow_short)


class Strategy(ABC):
    name: ClassVar[str] = "base"

    def __init__(self, params: Optional[dict] = None):
        self.params: Dict = {**self.default_params(), **(params or {})}
        self.validate_params()

    # ---- parameterisation --------------------------------------------------
    @classmethod
    @abstractmethod
    def default_params(cls) -> dict: ...

    @classmethod
    def param_grid(cls) -> Dict[str, list]:
        """Default search space for walk-forward optimisation (keep SMALL)."""
        return {}

    def validate_params(self) -> None:
        """Raise ValueError on inconsistent params (e.g. fast >= slow)."""

    # ---- signal generation ---------------------------------------------
    @abstractmethod
    def warmup_bars(self) -> int:
        """Bars of history needed before indicators are meaningful."""

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of `df` with indicator columns added (causal only)."""

    @abstractmethod
    def entry_signal(self, df: pd.DataFrame, i: int) -> Optional[EntrySignal]:
        """Signal to open a long at bar i's close (executed next bar open)."""

    @abstractmethod
    def exit_signal(self, df: pd.DataFrame, i: int, position: Position) -> Optional[str]:
        """Reason string to close the position, or None to hold."""

    def trail_stop(self, df: pd.DataFrame, i: int, position: Position) -> Optional[float]:
        """Optional new stop level; engine only ever RAISES the stop."""
        return None
