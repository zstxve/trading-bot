"""Configuration loading and validation.

All tunables live in config.yaml. Secrets never live in config — API keys
come from environment variables (see .env.example). Every model forbids
unknown keys, so a typo in config.yaml fails at startup instead of being
silently ignored.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Strategy parameter blocks
# --------------------------------------------------------------------------
class TrendParams(StrictModel):
    fast_ema: int = Field(20, ge=2)
    slow_ema: int = Field(50, ge=3)
    regime_timeframe: Literal["4h", "1d"] = "4h"
    regime_ema: int = Field(50, ge=2)
    atr_n: int = Field(14, ge=2)
    atr_stop_mult: float = Field(3.0, gt=0)
    trail_stop: bool = True
    atr_pct_min: float = Field(0.003, ge=0.0)
    atr_pct_max: float = Field(0.08, gt=0.0)

    @model_validator(mode="after")
    def _cross_checks(self) -> "TrendParams":
        if self.fast_ema >= self.slow_ema:
            raise ValueError("fast_ema must be < slow_ema")
        if self.atr_pct_min >= self.atr_pct_max:
            raise ValueError("atr_pct_min must be < atr_pct_max")
        return self


class MeanRevParams(StrictModel):
    rsi_n: int = Field(14, ge=2)
    rsi_buy: float = Field(30.0, gt=0, lt=50)
    rsi_exit: float = Field(55.0, gt=0, lt=100)
    bb_n: int = Field(20, ge=5)
    bb_k: float = Field(2.0, gt=0)
    regime_timeframe: Literal["4h", "1d"] = "4h"
    regime_ema: int = Field(50, ge=2)
    atr_n: int = Field(14, ge=2)
    atr_stop_mult: float = Field(2.0, gt=0)
    max_hold_bars: int = Field(48, ge=1)

    @model_validator(mode="after")
    def _cross_checks(self) -> "MeanRevParams":
        if self.rsi_exit <= self.rsi_buy:
            raise ValueError("rsi_exit must be > rsi_buy")
        return self


class StrategySection(StrictModel):
    active: Literal["trend_ma_cross", "meanrev_rsi_bb"] = "trend_ma_cross"
    trend_ma_cross: TrendParams = TrendParams()
    meanrev_rsi_bb: MeanRevParams = MeanRevParams()

    def active_params(self) -> dict:
        return getattr(self, self.active).model_dump()


# --------------------------------------------------------------------------
# Other sections
# --------------------------------------------------------------------------
class DataConfig(StrictModel):
    source: Literal["binance", "synthetic"] = "binance"
    symbols: List[str] = Field(min_length=1)
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1h"
    history_days: int = Field(730, ge=30)
    cache_dir: str = "data_cache"


class RiskConfig(StrictModel):
    # risk_per_trade capped at 2%: anything above that is gambling, not sizing.
    risk_per_trade: float = Field(0.005, gt=0, le=0.02)
    max_drawdown: float = Field(0.15, gt=0, le=0.5)
    daily_loss_limit: float = Field(0.02, gt=0, le=0.1)
    max_open_positions: int = Field(4, ge=1)
    max_asset_exposure: float = Field(0.25, gt=0, le=1.0)
    max_total_exposure: float = Field(0.80, gt=0, le=1.0)
    leverage: float = Field(1.0, ge=1.0)
    allow_leverage: bool = False
    min_order_notional: float = Field(10.0, ge=0)
    max_consecutive_api_errors: int = Field(5, ge=1)
    stale_data_factor: float = Field(2.0, gt=1.0)

    @model_validator(mode="after")
    def _no_accidental_leverage(self) -> "RiskConfig":
        if self.leverage > 1.0 and not self.allow_leverage:
            raise ValueError(
                "leverage > 1.0 requires allow_leverage: true. "
                "Spot long-only (leverage: 1.0) is the intended default."
            )
        return self


class CostConfig(StrictModel):
    fee_bps: float = Field(10.0, ge=0)
    slippage_bps: float = Field(5.0, ge=0)
    half_spread_bps: float = Field(2.0, ge=0)


class BacktestConfig(StrictModel):
    initial_capital: float = Field(10_000.0, gt=0)
    periods_per_year: int = Field(8760, gt=0)
    oos_fraction: float = Field(0.2, ge=0.0, le=0.5)


class WalkForwardConfig(StrictModel):
    train_bars: int = Field(4320, ge=200)
    test_bars: int = Field(1440, ge=50)
    min_trades: int = Field(8, ge=1)
    # strategy name -> {param name -> list of candidate values}
    grids: Dict[str, Dict[str, list]] = {}


class LiveConfig(StrictModel):
    enabled: bool = False
    exchange: str = "binance"
    poll_seconds: int = Field(60, ge=5)
    bar_close_buffer_seconds: int = Field(10, ge=1)
    state_dir: str = "state"


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    dir: str = "logs"
    json_logs: bool = False


class AppConfig(StrictModel):
    mode: Literal["backtest", "paper", "live"] = "paper"
    data: DataConfig
    strategy: StrategySection = StrategySection()
    risk: RiskConfig = RiskConfig()
    costs: CostConfig = CostConfig()
    backtest: BacktestConfig = BacktestConfig()
    walkforward: WalkForwardConfig = WalkForwardConfig()
    live: LiveConfig = LiveConfig()
    logging: LoggingConfig = LoggingConfig()


def load_config(path: str | Path) -> AppConfig:
    """Load and validate config.yaml. Raises on missing file / invalid keys."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"config file {p} must contain a YAML mapping")
    return AppConfig(**raw)
