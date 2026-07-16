"""Market data: Binance OHLCV download with CSV cache, plus a synthetic
regime-switching generator for offline tests and demos.

Conventions used everywhere in this project:
  - DataFrame index: tz-aware UTC DatetimeIndex of bar OPEN times (ccxt style)
  - columns: open, high, low, close, volume
  - the last, still-forming candle is always dropped (partial bars would be
    a subtle source of look-ahead in live trading)
"""
from __future__ import annotations

import logging
import time
import zlib
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

from tradingbot.config import DataConfig
from tradingbot.execution.retry import with_retries

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
# pandas offset aliases differ from exchange timeframe strings ("1m" would be
# ambiguous with month) — map explicitly.
PANDAS_FREQ = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}

COLUMNS = ["open", "high", "low", "close", "volume"]


def tf_timedelta(timeframe: str) -> pd.Timedelta:
    return pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])


# --------------------------------------------------------------------------
# Synthetic data (offline tests, demos — no network, deterministic)
# --------------------------------------------------------------------------
def generate_synthetic_ohlcv(
    symbol: str,
    n_bars: int = 20_000,
    timeframe: str = "1h",
    seed: int = 7,
    start: str = "2023-01-01",
    start_price: float = 100.0,
) -> pd.DataFrame:
    """Regime-switching random walk: alternating up/down/chop segments.

    Deterministic per (symbol, seed). Useful to exercise the full pipeline
    offline; it says nothing about real-market profitability.
    """
    rng = np.random.default_rng(zlib.crc32(symbol.encode()) ^ seed)
    drift_map = {"up": 0.0006, "down": -0.0004, "chop": 0.0}
    vol_map = {"up": 0.006, "down": 0.010, "chop": 0.004}

    mu, sig = [], []
    while len(mu) < n_bars:
        seg = int(rng.integers(200, 600))
        regime = rng.choice(["up", "down", "chop"], p=[0.40, 0.25, 0.35])
        mu.extend([drift_map[regime]] * seg)
        sig.extend([vol_map[regime]] * seg)
    mu_arr = np.array(mu[:n_bars])
    sig_arr = np.array(sig[:n_bars])

    rets = mu_arr + sig_arr * rng.standard_normal(n_bars)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.standard_normal(n_bars)) * sig_arr * close * 0.7
    high = np.maximum(open_, close) + wick
    low = np.maximum(np.minimum(open_, close) - wick, 1e-9)
    volume = rng.lognormal(mean=10.0, sigma=1.0, size=n_bars)

    idx = pd.date_range(start, periods=n_bars, freq=PANDAS_FREQ[timeframe], tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


# --------------------------------------------------------------------------
# Binance via ccxt (public endpoints; no API key needed for candles)
# --------------------------------------------------------------------------
def make_public_exchange(exchange_id: str = "binance"):
    import ccxt  # imported lazily so offline tests never need it

    klass = getattr(ccxt, exchange_id)
    return klass({"enableRateLimit": True})


def fetch_ohlcv(
    exchange,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> pd.DataFrame:
    """Paginated OHLCV download with retries; drops the still-forming candle."""
    import ccxt

    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    now_ms = int(time.time() * 1000)
    until_ms = until_ms or now_ms
    rows: list = []
    since = since_ms
    while since < until_ms:
        batch = with_retries(
            lambda: exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000),
            retriable=(ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable),
            logger=logger,
        )
        if not batch:
            break
        rows.extend(batch)
        new_since = batch[-1][0] + tf_ms
        if new_since <= since:  # defensive: exchange returned nothing new
            break
        since = new_since
        if len(batch) < 2:
            break

    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    arr = pd.DataFrame(rows, columns=["ts", *COLUMNS])
    arr = arr.drop_duplicates(subset="ts").sort_values("ts")
    # Drop candle whose period has not fully elapsed yet.
    arr = arr[arr["ts"] + tf_ms <= now_ms]
    idx = pd.to_datetime(arr.pop("ts"), unit="ms", utc=True)
    df = arr.set_index(idx)
    df.index.name = "timestamp"
    return df.astype(float)


def _cache_path(cfg: DataConfig, symbol: str) -> Path:
    safe = symbol.replace("/", "-")
    return Path(cfg.cache_dir) / f"{cfg.source}_{safe}_{cfg.timeframe}.csv"


def load_cached_history(cfg: DataConfig) -> Dict[str, pd.DataFrame]:
    """Cache-only variant of load_history: reads whatever is on disk, never
    touches the network. Used by offline consumers (dashboard)."""
    out: Dict[str, pd.DataFrame] = {}
    for symbol in cfg.symbols:
        path = _cache_path(cfg, symbol)
        if path.exists():
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            if len(df) > 0:
                out[symbol] = df
    return out


def load_history(
    cfg: DataConfig,
    synthetic: bool = False,
    history_days: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, pd.DataFrame]:
    """Load per-symbol history. Binance source is cached to CSV and topped up
    incrementally on each call."""
    days = history_days or cfg.history_days
    n_bars = int(days * 86400 / TIMEFRAME_SECONDS[cfg.timeframe])

    if synthetic or cfg.source == "synthetic":
        return {s: generate_synthetic_ohlcv(s, n_bars=n_bars, timeframe=cfg.timeframe) for s in cfg.symbols}

    exchange = make_public_exchange("binance")
    out: Dict[str, pd.DataFrame] = {}
    since_ms = int(time.time() * 1000) - days * 86_400_000
    Path(cfg.cache_dir).mkdir(parents=True, exist_ok=True)

    for symbol in cfg.symbols:
        path = _cache_path(cfg, symbol)
        cached = None
        if path.exists():
            cached = pd.read_csv(path, index_col=0, parse_dates=True)
            cached.index = pd.to_datetime(cached.index, utc=True)
        fetch_from = since_ms
        if cached is not None and len(cached) > 0:
            fetch_from = int(cached.index[-1].timestamp() * 1000) + TIMEFRAME_SECONDS[cfg.timeframe] * 1000

        fresh = fetch_ohlcv(exchange, symbol, cfg.timeframe, fetch_from, logger=logger)
        df = pd.concat([cached, fresh]) if cached is not None else fresh
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df[df.index >= pd.Timestamp(since_ms, unit="ms", tz="UTC")]
        if len(df) == 0:
            raise RuntimeError(f"no data returned for {symbol} — check symbol name / connectivity")
        df.to_csv(path)
        if logger:
            logger.info("loaded %s: %d bars (%s .. %s)", symbol, len(df), df.index[0], df.index[-1])
        out[symbol] = df
    return out
