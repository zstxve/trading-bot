"""Performance metrics and the buy-and-hold benchmark.

All ratios are annualized from bar returns using periods_per_year (8760 for
1h crypto bars). Sharpe/Sortino assume a 0% risk-free rate.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from tradingbot.costs import CostModel
from tradingbot.portfolio import Trade


def sharpe_ratio(equity: pd.Series, periods_per_year: int) -> float:
    rets = equity.pct_change().dropna()
    if len(rets) < 2 or rets.std(ddof=1) == 0:
        return 0.0
    return float(rets.mean() / rets.std(ddof=1) * math.sqrt(periods_per_year))


def compute_metrics(
    equity: pd.Series,
    trades: List[Trade],
    periods_per_year: int,
    initial_capital: float,
    turnover_notional: float = 0.0,
    exposure: float = 0.0,
) -> Dict[str, float]:
    if len(equity) < 2:
        return {"error": 1.0}

    end_value = float(equity.iloc[-1])
    years = len(equity) / periods_per_year
    total_return = end_value / initial_capital - 1.0
    cagr = (end_value / initial_capital) ** (1 / years) - 1.0 if years > 0 and end_value > 0 else float("nan")

    rets = equity.pct_change().dropna()
    sharpe = sharpe_ratio(equity, periods_per_year)
    downside = rets.clip(upper=0.0)
    downside_dev = float(np.sqrt((downside ** 2).mean()))
    sortino = float(rets.mean() / downside_dev * math.sqrt(periods_per_year)) if downside_dev > 0 else 0.0

    dd = equity / equity.cummax() - 1.0
    max_drawdown = float(dd.min())

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    avg_equity = float(equity.mean())
    turnover_per_year = turnover_notional / avg_equity / years if avg_equity > 0 and years > 0 else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "n_trades": float(len(trades)),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": profit_factor,
        "avg_trade_pct": float(np.mean([t.return_pct for t in trades])) if trades else 0.0,
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])) if trades else 0.0,
        "total_fees": float(sum(t.fees for t in trades)),
        "exposure": exposure,
        "turnover_per_year": turnover_per_year,
        "end_equity": end_value,
        "years": years,
    }


_PCT = {"total_return", "cagr", "max_drawdown", "win_rate", "avg_trade_pct", "exposure"}
_ORDER = [
    "total_return", "cagr", "sharpe", "sortino", "max_drawdown", "n_trades",
    "win_rate", "profit_factor", "avg_trade_pct", "avg_bars_held",
    "exposure", "turnover_per_year", "total_fees", "end_equity", "years",
]


def format_metrics_table(title: str, m: Dict[str, float]) -> str:
    lines = [title, "-" * len(title)]
    for key in _ORDER:
        if key not in m:
            continue
        v = m[key]
        if key in _PCT:
            lines.append(f"{key:<18} {v:>12.2%}")
        elif key == "n_trades":
            lines.append(f"{key:<18} {int(v):>12d}")
        else:
            lines.append(f"{key:<18} {v:>12.2f}")
    return "\n".join(lines)


def benchmark_equity(
    dfs: Dict[str, pd.DataFrame],
    initial_capital: float,
    cost_model: CostModel,
) -> Optional[pd.Series]:
    """Equal-weight buy-and-hold of all symbols, bought once at the first
    common bar (entry costs charged), never rebalanced."""
    dfs = {s: df for s, df in dfs.items() if len(df) > 0}
    if not dfs:
        return None
    start = max(df.index[0] for df in dfs.values())
    closes = pd.DataFrame({s: df["close"] for s, df in dfs.items()})
    closes = closes[closes.index >= start].ffill().dropna(how="any")
    if len(closes) < 2:
        return None

    alloc = initial_capital / len(closes.columns)
    fee_rate = cost_model.fee_bps / 1e4
    qty = {}
    for s in closes.columns:
        entry = cost_model.fill_price(float(closes[s].iloc[0]), "buy")
        qty[s] = alloc / (entry * (1 + fee_rate))
    equity = sum(closes[s] * qty[s] for s in closes.columns)
    equity.name = "benchmark"
    return equity


def trades_to_df(trades: List[Trade]) -> pd.DataFrame:
    cols = ["symbol", "entry_time", "exit_time", "entry_price", "exit_price",
            "qty", "pnl", "fees", "return_pct", "bars_held", "exit_reason"]
    if not trades:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{c: getattr(t, c) for c in cols} for t in trades])
