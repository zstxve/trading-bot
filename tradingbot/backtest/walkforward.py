"""Walk-forward optimisation and out-of-sample stitching.

Rolling scheme: optimise parameters on `train_bars`, trade the following
`test_bars` with the chosen (frozen) parameters, roll forward by `test_bars`,
repeat. Only the stitched TEST segments count as out-of-sample performance.

Overfitting guardrails baked in:
  - small grids (config) — every extra parameter combination is another
    lottery ticket for finding noise
  - parameter sets with < min_trades on the train window are rejected
  - the chosen-params-per-window table is reported: healthy systems pick
    similar parameters across windows; flip-flopping = fitting noise
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type

import pandas as pd

from tradingbot.backtest.engine import run_backtest
from tradingbot.backtest.metrics import sharpe_ratio
from tradingbot.config import RiskConfig, WalkForwardConfig
from tradingbot.costs import CostModel
from tradingbot.portfolio import Trade
from tradingbot.strategies.base import Strategy

OVERFITTING_WARNING = """
+---------------------------------------------------------------------------+
| OVERFITTING WARNING                                                        |
| The best in-sample parameters are, in part, fitted to noise. Expect the   |
| out-of-sample numbers below to be WORSE than any in-sample backtest, and  |
| expect live results to be worse than out-of-sample. If chosen parameters  |
| jump around between windows, the edge is probably not real. Never widen   |
| the grid until something 'works' — that is curve-fitting with extra steps.|
+---------------------------------------------------------------------------+
"""


@dataclass
class WindowResult:
    index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    params: dict
    train_sharpe: float
    train_trades: int
    test_sharpe: float
    test_trades: int
    test_return: float
    fallback_params: bool = False


@dataclass
class WalkForwardResult:
    windows: List[WindowResult]
    oos_equity: pd.Series
    oos_trades: List[Trade] = field(default_factory=list)
    oos_exposure: float = 0.0
    oos_turnover: float = 0.0

    def params_table(self) -> str:
        rows = ["win  train_sharpe  test_sharpe  test_trades  params"]
        for w in self.windows:
            tag = " (fallback)" if w.fallback_params else ""
            rows.append(f"{w.index:>3}  {w.train_sharpe:>12.2f}  {w.test_sharpe:>11.2f}"
                        f"  {w.test_trades:>11d}  {w.params}{tag}")
        distinct = len({tuple(sorted(w.params.items())) for w in self.windows})
        rows.append(f"\nDistinct parameter sets chosen: {distinct}/{len(self.windows)}"
                    " (fewer = more stable = more trustworthy)")
        return "\n".join(rows)


def _slice(dfs: Dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp) -> Dict[str, pd.DataFrame]:
    out = {}
    for s, df in dfs.items():
        part = df[(df.index >= start) & (df.index < end)]
        if len(part) > 0:
            out[s] = part
    return out


def run_walkforward(
    dfs_raw: Dict[str, pd.DataFrame],
    strategy_cls: Type[Strategy],
    base_params: dict,
    grid: Dict[str, list],
    wf_cfg: WalkForwardConfig,
    risk_cfg: RiskConfig,
    cost_model: CostModel,
    initial_capital: float,
    periods_per_year: int,
    logger: Optional[logging.Logger] = None,
) -> WalkForwardResult:
    union = pd.DatetimeIndex(sorted(set().union(*[df.index for df in dfs_raw.values()])))
    n = len(union)
    if n < wf_cfg.train_bars + wf_cfg.test_bars:
        raise ValueError(
            f"not enough history for walk-forward: have {n} bars, "
            f"need >= {wf_cfg.train_bars + wf_cfg.test_bars}"
        )

    # Prepare indicators ONCE per parameter combo on FULL history. Safe only
    # because all indicators are causal (verified in tests) — slicing prepared
    # data can then never leak the future into a window.
    combos: List[dict] = [dict(zip(grid.keys(), vals)) for vals in itertools.product(*grid.values())] or [{}]
    prepared_cache: Dict[Tuple, Tuple[Strategy, Dict[str, pd.DataFrame]]] = {}

    def get_prepared(combo: dict) -> Optional[Tuple[Strategy, Dict[str, pd.DataFrame]]]:
        key = tuple(sorted(combo.items()))
        if key not in prepared_cache:
            try:
                strat = strategy_cls({**base_params, **combo})
            except ValueError:   # inconsistent combo (e.g. fast >= slow): skip
                prepared_cache[key] = None
                return None
            prepared_cache[key] = (strat, {s: strat.prepare(df) for s, df in dfs_raw.items()})
        return prepared_cache[key]

    windows: List[WindowResult] = []
    oos_pieces: List[pd.Series] = []
    oos_trades: List[Trade] = []
    oos_turnover = 0.0
    exposure_acc: List[float] = []
    running_equity = initial_capital

    start = 0
    w_idx = 0
    while start + wf_cfg.train_bars + wf_cfg.test_bars <= n:
        t0 = union[start]
        t1 = union[start + wf_cfg.train_bars]
        test_last = min(start + wf_cfg.train_bars + wf_cfg.test_bars, n - 1)
        t2 = union[test_last] + pd.Timedelta(seconds=1)  # inclusive end of test slice

        best_combo, best_score, best_sharpe, best_trades = None, -float("inf"), 0.0, 0
        for combo in combos:
            prep = get_prepared(combo)
            if prep is None:
                continue
            strat, prepared = prep
            res = run_backtest(_slice(prepared, t0, t1), strat, risk_cfg, cost_model,
                               initial_capital)
            if res.n_trades < wf_cfg.min_trades:
                continue
            score = sharpe_ratio(res.equity, periods_per_year)
            if score > best_score:
                best_combo, best_score = combo, score
                best_sharpe, best_trades = score, res.n_trades

        fallback = best_combo is None
        if fallback:
            best_combo, best_sharpe, best_trades = {}, 0.0, 0
            if logger:
                logger.warning("window %d: no parameter set produced >= %d train trades; "
                               "using config defaults", w_idx, wf_cfg.min_trades)

        strat, prepared = get_prepared(best_combo)
        test_res = run_backtest(_slice(prepared, t1, t2), strat, risk_cfg, cost_model,
                                initial_capital)
        test_sharpe = sharpe_ratio(test_res.equity, periods_per_year)
        test_return = float(test_res.equity.iloc[-1] / initial_capital - 1)

        windows.append(WindowResult(
            index=w_idx, train_start=t0, train_end=t1, test_start=t1,
            test_end=test_res.equity.index[-1], params=best_combo,
            train_sharpe=best_sharpe, train_trades=best_trades,
            test_sharpe=test_sharpe, test_trades=test_res.n_trades,
            test_return=test_return, fallback_params=fallback,
        ))
        if logger:
            logger.info("window %d: params=%s train_sharpe=%.2f test_sharpe=%.2f test_trades=%d",
                        w_idx, best_combo, best_sharpe, test_sharpe, test_res.n_trades)

        # Chain the test segment onto the running out-of-sample equity curve.
        # Each window restarts flat with fresh capital — a conservative
        # simplification (positions never straddle window boundaries).
        seg = test_res.equity / float(test_res.equity.iloc[0]) * running_equity
        oos_pieces.append(seg)
        running_equity = float(seg.iloc[-1])
        oos_trades.extend(test_res.trades)
        oos_turnover += test_res.turnover_notional * running_equity / initial_capital
        exposure_acc.append(test_res.exposure)

        start += wf_cfg.test_bars
        w_idx += 1

    oos_equity = pd.concat(oos_pieces)
    oos_equity = oos_equity[~oos_equity.index.duplicated(keep="first")]
    return WalkForwardResult(
        windows=windows, oos_equity=oos_equity, oos_trades=oos_trades,
        oos_exposure=sum(exposure_acc) / len(exposure_acc) if exposure_acc else 0.0,
        oos_turnover=oos_turnover,
    )
