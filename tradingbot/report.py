"""Paper-vs-backtest divergence report.

The core of the paper-trading protocol: over the SAME date range, does the
paper session look like what the backtest predicts? Large divergence in trade
frequency, average trade or drawdown means the simulation is missing
something (costs, fills, data) — find out what before trusting either number.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from tradingbot.config import AppConfig
from tradingbot.costs import CostModel
from tradingbot.portfolio import Trade

_ROWS = ["total_return", "cagr", "sharpe", "sortino", "max_drawdown", "n_trades",
         "win_rate", "profit_factor", "avg_trade_pct", "avg_bars_held", "exposure"]
_PCT = {"total_return", "cagr", "max_drawdown", "win_rate", "avg_trade_pct", "exposure"}


def load_paper_history(state_dir: Path, prefix: str = "paper"
                       ) -> Tuple[Optional[pd.Series], List[Trade]]:
    """Read the append-only equity/trade files the runner writes."""
    equity_path = state_dir / f"{prefix}_equity.csv"
    trades_path = state_dir / f"{prefix}_trades.csv"

    equity = None
    if equity_path.exists():
        eq = pd.read_csv(equity_path, parse_dates=["timestamp"])
        if len(eq) > 0:
            equity = pd.Series(eq["equity"].to_numpy(), index=pd.DatetimeIndex(eq["timestamp"]),
                               name="equity")
            equity = equity[~equity.index.duplicated(keep="last")].sort_index()

    trades: List[Trade] = []
    if trades_path.exists():
        tf = pd.read_csv(trades_path, parse_dates=["entry_time", "exit_time"])
        for _, r in tf.iterrows():
            trades.append(Trade(
                symbol=r["symbol"], entry_time=r["entry_time"], exit_time=r["exit_time"],
                entry_price=float(r["entry_price"]), exit_price=float(r["exit_price"]),
                qty=float(r["qty"]), pnl=float(r["pnl"]), fees=float(r["fees"]),
                return_pct=float(r["return_pct"]), bars_held=int(r["bars_held"]),
                exit_reason=str(r["exit_reason"]),
            ))
    return equity, trades


def _fmt(key: str, value: float) -> str:
    if key in _PCT:
        return f"{value:.2%}"
    if key == "n_trades":
        return f"{int(value)}"
    return f"{value:.2f}"


def side_by_side(paper: Dict[str, float], backtest: Dict[str, float]) -> str:
    lines = [f"{'metric':<16} {'paper':>12} {'backtest':>12}",
             f"{'-' * 16} {'-' * 12} {'-' * 12}"]
    for key in _ROWS:
        if key in paper and key in backtest:
            lines.append(f"{key:<16} {_fmt(key, paper[key]):>12} {_fmt(key, backtest[key]):>12}")
    return "\n".join(lines)


def divergence_notes(paper: Dict[str, float], backtest: Dict[str, float],
                     n_paper_trades: int) -> List[str]:
    """Heuristic red flags. Thresholds are deliberately loose — this points a
    finger, the human investigates."""
    notes: List[str] = []
    if n_paper_trades < 30:
        notes.append(f"only {n_paper_trades} paper trades — every comparison below is "
                     "statistically weak; keep collecting before concluding anything")
    bt_n, pp_n = backtest.get("n_trades", 0), paper.get("n_trades", 0)
    if bt_n > 0 and (pp_n < 0.5 * bt_n or pp_n > 2.0 * bt_n):
        notes.append(f"trade count diverges (paper {int(pp_n)} vs backtest {int(bt_n)}): "
                     "check data gaps, downtime, or signal logic drift")
    if paper.get("avg_trade_pct", 0) < backtest.get("avg_trade_pct", 0) - 0.002:
        notes.append("paper average trade is >20bps worse than backtest: real costs likely "
                     "exceed the model — consider raising slippage_bps and re-validating")
    if paper.get("max_drawdown", 0) < backtest.get("max_drawdown", 0) * 1.5:
        notes.append("paper drawdown is much deeper than backtest over the same period: "
                     "investigate before continuing the validation clock")
    if not notes:
        notes.append("no gross divergence detected on these heuristics — keep the clock "
                     "running and re-check weekly")
    return notes


def build_report(cfg: AppConfig, logger: Optional[logging.Logger] = None) -> str:
    """Full text report: paper session vs a backtest of the same period."""
    from tradingbot.backtest.engine import run_backtest
    from tradingbot.backtest.metrics import compute_metrics
    from tradingbot.data.loader import load_history
    from tradingbot.strategies.registry import build_strategy

    state_dir = Path(cfg.live.state_dir)
    equity, trades = load_paper_history(state_dir)
    if equity is None or len(equity) < 2:
        return ("no paper history found in "
                f"{state_dir}/ — run `python -m tradingbot paper` first; the runner "
                "records equity and trades automatically.")

    start, end = equity.index[0], equity.index[-1]
    ppy = cfg.backtest.periods_per_year
    initial = float(equity.iloc[0])
    paper_m = compute_metrics(equity, trades, ppy, initial)

    # Backtest the SAME strategy/config over the SAME window on cached data.
    strategy = build_strategy(cfg.strategy.active, cfg.strategy.active_params())
    dfs = load_history(cfg.data, logger=logger)
    prepared = {}
    for s, df in dfs.items():
        p = strategy.prepare(df)
        prepared[s] = p[(p.index >= start - pd.Timedelta("1h")) & (p.index <= end)]
    prepared = {s: df for s, df in prepared.items() if len(df) > 10}
    if not prepared:
        return "paper history exists but no overlapping market data cached — run fetch-data."
    res = run_backtest(prepared, strategy, cfg.risk, CostModel(**cfg.costs.model_dump()),
                       initial, logger)
    bt_m = compute_metrics(res.equity, res.trades, ppy, initial,
                           res.turnover_notional, res.exposure)

    header = (f"PAPER vs BACKTEST — {cfg.strategy.active} — "
              f"{start.date()} .. {end.date()} ({len(trades)} paper trades)")
    notes = divergence_notes(paper_m, bt_m, len(trades))
    return "\n".join([header, "=" * len(header), "",
                      side_by_side(paper_m, bt_m), "",
                      "Divergence notes:", *[f"  - {n}" for n in notes]])
