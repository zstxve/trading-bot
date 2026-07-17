"""Command-line interface.

Commands:
  fetch-data    download/refresh cached OHLCV history
  backtest      in-sample + out-of-sample holdout backtest vs buy & hold
  walkforward   rolling walk-forward optimisation, out-of-sample stitched
  paper         paper trading loop (default mode, simulated fills, real data)
  live          live trading — DISABLED unless all three safety gates pass
  report        paper-vs-backtest divergence report over the same dates
  dashboard     regenerate the read-only HTML status page (state/dashboard.html)
  dashboard-server  serve the live dashboard at http://127.0.0.1:8791/
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from tradingbot.config import AppConfig, load_config
from tradingbot.costs import CostModel
from tradingbot.logging_setup import setup_logging


def _run_id(kind: str) -> str:
    return f"{kind}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def _load(args) -> AppConfig:
    cfg = load_config(args.config)
    if getattr(args, "strategy", None):
        cfg.strategy.active = args.strategy
    if getattr(args, "history_days", None):
        cfg.data.history_days = args.history_days
    return cfg


def _load_data(cfg: AppConfig, args, logger):
    from tradingbot.data.loader import load_history
    return load_history(cfg.data, synthetic=getattr(args, "synthetic", False), logger=logger)


# ---------------------------------------------------------------- commands
def cmd_fetch_data(args) -> int:
    cfg = _load(args)
    logger = setup_logging(cfg.logging, _run_id("fetch"))
    dfs = _load_data(cfg, args, logger)
    for s, df in dfs.items():
        print(f"{s:>10}: {len(df):>7d} bars  {df.index[0]}  ..  {df.index[-1]}")
    return 0


def cmd_backtest(args) -> int:
    from tradingbot.backtest.engine import run_backtest
    from tradingbot.backtest.metrics import (benchmark_equity, compute_metrics,
                                             format_metrics_table, trades_to_df)
    from tradingbot.backtest.plots import save_report_plots
    from tradingbot.strategies.registry import build_strategy

    cfg = _load(args)
    run_id = _run_id("backtest")
    logger = setup_logging(cfg.logging, run_id)
    cost = CostModel(**cfg.costs.model_dump())
    strategy = build_strategy(cfg.strategy.active, cfg.strategy.active_params())

    dfs_raw = _load_data(cfg, args, logger)
    prepared = {s: strategy.prepare(df) for s, df in dfs_raw.items()}

    # In-sample / out-of-sample holdout split on the union timeline. The OOS
    # slice is only meaningful if you have NOT been tuning parameters on it.
    import pandas as pd
    union = pd.DatetimeIndex(sorted(set().union(*[df.index for df in prepared.values()])))
    split_i = int(len(union) * (1 - cfg.backtest.oos_fraction))
    split_ts = union[min(split_i, len(union) - 1)]
    slices = {"IS": lambda df: df[df.index < split_ts]}
    if cfg.backtest.oos_fraction > 0:
        slices["OOS"] = lambda df: df[df.index >= split_ts]

    outdir = Path("reports") / run_id
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"strategy: {strategy.name}   params: {strategy.params}")
    print(f"symbols: {cfg.data.symbols}   timeframe: {cfg.data.timeframe}   "
          f"costs/side: {cost.total_side_bps:.0f} bps\n")

    for label, slicer in slices.items():
        part = {s: slicer(df) for s, df in prepared.items()}
        part = {s: df for s, df in part.items() if len(df) > 10}
        if not part:
            print(f"[{label}] slice empty — skipped")
            continue
        res = run_backtest(part, strategy, cfg.risk, cost, cfg.backtest.initial_capital, logger)
        m = compute_metrics(res.equity, res.trades, cfg.backtest.periods_per_year,
                            cfg.backtest.initial_capital, res.turnover_notional, res.exposure)
        bench = benchmark_equity(part, cfg.backtest.initial_capital, cost)
        print(format_metrics_table(f"[{label}] strategy ({res.equity.index[0].date()} .. "
                                   f"{res.equity.index[-1].date()})", m))
        if bench is not None:
            bm = compute_metrics(bench, [], cfg.backtest.periods_per_year,
                                 cfg.backtest.initial_capital)
            print()
            print(format_metrics_table(f"[{label}] buy & hold benchmark", bm))
        if res.breaches:
            print(f"\n[{label}] RISK BREACHES: {[(str(t), e) for t, e in res.breaches]}")
        print()

        save_report_plots(outdir, res.equity, bench, res.trades, prefix=label)
        trades_to_df(res.trades).to_csv(outdir / f"{label}_trades.csv", index=False)
        (outdir / f"{label}_metrics.json").write_text(json.dumps(m, indent=2, default=float))

    print(f"report written to {outdir}/")
    return 0


def cmd_walkforward(args) -> int:
    from tradingbot.backtest.metrics import (benchmark_equity, compute_metrics,
                                             format_metrics_table, trades_to_df)
    from tradingbot.backtest.plots import save_report_plots
    from tradingbot.backtest.walkforward import OVERFITTING_WARNING, run_walkforward
    from tradingbot.strategies.registry import STRATEGIES

    cfg = _load(args)
    run_id = _run_id("walkforward")
    logger = setup_logging(cfg.logging, run_id)
    cost = CostModel(**cfg.costs.model_dump())
    strategy_cls = STRATEGIES[cfg.strategy.active]
    base_params = cfg.strategy.active_params()
    grid = cfg.walkforward.grids.get(cfg.strategy.active) or strategy_cls.param_grid()

    dfs_raw = _load_data(cfg, args, logger)
    print(f"strategy: {cfg.strategy.active}   grid: {grid}")
    print(f"train {cfg.walkforward.train_bars} bars / test {cfg.walkforward.test_bars} bars\n")
    t0 = time.time()
    wf = run_walkforward(dfs_raw, strategy_cls, base_params, grid, cfg.walkforward,
                         cfg.risk, cost, cfg.backtest.initial_capital,
                         cfg.backtest.periods_per_year, logger)
    print(f"({len(wf.windows)} windows, {time.time() - t0:.0f}s)\n")
    print(wf.params_table())
    print()

    m = compute_metrics(wf.oos_equity, wf.oos_trades, cfg.backtest.periods_per_year,
                        cfg.backtest.initial_capital, wf.oos_turnover, wf.oos_exposure)
    print(format_metrics_table("STITCHED OUT-OF-SAMPLE (the only numbers that count)", m))

    oos_dfs = {s: df[(df.index >= wf.oos_equity.index[0]) & (df.index <= wf.oos_equity.index[-1])]
               for s, df in dfs_raw.items()}
    bench = benchmark_equity(oos_dfs, cfg.backtest.initial_capital, cost)
    if bench is not None:
        print()
        print(format_metrics_table("buy & hold benchmark (same period)",
                                   compute_metrics(bench, [], cfg.backtest.periods_per_year,
                                                   cfg.backtest.initial_capital)))
    print(OVERFITTING_WARNING)

    outdir = Path("reports") / run_id
    save_report_plots(outdir, wf.oos_equity, bench, wf.oos_trades, prefix="OOS")
    trades_to_df(wf.oos_trades).to_csv(outdir / "OOS_trades.csv", index=False)
    (outdir / "OOS_metrics.json").write_text(json.dumps(m, indent=2, default=float))
    (outdir / "windows.txt").write_text(wf.params_table())
    # Metadata so consumers (dashboard) can match reports to a configuration
    # instead of blindly trusting "newest".
    (outdir / "run_info.json").write_text(json.dumps({
        "strategy": cfg.strategy.active,
        "timeframe": cfg.data.timeframe,
        "symbols": cfg.data.symbols,
        "train_bars": cfg.walkforward.train_bars,
        "test_bars": cfg.walkforward.test_bars,
        "generated": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    print(f"report written to {outdir}/")
    return 0


def cmd_paper(args) -> int:
    from tradingbot.live.runner import AlreadyRunning, LiveRunner

    cfg = _load(args)
    logger = setup_logging(cfg.logging, _run_id("paper"))
    print("PAPER TRADING — simulated fills, real market data, no orders sent anywhere.")
    print("Stop with Ctrl-C (state is persisted; restart resumes).\n")
    try:
        LiveRunner(cfg, logger, live_mode=False).run()
    except AlreadyRunning as exc:
        print(f"NOT STARTED: {exc}")
        return 3
    return 0


def cmd_live(args) -> int:
    from tradingbot.execution.binance_live import LiveTradingBlocked
    from tradingbot.live.runner import AlreadyRunning, LiveRunner

    cfg = _load(args)
    logger = setup_logging(cfg.logging, _run_id("live"))
    print("=" * 74)
    print("WARNING: LIVE TRADING SENDS REAL ORDERS AND CAN LOSE REAL MONEY.")
    print("You are expected to have completed the paper-trading protocol in the")
    print("README (>= 3 months / >= 100 paper trades) before reaching this point.")
    print("=" * 74)
    time.sleep(5)  # last chance to Ctrl-C
    try:
        LiveRunner(cfg, logger, live_mode=True, cli_live_flag=args.i_really_want_live).run()
    except LiveTradingBlocked as exc:
        print(f"\n{exc}")
        return 2
    except AlreadyRunning as exc:
        print(f"NOT STARTED: {exc}")
        return 3
    return 0


def cmd_report(args) -> int:
    from tradingbot.report import build_report

    cfg = _load(args)
    logger = setup_logging(cfg.logging, _run_id("report"))
    print(build_report(cfg, logger))
    return 0


def cmd_dashboard(args) -> int:
    from tradingbot.dashboard import generate_dashboard

    cfg = _load(args)
    out = generate_dashboard(cfg, prefix=args.session)
    print(f"dashboard written to {out}\nopen it:  open {out}")
    return 0


def cmd_dashboard_server(args) -> int:
    from tradingbot.dashboard_server import serve

    cfg = _load(args)
    logger = setup_logging(cfg.logging, _run_id("dashboard"))
    serve(cfg, port=args.port, prefix=args.session, logger=logger)
    return 0


# ---------------------------------------------------------------- entrypoint
def main(argv=None) -> None:
    load_dotenv()  # pull BINANCE_API_KEY etc. from .env if present

    parser = argparse.ArgumentParser(prog="tradingbot",
                                     description="Paper-trading-first crypto trading bot")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, data_opts=True):
        p.add_argument("--config", default="config.yaml", help="path to config.yaml")
        if data_opts:
            p.add_argument("--synthetic", action="store_true",
                           help="use deterministic synthetic data (offline demo/testing)")
            p.add_argument("--strategy",
                           choices=["trend_ma_cross", "meanrev_rsi_bb", "breakout_donchian"],
                           help="override strategy.active")
            p.add_argument("--history-days", type=int, help="override data.history_days")

    p = sub.add_parser("fetch-data", help="download/refresh OHLCV cache")
    add_common(p)
    p.set_defaults(func=cmd_fetch_data)

    p = sub.add_parser("backtest", help="run IS/OOS backtest vs buy & hold")
    add_common(p)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("walkforward", help="walk-forward optimisation + OOS report")
    add_common(p)
    p.set_defaults(func=cmd_walkforward)

    p = sub.add_parser("paper", help="paper trading loop (default, safe)")
    add_common(p, data_opts=False)
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("live", help="LIVE trading (disabled by default; 3 gates required)")
    add_common(p, data_opts=False)
    p.add_argument("--i-really-want-live", action="store_true",
                   help="third safety gate (with live.enabled and TRADINGBOT_LIVE_ACK)")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("report", help="paper-vs-backtest divergence report")
    add_common(p, data_opts=False)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("dashboard", help="regenerate read-only HTML status page")
    add_common(p, data_opts=False)
    p.add_argument("--session", choices=["paper", "live"], default="paper")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("dashboard-server",
                       help="serve the live dashboard at http://127.0.0.1:<port>/")
    add_common(p, data_opts=False)
    p.add_argument("--session", choices=["paper", "live"], default="paper")
    p.add_argument("--port", type=int, default=8791)
    p.set_defaults(func=cmd_dashboard_server)

    args = parser.parse_args(argv)
    try:
        sys.exit(args.func(args))
    except ValidationError as exc:
        print("CONFIG ERROR:")
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            print(f"  {loc}: {err['msg']}")
        sys.exit(2)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
