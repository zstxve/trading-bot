"""Transaction cost model.

Assumptions (all configurable in config.yaml):
  - fee_bps:        exchange taker fee per side (Binance spot: 10 bps)
  - slippage_bps:   adverse market impact per side
  - half_spread_bps: you cross half the bid/ask spread per side

Latency is modelled structurally, not here: signals computed on a bar close
are executed at the NEXT bar open. Nothing in the system fills on the same
bar a signal is generated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CostModel:
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    half_spread_bps: float = 2.0
    # Short carry (borrow interest / funding proxy), bps of notional per day.
    short_borrow_bps_per_day: float = 3.0

    @property
    def adverse_bps(self) -> float:
        """Price degradation per side, excluding the explicit fee."""
        return self.slippage_bps + self.half_spread_bps

    @property
    def total_side_bps(self) -> float:
        """Everything you pay per side, in bps of notional."""
        return self.fee_bps + self.adverse_bps

    def fill_price(self, ref_price: float, side: Literal["buy", "sell"]) -> float:
        """Executed price: reference price degraded against you."""
        adj = self.adverse_bps / 1e4
        return ref_price * (1 + adj) if side == "buy" else ref_price * (1 - adj)

    def fee(self, notional: float) -> float:
        return abs(notional) * self.fee_bps / 1e4
